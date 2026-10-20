"""Stage 3 - VERIFY (model: independent verifier role; code: sampling, comparison, planted-error test).

The verifier receives only the original review text and its own rubric (prompt verify_v1); it never sees the
enricher's prediction. Code draws a declared seeded random sample, compares labels and writes a disagreement report.
"""

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from . import db
from .calllog import log_attempts
from .common import ROOT, TOPICS, INTENTS, label_config, write_csv, write_json, Clock
from .enrich import flatten
from .llm import BudgetExceeded
from .records import load_completed, primary_lc


def parse_verify(output, n):
    res = {}
    for line in output.strip().splitlines():
        parts = [p.strip() for p in line.strip().strip("`").split("|")]
        if len(parts) != 4 or not re.match(r"^r\d+$", parts[0]):
            continue
        k = int(parts[0][1:]) - 1
        if not 0 <= k < n or k in res:
            continue
        topic, intent, sev = parts[1], parts[2], parts[3]
        if topic in TOPICS and intent in INTENTS and sev.isdigit() and 1 <= int(sev) <= 5:
            res[k] = {"topic": topic, "intent": intent, "severity": int(sev)}
    return res


def choose_sample(rep_ids, cfg_v, seed):
    n = max(cfg_v["min_sample"], int(round(cfg_v["sample_fraction"] * len(rep_ids))))
    n = min(n, cfg_v["max_sample"], len(rep_ids))
    ordered = sorted(rep_ids, key=lambda rid: hashlib.sha256(f"{seed}:verify:{rid}".encode()).hexdigest())
    return ordered[:n]


def run(cfg, run_dir, caller, run_id, out_dir=None, log=print):
    clock = Clock()
    run_dir = Path(run_dir)
    out_dir = Path(out_dir or run_dir / "verify")
    con = db.connect(run_dir / "state.sqlite")
    v = cfg["roles"]["verify"]
    vlc = label_config({**v, "schema_version": "lines_v1"})
    lc = primary_lc(cfg)
    completed = load_completed(con, lc)
    reps = sorted(rid for rid, (rec, _) in completed.items() if "cache_source_id" not in rec)
    sample = choose_sample(reps, v, cfg["scope"]["seed"])
    done = {r["review_id"] for r in con.execute("SELECT review_id FROM verify WHERE label_config=?", (vlc,))}
    todo = [rid for rid in sample if rid not in done]
    instructions = (ROOT / v["prompt"]).read_text(encoding="utf-8")
    bs = int(v["batch_size"])
    log(f"verify: sample {len(sample)} of {len(reps)} model-labelled texts ({len(todo)} not yet verified)")
    stop = ""
    for i in range(0, len(todo), bs):
        chunk = todo[i:i + bs]
        user = f"Label these {len(chunk)} reviews.\n\n" + "\n".join(
            f"r{k + 1}: {flatten(completed[rid][1]['review_text'])}" for k, rid in enumerate(chunk))
        try:
            attempts, final = caller.call("verify", v["model"], v["effort"], instructions, user,
                                          len(chunk) * v["max_output_tokens_per_review"] + 60, cache_key="verify-v1")
        except BudgetExceeded as e:
            stop = f"budget stop: {e}"
            log(stop)
            break
        parsed = parse_verify(final.text, len(chunk)) if final else {}
        con.execute("BEGIN IMMEDIATE")
        log_attempts(con, run_dir, run_id, attempts, "verify", "initial", vlc, chunk)
        for k, rid in enumerate(chunk):
            if k in parsed:
                p = parsed[k]
                con.execute("INSERT OR REPLACE INTO verify VALUES (?,?,?,?,?,?,0)",
                            (rid, vlc, p["topic"], p["intent"], p["severity"], final.request_id))
        con.execute("COMMIT")
    report = compare(con, completed, sample, vlc, out_dir)
    report.update(sample_rule=f"seeded SHA-256 order, {v['sample_fraction']:.0%} of model-labelled texts (min {v['min_sample']}, max {v['max_sample']})",
                  verifier_label_config=vlc, stop_reason=stop or "complete", runtime_s=clock.elapsed())
    planted = planted_error_test(con, completed, vlc, out_dir)
    report["planted_error_test"] = planted
    write_json(out_dir / "verifier_report.json", report)
    log(f"verify: topic agreement {report['agreement']['topic']}, intent {report['agreement']['intent']}, "
        f"severity exact {report['agreement']['severity_exact']}, disagreements {report['disagreement_count']}; "
        f"planted errors detected {planted['detected']}/{planted['planted']}")
    return report


