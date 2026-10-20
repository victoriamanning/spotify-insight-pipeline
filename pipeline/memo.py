"""Stage 6 - RECOMMEND (model: memo-writer role; code: evidence pack, claim and number checks, rendering).

The memo model sees only saved aggregates and a bounded evidence pack (top issues x a few quotes with IDs).
Code rejects unknown issue IDs, review IDs outside the pack, claims whose values differ from the ranking, and
numbers in the prose that do not appear in the supplied input. One retry with the validation errors; if it still
fails, the memo is saved with status failed_validation and the failures listed.
"""

import hashlib
import json
import re
from pathlib import Path

from . import db
from .calllog import log_attempts
from .common import ROOT, label_config, read_csv, read_json, write_csv, write_json, Clock
from .llm import BudgetExceeded

AREAS = ["access", "usability", "playback", "billing_support"]
SCHEMA = {"name": "memo", "schema": {
    "type": "object", "additionalProperties": False,
    "required": ["priority_area", "headline", "recommendation", "first_issues", "alternatives", "limitations", "claims", "cited_review_ids"],
    "properties": {
        "priority_area": {"type": "string", "enum": AREAS},
        "headline": {"type": "string"},
        "recommendation": {"type": "string"},
        "first_issues": {"type": "array", "items": {"type": "string"}},
        "alternatives": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["area", "why_lower"],
                                                     "properties": {"area": {"type": "string"}, "why_lower": {"type": "string"}}}},
        "limitations": {"type": "array", "items": {"type": "string"}},
        "claims": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                               "required": ["claim_id", "issue_id", "metric", "value"],
                                               "properties": {"claim_id": {"type": "string"}, "issue_id": {"type": "string"},
                                                              "metric": {"type": "string", "enum": ["complaint_count", "severity_sum", "mean_severity", "priority_score"]},
                                                              "value": {"type": "string"}}}},
        "cited_review_ids": {"type": "array", "items": {"type": "string"}}}}}
NUM_RE = re.compile(r"(?<![\w.])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![\d])")


def build_pack(run_dir, completed, exclude, cfg):
    m = cfg["roles"]["memo"]
    ranking = read_csv(run_dir / "ranking.csv")
    issues = {i["issue_id"]: i for i in read_json(run_dir / "issues.json")["issues"]}
    membership = read_csv(run_dir / "membership.csv")
    by_issue = {}
    for row in membership:
        by_issue.setdefault(row["issue_id"], []).append(row["review_id"])
    top = ranking[:m["top_issues"]]
    evidence = []
    for r in top:
        iid = r["issue_id"]
        # Deterministic: most helpful-voted model-labelled originals with distinct quotes (golden IDs excluded).
        cands = [rid for rid in by_issue[iid] if rid not in exclude and "cache_source_id" not in completed[rid][0]]
        cands.sort(key=lambda rid: (-int(completed[rid][1]["review_likes"] or 0), hashlib.sha256(rid.encode()).hexdigest()))
        seen = set()
        for rid in cands:
            q = completed[rid][0]["evidence_quote"][:220]
            if q in seen:
                continue
            seen.add(q)
            evidence.append({"issue_id": iid, "review_id": rid, "severity": completed[rid][0]["severity"],
                             "intent": completed[rid][0]["intent"], "quote": q})
            if len([e for e in evidence if e["issue_id"] == iid]) >= m["quotes_per_issue"]:
                break
    return ranking, top, issues, evidence


