"""Stage 4 - GROUP (code: stable membership; model: issue-namer role on bounded examples).

Membership is deterministic: every completed complaint/cancellation record belongs to exactly one issue,
issue_id = "<topic>.<subtopic>". The model only proposes a human-readable name and description from at most
`examples_per_issue` quotes per issue; it cannot add, drop or merge issues or members. The accepted mapping is
saved so ranking never needs a model.
"""

import hashlib
import json
from collections import defaultdict
from pathlib import Path

from . import db
from .calllog import log_attempts
from .common import ROOT, COMPLAINT_INTENTS, csv_rows, label_config, load_taxonomy, write_csv, write_json, Clock
from .llm import BudgetExceeded
from .records import load_completed, primary_lc

SCHEMA = {"name": "issue_names", "schema": {
    "type": "object", "additionalProperties": False, "required": ["issues"],
    "properties": {"issues": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["issue_id", "name", "description", "example_review_ids"],
        "properties": {"issue_id": {"type": "string"}, "name": {"type": "string"}, "description": {"type": "string"},
                       "example_review_ids": {"type": "array", "items": {"type": "string"}}}}}}}}


def golden_ids(input_csv):
    p = Path(input_csv).parent / "golden_50_to_label.csv"
    return {r["review_id"] for r in csv_rows(p)} if p.exists() else set()


def build_membership(completed):
    members = defaultdict(list)
    for rid, (rec, _) in completed.items():
        if rec["intent"] in COMPLAINT_INTENTS:
            members[f"{rec['topic']}.{rec['subtopic']}"].append(rid)
    return {k: sorted(v) for k, v in sorted(members.items())}


def pick_examples(ids, completed, exclude, k, seed):
    """Bounded, deterministic example pack: distinct texts, model-labelled originals only, golden IDs excluded."""
    seen, out = set(), []
    for rid in sorted(ids, key=lambda r: hashlib.sha256(f"{seed}:group:{r}".encode()).hexdigest()):
        rec, meta = completed[rid]
        if rid in exclude or "cache_source_id" in rec or rec["evidence_quote"] in seen:
            continue
        seen.add(rec["evidence_quote"])
        out.append((rid, rec["evidence_quote"][:200]))
        if len(out) >= k:
            break
    return out


def run(cfg, run_dir, caller, run_id, input_csv, log=print):
    clock = Clock()
    run_dir = Path(run_dir)
    con = db.connect(run_dir / "state.sqlite")
    g = cfg["roles"]["group"]
    glc = label_config({**g, "schema_version": "json_v1"})
    tax = load_taxonomy()
    completed = load_completed(con, primary_lc(cfg))
    members = build_membership(completed)
    exclude = golden_ids(input_csv)
    packs = {iid: pick_examples(ids, completed, exclude, g["examples_per_issue"], cfg["scope"]["seed"]) for iid, ids in members.items()}
    write_csv(run_dir / "membership.csv", ["issue_id", "review_id"],
              [{"issue_id": iid, "review_id": rid} for iid, ids in members.items() for rid in ids])

    blocks = []
    for iid, ids in members.items():
        topic, sub = iid.split(".", 1)
        desc = tax["topics"][topic]["subtopics"].get(sub, "")
        ex = "\n".join(f"  [{rid}] {q}" for rid, q in packs[iid])
        blocks.append(f"ISSUE {iid}\n  topic: {topic} - {tax['topics'][topic]['definition']}\n  subtopic: {sub} - {desc}\n"
                      f"  members: {len(ids)}\n  examples:\n{ex}")
    user = "Name these candidate issues.\n\n" + "\n\n".join(blocks)
    instructions = (ROOT / g["prompt"]).read_text(encoding="utf-8")
    sent_ids = sorted({rid for p in packs.values() for rid, _ in p})
    input_sha = hashlib.sha256((glc + instructions + user).encode()).hexdigest()
    names, status, err = {}, "model", ""
    prev = run_dir / "issues.json"
    if prev.exists():
        old = json.loads(prev.read_text(encoding="utf-8"))
        if old.get("input_sha256") == input_sha and old.get("naming_status") == "model":
            names = {i["issue_id"]: {k: i[k] for k in ("name", "description", "example_review_ids")} for i in old["issues"]}
            status = "model_cached"
            log("group: identical input and settings -> reusing saved issue names (0 calls)")
    for attempt in range(0 if names else 2):  # one retry on invalid output
        try:
            attempts, final = caller.call("group", g["model"], g["effort"], instructions, user, g["max_output_tokens"],
                                          json_schema=SCHEMA, cache_key="group-v1")
        except BudgetExceeded as e:
            status, err = "code_fallback", f"budget stop: {e}"
            break
        log_attempts(con, run_dir, run_id, attempts, "group", "initial", glc, sent_ids)
        if not final:
            err = attempts[-1].error
            continue
        try:
            names, err = validate(json.loads(final.text), packs), ""
            break
        except (ValueError, KeyError) as e:
            err = f"invalid_group_output: {e}"
    if not names:
        status = "code_fallback"
        names = {iid: {"name": iid.replace(".", ": ").replace("_", " "), "description": "", "example_review_ids": [r for r, _ in packs[iid][:2]]}
                 for iid in members}
    issues = [{"issue_id": iid, "topic": iid.split(".")[0], "subtopic": iid.split(".", 1)[1], "member_count": len(ids),
               **names[iid], "naming_source": status, "example_pack": [{"review_id": r, "quote": q} for r, q in packs[iid]]}
              for iid, ids in members.items()]
    write_json(run_dir / "issues.json", {"label_config": glc, "naming_status": "model" if status == "model_cached" else status,
                                          "input_sha256": input_sha, "reused_saved_output": status == "model_cached", "error": err,
                                          "membership_rule": "one issue per complaint/cancellation record: topic.subtopic",
                                          "issues": issues, "runtime_s": clock.elapsed()})
    log(f"group: {len(issues)} issues, {sum(len(v) for v in members.values())} memberships, naming={status} {err}")
    return issues


def validate(obj, packs):
    got = {i["issue_id"]: i for i in obj["issues"]}
    if set(got) != set(packs):
        raise ValueError(f"issue IDs changed: missing {sorted(set(packs) - set(got))[:3]} extra {sorted(set(got) - set(packs))[:3]}")
    out = {}
    for iid, item in got.items():
        allowed = {r for r, _ in packs[iid]}
        bad = [r for r in item["example_review_ids"] if r not in allowed]
        if bad:
            raise ValueError(f"{iid}: example IDs not in its pack: {bad[:2]}")
        out[iid] = {"name": item["name"].strip()[:80], "description": item["description"].strip()[:300],
                    "example_review_ids": item["example_review_ids"][:3]}
    return out
