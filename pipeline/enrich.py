"""Stage 2 - CLASSIFY (model: enricher role; code: dispatch, validation, state).

Code owns: the queue, batching (<=50 distinct texts per request), budget reservation, retries, ID validation,
schema validation, quote membership, rule checks and atomic per-batch saves. The model only reads language.
"""

import json
import re
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait
from pathlib import Path

from . import db
from .common import ROOT, TOPICS, INTENTS, label_config, load_taxonomy, now_iso, write_json, Clock
from .llm import BudgetExceeded
from .calllog import log_attempts

REF_RE = re.compile(r"^r\d{1,3}$")


def render_instructions(prompt_path, taxonomy):
    lines = []
    for t, spec in taxonomy["topics"].items():
        subs = "; ".join(f"{s} ({d})" for s, d in spec["subtopics"].items())
        lines.append(f"- {t}: {spec['definition']}\n  subtopics: {subs}")
    return (ROOT / prompt_path).read_text(encoding="utf-8").replace("{{TAXONOMY}}", "\n".join(lines))


def flatten(text):
    return " ".join(text.split())


def build_user_text(items):
    body = "\n".join(f"r{k + 1}: {flatten(it['text'])}" for k, it in enumerate(items))
    return f"Label these {len(items)} reviews. Output exactly {len(items)} lines.\n\n{body}"


def find_quote(quote, text):
    """Return (exact_source_substring, method). Code, not the model, guarantees membership."""
    q = quote.strip().strip('"“”\'‘’').strip()
    q = re.sub(r"(\.\.\.|…)$", "", q).strip()
    if q and q in text:
        return q, "exact"
    if q:
        idx = text.lower().find(q.lower())
        if idx >= 0 and text[idx:idx + len(q)].lower() == q.lower():
            return text[idx:idx + len(q)], "case_repaired"
        tokens = q.split()
        if tokens:
            m = re.search(r"\s+".join(re.escape(t) for t in tokens), text, re.I)
            if m:
                return m.group(0), "whitespace_repaired"
    if len(text) <= 300:
        return text, "full_text_fallback"
    return text[:200], "prefix_fallback"


def parse_output(output, items, taxonomy):
    """Validate every returned ref against the request. Returns ({idx: record}, {idx: error})."""
    by_ref = {f"r{k + 1}": k for k in range(len(items))}
    results, errors, seen, first_line = {}, {}, set(), {}
    for raw in output.strip().splitlines():
        line = raw.strip().strip("`").strip()
        if not line or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|", 8)]
        ref = parts[0]
        if not REF_RE.match(ref) or ref not in by_ref:
            continue  # foreign / invented reference: ignored, the expected ref is then reported missing
        k = by_ref[ref]
        if ref in seen:
            # The model sometimes restarts its list after finishing; the first answer for a ref wins.
            if k in results and line != first_line.get(k):
                results[k]["flags"].append("repeated_ref_later_line_ignored")
            continue
        seen.add(ref)
        first_line[k] = line
        if len(parts) != 9:
            errors[k] = f"wrong_field_count:{len(parts)}"
            continue
        _, topic, sub, intent, sev, sent, nr, ents, quote = parts
        text = items[k]["text"]
        flags = []
        if topic not in TOPICS:
            errors[k] = f"invalid_topic:{topic[:20]}"
            continue
        if intent not in INTENTS:
            errors[k] = f"invalid_intent:{intent[:20]}"
            continue
        try:
            severity = int(sev)
            sentiment_level = float(sent)
            needs_review = bool(int(nr))
        except ValueError:
            errors[k] = "non_numeric_field"
            continue
        if not 1 <= severity <= 5 or not -2 <= sentiment_level <= 2:
            errors[k] = "out_of_range"
            continue
        if sub not in taxonomy["topics"][topic]["subtopics"]:
            flags.append(f"subtopic_coerced:{sub[:20]}")
            sub = "other"
        if intent in ("praise", "request", "unclear") and severity != 1:
            flags.append(f"severity_rule:{intent}:{severity}->1")
            severity = 1
        if intent in ("complaint", "cancellation") and severity == 1:
            flags.append("severity_rule:complaint:1->2")
            severity = 2
        evidence, method = find_quote(quote, text)
        if method != "exact":
            flags.append(f"quote:{method}")
        entities, dropped = [], 0
        for e in ([] if ents in ("-", "") else ents.split(";")):
            e = e.strip()
            if e and e.lower() in text.lower() and e not in entities:
                entities.append(e)
            elif e:
                dropped += 1
        if dropped:
            flags.append(f"entities_dropped:{dropped}")
        if method in ("full_text_fallback", "prefix_fallback"):
            needs_review = True
        results[k] = {"topic": topic, "subtopic": sub, "intent": intent, "severity": severity,
                      "sentiment": round(sentiment_level / 2, 4), "entities": entities, "evidence_quote": evidence,
                      "needs_review": needs_review, "quote_method": method, "flags": flags}
    for k in range(len(items)):
        if k not in results and k not in errors:
            errors[k] = "missing_in_output"
    return results, errors