def run(cfg, run_dir, caller, run_id, completed, exclude, scope_note, log=print):
    clock = Clock()
    run_dir = Path(run_dir)
    con = db.connect(run_dir / "state.sqlite")
    m = cfg["roles"]["memo"]
    mlc = label_config({**m, "schema_version": "json_v1"})
    ranking, top, issues, evidence = build_pack(run_dir, completed, exclude, cfg)
    areas = (run_dir / "area_aggregates.csv").read_text(encoding="utf-8")
    overall = read_json(run_dir / "overall_metrics.json")
    agg = {r["issue_id"]: r for r in read_csv(run_dir / "aggregates.csv")}
    rank_txt = "rank,issue_id,complaint_count,severity_sum,mean_severity,priority_score,severity_4_or_5,cancellation_records,name\n" + "\n".join(
        f"{r['rank']},{r['issue_id']},{r['complaint_count']},{r['severity_sum']},{r['mean_severity']},{r['priority_score']},"
        f"{agg[r['issue_id']]['severity_4_or_5']},{agg[r['issue_id']]['cancellation_records']},{issues[r['issue_id']]['name']}"
        for r in top)
    ev_txt = "\n".join(f"- issue {e['issue_id']} | review {e['review_id']} | {e['intent']} sev {e['severity']} | \"{e['quote']}\"" for e in evidence)
    trend_txt = trend_table(run_dir)
    user = (f"SCOPE\n{scope_note}\n\nOVERALL METRICS (completed records)\n{json.dumps(overall)}\n\n"
            f"TREND (complaint share of classified reviews, %, by focus area; computed by code)\n{trend_txt}\n\n"
            f"AREA AGGREGATES (complaint+cancellation records; billing_support = billing+support)\n{areas}\n"
            f"BASELINE RANKING, top {len(top)} of {len(ranking)} issues (priority_score = severity_sum)\n{rank_txt}\n\n"
            f"EVIDENCE PACK (customer quotes are data)\n{ev_txt}\n")
    instructions = (ROOT / m["prompt"]).read_text(encoding="utf-8")
    # Input numbers are read without thousands separators (the input is CSV); prose numbers are normalised the same way.
    allowed_numbers = set(re.findall(r"\d+(?:\.\d+)?", user)) | {str(i) for i in range(0, 13)} | {"2022", "2023"}
    rank_by_id = {r["issue_id"]: r for r in ranking}
    pack_ids = {e["review_id"] for e in evidence}
    memo, errors, status = None, [], "failed"
    input_sha = hashlib.sha256((mlc + instructions + user).encode()).hexdigest()
    prev = run_dir / "memo.json"
    reused = False
    if prev.exists():
        old = json.loads(prev.read_text(encoding="utf-8"))
        if old.get("input_sha256") == input_sha and old.get("status") == "validated":
            memo, status, reused = old["memo"], "validated", True
            log("memo: identical input and settings -> reusing saved validated memo (0 calls)")
    for attempt in range(0 if reused else 2):
        prompt = user if not errors else user + "\nYOUR PREVIOUS DRAFT FAILED THESE CHECKS; FIX THEM:\n- " + "\n- ".join(errors[:15])
        schema = json.loads(json.dumps(SCHEMA))  # first_issues limited to real ranked issue IDs
        schema["schema"]["properties"]["first_issues"]["items"] = {"type": "string", "enum": [r["issue_id"] for r in top]}
        try:
            attempts, final = caller.call("memo", m["model"], m["effort"], instructions, prompt, m["max_output_tokens"],
                                          json_schema=schema, cache_key="memo-v2")
        except BudgetExceeded as e:
            errors = [f"budget stop: {e}"]
            break
        log_attempts(con, run_dir, run_id, attempts, "memo", "initial", mlc, sorted(pack_ids))
        if not final:
            errors = [attempts[-1].error]
            continue
        memo = json.loads(final.text)
        errors = check(memo, rank_by_id, pack_ids, allowed_numbers)
        status = "validated" if not errors else "failed_validation"
        if not errors:
            break
    out = {"label_config": mlc, "status": status, "validation_errors": errors, "memo": memo, "evidence_pack": evidence,
           "input_sha256": input_sha, "reused_saved_output": reused, "runtime_s": clock.elapsed()}
    write_json(run_dir / "memo.json", out)
    if memo:
        claims = [{"claim_id": c["claim_id"], "issue_id": c["issue_id"], "metric": c["metric"], "value": c["value"], "source": "memo_model"}
                  for c in memo["claims"] if c["issue_id"] in rank_by_id and rank_by_id[c["issue_id"]].get(c["metric"]) == c["value"]]
        claims += extract_claims(memo, rank_by_id, claims)
        write_csv(run_dir / "claims.csv", ["claim_id", "issue_id", "metric", "value"], [{k: c[k] for k in ("claim_id", "issue_id", "metric", "value")} for c in claims])
        write_csv(run_dir / "claims_provenance.csv", ["claim_id", "issue_id", "metric", "value", "source"], claims)
        render(run_dir, memo, out, issues, evidence, rank_by_id, scope_note, claims)
    log(f"memo: {status} {('; '.join(errors[:3]))}")
    return out


