# Architecture

```mermaid
flowchart TB
  subgraph CODE1["1 PREPARE - code only (pipeline/ingest.py)"]
    CSV[(input CSV<br/>660,622 rows)] --> ING[parse multiline CSV<br/>row SHA-256, profile,<br/>exact-duplicate texts]
    ING --> SCOPE[declared scope:<br/>golden50 -> checkpoint500 -> analysis10k -> seeded hash<br/>100k reviews = 78,099 distinct texts]
    SCOPE --> Q[(SQLite work queue<br/>one item per distinct text x label_config<br/>status pending/completed/quarantined)]
  end
  subgraph S2["2 CLASSIFY - ENRICHER model role (pipeline/enrich.py, prompts/enrich_v1.md)"]
    Q --> DISP{orchestrator:<br/>budget reserve, rate limit,<br/><=8 workers}
    DISP -->|<=50 texts/request| LUNA1[gpt-6-luna effort=none]
    LUNA1 --> VAL[code: validate every ref, labels,<br/>ranges, quote is exact substring,<br/>rule checks]
    VAL -->|valid| SAVE[(atomic per-batch save<br/>+ calls.jsonl)]
    VAL -->|<=5 bad lines| FB[capped fallback: 1 review,<br/>effort=low, <=2%]
    VAL -->|whole request failed| RB[retry once as batch]
    FB --> VAL
    RB --> VAL
    VAL -->|2nd failure| QUAR[(quarantine + reason)]
    DISP -->|spent+reserved+next > cap,<br/>time cap, Ctrl-C| STOP[stop admitting work,<br/>finish in-flight, checkpoint]
  end
  subgraph S3["3 VERIFY - VERIFIER model role (pipeline/verify.py, prompts/verify_v1.md)"]
    SAVE --> SAMP[code: seeded 2% sample] --> LUNA2[gpt-6-luna: re-label from text only,<br/>never sees enrich labels] --> CMP[code: compare, disagreements.csv,<br/>planted-error test copy]
  end
  subgraph S4["4 GROUP - code membership + ISSUE-NAMER role (pipeline/group.py)"]
    SAVE --> MEM[code: each complaint/cancellation -> one issue<br/>issue_id = topic.subtopic -> membership.csv]
    MEM --> PACK[code: <=8 example quotes/issue, golden excluded] --> LUNA3[gpt-6-luna: names only;<br/>code rejects new IDs] --> ISS[(issues.json)]
  end
  subgraph S5["5 RANK - code only (pipeline/rank.py)"]
    MEM --> RANK[priority = count x mean severity = severity_sum<br/>sort score desc, issue_id asc] --> RK[(ranking.csv, aggregates.csv,<br/>area/topic/monthly tables)]
  end
  subgraph S6["6 RECOMMEND - MEMO-WRITER role (pipeline/memo.py)"]
    RK --> EP[code: top-12 issues + 3 quotes each] --> LUNA4[gpt-6-luna effort=low] --> CHK[code: claims = ranking values,<br/>review IDs in pack, every number<br/>in prose appears in input; 1 retry] --> MEMO[(memo.md, claims.csv)]
  end
  SAVE --> EXP[export: grading/ records.jsonl.gz, calls.jsonl, checkpoints]
  RK --> EXP
  MEMO --> EXP
  EXP --> PUB[publish.py: COPY into Supabase Postgres]
  PUB --> PG[(Supabase Postgres:<br/>runs, issues, reviews, areas,<br/>trends, recommendations, claims)]
  PG -->|read-only role| API[Vercel serverless API<br/>dashboard/api/*.js]
  API --> UI[Dashboard index.html<br/>metrics, ranking, AI recommendation<br/>with live claim re-check, review trace]
```

## Who decides what

| Stage | Code decides | Model judges | Why a model call is needed (or not) |
|---|---|---|---|
| 1 Prepare | everything: parsing, hashes, profile, duplicates, scope, queue | - | counting/sorting needs no model |
| 2 Classify | batching, IDs, schema, quote membership, retries, budget, state | topic, subtopic, intent, severity, sentiment, quote span, entities | reading messy, multilingual customer language |
| 3 Verify | sample, comparison, planted-error test | independent re-label | an independent second judgement estimates label error |
| 4 Group | membership (topic.subtopic) - deterministic | short human-readable issue names | naming from examples is a language task; membership is not |
| 5 Rank | all arithmetic | - | arithmetic must be exact and reproducible |
| 6 Recommend | evidence pack, claim/number/ID validation | argument and prose | synthesis; code guards every number |

## Stage contracts

| Stage | Input | Output (saved handoff) | Failure behaviour | Stop condition |
|---|---|---|---|---|
| ingest | CSV path | `state.sqlite` reviews+work, `ingestion_report.json`, `data_manifest.json` | malformed CSV raises; duplicate IDs counted | file read |
| enrich | pending work items | work rows completed/quarantined, `calls.jsonl`, `raw_failed_outputs.jsonl` | transient: backoff x4; invalid: retry once (batch) or capped fallback; then quarantine | queue drained, spend cap, time cap, `--limit`, Ctrl-C |
| verify | completed originals | `verify/verifier_report.json`, `disagreements.csv`, `planted_error_cases.json` | missing lines counted, not retried | sample done or verify cap |
| group | completed complaint/cancellation records | `membership.csv`, `issues.json` | invalid names -> retry once -> code names | one call (cached by input hash) |
| rank | records + membership | `ranking.csv`, `aggregates.csv`, `area_aggregates.csv`, `topic_aggregates.csv`, `monthly_trends.csv`, `sample_only/` | - | deterministic |
| memo | aggregates + evidence pack | `memo.json`, `memo.md`, `claims.csv` | failed checks -> retry once with errors -> saved as failed_validation | validated or 2 attempts |
| export | state + stage files | `grading/` | - | - |
| publish | run folder | Postgres tables | transaction rolls back | - |
