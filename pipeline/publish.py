"""Load a finished run's processed data, aggregates and memo into Postgres (Supabase) for the dashboard. Code only.

  python -m pipeline.publish --run-dir runs/final        # needs DATABASE_URL in .env
"""

import argparse
import json
import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

from . import db
from .common import ROOT, load_config, read_csv, read_json
from .records import iter_records, primary_lc


def opt_json(path):
    return read_json(path) if Path(path).exists() else None


def main():
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    a = ap.parse_args()
    run_dir = Path(a.run_dir)
    run_id = run_dir.name
    cfg = load_config()
    lc = primary_lc(cfg)
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is not set (Supabase -> Connect -> Session pooler URI) in .env")
    state = db.connect(run_dir / "state.sqlite")
    ver = {r["review_id"]: dict(r) for r in state.execute("SELECT * FROM verify WHERE synthetic=0")}
    membership = {m["review_id"]: m["issue_id"] for m in read_csv(run_dir / "membership.csv")}
    issues_meta = {i["issue_id"]: i for i in read_json(run_dir / "issues.json")["issues"]}
    aggregates = {r["issue_id"]: r for r in read_csv(run_dir / "aggregates.csv")}
    ranking = read_csv(run_dir / "ranking.csv")
    memo = read_json(run_dir / "memo.json")
    calls = [dict(r) for r in state.execute("SELECT role, outcome, COUNT(*) n, SUM(input_tokens) input_tokens, SUM(cached_tokens) cached_tokens, "
                                            "SUM(output_tokens) output_tokens, SUM(cost_usd) cost_usd FROM calls GROUP BY role, outcome")]
    summary = {"ingestion": {k: v for k, v in db.get_meta(state, "ingestion_report").items() if k not in ("checker_profile", "empty_review_ids")},
               "export": opt_json(run_dir / "export_summary.json"), "overall": read_json(run_dir / "overall_metrics.json"),
               "overall_sample_only": opt_json(run_dir / "sample_only" / "overall_metrics.json"),
               "verifier": opt_json(run_dir / "verify" / "verifier_report.json"),
               "golden": opt_json(ROOT / "evals" / "golden" / "v1" / "golden_results.json"),
               "golden_v2": opt_json(ROOT / "evals" / "golden" / "v2" / "golden_results.json"),
               "calls": calls, "spent_usd": db.spent(state), "run_summary": opt_json(run_dir / "run_summary.json"),
               "cost_report": "cost/report.md"}
    with psycopg.connect(url, autocommit=False) as con:
        con.execute((ROOT / "db" / "schema.sql").read_text())
        con.execute("DELETE FROM runs WHERE run_id=%s", (run_id,))
        con.execute("INSERT INTO runs(run_id, source_sha256, label_config, summary) VALUES (%s,%s,%s,%s)",
                    (run_id, summary["ingestion"]["file_sha256"], lc, json.dumps(summary)))
        for r in ranking:
            m, g = issues_meta[r["issue_id"]], aggregates[r["issue_id"]]
            con.execute("INSERT INTO issues VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (run_id, int(r["rank"]), r["issue_id"], m.get("name"), m.get("description"), m["topic"], m["subtopic"],
                         int(r["complaint_count"]), int(r["severity_sum"]), r["mean_severity"], int(r["priority_score"]),
                         int(g["cancellation_records"]), int(g["severity_4_or_5"]), int(g["needs_review"]), m.get("example_review_ids", [])))
        for scope, path in (("all_completed", run_dir / "area_aggregates.csv"), ("sample_only", run_dir / "sample_only" / "area_aggregates.csv")):
            for r in read_csv(path):
                con.execute("INSERT INTO area_aggregates VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            (run_id, scope, r["area"], r["topics"], int(r["complaints"]), r["share_of_complaints_pct"], int(r["severity_sum"]),
                             r["mean_severity"] or None, int(r["cancellation_records"]), int(r["severity_4_or_5"])))
        for r in read_csv(run_dir / "monthly_trends.csv"):
            con.execute("INSERT INTO monthly_trends VALUES (%s,%s,%s,%s,%s)",
                        (run_id, r["month"], int(r["completed_records"]), r["partial_month"] == "True", json.dumps(r)))
        con.execute("INSERT INTO recommendations VALUES (%s,%s,%s,%s,%s,%s)",
                    (run_id, memo["label_config"], memo["status"], json.dumps(memo["memo"]), json.dumps(memo["evidence_pack"]),
                     json.dumps(memo["validation_errors"])))
        for c in read_csv(run_dir / "claims.csv"):
            con.execute("INSERT INTO claims VALUES (%s,%s,%s,%s,%s)", (run_id, c["claim_id"], c["issue_id"], c["metric"], c["value"]))
        n = 0
        with con.cursor().copy("COPY reviews FROM STDIN") as cp:
            for rec, meta in iter_records(state, lc):
                if rec["status"] == "quarantined" and rec["reason"] == "out_of_scope_budget_sample":
                    continue  # counted in runs.summary; not stored row-by-row
                v = ver.get(rec["review_id"], {})
                done = rec["status"] == "completed"
                cp.write_row((run_id, rec["review_id"], rec["status"], rec.get("reason"), rec.get("topic"), rec.get("subtopic"), rec.get("intent"),
                              rec.get("severity"), rec.get("sentiment"), rec.get("needs_review"), rec.get("evidence_quote"),
                              rec.get("entities") if done else None, rec.get("cache_source_id"), membership.get(rec["review_id"]),
                              int(meta["review_rating"]) if meta["review_rating"].isdigit() else None,
                              int(meta["review_likes"]) if meta["review_likes"].isdigit() else None, meta["review_timestamp"],
                              meta["in_sample"], v.get("topic"), v.get("intent"), v.get("severity")))
                n += 1
        con.execute("INSERT INTO status_counts SELECT run_id, status, COALESCE(reason,''), COUNT(*), "
                    "SUM(CASE WHEN cache_source_id IS NOT NULL THEN 1 ELSE 0 END) FROM reviews WHERE run_id=%s GROUP BY run_id, status, reason", (run_id,))
        con.commit()
    print(f"published run {run_id}: {len(ranking)} issues, {n} review rows, memo status {memo['status']}")


if __name__ == "__main__":
    main()
