# Spotify Insight Pipeline - multi-agent review analysis

**Question:** advising Spotify at the end of the review window (May 2022 - Nov 2023), where should the next quarter of product effort go - access, usability, playback, or billing/support?

**Live dashboard:** https://spotify-insight-dashboard-rosy.vercel.app (public, no login; reads the processed data from Supabase Postgres through a Vercel serverless backend)
**Decision memo:** [`runs/final/memo.md`](runs/final/memo.md) · **Grading export:** [`grading/`](grading/) · **Cost/runtime calculator:** [`cost/`](cost/) · **Architecture:** [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)

{{RESULTS}}

---

## Rubric -> evidence map

| Rubric criterion | Evidence |
|---|---|
| **D1 Accessible code/setup and artifacts** | [Setup](#setup) and [commands](#commands) below; all outputs committed under `runs/final/`, `grading/`, `cost/`, `evals/`; no API key needed to inspect, replay cost or rerank |
| **D2 Architecture, shared schema, provenance** | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (diagram, code/model boundaries, stage contracts); labels [`labels/taxonomy.json`](labels/taxonomy.json); prompts [`prompts/`](prompts/); every record carries `source_sha256` + `label_config`; duplicates carry `cache_source_id`; [`runs/final/data_manifest.json`](runs/final/data_manifest.json) |
| **D3 Memo numbers linked to calculations and evidence** | [`runs/final/memo.md`](runs/final/memo.md) cites `[C#]` rows of [`grading/claims.csv`](grading/claims.csv), re-checked by code against [`grading/ranking.csv`](grading/ranking.csv) and by the course checker ([`evals/self_check_summary.json`](evals/self_check_summary.json)); the dashboard re-checks every claim against the database live |
| **D4 Recommendation, alternatives, limitations** | Memo sections *Recommendation / Alternatives considered / Limitations*; [Limitations](#limitations) below |
| **T1 50 human labels, per-field comparison, error analysis** | [`evals/golden/`](evals/golden/): `golden_50_labels_v1.csv` (official), `golden_50_labels_v2.csv` + [`v2_revision_log.csv`](evals/golden/v2_revision_log.csv), per-case results `v1/golden_per_case.csv`, summaries `v1/golden_results.json`, `v2/golden_results.json`; [error analysis](#golden-set-evaluation) below |
| **T2 Independent verification, planted-error and injection tests** | [`runs/final/verify/`](runs/final/verify/) (verifier report, `disagreements.csv`, `planted_error_cases.json`); [`evals/system/system_test_results.json`](evals/system/system_test_results.json) (injection, malformed output, retry, spend-cap) |
| **T3 Real cold/warm pilot, offline calculator, retry/spend/recovery controls** | [`cost/report.md`](cost/report.md), [`cost/pilot_calls.jsonl`](cost/pilot_calls.jsonl), [`cost/pilot_records.jsonl`](cost/pilot_records.jsonl), [`cost/usage.csv`](cost/usage.csv), [`cost/rates.csv`](cost/rates.csv); `python cost/calculator.py` (offline); controls demonstrated in `evals/system/` and the recorded interruption |
| **W1 Full ingestion, coverage, classification** | [`runs/final/ingestion_report.json`](runs/final/ingestion_report.json), [`grading/ingestion.json`](grading/ingestion.json) (identical to the checker's profile), [`grading/records.jsonl.gz`](grading/records.jsonl.gz) (one record per source ID) |
| **W2 Staged program, bounded calls, saved handoffs, resume** | `python -m pipeline run --input <csv> --run-dir <dir>`; `grading/calls.jsonl` (every call <=50 reviews); `grading/checkpoint_before.json` / `checkpoint_after.json`; [recording](#interruption-and-resume) |
| **W3 Reproducible ranking + deployed dashboard with DB + AI recommendations** | `python -m pipeline rerank ...` reproduces `ranking.csv` byte-for-byte; dashboard URL above; schema [`db/schema.sql`](db/schema.sql); loader [`pipeline/publish.py`](pipeline/publish.py); backend [`dashboard/api/`](dashboard/api/) |

---

## Setup

Python 3.11+ (tested 3.13.5), Node 20+ only for the dashboard.

```bash
git clone https://github.com/victoriamanning/spotify-insight-pipeline && cd spotify-insight-pipeline
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt      # exact versions: requirements.lock.txt
cp .env.example .env                                                     # only needed for paid runs / publishing
```

**Data.** Download the course ZIP ([Google Drive](https://drive.google.com/file/d/1P0rUoAS_wVjp3BYKqXMEyD4u0uJP1Bvf/view)), unzip, and put the CSVs in `data/` (not committed). `spotify_reviews_18months.csv` must have SHA-256 `1fc85de68a304dd8978b537cfa58793d5f41cbaf417fa32cb53899f83a2fcef6` (97,400,616 bytes). Source: BwandoWando, *3.4 Million Spotify Google Store Reviews* v2, Kaggle, CC0.

**Keys** (`.env`, never committed; `.gitignore` excludes `.env` and `.env.*` except `.env.example`): `OPENAI_API_KEY` for paid stages, `DATABASE_URL` (Supabase pooler URI) for `publish`. The deployed dashboard uses a separate **read-only** Postgres role stored only as a Vercel environment variable.

## Commands

No key needed:

```bash
python cost/calculator.py                                    # offline cost/runtime replay -> cost/report.md
python cost/calculator.py --rate-multiplier 2                # doubles API cost, time unchanged
python -m pipeline rerank --records grading/records.jsonl.gz --membership grading/membership.csv --out /tmp/ranking.csv
python evals/eval_golden.py --records runs/final/golden_predictions.jsonl.gz --labels evals/golden/golden_50_labels_v1.csv --out /tmp
python evals/system_tests.py                                 # offline system tests (mock provider)
python vendor/check_submission.py reference --full data/spotify_reviews_18months.csv --analysis data/spotify_reviews_18months.csv --out local-reference.json
python vendor/check_submission.py check --reference local-reference.json --submission grading --out self-check.json
python -m pipeline run --input data/cost_100.csv --run-dir runs/smoke --provider mock   # whole pipeline, synthetic labels, $0
```

Paid (explicit):

```bash
python -m pipeline pilot                                     # real cold + warm 100-review pilot (~$0.005)
python -m pipeline run --input data/spotify_reviews_18months.csv --run-dir runs/final --workers 8
python -m pipeline enrich --run-dir runs/final --limit 500   # any stage alone; re-running resumes
python -m pipeline.publish --run-dir runs/final              # load Postgres for the dashboard
```

Any CSV with the six source columns works as `--input`.

{{BODY}}