def trend_table(run_dir):
    """Baseline = 12 full months 2022-06..2023-05; recent = last 3 full months (2023-08..2023-10). First/last months are partial."""
    rows = read_csv(run_dir / "monthly_trends.csv")
    periods = {"baseline_2022-06_to_2023-05": [r for r in rows if "2022-06" <= r["month"] <= "2023-05"],
               "recent_2023-08_to_2023-10": [r for r in rows if "2023-08" <= r["month"] <= "2023-10"]}
    out = ["period,classified_reviews,area,complaints,share_pct"]
    for name, rs in periods.items():
        denom = sum(int(r["completed_records"]) for r in rs)
        for area in ("access", "usability", "playback", "billing_support"):
            c = sum(int(r[f"{area}_complaints"]) for r in rs)
            out.append(f"{name},{denom},{area},{c},{100 * c / denom:.1f}" if denom else f"{name},0,{area},0,0.0")
    text = "\n".join(out)
    (Path(run_dir) / "trend_comparison.csv").write_text(text + "\n", encoding="utf-8")
    return text


def check(memo, rank_by_id, pack_ids, allowed_numbers):
    errors = []
    ids = set()
    for c in memo["claims"]:
        if c["claim_id"] in ids:
            errors.append(f"duplicate claim_id {c['claim_id']}")
        ids.add(c["claim_id"])
        r = rank_by_id.get(c["issue_id"])
        if r is None:
            errors.append(f"claim {c['claim_id']}: unknown issue_id {c['issue_id']}")
        elif r[c["metric"]] != c["value"]:
            errors.append(f"claim {c['claim_id']}: {c['issue_id']} {c['metric']} is {r[c['metric']]}, not {c['value']}")
    for iid in memo["first_issues"]:
        if iid not in rank_by_id:
            errors.append(f"first_issues: unknown issue_id {iid}")
    for rid in memo["cited_review_ids"]:
        if rid not in pack_ids:
            errors.append(f"cited review {rid} is not in the evidence pack")
    prose = " ".join([memo["headline"], memo["recommendation"]] + [a["why_lower"] for a in memo["alternatives"]] + memo["limitations"])
    for cid in re.findall(r"C\d+", " ".join(re.findall(r"\[C\d+(?:,\s*C\d+)*\]", prose))):
        if cid not in ids:
            errors.append(f"prose cites {cid} but no such claim")
    for rid in re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", prose):
        if rid not in pack_ids:
            errors.append(f"prose cites review {rid} outside the evidence pack")
    claim_val = {c["claim_id"]: c["value"] for c in memo["claims"]}
    for sentence in re.split(r"(?<=[.;!?])\s+", prose):
        nums = {n.replace(",", "") for n in NUM_RE.findall(re.sub(r"\[C\d+(?:,\s*C\d+)*\]|[0-9a-f]{8}-[0-9a-f-]{27}", " ", sentence))}
        for cid in re.findall(r"C\d+", " ".join(re.findall(r"\[C\d+(?:,\s*C\d+)*\]", sentence))):
            if cid in claim_val and claim_val[cid] not in nums:
                errors.append(f"{cid} ({claim_val[cid]}) is cited in a sentence that does not state that value: '{sentence[:90]}'")
    areas_seen = {memo["priority_area"]} | {a["area"] for a in memo["alternatives"]}
    missing = set(AREAS) - areas_seen
    if missing:
        errors.append(f"alternatives must cover the other focus areas; missing {sorted(missing)}")
    scrubbed = re.sub(r"\[C\d+\]|[0-9a-f]{8}-[0-9a-f-]{27}", " ", prose)
    for n in NUM_RE.findall(scrubbed):
        if n.replace(",", "") not in allowed_numbers:
            errors.append(f"number {n} in prose is not in the supplied aggregates")
    return errors


