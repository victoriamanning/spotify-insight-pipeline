"""Materialize exactly one final record per source review ID (code only).

- Empty text -> quarantined/empty_review_text.
- Text whose distinct-text work item completed -> completed. The representative ID (the one actually sent to
  the model) is the original; every other ID with identical text gets cache_source_id -> representative.
- In-sample text that failed twice -> quarantined with the recorded reason; never counted as completed.
- Text outside the declared 100k-sample scope with no saved result -> quarantined/out_of_scope_budget_sample.
"""

import json

from . import db
from .common import label_config

CONTRACT_FIELDS = ("topic", "intent", "sentiment", "severity", "entities", "evidence_quote", "needs_review")


def iter_records(con, lc, include_text=False):
    work = {r["text_sha256"]: r for r in con.execute(
        "SELECT text_sha256, rep_review_id, status, reason, result_json, final_label_config, attempts, phase FROM work WHERE label_config=?",
        (lc,))}
    cur = con.execute("SELECT review_id, review_text, review_rating, review_likes, app_version, review_timestamp, source_sha256, "
                      "text_sha256, is_empty, in_sample FROM reviews ORDER BY row_idx")
    for r in cur:
        base = {"review_id": r["review_id"], "source_sha256": r["source_sha256"]}
        meta = {"in_sample": bool(r["in_sample"]), "review_rating": r["review_rating"], "review_likes": r["review_likes"],
                "app_version": r["app_version"], "review_timestamp": r["review_timestamp"]}
        if include_text:
            meta["review_text"] = r["review_text"]
        if r["is_empty"]:
            yield {**base, "status": "quarantined", "reason": "empty_review_text", "attempts": 0}, meta
            continue
        w = work.get(r["text_sha256"])
        if w is None:
            yield {**base, "status": "quarantined", "reason": "out_of_scope_budget_sample", "attempts": 0}, meta
            continue
        if w["status"] == "completed":
            res = json.loads(w["result_json"])
            rec = {**base, "status": "completed", **{k: res[k] for k in CONTRACT_FIELDS},
                   "label_config": w["final_label_config"], "subtopic": res["subtopic"]}
            if r["review_id"] != w["rep_review_id"]:
                rec["cache_source_id"] = w["rep_review_id"]
            meta.update(flags=res.get("flags", []), quote_method=res.get("quote_method"), phase=w["phase"])
            yield rec, meta
        elif w["status"] == "quarantined":
            yield {**base, "status": "quarantined", "reason": w["reason"], "attempts": w["attempts"]}, meta
        else:
            yield {**base, "status": "quarantined", "reason": "pending_not_processed" + (f":{w['reason']}" if w["reason"] else ""),
                   "attempts": w["attempts"]}, meta


def load_completed(con, lc):
    """{review_id: (record, meta)} for completed records only."""
    return {rec["review_id"]: (rec, meta) for rec, meta in iter_records(con, lc, include_text=True) if rec["status"] == "completed"}


def primary_lc(cfg):
    return label_config(cfg["roles"]["enrich"])
