# Golden-50 labelling guide (human labels)

Open `golden_50_labels.csv` in Numbers/Excel and fill one row per review **from the review text only** (ignore stars). Save as CSV with the same name. These labels are never sent to a model; they are only compared with predictions afterwards by `evals/eval_golden.py`.

| column | what to write |
|---|---|
| `topic` | one of `access, usability, playback, downloads, catalog, billing, support, other` |
| `intent` | one of `cancellation, complaint, request, praise, unclear` (first that applies, in that order) |
| `severity` | 1-5 (below) |
| `sentiment` | a number from -1 to 1 (-1, -0.5, 0, 0.5, 1 is fine) |
| `evidence_quote` | the words that justify your label, copied exactly |
| `entities` | named features/devices/artists, `;`-separated, or blank |
| `needs_review` | `1` if you would want a second opinion, else `0` |
| `ambiguous` | `yes` if two labels are honestly defensible |
| `alt_topic` / `alt_intent` / `alt_severity` | the other acceptable label(s), only if ambiguous |
| `labeler_notes` | optional: why |

**Topic.** Pick the highest-severity specific problem; on a tie the first one mentioned. For praise, the first specific praised feature; general praise ("good app") is `other`. Premium mentioned alone is not billing; premium not activating, charges and premium-only controls (skip limits, shuffle-only) are billing. Ads are usability. Crashes/lag/won't play are playback. Political/boycott slogans and nonsense are `other` + `unclear`.

**Intent.** cancellation = explicitly leaving/uninstalling/cancelling or threatening to → complaint = any negative experience (including mixed) → request = wants something, nothing broken → praise → unclear.

**Severity.** 1 no problem / praise / unclear / pure request · 2 dislike, generic criticism, minor annoyance (most ad complaints) · 3 degraded or restricted, still usable · 4 core task blocked (can't log in, can't play) · 5 explicit serious financial/privacy/data harm (unauthorised charges, hacked account). Stars, anger or "I'm cancelling" alone don't raise severity.
