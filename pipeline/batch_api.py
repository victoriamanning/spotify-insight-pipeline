"""OpenAI Batch API path for the enricher (same model, prompt, schema and validator; 50% price; async turnaround).

  python -m pipeline.batch_api expand-scope --run-dir runs/final     # add every remaining distinct nonempty text to the queue
  python -m pipeline.batch_api submit --run-dir runs/final --texts 100000
  python -m pipeline.batch_api collect --run-dir runs/final          # poll; parse + validate finished jobs; safe to repeat

Each batch line is one request of <=50 distinct texts, identical to the synchronous request. Submitted texts get
status 'submitted' so no other worker re-sends them. Budget: before submitting, code reserves the expected cost of
the job at 1.5x the measured output tokens per review (not the 90-token cap) and refuses if spent + open reservations
+ this job would exceed the cap. Invalid or missing lines go back to the queue (attempts=1) and are retried by the
normal synchronous `enrich` command (one retry / capped fallback, then quarantine).
"""

import argparse
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv

from . import db
from .common import ROOT, label_config, load_config, now_iso
from .enrich import Enricher, build_user_text
from .llm import Attempt, cost_of, load_rates

EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS batch_jobs (batch_id TEXT PRIMARY KEY, status TEXT, input_file_id TEXT, output_file_id TEXT,
  error_file_id TEXT, n_requests INTEGER, n_texts INTEGER, reserved_usd REAL, created_at TEXT, collected_at TEXT);
