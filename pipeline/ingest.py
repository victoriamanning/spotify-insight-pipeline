"""Stage 1 - PREPARE (code only, no model).

Reads every row of the input CSV with a multiline-safe parser, preserves the six source strings and IDs,
hashes each row exactly as the grading contract specifies, profiles quality, finds exact duplicate texts,
selects the declared classification scope and builds the pending work queue (one item per distinct text).
"""

from collections import Counter
from pathlib import Path

from vendor import check_submission

from . import db
from .common import ROOT, csv_rows, file_sha, label_config, row_sha, sample_key, text_sha, write_json, Clock


def priority_ids(data_dir, files):
    order = []
    for name in files:
        p = Path(data_dir) / name
        if p.exists():
            order += [r["review_id"] for r in csv_rows(p)]
    seen, out = set(), []
    for rid in order:
        if rid not in seen:
            seen.add(rid)
            out.append(rid)
    return out


def run(cfg, csv_path, run_dir, sample_size=None, log=print):
    clock = Clock()
    csv_path = Path(csv_path)
    con = db.connect(Path(run_dir) / "state.sqlite")
    enrich_cfg = cfg["roles"]["enrich"]
    lc = label_config(enrich_cfg)
    seed = cfg["scope"]["seed"]
    sample_size = int(sample_size or cfg["scope"]["sample_size"])

    existing = con.execute("SELECT COUNT(*) c FROM reviews").fetchone()["c"]
    if existing:
        log(f"ingest: state already holds {existing} rows; reusing (delete the run folder to re-ingest)")
        return db.get_meta(con, "ingestion_report")

    rows, ids = [], set()
    dup_ids = 0
    for i, r in enumerate(csv_rows(csv_path)):
        if r["review_id"] in ids:
            dup_ids += 1
            continue  # cannot happen for the course file; counted and reported if it does
        ids.add(r["review_id"])
        rows.append((i, r))
    log(f"ingest: parsed {len(rows)} rows in {clock.elapsed()}s")

    # Declared scope: priority development/golden files first (if present), then seeded-hash order.
    pri = priority_ids(csv_path.parent, cfg["scope"]["priority_files"]) if sample_size < len(rows) else []
    pri_rank = {rid: k for k, rid in enumerate(pri)}
    nonempty = [(i, r) for i, r in rows if r["review_text"].strip()]
    nonempty.sort(key=lambda x: (0, pri_rank[x[1]["review_id"]], "") if x[1]["review_id"] in pri_rank
                  else (1, 0, sample_key(seed, x[1]["review_id"])))
    queue_rank = {r["review_id"]: k for k, (_, r) in enumerate(nonempty)}
    in_sample = {r["review_id"] for _, r in nonempty[:sample_size]}

    con.execute("BEGIN")
    con.executemany(
        "INSERT INTO reviews(row_idx,review_id,review_text,review_rating,review_likes,app_version,review_timestamp,"
        "source_sha256,text_sha256,is_empty,in_sample,queue_rank) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [(i, r["review_id"], r["review_text"], r["review_rating"], r["review_likes"], r["app_version"], r["review_timestamp"],
          row_sha(r), text_sha(r["review_text"]), int(not r["review_text"].strip()), int(r["review_id"] in in_sample),
          queue_rank.get(r["review_id"])) for i, r in rows])
    # Work queue: one item per distinct in-sample text; the representative is its first ID in queue order.
    con.execute(
        "INSERT OR IGNORE INTO work(text_sha256,label_config,rep_review_id,queue_rank) "
        "SELECT text_sha256, ?, review_id, queue_rank FROM reviews r WHERE in_sample=1 AND queue_rank = "
        "(SELECT MIN(queue_rank) FROM reviews r2 WHERE r2.text_sha256=r.text_sha256 AND r2.in_sample=1)", (lc,))
    con.execute("COMMIT")

    texts = Counter(r["review_text"] for _, r in nonempty)
    sample_texts = {text_sha(r["review_text"]) for _, r in nonempty[:sample_size]}
    reuse_rows = con.execute(
        "SELECT COUNT(*) c FROM reviews WHERE is_empty=0 AND in_sample=0 AND text_sha256 IN "
        "(SELECT text_sha256 FROM work WHERE label_config=?)", (lc,)).fetchone()["c"]
    profile = check_submission.profile(csv_path)  # the course's exact deterministic profile
    empties = [r["review_id"] for _, r in rows if not r["review_text"].strip()]
    report = {
        "source_file": csv_path.name,
        "file_sha256": profile["file_sha256"],
        "bytes": csv_path.stat().st_size,
        "checker_profile": profile,
        "records": len(rows) + dup_ids,
        "unique_review_ids": len(rows),
        "duplicate_review_ids": dup_ids,
        "empty_review_text": len(empties),
        "empty_review_ids": empties,
        "missing_app_version": sum(1 for _, r in rows if not r["app_version"].strip()),
        "nonempty_reviews": len(nonempty),
        "distinct_nonempty_texts": len(texts),
        "rows_sharing_a_duplicated_text": sum(c for c in texts.values() if c > 1),
        "top_duplicate_texts": [{"text": t[:80], "count": c} for t, c in texts.most_common(15)],
        "ratings": dict(sorted(Counter(r["review_rating"] for _, r in rows).items())),
        "first_review": profile["first_review"], "last_review": profile["last_review"],
        "scope": {
            "label_config": lc,
            "sample_size_requested": sample_size,
            "sample_reviews": len(in_sample),
            "sample_distinct_texts_to_classify": len(sample_texts),
            "priority_ids_first": len(pri),
            "out_of_sample_rows_reusing_a_sample_text": reuse_rows,
            "rows_out_of_scope": len(nonempty) - len(in_sample) - reuse_rows,
            "ordering": "golden_50 -> checkpoint_500 -> analysis_10000 -> SHA-256(seed:review_id) ascending",
            "seed": seed,
        },
        "runtime_s": clock.elapsed(),
    }
    db.set_meta(con, "ingestion_report", report)
    db.set_meta(con, "input_csv", str(csv_path))
    write_json(Path(run_dir) / "ingestion_report.json", report)
    write_json(Path(run_dir) / "data_manifest.json", {
        "input_csv": csv_path.name, "file_sha256": profile["file_sha256"], "bytes": report["bytes"],
        "parsed_rows_sha256": profile["parsed_rows_sha256"], "records": report["records"],
        "dataset_manifest_sha256_expected": "1fc85de68a304dd8978b537cfa58793d5f41cbaf417fa32cb53899f83a2fcef6",
        "dataset_source": "https://drive.google.com/file/d/1P0rUoAS_wVjp3BYKqXMEyD4u0uJP1Bvf/view"})
    log(f"ingest: {report['records']} rows, {len(empties)} empty, {report['distinct_nonempty_texts']} distinct texts; "
        f"sample {len(in_sample)} reviews -> {len(sample_texts)} texts to classify; +{reuse_rows} rows reuse sample texts "
        f"({clock.elapsed()}s)")
    return report
