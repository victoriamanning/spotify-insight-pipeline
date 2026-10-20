"""Append every attempted model call (success or failure) to calls.jsonl and the state DB."""

import json
from pathlib import Path


def log_attempts(con, run_dir, run_id, attempts, role, phase, label_cfg, review_ids):
    rows = []
    with open(Path(run_dir) / "calls.jsonl", "a", encoding="utf-8") as f:
        for a in attempts:
            rec = {"request_id": a.request_id, "run_id": run_id, "role": role, "phase": phase, "model": a.model,
                   "effort": a.effort, "label_config": label_cfg, "review_ids": list(review_ids),
                   "n_items": len(review_ids), "attempt": a.attempt, "outcome": a.outcome, "error": a.error,
                   "input_tokens": a.input_tokens, "cached_tokens": a.cached_tokens, "output_tokens": a.output_tokens,
                   "reasoning_tokens": a.reasoning_tokens, "usage_known": a.usage_known,
                   "cost_usd": round(a.cost_usd, 8), "started_at": a.started_at, "duration_s": a.duration_s,
                   "provider_request_id": a.provider_request_id}
            f.write(json.dumps(rec) + "\n")
            rows.append(rec)
    for r in rows:
        con.execute(
            "INSERT INTO calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["request_id"], r["run_id"], r["role"], r["phase"], r["model"], r["effort"], r["label_config"],
             json.dumps(r["review_ids"]), r["n_items"], r["attempt"], r["outcome"], r["error"], r["input_tokens"],
             r["cached_tokens"], r["output_tokens"], r["reasoning_tokens"], r["cost_usd"], r["started_at"],
             r["duration_s"], r["provider_request_id"]))
    return rows