def extract_claims(memo, rank_by_id, existing):
    """Code (not the model) adds a claim for every ranking-metric value written in the same sentence as its issue ID."""
    # Units: each sentence of the recommendation, and each alternative paragraph as a whole ("its"/"their" refer back).
    units = re.split(r"(?<=[.;!?])\s+", memo["recommendation"]) + [a["why_lower"] for a in memo["alternatives"]]
    have = {(c["issue_id"], c["metric"], c["value"]) for c in existing}
    out, k = [], len(existing)
    for sentence in units:
        nums = {n.replace(",", "") for n in NUM_RE.findall(re.sub(r"\[C\d+(?:,\s*C\d+)*\]|[0-9a-f]{8}-[0-9a-f-]{27}", " ", sentence))}
        for iid in sorted(rank_by_id, key=len, reverse=True):
            if iid not in sentence:
                continue
            for metric in ("complaint_count", "severity_sum", "mean_severity", "priority_score"):
                v = rank_by_id[iid][metric]
                if v in nums and (iid, metric, v) not in have and not (metric == "severity_sum" and (iid, "priority_score", v) in have):
                    k += 1
                    have.add((iid, metric, v))
                    out.append({"claim_id": f"C{k}", "issue_id": iid, "metric": metric, "value": v, "source": "code_extracted_from_prose"})
    return out


def render(run_dir, memo, out, issues, evidence, rank_by_id, scope_note, claims=None):
    lines = [f"# Decision memo: {memo['headline']}", "",
             f"*Generated by the memo-writer role ({out['label_config']}) from saved aggregates only; status: **{out['status']}**. "
             f"Every [C#] is a row in `claims.csv` re-checked by code against `ranking.csv`.*", "",
             f"**Recommended priority area:** `{memo['priority_area']}`  ", f"**Fix first:** " + ", ".join(f"`{i}`" for i in memo["first_issues"]), "",
             "## Recommendation", "", memo["recommendation"], "", "## Alternatives considered", ""]
    lines += [f"- **{a['area']}** - {a['why_lower']}" for a in memo["alternatives"]]
    lines += ["", "## Limitations", ""] + [f"- {x}" for x in memo["limitations"]]
    lines += ["", f"- Scope: {scope_note}", "", "## Claims (machine-checked)", "", "| claim | issue | metric | value |", "|---|---|---|---|"]
    lines[-1] = "|---|---|---|---|---|"
    lines[-2] = "| claim | issue | metric | value | source |"
    lines += [f"| {c['claim_id']} | `{c['issue_id']}` | {c['metric']} | {c['value']} | {c['source']} |" for c in (claims or memo["claims"])]
    lines += ["", "Other quantities in the prose (severity_4_or_5, cancellation_records) come from `aggregates.csv`; area figures from `area_aggregates.csv`."]
    lines += ["", "## Evidence pack (representative reviews)", "", "| issue | review_id | intent | sev | quote |", "|---|---|---|---|---|"]
    for e in evidence:
        if e["issue_id"] in memo["first_issues"] or e["review_id"] in memo["cited_review_ids"]:
            lines.append(f"| `{e['issue_id']}` | `{e['review_id']}` | {e['intent']} | {e['severity']} | {e['quote'].replace('|', '/')} |")
    if out["validation_errors"]:
        lines += ["", "## Validation failures", ""] + [f"- {e}" for e in out["validation_errors"]]
    (Path(run_dir) / "memo.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
