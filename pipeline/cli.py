"""Orchestrator CLI. Code decides the next step; models are called only inside enrich/verify/group/memo.

  python -m pipeline run --input data/spotify_reviews_18months.csv --run-dir runs/final      # all stages
  python -m pipeline enrich --run-dir runs/final --limit 500                                  # one stage
  python -m pipeline rerank --records grading/records.jsonl.gz --membership grading/membership.csv --out ranking.csv
  python -m pipeline pilot                                                                     # PAID: cold+warm 100-review pilot
Use --provider mock for an offline smoke test (synthetic labels; never graded).
"""

import argparse
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from . import db, enrich, export, group, ingest, memo, rank, verify
from .common import ROOT, load_config, now_iso, read_csv, read_json, write_json, Clock
from .llm import Caller, Ledger, RateLimiter, load_rates
from .records import load_completed, primary_lc


def log_event(run_dir, **ev):
    ev = {"ts": now_iso(), **ev}
    with open(Path(run_dir) / "run_log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(ev) + "\n")
    return ev


def make_caller(cfg, run_dir, provider, fault_plan=None):
    con = db.connect(Path(run_dir) / "state.sqlite")
    stage_spent = {r["role"]: r["s"] for r in con.execute("SELECT role, SUM(cost_usd) s FROM calls GROUP BY role")}
    ledger = Ledger(cfg["budget"]["spend_cap_usd"], already_spent=db.spent(con), stage_caps=cfg["budget"]["stage_caps_usd"],
                    stage_spent=stage_spent)
    limiter = RateLimiter(cfg["roles"]["enrich"]["max_requests_per_minute"])
    return Caller(provider, load_rates(ROOT / cfg["rates_file"]), ledger, limiter, cfg["retry"], fault_plan=fault_plan)


def scope_note(run_dir):
    con = db.connect(Path(run_dir) / "state.sqlite")
    rep = db.get_meta(con, "ingestion_report")
    s = rep["scope"]
    return (f"{rep['records']} source rows ingested ({rep['empty_review_text']} empty texts quarantined). Classification scope: a uniform "
            f"seeded sample of {s['sample_reviews']} reviews ({s['sample_distinct_texts_to_classify']} distinct texts); "
            f"{s['out_of_sample_rows_reusing_a_sample_text']} further rows reuse an identical classified text; "
            f"{s['rows_out_of_scope']} rows were not classified (budget scope). Historical self-selected Google Play reviews, "
            f"{rep['first_review'][:10]} to {rep['last_review'][:10]}.")


def stage_checkpoint(run_dir, name, cfg):
    con = db.connect(Path(run_dir) / "state.sqlite")
    ids = sorted(rid for rid in load_completed(con, primary_lc(cfg)))
    write_json(Path(run_dir) / f"checkpoint_{name}.json", {"name": name, "saved_at": now_iso(), "completed_ids": ids,
                                                          "count": len(ids)})
    print(f"checkpoint_{name}.json: {len(ids)} completed IDs")
    return len(ids)


def do_rank(cfg, run_dir):
    con = db.connect(Path(run_dir) / "state.sqlite")
    completed = load_completed(con, primary_lc(cfg))
    membership = [(m["issue_id"], m["review_id"]) for m in read_csv(Path(run_dir) / "membership.csv")]
    rows = rank.baseline({rid: rec for rid, (rec, _) in completed.items()}, membership)
    rank.write_csv(Path(run_dir) / "ranking.csv", rank.RANK_FIELDS, rows)
    issues_meta = {i["issue_id"]: i for i in read_json(Path(run_dir) / "issues.json")["issues"]}
    rank.analysis(completed, membership, issues_meta, Path(run_dir))
    sample_only = {rid: v for rid, v in completed.items() if v[1]["in_sample"]}
    sdir = Path(run_dir) / "sample_only"
    sdir.mkdir(exist_ok=True)
    rank.analysis(sample_only, [(i, r) for i, r in membership if r in sample_only], issues_meta, sdir)
    print(f"rank: {len(rows)} issues ranked; top: " + ", ".join(f"{r['issue_id']}={r['priority_score']}" for r in rows[:5]))
    return rows


def run_stages(args, cfg, stages):
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    run_id = run_dir.name
    timings = {}
    caller = None
    if any(s in stages for s in ("enrich", "verify", "group", "memo")):
        caller = make_caller(cfg, run_dir, args.provider, fault_plan=json.loads(args.fault_plan) if getattr(args, "fault_plan", None) else None)
    total = Clock()
    result = {}
    for s in stages:
        c = Clock()
        log_event(run_dir, stage=s, event="start", provider=getattr(args, "provider", None))
        if s == "ingest":
            ingest.run(cfg, args.input, run_dir, sample_size=getattr(args, "sample_size", None))
        elif s == "enrich":
            summ = enrich.Enricher(cfg, run_dir, caller, run_id).run(limit=args.limit, workers=args.workers, phase=args.phase,
                                                                       stop_after_batches=args.stop_after_batches)
            result["enrich"] = summ
            if summ["stop_reason"] != "queue drained":
                if summ["phase"] == "initial" and not (run_dir / "checkpoint_before.json").exists() and summ["stop_reason"].startswith(("interrupted", "--stop-after")):
                    stage_checkpoint(run_dir, "before", cfg)
                timings[s] = c.elapsed()
                log_event(run_dir, stage=s, event="stopped", reason=summ["stop_reason"], seconds=timings[s])
                print(f"stopped: {summ['stop_reason']}. Re-run the same command to resume.")
                break
            if summ["phase"] == "resume" and (run_dir / "checkpoint_before.json").exists() and not (run_dir / "checkpoint_after.json").exists():
                stage_checkpoint(run_dir, "after", cfg)
            if args.limit is not None and stages[-1] != "enrich":
                pending = db.connect(run_dir / "state.sqlite").execute(
                    "SELECT COUNT(*) c FROM work WHERE status='pending' AND label_config=?", (primary_lc(cfg),)).fetchone()["c"]
                if pending:
                    print(f"enrich --limit reached; {pending} texts still pending; downstream stages run on completed records only")
        elif s == "verify":
            result["verify"] = verify.run(cfg, run_dir, caller, run_id)
        elif s == "group":
            con = db.connect(run_dir / "state.sqlite")
            group.run(cfg, run_dir, caller, run_id, db.get_meta(con, "input_csv"))
        elif s == "rank":
            do_rank(cfg, run_dir)
        elif s == "memo":
            con = db.connect(run_dir / "state.sqlite")
            completed = load_completed(con, primary_lc(cfg))
            result["memo"] = memo.run(cfg, run_dir, caller, run_id, completed, group.golden_ids(db.get_meta(con, "input_csv")), scope_note(run_dir))
        elif s == "export":
            export.run(cfg, run_dir, Path(args.export_dir) if getattr(args, "export_dir", None) else run_dir / "grading")
        timings[s] = c.elapsed()
        log_event(run_dir, stage=s, event="end", seconds=timings[s])
    con = db.connect(run_dir / "state.sqlite")
    calls = [dict(r) for r in con.execute("SELECT role, outcome, COUNT(*) n, SUM(input_tokens) i, SUM(cached_tokens) ci, "
                                          "SUM(output_tokens) o, SUM(cost_usd) usd FROM calls GROUP BY role, outcome")]
    summary = {"run_id": run_id, "finished_at": now_iso(), "stages": stages, "stage_seconds": timings, "wall_clock_s": total.elapsed(),
               "provider": getattr(args, "provider", None), "spend_cap_usd": cfg["budget"]["spend_cap_usd"],
               "spent_usd_total": round(db.spent(con), 6), "calls_by_role_outcome": calls,
               "queue_status": {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) n FROM work GROUP BY status")},
               "enrich": result.get("enrich")}
    write_json(run_dir / "run_summary.json", summary)
    if getattr(args, "summary_name", None):
        write_json(run_dir / f"run_summary_{args.summary_name}.json", summary)
    print(f"done: {stages} in {summary['wall_clock_s']}s; total spent ${summary['spent_usd_total']:.4f}")
    return summary


def main(argv=None):
    load_dotenv(ROOT / ".env")
    p = argparse.ArgumentParser(prog="pipeline", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, needs_input=False):
        sp.add_argument("--run-dir", required=True)
        sp.add_argument("--provider", default="openai", choices=["openai", "mock"])
        sp.add_argument("--limit", type=int, default=None, help="max NEW distinct texts to enrich in this invocation")
        sp.add_argument("--workers", type=int, default=None)
        sp.add_argument("--phase", choices=["initial", "resume"], default=None)
        sp.add_argument("--stop-after-batches", type=int, default=None, help="simulate an interruption after N batches")
        sp.add_argument("--fault-plan", default=None, help='test hook, e.g. {"transient_first_n": 2}')
        sp.add_argument("--summary-name", default=None)
        sp.add_argument("--export-dir", default=None)
        if needs_input:
            sp.add_argument("--input", required=True, help="input CSV path (any file with the six source columns)")
            sp.add_argument("--sample-size", type=int, default=None)

    common(sub.add_parser("run", help="ingest -> enrich -> verify -> group -> rank -> memo -> export"), needs_input=True)
    common(sub.add_parser("ingest"), needs_input=True)
    for name in ("enrich", "verify", "group", "rank", "memo", "export", "downstream"):
        common(sub.add_parser(name))
    sp = sub.add_parser("checkpoint"); sp.add_argument("--run-dir", required=True); sp.add_argument("--name", required=True, choices=["before", "after"])
    sp = sub.add_parser("rerank", help="offline: recompute baseline ranking from saved records + membership")
    sp.add_argument("--records", required=True); sp.add_argument("--membership", required=True); sp.add_argument("--out", required=True)
    sp = sub.add_parser("status"); sp.add_argument("--run-dir", required=True)
    sp = sub.add_parser("pilot", help="PAID: cold + warm run of the full pipeline on cost_100.csv (1 worker, empty cache)")
    sp.add_argument("--input", default=str(ROOT / "data" / "cost_100.csv")); sp.add_argument("--run-dir", default=str(ROOT / "runs" / "pilot-100"))
    sp.add_argument("--provider", default="openai", choices=["openai", "mock"])
    args = p.parse_args(argv)
    cfg = load_config(args.config)
    if getattr(args, "provider", None) == "mock":  # mock labels are marked in every label_config and cost nothing
        for role in cfg["roles"].values():
            role["model"] = "mock-" + role["model"]

    if args.cmd == "run":
        return run_stages(args, cfg, ["ingest", "enrich", "verify", "group", "rank", "memo", "export"])
    if args.cmd == "downstream":
        return run_stages(args, cfg, ["verify", "group", "rank", "memo", "export"])
    if args.cmd in ("ingest", "enrich", "verify", "group", "rank", "memo", "export"):
        return run_stages(args, cfg, [args.cmd])
    if args.cmd == "checkpoint":
        return stage_checkpoint(args.run_dir, args.name, cfg)
    if args.cmd == "rerank":
        rows = rank.rerank(args.records, args.membership, args.out)
        print(f"rerank: {len(rows)} issues -> {args.out}; top 3: " + ", ".join(f"{r['issue_id']}={r['priority_score']}" for r in rows[:3]))
        return rows
    if args.cmd == "status":
        con = db.connect(Path(args.run_dir) / "state.sqlite")
        print(json.dumps({"queue": {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) n FROM work GROUP BY status")},
                          "spent_usd": db.spent(con),
                          "calls": [dict(r) for r in con.execute("SELECT role, outcome, COUNT(*) n FROM calls GROUP BY role, outcome")]}, indent=2))
        return None
    if args.cmd == "pilot":
        run_dir = Path(args.run_dir)
        if (run_dir / "state.sqlite").exists():
            sys.exit(f"{run_dir} already exists; the cold pilot needs an empty result cache. Move it aside first.")
        base = dict(run_dir=str(run_dir), provider=args.provider, limit=None, workers=1, phase=None, stop_after_batches=None,
                    fault_plan=None, export_dir=str(run_dir / "grading"), input=args.input, sample_size=None)
        stages = ["ingest", "enrich", "verify", "group", "rank", "memo", "export"]
        print("== COLD run (empty cache, 1 worker) ==")
        run_stages(argparse.Namespace(**base, summary_name="cold"), cfg, stages)
        print("== WARM run (same settings, saved results) ==")
        run_stages(argparse.Namespace(**base, summary_name="warm"), cfg, stages)
        return None


if __name__ == "__main__":
    main()