class Enricher:
    def __init__(self, cfg, run_dir, caller, run_id, log=print):
        self.cfg = cfg
        self.run_dir = Path(run_dir)
        self.caller = caller
        self.run_id = run_id
        self.log = log
        self.tax = load_taxonomy()
        self.primary = cfg["roles"]["enrich"]
        self.fallback = cfg["roles"]["fallback"]
        self.lc = label_config(self.primary)
        self.lc_fallback = label_config(self.fallback)
        self.instructions = render_instructions(self.primary["prompt"], self.tax)
        self.fb_instructions = render_instructions(self.fallback["prompt"], self.tax)
        self.con = db.connect(self.run_dir / "state.sqlite")
        self.stop = threading.Event()
        self.stop_reason = ""

    # ---- logging (single writer) ----
    def log_attempts(self, attempts, role, phase, cfg_used, review_ids):
        log_attempts(self.con, self.run_dir, self.run_id, attempts, role, phase, label_config(cfg_used), review_ids)

    # ---- one request (runs in a worker thread) ----
    def request(self, items, use_fallback):
        cfg = self.fallback if use_fallback else self.primary
        instr = self.fb_instructions if use_fallback else self.instructions
        user = build_user_text(items)
        max_out = cfg.get("max_output_tokens") or (len(items) * cfg["max_output_tokens_per_review"] + 100)
        attempts, final = self.caller.call("enrich", cfg["model"], cfg["effort"], instr, user, max_out,
                                           cache_key=f"enrich-{cfg['prompt_version']}",
                                           est_input_tokens=len(instr) // 4 + len(user) // 3 + 20)
        return items, use_fallback, attempts, final

    def run(self, limit=None, workers=None, phase=None, stop_after_batches=None, max_minutes=None):
        clock = Clock()
        workers = int(workers or self.primary["workers"])
        phase = phase or ("resume" if (self.run_dir / "checkpoint_before.json").exists() else "initial")
        max_minutes = max_minutes or self.cfg["budget"]["max_minutes"]
        bs = int(self.primary["batch_size"])
        assert 1 <= bs <= 50, "enrichment batches are limited to 50 reviews"

        q = ("SELECT w.text_sha256, w.rep_review_id, r.review_text FROM work w JOIN reviews r ON r.review_id=w.rep_review_id "
             "WHERE w.label_config=? AND w.status='pending' ORDER BY w.attempts, w.queue_rank")
        pending = [dict(text_sha=r[0], rid=r[1], text=r[2]) for r in self.con.execute(q, (self.lc,)).fetchall()]
        retry_items = [p for p in pending if self.attempts_of(p["text_sha"]) > 0]
        fresh = [p for p in pending if self.attempts_of(p["text_sha"]) == 0]
        if limit is not None:
            fresh = fresh[:max(0, int(limit))]
        total_work = self.con.execute("SELECT COUNT(*) c FROM work WHERE label_config=?", (self.lc,)).fetchone()["c"]
        fb_used = self.con.execute("SELECT COUNT(*) c FROM work WHERE label_config=? AND final_label_config=?",
                                   (self.lc, self.lc_fallback)).fetchone()["c"]
        fb_quota = int(self.fallback["max_fraction"] * total_work)
        batches = [fresh[i:i + bs] for i in range(0, len(fresh), bs)]
        retry_queue = []

        def queue_retries(items):
            nonlocal fb_used
            # A few bad lines in an otherwise valid response -> capped single-review fallback (stronger setting).
            # A whole failed request (e.g. truncated output) -> retry the group once as a batch with the primary config.
            if len(items) > 1 and any("flagged" in (it.get("last_error") or "") for it in items):
                retry_queue.extend(([it], False) for it in items)  # isolate a content-filtered review
            elif len(items) <= 5 and fb_used + len(items) <= fb_quota:
                retry_queue.extend(([it], True) for it in items)
                fb_used += len(items)
            else:
                retry_queue.append((items, False))
        for i in range(0, len(retry_items), bs):
            queue_retries(retry_items[i:i + bs])
        self.log(f"enrich[{phase}]: {len(fresh)} new texts in {len(batches)} batches, {len(retry_items)} retries, "
                 f"workers={workers}, cap=${self.caller.ledger.cap:.2f}, spent so far=${self.caller.ledger.spent:.4f}")

        old_handler = signal.getsignal(signal.SIGINT)

        def on_sigint(sig, frame):
            self.stop.set()
            self.stop_reason = "interrupted (SIGINT) - finishing in-flight batches, then saving"
            self.log("\n" + self.stop_reason)
        signal.signal(signal.SIGINT, on_sigint)

        stats = dict(batches_done=0, completed=0, quarantined=0, retried=0, fallback=0, failed_calls=0)
        inflight = {}
        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                while (batches or retry_queue or inflight):
                    while not self.stop.is_set() and len(inflight) < workers and (batches or retry_queue):
                        if clock.elapsed() > max_minutes * 60:
                            self.stop.set(); self.stop_reason = f"time cap {max_minutes} min reached"; break
                        if stop_after_batches is not None and stats["batches_done"] + len(inflight) >= stop_after_batches:
                            self.stop.set(); self.stop_reason = f"--stop-after-batches {stop_after_batches} (simulated interruption)"; break
                        if retry_queue:
                            items, use_fb = retry_queue.pop(0)
                            inflight[pool.submit(self.request, items, use_fb)] = 1
                        else:
                            inflight[pool.submit(self.request, batches.pop(0), False)] = 1
                    if not inflight:
                        break
                    done, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
                    for fut in done:
                        inflight.pop(fut)
                        try:
                            items, used_fb, attempts, final = fut.result()
                        except BudgetExceeded as e:
                            self.stop.set(); self.stop_reason = f"budget stop: {e}"
                            self.log(self.stop_reason)
                            continue
                        new_retry = self.save_batch(items, used_fb, attempts, final, phase, stats)
                        if new_retry:
                            queue_retries(new_retry)
                        stats["batches_done"] += 1
                        if stats["batches_done"] % 20 == 0:
                            self.progress(stats, clock)
        finally:
            signal.signal(signal.SIGINT, old_handler)
        self.progress(stats, clock)
        summary = self.summary(stats, clock, phase)
        write_json(self.run_dir / f"enrich_summary_{phase}.json", summary)
        return summary

    def attempts_of(self, tsha):
        return self.con.execute("SELECT attempts FROM work WHERE text_sha256=? AND label_config=?", (tsha, self.lc)).fetchone()[0]

    def save_batch(self, items, used_fb, attempts, final, phase, stats):
        """Atomic per-batch save: call log + every item's new status in one transaction."""
        cfg_used = self.fallback if used_fb else self.primary
        rids = [it["rid"] for it in items]
        stats["failed_calls"] += sum(a.outcome == "failed" for a in attempts)
        results, errors = (parse_output(final.text, items, self.tax) if final else ({}, {k: "call_failed" for k in range(len(items))}))
        if errors:  # keep raw model output for failure analysis (no keys; review text only)
            with open(self.run_dir / "raw_failed_outputs.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"request_id": attempts[-1].request_id, "review_ids": rids, "errors": {rids[k]: v for k, v in errors.items()},
                                    "error": attempts[-1].error, "output": (final.text if final else attempts[-1].text)}) + "\n")
        retry = []
        self.con.execute("BEGIN IMMEDIATE")
        try:
            self.log_attempts(attempts, "enrich", phase, cfg_used, rids)
            for k, it in enumerate(items):
                cur = self.attempts_of(it["text_sha"]) + 1
                if k in results:
                    self.con.execute(
                        "UPDATE work SET status='completed', attempts=?, result_json=?, final_label_config=?, request_id=?, phase=?, "
                        "completed_at=?, reason=NULL WHERE text_sha256=? AND label_config=?",
                        (cur, json.dumps(results[k]), label_config(cfg_used), final.request_id, phase, now_iso(), it["text_sha"], self.lc))
                    stats["completed"] += 1
                    stats["fallback"] += used_fb
                elif cur >= 1 + int(self.cfg["retry"]["invalid_output_retries"]):
                    self.con.execute("UPDATE work SET status='quarantined', attempts=?, reason=? WHERE text_sha256=? AND label_config=?",
                                     (cur, ("provider_content_filter" if not final and "flagged" in attempts[-1].error else
                                            f"invalid_model_output:{errors.get(k, 'unknown')}"), it["text_sha"], self.lc))
                    stats["quarantined"] += 1
                else:
                    self.con.execute("UPDATE work SET attempts=?, reason=? WHERE text_sha256=? AND label_config=?",
                                     (cur, f"retry_pending:{errors.get(k, 'unknown')}", it["text_sha"], self.lc))
                    it["last_error"] = attempts[-1].error if not final else ""
                    retry.append(it)
                    stats["retried"] += 1
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise
        return retry

    def progress(self, stats, clock):
        c = self.con.execute("SELECT status, COUNT(*) n FROM work WHERE label_config=? GROUP BY status", (self.lc,)).fetchall()
        counts = {r["status"]: r["n"] for r in c}
        el = clock.elapsed()
        self.log(f"  batches={stats['batches_done']} completed+={stats['completed']} retried={stats['retried']} "
                 f"quarantined+={stats['quarantined']} failed_calls={stats['failed_calls']} | queue {counts} | "
                 f"spent ${self.caller.ledger.spent:.4f} | {el:.0f}s ({stats['completed'] / el if el else 0:.1f} texts/s)")

    def summary(self, stats, clock, phase):
        counts = {r["status"]: r["n"] for r in self.con.execute(
            "SELECT status, COUNT(*) n FROM work WHERE label_config=? GROUP BY status", (self.lc,)).fetchall()}
        return {"phase": phase, "label_config": self.lc, "stats": stats, "queue_status": counts,
                "stop_reason": self.stop_reason or "queue drained", "wall_clock_s": clock.elapsed(),
                "spent_usd_total": round(self.caller.ledger.spent, 6), "finished_at": now_iso()}
