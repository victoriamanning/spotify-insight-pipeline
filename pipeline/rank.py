"""Stage 5 - RANK (code only, no model).

Baseline: priority(issue) = complaint_count x mean_severity = severity_sum, over completed complaint/cancellation
records in saved membership; each review counted once per issue; sort by score desc, then issue_id asc.
`rerank` recomputes this from saved files (records.jsonl[.gz] + membership.csv) with no model and no DB.
"""

from collections import Counter, defaultdict
from decimal import Decimal, ROUND_HALF_UP

from .common import COMPLAINT_INTENTS, TOPICS, read_csv, read_jsonl, write_csv, write_json

RANK_FIELDS = ["rank", "issue_id", "complaint_count", "severity_sum", "mean_severity", "priority_score"]


def mean_string(total, n):
    return str((Decimal(total) / Decimal(n)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


def pct(a, b):
    return str((Decimal(100 * a) / Decimal(b)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)) if b else "0.0"


def baseline(records, membership):
    """records: {review_id: record}; membership: [(issue_id, review_id)]. Returns ranking rows."""
    sev = defaultdict(list)
    seen = set()
    for iid, rid in membership:
        rec = records.get(rid)
        if rec is None or rec.get("status") != "completed" or rec["intent"] not in COMPLAINT_INTENTS or (iid, rid) in seen:
            continue
        seen.add((iid, rid))
        sev[iid].append(rec["severity"])
    rows = [{"issue_id": iid, "complaint_count": len(v), "severity_sum": sum(v), "mean_severity": mean_string(sum(v), len(v)),
             "priority_score": sum(v)} for iid, v in sev.items()]
    rows.sort(key=lambda r: (-r["priority_score"], r["issue_id"]))
    for k, r in enumerate(rows, 1):
        r["rank"] = k
    return [{f: str(r[f]) for f in RANK_FIELDS} for r in rows]


def rerank(records_path, membership_path, out_path):
    records = {r["review_id"]: r for r in read_jsonl(records_path)}
    membership = [(m["issue_id"], m["review_id"]) for m in read_csv(membership_path)]
    rows = baseline(records, membership)
    write_csv(out_path, RANK_FIELDS, rows)
    return rows


def analysis(completed, membership, issues_meta, out_dir):
    """Additional (non-baseline) aggregates for the memo and dashboard. completed: {rid: (rec, meta)}."""
    members = {rid: iid for iid, rid in membership}
    n = len(completed)
    topic = defaultdict(Counter)
    intents = Counter()
    for rid, (rec, meta) in completed.items():
        intents[rec["intent"]] += 1
        t = topic[rec["topic"]]
        t["records"] += 1
        if rec["intent"] in COMPLAINT_INTENTS:
            t["complaints"] += 1
            t["severity_sum"] += rec["severity"]
            t["cancellation"] += rec["intent"] == "cancellation"
            t["sev45"] += rec["severity"] >= 4
        t["needs_review"] += rec["needs_review"]
    total_complaints = sum(t["complaints"] for t in topic.values())
    topic_rows = []
    for name in TOPICS:
        t = topic[name]
        topic_rows.append({"topic": name, "records": t["records"], "complaints": t["complaints"],
                           "share_of_complaints_pct": pct(t["complaints"], total_complaints),
                           "severity_sum": t["severity_sum"], "mean_severity": mean_string(t["severity_sum"], t["complaints"]) if t["complaints"] else "",
                           "cancellation_records": t["cancellation"], "severity_4_or_5": t["sev45"], "needs_review": t["needs_review"]})
    topic_rows.sort(key=lambda r: (-r["severity_sum"], r["topic"]))
    write_csv(out_dir / "topic_aggregates.csv", list(topic_rows[0].keys()), topic_rows)

    # Focus areas named in the business question (billing and support combined as asked).
    areas = {"access": ["access"], "usability": ["usability"], "playback": ["playback"], "billing_support": ["billing", "support"],
             "downloads": ["downloads"], "catalog": ["catalog"], "other": ["other"]}
    area_rows = []
    for a, ts in areas.items():
        c = sum(topic[t]["complaints"] for t in ts)
        s = sum(topic[t]["severity_sum"] for t in ts)
        area_rows.append({"area": a, "topics": "+".join(ts), "complaints": c, "share_of_complaints_pct": pct(c, total_complaints),
                          "severity_sum": s, "mean_severity": mean_string(s, c) if c else "",
                          "cancellation_records": sum(topic[t]["cancellation"] for t in ts),
                          "severity_4_or_5": sum(topic[t]["sev45"] for t in ts)})
    area_rows.sort(key=lambda r: (-r["severity_sum"], r["area"]))
    write_csv(out_dir / "area_aggregates.csv", list(area_rows[0].keys()), area_rows)

    # Monthly complaint share by area with denominators (first and last months are partial).
    month = defaultdict(Counter)
    for rid, (rec, meta) in completed.items():
        m = meta["review_timestamp"][:7]
        month[m]["records"] += 1
        if rec["intent"] in COMPLAINT_INTENTS:
            for a, ts in areas.items():
                if rec["topic"] in ts:
                    month[m][a] += 1
    trend_rows = []
    months = sorted(month)
    for m in months:
        row = {"month": m, "completed_records": month[m]["records"], "partial_month": m in (months[0], months[-1])}
        for a in areas:
            row[f"{a}_complaints"] = month[m][a]
            row[f"{a}_share_pct"] = pct(month[m][a], month[m]["records"])
        trend_rows.append(row)
    write_csv(out_dir / "monthly_trends.csv", list(trend_rows[0].keys()), trend_rows)

    # Per-issue aggregates with extra context (cancellations, stars are metadata only).
    issue_rows = []
    by_issue = defaultdict(list)
    for iid, rid in membership:
        by_issue[iid].append(completed[rid][0])
    for iid, recs in by_issue.items():
        s = sum(r["severity"] for r in recs)
        issue_rows.append({"issue_id": iid, "name": issues_meta.get(iid, {}).get("name", iid), "complaint_count": len(recs),
                           "severity_sum": s, "mean_severity": mean_string(s, len(recs)), "priority_score": s,
                           "cancellation_records": sum(r["intent"] == "cancellation" for r in recs),
                           "severity_4_or_5": sum(r["severity"] >= 4 for r in recs),
                           "needs_review": sum(r["needs_review"] for r in recs),
                           "cache_reused_rows": sum("cache_source_id" in r for r in recs)})
    issue_rows.sort(key=lambda r: (-r["priority_score"], r["issue_id"]))
    write_csv(out_dir / "aggregates.csv", list(issue_rows[0].keys()), issue_rows)
    overall = {"completed_records": n, "intents": dict(intents), "complaint_or_cancellation_records": total_complaints,
               "cancellation_records": intents["cancellation"],
               "needs_review_records": sum(rec["needs_review"] for rec, _ in completed.values()),
               "cache_reused_records": sum("cache_source_id" in rec for rec, _ in completed.values())}
    write_json(out_dir / "overall_metrics.json", overall)
    return {"topics": topic_rows, "areas": area_rows, "trends": trend_rows, "issues": issue_rows, "overall": overall}
