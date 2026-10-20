"""Shared helpers: paths, config, hashing, atomic writes. No model calls here."""

import csv
import gzip
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIELDS = ("review_id", "review_text", "review_rating", "review_likes", "app_version", "review_timestamp")
TOPICS = ("access", "usability", "playback", "downloads", "catalog", "billing", "support", "other")
INTENTS = ("cancellation", "complaint", "request", "praise", "unclear")  # precedence order
COMPLAINT_INTENTS = ("complaint", "cancellation")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def row_sha(row):
    """Identical to check_submission.row_sha: SHA-256 of compact JSON of the six original strings."""
    return hashlib.sha256(canonical([row[k] for k in FIELDS]).encode("utf-8")).hexdigest()


def text_sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def sample_key(seed, review_id):
    return hashlib.sha256(f"{seed}:{review_id}".encode("utf-8")).hexdigest()


def csv_rows(path):
    """Same parser settings as the course checker (handles multiline quoted text)."""
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, strict=True)
        missing = [k for k in FIELDS if k not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path}: missing columns {missing}")
        for row in reader:
            if None in row or any(v is None for v in row.values()):
                raise ValueError("Malformed CSV row")
            yield row


def load_config(path=None):
    path = Path(path or ROOT / "config" / "pipeline.json")
    return json.loads(path.read_text(encoding="utf-8"))


def load_taxonomy():
    return json.loads((ROOT / "labels" / "taxonomy.json").read_text(encoding="utf-8"))


def label_config(role_cfg):
    return f"{role_cfg['model']}@{role_cfg['effort']}+{role_cfg['prompt_version']}+{role_cfg.get('schema_version', 'v1')}"


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def atomic_write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_json(path, value):
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False, allow_nan=False) + "\n")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_jsonl(path, rows, compress=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".tmp-" + path.name)
    opener = (lambda p: gzip.open(p, "wt", encoding="utf-8", compresslevel=6)) if compress else (lambda p: open(p, "w", encoding="utf-8"))
    with opener(tmp) as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, allow_nan=False) + "\n")
    os.replace(tmp, path)


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_csv(path, fieldnames, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".tmp-" + path.name)
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    os.replace(tmp, path)


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


class Clock:
    def __init__(self):
        self.t0 = time.perf_counter()

    def elapsed(self):
        return round(time.perf_counter() - self.t0, 3)