def compare(con, completed, sample, vlc, out_dir):
    ver = {r["review_id"]: dict(r) for r in con.execute("SELECT * FROM verify WHERE label_config=? AND synthetic=0", (vlc,))}
    rows, agree = [], Counter()
    confusion = Counter()
    n = 0
    for rid in sample:
        if rid not in ver:
            continue
        n += 1
        e, vv = completed[rid][0], ver[rid]
        t, it, s = e["topic"] == vv["topic"], e["intent"] == vv["intent"], e["severity"] == vv["severity"]
        agree["topic"] += t; agree["intent"] += it; agree["severity_exact"] += s
        agree["severity_abs_diff"] += abs(e["severity"] - vv["severity"])
        confusion[(e["topic"], vv["topic"])] += 1
        if not (t and it and s):
            rows.append({"review_id": rid, "review_text": completed[rid][1]["review_text"][:300],
                         "enrich_topic": e["topic"], "verify_topic": vv["topic"], "enrich_intent": e["intent"],
                         "verify_intent": vv["intent"], "enrich_severity": e["severity"], "verify_severity": vv["severity"],
                         "fields_disagreeing": ";".join(f for f, ok in (("topic", t), ("intent", it), ("severity", s)) if not ok),
                         "enrich_needs_review": e["needs_review"]})
    write_csv(out_dir / "disagreements.csv", list(rows[0].keys()) if rows else ["review_id"], rows)
    flagged = sum(r["enrich_needs_review"] for r in rows)
    return {"sampled": len(sample), "verified": n, "missing_verifier_output": len(sample) - n,
            "agreement": {k: round(agree[k] / n, 4) if n else None for k in ("topic", "intent", "severity_exact")},
            "severity_mae": round(agree["severity_abs_diff"] / n, 4) if n else None,
            "disagreement_count": len(rows), "disagreements_already_flagged_needs_review": flagged,
            "topic_confusion_enrich_vs_verify": {f"{a}->{b}": c for (a, b), c in sorted(confusion.items()) if a != b}}


def planted_error_test(con, completed, vlc, out_dir, k=30):
    """Separate TEST COPY: take verified records where verifier and enricher agree on topic, plant a wrong topic
    (and a severity shifted by 2) and check that code comparison against the independent verifier flags each one.
    No business data is modified; this uses existing verifier outputs only."""
    ver = {r["review_id"]: dict(r) for r in con.execute("SELECT * FROM verify WHERE label_config=? AND synthetic=0", (vlc,))}
    agreed = sorted(rid for rid in ver if rid in completed and completed[rid][0]["topic"] == ver[rid]["topic"])[:k]
    cases = []
    for idx, rid in enumerate(agreed):
        rec = completed[rid][0]
        wrong_topic = TOPICS[(TOPICS.index(rec["topic"]) + 1 + idx % 6) % len(TOPICS)]
        wrong_sev = rec["severity"] + 2 if rec["severity"] <= 3 else rec["severity"] - 2
        detected = (wrong_topic != ver[rid]["topic"]) or (wrong_sev != ver[rid]["severity"])
        cases.append({"review_id": rid, "synthetic_test_copy": True, "true_topic": rec["topic"], "planted_topic": wrong_topic,
                      "true_severity": rec["severity"], "planted_severity": wrong_sev,
                      "verifier_topic": ver[rid]["topic"], "verifier_severity": ver[rid]["severity"], "detected": detected})
    write_json(Path(out_dir) / "planted_error_cases.json", cases)
    return {"planted": len(cases), "detected": sum(c["detected"] for c in cases),
            "note": "Synthetic test copy; excluded from business aggregates."}