CREATE TABLE IF NOT EXISTS batch_items (custom_id TEXT PRIMARY KEY, batch_id TEXT, text_shas TEXT, rep_ids TEXT, collected INTEGER DEFAULT 0);
"""


def connect(run_dir):
    con = db.connect(Path(run_dir) / "state.sqlite")
    con.executescript(EXTRA_SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(calls)")}
    if "tier" not in cols:
        con.execute("ALTER TABLE calls ADD COLUMN tier TEXT DEFAULT 'standard'")
    return con


def expand_scope(cfg, run_dir):
    con = connect(run_dir)
    lc = label_config(cfg["roles"]["enrich"])
    before = con.execute("SELECT COUNT(*) FROM work WHERE label_config=?", (lc,)).fetchone()[0]
    con.execute("BEGIN")
    con.execute("CREATE TEMP TABLE firsts AS SELECT text_sha256, MIN(queue_rank) qr FROM reviews WHERE is_empty=0 GROUP BY text_sha256")
    con.execute("INSERT OR IGNORE INTO work(text_sha256,label_config,rep_review_id,queue_rank) "
                "SELECT f.text_sha256, ?, r.review_id, f.qr FROM firsts f JOIN reviews r ON r.text_sha256=f.text_sha256 AND r.queue_rank=f.qr", (lc,))
    con.execute("COMMIT")
    after = con.execute("SELECT COUNT(*) FROM work WHERE label_config=?", (lc,)).fetchone()[0]
    rep = db.get_meta(con, "ingestion_report")
    rep["scope"]["expanded_to_full_corpus_at"] = now_iso()
    rep["scope"]["expansion_note"] = ("Scope expanded from the 100k sample to every distinct nonempty text after budget allowed; "
                                      "remaining texts queued in the same seeded order; sample_only/ aggregates keep the uniform 100k view.")
    rep["scope"]["distinct_texts_in_queue"] = after
    db.set_meta(con, "ingestion_report", rep)
    with open(Path(run_dir) / "run_log.jsonl", "a") as f:
        f.write(json.dumps({"ts": now_iso(), "stage": "enrich", "event": "expand_scope", "work_before": before, "work_after": after}) + "\n")
    print(f"expand-scope: work items {before} -> {after} (+{after - before} distinct texts)")


def expected_cost(con, rates, model, n_texts):
    r = con.execute("SELECT SUM(input_tokens), SUM(cached_tokens), SUM(output_tokens), SUM(n_items) FROM calls "
                    "WHERE role='enrich' AND outcome='succeeded' AND n_items>1").fetchone()
    inp, cached, out, items = r
    per = cost_of(rates, model, inp / items, cached / items, 1.5 * out / items)
    return per * n_texts


def submit(cfg, run_dir, n_texts):
    from openai import OpenAI
    con = connect(run_dir)
    e = cfg["roles"]["enrich"]
    lc = label_config(e)
    rates = load_rates(ROOT / cfg["rates_file"], tier="batch")
    spent = db.spent(con)
    open_res = con.execute("SELECT COALESCE(SUM(reserved_usd),0) FROM batch_jobs WHERE status NOT IN ('collected','failed','expired','cancelled')").fetchone()[0]
    rows = con.execute("SELECT w.text_sha256, w.rep_review_id, r.review_text FROM work w JOIN reviews r ON r.review_id=w.rep_review_id "
                       "WHERE w.label_config=? AND w.status='pending' AND w.attempts=0 ORDER BY w.queue_rank LIMIT ?", (lc, n_texts)).fetchall()
    if not rows:
        print("submit: nothing pending"); return
    est = expected_cost(con, rates, e["model"], len(rows))
    cap = cfg["budget"]["spend_cap_usd"]
    if spent + open_res + est > cap:
        n_ok = int(len(rows) * (cap - spent - open_res) / est) if est else 0
        print(f"submit: refused - spent {spent:.4f} + open reservations {open_res:.4f} + this job {est:.4f} > cap {cap:.2f}; "
              f"at most ~{max(0, n_ok)} texts fit"); return
    tax_enr = Enricher(cfg, run_dir, None, Path(run_dir).name)
    bs = int(e["batch_size"])
    lines, items = [], []
    for i in range(0, len(rows), bs):
        chunk = rows[i:i + bs]
        cid = f"enrich-b-{os.urandom(8).hex()}"
        user = build_user_text([{"text": t} for _, _, t in chunk])
        body = {"model": e["model"], "instructions": tax_enr.instructions, "input": user,
                "max_output_tokens": len(chunk) * e["max_output_tokens_per_review"] + 100,
                "reasoning": {"effort": e["effort"]}, "store": False, "prompt_cache_key": f"enrich-{e['prompt_version']}"}
        lines.append(json.dumps({"custom_id": cid, "method": "POST", "url": "/v1/responses", "body": body}))
        items.append((cid, [s for s, _, _ in chunk], [rid for _, rid, _ in chunk]))
    path = Path(run_dir) / "tmp" / f"batch-{int(time.time())}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    client = OpenAI()
    f = client.files.create(file=open(path, "rb"), purpose="batch")
    b = client.batches.create(input_file_id=f.id, endpoint="/v1/responses", completion_window="24h",
                              metadata={"run": Path(run_dir).name, "label_config": lc})
    con.execute("BEGIN IMMEDIATE")
    con.execute("INSERT INTO batch_jobs VALUES (?,?,?,?,?,?,?,?,?,NULL)", (b.id, b.status, f.id, None, None, len(items), len(rows), est, now_iso()))
    con.executemany("INSERT INTO batch_items(custom_id,batch_id,text_shas,rep_ids) VALUES (?,?,?,?)",
                    [(cid, b.id, json.dumps(s), json.dumps(r)) for cid, s, r in items])
    con.executemany("UPDATE work SET status='submitted', reason=? WHERE text_sha256=? AND label_config=?",
                    [(f"batch:{b.id}", s, lc) for _, ss, _ in items for s in ss])
    con.execute("COMMIT")
    with open(Path(run_dir) / "run_log.jsonl", "a") as fh:
        fh.write(json.dumps({"ts": now_iso(), "stage": "enrich", "event": "batch_submit", "batch_id": b.id, "requests": len(items),
                             "texts": len(rows), "reserved_usd": round(est, 4)}) + "\n")
    print(f"submit: batch {b.id} with {len(items)} requests / {len(rows)} texts; reserved ${est:.4f}; spent ${spent:.4f}")


def output_text(body):
    parts = []
    for item in body.get("output", []):
        for c in item.get("content", []) or []:
            if c.get("type") == "output_text":
                parts.append(c.get("text", ""))
    return "".join(parts)


def collect(cfg, run_dir):
    from openai import OpenAI
    client = OpenAI()
    con = connect(run_dir)
    e = cfg["roles"]["enrich"]
    lc = label_config(e)
    rates = load_rates(ROOT / cfg["rates_file"], tier="batch")
    enr = Enricher(cfg, run_dir, None, Path(run_dir).name)
    phase = "resume" if (Path(run_dir) / "checkpoint_before.json").exists() else "initial"
    for job in con.execute("SELECT * FROM batch_jobs WHERE status NOT IN ('collected','failed','expired','cancelled')").fetchall():
        b = client.batches.retrieve(job["batch_id"])
        rc = b.request_counts
        print(f"{b.id}: {b.status} ({rc.completed if rc else '?'}/{rc.total if rc else '?'} done, {rc.failed if rc else '?'} failed)")
        con.execute("UPDATE batch_jobs SET status=? WHERE batch_id=?", (b.status, b.id))
        if b.status not in ("completed", "expired", "cancelled", "failed"):
            continue
        results = {}
        for fid in (b.output_file_id, b.error_file_id):
            if fid:
                for line in client.files.content(fid).text.splitlines():
                    if line.strip():
                        d = json.loads(line)
                        results[d["custom_id"]] = d
        stats = dict(batches_done=0, completed=0, quarantined=0, retried=0, fallback=0, failed_calls=0)
        for it in con.execute("SELECT * FROM batch_items WHERE batch_id=? AND collected=0", (b.id,)).fetchall():
            shas, rids = json.loads(it["text_shas"]), json.loads(it["rep_ids"])
            texts = {r["review_id"]: r["review_text"] for r in con.execute(
                f"SELECT review_id, review_text FROM reviews WHERE review_id IN ({','.join('?' * len(rids))})", rids)}
            items = [{"text_sha": s, "rid": r, "text": texts[r]} for s, r in zip(shas, rids)]
            d = results.get(it["custom_id"])
            a = Attempt(request_id=it["custom_id"], model=e["model"], effort=e["effort"], attempt=1, outcome="failed",
                        started_at=job["created_at"], provider_request_id=b.id)
            final = None
            resp = (d or {}).get("response") or {}
            body = resp.get("body") or {}
            u = body.get("usage") or {}
            a.input_tokens = u.get("input_tokens", 0) or 0
            a.cached_tokens = (u.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0
            a.output_tokens = u.get("output_tokens", 0) or 0
            a.reasoning_tokens = (u.get("output_tokens_details") or {}).get("reasoning_tokens", 0) or 0
            a.usage_known = bool(u)
            a.cost_usd = cost_of(rates, e["model"], a.input_tokens, a.cached_tokens, a.output_tokens)
            a.text = output_text(body)
            if resp.get("status_code") == 200 and a.text.strip():
                a.outcome = "succeeded"
                if body.get("status") == "incomplete":
                    a.error = "incomplete:max_output_tokens (partial output parsed)"
                final = a
            else:
                a.error = json.dumps((d or {}).get("error") or body.get("error") or {"missing": "no result line"})[:300]
            con.execute("UPDATE work SET status='pending' WHERE label_config=? AND status='submitted' AND text_sha256 IN (%s)" % ",".join("?" * len(shas)),
                        [lc, *shas])
            enr.save_batch(items, False, [a], final, phase, stats)
            con.execute("UPDATE calls SET tier='batch' WHERE request_id=?", (a.request_id,))
            con.execute("UPDATE batch_items SET collected=1 WHERE custom_id=?", (it["custom_id"],))
        con.execute("UPDATE batch_jobs SET status='collected', output_file_id=?, error_file_id=?, collected_at=? WHERE batch_id=?",
                    (b.output_file_id, b.error_file_id, now_iso(), b.id))
        with open(Path(run_dir) / "run_log.jsonl", "a") as fh:
            fh.write(json.dumps({"ts": now_iso(), "stage": "enrich", "event": "batch_collect", "batch_id": b.id, **stats}) + "\n")
        print(f"collected {b.id}: {stats}")
    counts = {r[0]: r[1] for r in con.execute("SELECT status, COUNT(*) FROM work WHERE label_config=? GROUP BY status", (lc,))}
    print(f"queue: {counts}; spent ${db.spent(con):.4f}")


def main():
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n in ("expand-scope", "submit", "collect"):
        sp = sub.add_parser(n); sp.add_argument("--run-dir", required=True)
        if n == "submit":
            sp.add_argument("--texts", type=int, required=True)
    a = ap.parse_args()
    cfg = load_config()
    {"expand-scope": lambda: expand_scope(cfg, a.run_dir), "submit": lambda: submit(cfg, a.run_dir, a.texts),
     "collect": lambda: collect(cfg, a.run_dir)}[a.cmd]()


if __name__ == "__main__":
    main()
