# 100-review cost and runtime report

Generated offline by `cost/calculator.py` from saved usage (`pilot_calls.jsonl`) and editable rates (`rates.csv`). No API calls were made to produce this report.

## Measured: real 100-review pilot (`cost_100.csv`, unchanged)

- Input checksum: `c884ac3b9be5066995d5063f96ad9af6e5e082975788c1684c4f6b6ea661dd0e` (100 rows, 100 distinct texts)
- Final statuses: {'completed': 100}
- Cold run: empty result cache, 1 worker; wall-clock **88.2 s**; API cost **$0.004818**
- Warm run: same settings, saved results; wall-clock **0.0 s**; new enrichment calls **0**; incremental API cost **$0.000000** (downstream group/memo/verify reuse saved outputs keyed by input hash)
- Cost per 1,000 input rows (cold, all stages): **$0.0482**; per completed record: **$0.000048**
- Enrichment throughput (cold, 1 worker): **3.20 texts/s**

### By stage (cold and warm)

| run | role | model | effort | label_config | requests | failed | retries | items sent | input tok | cached tok | output tok | reasoning tok | summed call s | API cost USD |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cold | enrich | gpt-6-luna | none | `gpt-6-luna@none+enrich_v1+lines_v1` | 2 | 0 | 0 | 100 | 6368 | 1987 | 2792 | 0 | 31.3 | 0.001854 |
| cold | group | gpt-6-luna | low | `gpt-6-luna@low+group_v1+json_v1` | 1 | 0 | 0 | 48 | 2835 | 0 | 1405 | 0 | 19.7 | 0.000986 |
| cold | memo | gpt-6-luna | low | `gpt-6-luna@low+memo_v1+json_v1` | 2 | 0 | 0 | 58 | 5136 | 0 | 1993 | 348 | 26.7 | 0.001510 |
| cold | verify | gpt-6-luna | none | `gpt-6-luna@none+verify_v1+lines_v1` | 2 | 0 | 0 | 50 | 2087 | 0 | 518 | 0 | 10.4 | 0.000468 |

Stage wall-clock seconds (cold): ingest 0.00, enrich 31.28, verify 10.45, group 19.76, rank 0.02, memo 26.70, export 0.03

Stage wall-clock seconds (warm): ingest 0.00, enrich 0.00, verify 0.00, group 0.00, rank 0.01, memo 0.00, export 0.01

Batch size 50 (enrich), 25 (verify); 1 worker; provider OpenAI Responses API, standard tier (no Batch API discount applied to measured costs).
Summed call seconds are not wall-clock time. Local compute (laptop CPU, SQLite) is not billed: **API spend only**; local electricity/hardware cost is **unknown, not zero**.

## Rates used (editable `cost/rates.csv`)

| model | tier | item | USD per unit | quoted | checked | source |
|---|---|---|---|---|---|---|
| gpt-6-luna | standard | input | 0.0000001000 | $0.10 per 1M tokens | 2026-10-19 | https://developers.openai.com/api/docs/models/gpt-6-luna |
| gpt-6-luna | standard | cached_input | 0.0000000100 | $0.01 per 1M tokens | 2026-10-19 | https://developers.openai.com/api/docs/models/gpt-6-luna |
| gpt-6-luna | standard | output | 0.0000005000 | $0.50 per 1M tokens (includes reasoning tokens) | 2026-10-19 | https://developers.openai.com/api/docs/models/gpt-6-luna |
| gpt-6-luna | batch | input | 0.0000000500 | $0.05 per 1M tokens (50% of standard; scenario only - not used in measured runs) | 2026-10-19 | https://developers.openai.com/api/docs/models/gpt-6-luna |
| gpt-6-luna | batch | cached_input | 0.0000000050 | $0.005 per 1M tokens (scenario only) | 2026-10-19 | https://developers.openai.com/api/docs/models/gpt-6-luna |
| gpt-6-luna | batch | output | 0.0000002500 | $0.25 per 1M tokens (scenario only) | 2026-10-19 | https://developers.openai.com/api/docs/models/gpt-6-luna |

Formula: `item_cost = billed_units x price_per_unit`; items are mutually exclusive (uncached input = input - cached; output includes reasoning). Per-call items are in `usage.csv`.

## Refreshed measurements from the main run

| checkpoint (texts) | measured texts | enrich cost USD | cost/text | output tok/text | summed call s |
|---|---|---|---|---|---|
| 550 | 550 | 0.017770 | 0.00003231 | 50.3 | 486 |
| 10050 | 10087 | 0.219077 | 0.00002172 | 37.3 | 3651 |

## Actual full run (`runs/final`), recomputed from its call log

Scope grew from the declared 100k sample to the full corpus after the budget allowed; the remaining 406,090 distinct texts used the OpenAI **Batch API** (50% rates, tier recorded per call).

| role | tier | calls | failed | review IDs sent | input tok | cached tok | output tok | API USD |
|---|---|---|---|---|---|---|---|---|
| enrich | batch | 8122 | 3 | 406090 | 26956131 | 16160911 | 14383804 | 4.2165 |
| enrich | standard | 1897 | 9 | 81766 | 5828863 | 3772397 | 2875335 | 1.6810 |
| group | standard | 4 | 0 | 1280 | 53952 | 26954 | 15948 | 0.0109 |
| memo | standard | 10 | 0 | 360 | 34950 | 3675 | 11372 | 0.0089 |
| verify | standard | 131 | 0 | 3261 | 136870 | 0 | 34280 | 0.0308 |

**Total full-run API spend: $5.9482** (pilot excluded; spend cap $6.50).

## Projections (estimates, not measurements)

Controls: spend limit **$6.50**, output cap **60** tokens/review, max workers **8**, max fallback fraction **2%**, verify 2% (min 50, max 2000).
Full corpus accounting: 660,622 rows = 660,609 nonempty + 13 empty-text quarantines; 484,189 distinct nonempty texts.
Inputs: enrich $0.00002172/text (refreshed), verify $0.00000935/review, fixed group+memo $0.002496 once, measured failed-request rate 0.0%. Time assumes linear speed-up to 8 workers (modeled, not measured).

| scope | case | texts | enrich | fallback | verify | group+memo | **total USD** | est. hours | budget |
|---|---|---|---|---|---|---|---|---|---|
| declared scope (100k sample, exact-text reuse) | base | 78,099 | 1.7132 | 0.0678 | 0.0148 | 0.0025 | **1.7983** | 0.85 | within limit |
| declared scope (100k sample, exact-text reuse) | conservative | 78,099 | 2.0278 | 0.2714 | 0.0150 | 0.0025 | **2.3168** | 1.27 | within limit |
| full corpus with exact-text reuse | base | 484,189 | 10.6211 | 0.4206 | 0.0189 | 0.0025 | **11.0632** | 5.26 | **EXCEEDS LIMIT** |
| full corpus with exact-text reuse | conservative | 484,189 | 12.5720 | 1.6826 | 0.0193 | 0.0025 | **14.2763** | 7.89 | **EXCEEDS LIMIT** |
| full corpus, no reuse | base | 660,609 | 14.4911 | 0.5739 | 0.0189 | 0.0025 | **15.0864** | 7.18 | **EXCEEDS LIMIT** |
| full corpus, no reuse | conservative | 660,609 | 17.1527 | 2.2956 | 0.0193 | 0.0025 | **19.4701** | 10.76 | **EXCEEDS LIMIT** |

Assumptions file: `cost/assumptions.json`. Changing projected volume does not change the measured pilot section above.

