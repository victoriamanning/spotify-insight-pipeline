"""Standardized grading/ export (GRADING_CONTRACT.md) plus enriched outputs. Code only."""

import json
import shutil
from pathlib import Path

from . import db
from .common import file_sha, read_json, write_json, write_jsonl
from .records import iter_records, primary_lc

CALL_FIELDS = ("request_id", "role", "review_ids", "model", "phase", "outcome", "label_config", "input_tokens", "output_tokens",
               "cached_tokens", "reasoning_tokens", "effort", "attempt", "error", "cost_usd", "started_at", "duration_s", "run_id")


def run(cfg, run_dir, out_dir, log=print):
    run_dir, out_dir = Path(run_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    con = db.connect(run_dir / "state.sqlite")
    lc = primary_lc(cfg)
    report = db.get_meta(con, "ingestion_report")
    input_csv = db.get_meta(con, "input_csv")
    write_json(out_dir / "run.json", {
        "version": "a5-audit-v1", "analysis_count": report["records"], "analysis_sha256": report["file_sha256"],
        "classification_input_fields": ["review_text"], "allow_multi_issue": False,
        "run_id": run_dir.name, "pipeline_version": cfg["pipeline_version"], "enrich_label_config": lc,
        "scope": report["scope"]})
    # ingestion.json must equal check_submission.profile(...) byte-for-value; write it with the checker's own writer.
    from vendor import check_submission
    check_submission.write_json(out_dir / "ingestion.json", report["checker_profile"])

    graded, enriched = [], []
    for rec, meta in iter_records(con, lc):
        graded.append(rec)
        if rec["status"] == "completed":
            enriched.append({**rec, **{k: v for k, v in meta.items() if k != "review_text"}})
    write_jsonl(out_dir / "records.jsonl.gz", graded, compress=True)
    plain = out_dir / "records.jsonl"
    if plain.exists():
        plain.unlink()
    write_jsonl(run_dir / "enriched.jsonl.gz", enriched, compress=True)
    write_jsonl(run_dir / "quarantine.jsonl", [r for r in graded if r["status"] == "quarantined" and r["reason"] != "out_of_scope_budget_sample"])

    calls = []
    for r in con.execute("SELECT * FROM calls ORDER BY started_at"):
        d = dict(r)
        d["review_ids"] = json.loads(d["review_ids"])
        calls.append({k: d[k] for k in CALL_FIELDS})
    write_jsonl(out_dir / "calls.jsonl", calls)
    for name in ("membership.csv", "ranking.csv", "claims.csv", "checkpoint_before.json", "checkpoint_after.json"):
        src = run_dir / name
        if src.exists():
            shutil.copyfile(src, out_dir / name)
        else:
            log(f"export: {name} not present yet")
    counts = {}
    for r in graded:
        key = r["status"] if r["status"] == "completed" else f"quarantined:{r['reason'].split(':')[0]}"
        counts[key] = counts.get(key, 0) + 1
    summary = {"records": len(graded), "status_counts": counts, "calls": len(calls),
               "cache_reuse_rows": sum("cache_source_id" in r for r in graded),
               "files": {p.name: file_sha(p) for p in sorted(out_dir.iterdir()) if p.is_file()}}
    write_json(run_dir / "export_summary.json", summary)
    log(f"export: {summary['records']} records {counts}; {len(calls)} calls -> {out_dir}")
    return summary
