"""100-review cost and runtime calculator. OFFLINE by default: no API key, no network, no model calls.

  python cost/calculator.py                         # replay saved pilot usage with cost/rates.csv -> cost/report.md
  python cost/calculator.py --rate-multiplier 2     # what-if: doubles every API rate (usage and time unchanged)
  python cost/calculator.py --spend-limit 5 --workers 4 --fallback-fraction 0.05 --output-cap 60
  python cost/calculator.py --import-run runs/pilot-100           # copy a finished pilot's evidence into cost/
  python cost/calculator.py --refresh runs/final                  # add 500 / 10,000-text checkpoint measurements

The PAID pilot is a separate explicit command: `python -m pipeline pilot` (see cost/README.md).
"""

import argparse
import csv
import json
import shutil
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
FULL_ROWS, FULL_NONEMPTY, FULL_EMPTY, FULL_DISTINCT = 660622, 660609, 13, 484189


def load_rates(path, multiplier=1.0):
    rates = defaultdict(dict)
    meta = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rates[(r["model"], r["tier"])][r["item"]] = float(r["price_usd_per_unit"]) * multiplier
            meta.append(r)
    return rates, meta


def read_jsonl(p):
    p = Path(p)
    return [json.loads(l) for l in p.open(encoding="utf-8") if l.strip()] if p.exists() else []


def bill(call, rates, tier="standard"):
    """Mutually exclusive billing items; reasoning tokens are already inside output tokens."""
    r = rates.get((call["model"], tier))
    inp, cached, out = call["input_tokens"], call.get("cached_tokens", 0), call["output_tokens"]
    items = {"input": max(0, inp - cached), "cached_input": cached, "output": out}
    if r is None:
        return items, {k: None for k in items}, None
    costs = {k: items[k] * r[k] for k in items}
    return items, costs, sum(costs.values())


def stage_table(calls, rates):
    st = defaultdict(lambda: defaultdict(float))
    usage_rows = []
    for c in calls:
        items, costs, total = bill(c, rates)
        key = (c["run"], c["role"])
        s = st[key]
        s["requests"] += 1
        s["failed"] += c["outcome"] == "failed"
        s["retries"] += c.get("attempt", 1) > 1
        s["items_sent"] += len(c["review_ids"])
        s["input_tokens"] += c["input_tokens"]; s["cached_tokens"] += c.get("cached_tokens", 0); s["output_tokens"] += c["output_tokens"]
        s["reasoning_tokens"] += c.get("reasoning_tokens", 0)
        s["call_seconds"] += c.get("duration_s", 0)
        s["cost"] += total or 0
        s["model"] = c["model"]; s["effort"] = c.get("effort", ""); s["label_config"] = c["label_config"]
        for item, units in items.items():
            usage_rows.append({"run": c["run"], "request_id": c["request_id"], "role": c["role"], "model": c["model"], "tier": "standard",
                               "item": item, "billed_units": units, "unit": "token",
                               "price_usd_per_unit": (rates.get((c["model"], "standard")) or {}).get(item, ""),
                               "item_cost_usd": "" if costs[item] is None else round(costs[item], 10)})
    return st, usage_rows


def fmt(x, d=4):
    return "n/a" if x is None else f"{x:,.{d}f}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rates", default=HERE / "rates.csv")
    ap.add_argument("--rate-multiplier", type=float, default=1.0)
    ap.add_argument("--spend-limit", type=float, default=None)
    ap.add_argument("--output-cap", type=int, default=None, help="max output tokens per review (enrich)")
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--fallback-fraction", type=float, default=None)
    ap.add_argument("--scope-texts", type=int, default=None, help="distinct texts in the declared scope (default from assumptions)")
    ap.add_argument("--import-run", default=None)
    ap.add_argument("--refresh", default=None, help="main run dir: measure the first 550 and 10,050 enriched texts")
    ap.add_argument("--out", default=HERE / "report.md")
    a = ap.parse_args()

    if a.import_run:
        src = Path(a.import_run)
        calls = read_jsonl(src / "calls.jsonl")
        cold = json.loads((src / "run_summary_cold.json").read_text())
        warm = json.loads((src / "run_summary_warm.json").read_text())
        cold_ids = {c["request_id"] for c in calls if c["started_at"] <= cold["finished_at"]}
        with open(HERE / "pilot_calls.jsonl", "w", encoding="utf-8") as f:
            for c in calls:
                f.write(json.dumps({**c, "run": "cold" if c["request_id"] in cold_ids else "warm"}) + "\n")
        shutil.copyfile(src / "grading" / "records.jsonl.gz", HERE / "pilot_records.jsonl.gz")
        import gzip
        with gzip.open(HERE / "pilot_records.jsonl.gz", "rt", encoding="utf-8") as g, open(HERE / "pilot_records.jsonl", "w", encoding="utf-8") as f:
            f.write(g.read())
        (HERE / "pilot_records.jsonl.gz").unlink()
        json.dump({"cold": cold, "warm": warm, "ingestion": json.loads((src / "ingestion_report.json").read_text())},
                  open(HERE / "pilot_runs.json", "w"), indent=2)
        print("imported pilot evidence from", src)

    assumptions = json.loads((HERE / "assumptions.json").read_text())
    if a.spend_limit is not None: assumptions["spend_limit_usd"] = a.spend_limit
    if a.output_cap is not None: assumptions["output_cap_tokens_per_review"] = a.output_cap
    if a.workers is not None: assumptions["max_workers"] = a.workers
    if a.fallback_fraction is not None: assumptions["max_fallback_fraction"] = a.fallback_fraction
    if a.scope_texts is not None: assumptions["declared_scope_distinct_texts"] = a.scope_texts

    rates, rate_meta = load_rates(a.rates, a.rate_multiplier)
    calls = read_jsonl(HERE / "pilot_calls.jsonl")
    if not calls:
        raise SystemExit("No cost/pilot_calls.jsonl yet. Run the paid pilot (python -m pipeline pilot) then --import-run runs/pilot-100.")
    runs = json.loads((HERE / "pilot_runs.json").read_text())
    records = read_jsonl(HERE / "pilot_records.jsonl")
    st, usage_rows = stage_table(calls, rates)
    with open(HERE / "usage.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(usage_rows[0].keys()), lineterminator="\n"); w.writeheader(); w.writerows(usage_rows)

    cold, warm = runs["cold"], runs["warm"]
    status = defaultdict(int)
    for r in records:
        status[r["status"]] += 1
    n_in = runs["ingestion"]["records"]
    texts = runs["ingestion"]["distinct_nonempty_texts"]
    cold_cost = sum(v["cost"] for (run, _), v in st.items() if run == "cold")
    warm_cost = sum(v["cost"] for (run, _), v in st.items() if run == "warm")
    warm_enrich = st.get(("warm", "enrich"), {}).get("requests", 0)
    e = st[("cold", "enrich")]
    v = st.get(("cold", "verify"), defaultdict(float))
    fixed = sum(st.get(("cold", r), {}).get("cost", 0) for r in ("group", "memo"))
    enrich_per_text = e["cost"] / texts
    verify_per_item = v["cost"] / v["items_sent"] if v["items_sent"] else 0
    out_per_review = e["output_tokens"] / max(1, e["items_sent"])
    fail_rate = e["failed"] / max(1, e["requests"])
    enrich_secs_cold = cold["stage_seconds"].get("enrich", 0)
    throughput = texts / enrich_secs_cold if enrich_secs_cold else None

    # Optional refresh from the main run (first 550 and first 10,050 distinct texts in queue order).
    refresh_rows = []
    if a.refresh:
        main_calls = [c for c in read_jsonl(Path(a.refresh) / "calls.jsonl") if c["role"] == "enrich"]
        main_calls.sort(key=lambda c: c["started_at"])
        for target in (550, 10050):
            n = cost = out = 0
            secs = 0.0
            for c in main_calls:
                if n >= target:
                    break
                _, _, t = bill(c, rates)
                cost += t or 0
                out += c["output_tokens"]
                secs += c.get("duration_s", 0)
                if c["outcome"] == "succeeded":
                    n += len(c["review_ids"])
            if n:
                refresh_rows.append({"checkpoint_texts": target, "measured_texts": n, "enrich_cost_usd": cost, "cost_per_text": cost / n,
                                     "output_tokens_per_text": out / n, "summed_call_seconds": secs})
        if refresh_rows:
            enrich_per_text = refresh_rows[-1]["cost_per_text"]  # most recent, largest measurement drives projections
        json.dump(refresh_rows, open(HERE / "refresh_measurements.json", "w"), indent=2)

    W = assumptions["max_workers"]
    cap_out = assumptions["output_cap_tokens_per_review"]
    out_rate = (rates.get((e["model"], "standard")) or {}).get("output", 0)

    def scenario(name, n_texts, conservative):
        retry = max(fail_rate, assumptions["base_retry_rate"]) * (assumptions["conservative_retry_multiplier"] if conservative else 1)
        fb = assumptions["max_fallback_fraction"] if conservative else assumptions["base_fallback_fraction"]
        out_extra = (min(cap_out, out_per_review * assumptions["conservative_output_multiplier"]) - out_per_review) if conservative else 0
        enrich_cost = n_texts * (enrich_per_text + max(0, out_extra) * out_rate) * (1 + retry)
        fb_cost = n_texts * fb * assumptions["fallback_cost_multiplier_vs_batched"] * enrich_per_text
        verify_n = min(assumptions["verify_max_sample"], max(assumptions["verify_min_sample"], n_texts * assumptions["verify_fraction"]))
        verify_cost = verify_n * verify_per_item * (1 + retry)
        total = enrich_cost + fb_cost + verify_cost + fixed
        secs = n_texts / throughput / W if throughput else None
        if conservative and secs:
            secs *= assumptions["conservative_time_multiplier"]
        return {"scenario": name, "case": "conservative" if conservative else "base", "texts": n_texts, "enrich": enrich_cost,
                "fallback": fb_cost, "verify": verify_cost, "fixed_group_memo": fixed, "total": total,
                "hours": secs / 3600 if secs else None, "over_budget": total > assumptions["spend_limit_usd"]}

    scope = assumptions["declared_scope_distinct_texts"]
    scen = []
    for label, n in (("declared scope (100k sample, exact-text reuse)", scope), ("full corpus with exact-text reuse", FULL_DISTINCT),
                     ("full corpus, no reuse", FULL_NONEMPTY)):
        scen += [scenario(label, n, False), scenario(label, n, True)]

    L = []
    L += ["# 100-review cost and runtime report", "",
          f"Generated offline by `cost/calculator.py` from saved usage (`pilot_calls.jsonl`) and editable rates (`{Path(a.rates).name}`)"
          + (f" x **{a.rate_multiplier}** rate multiplier" if a.rate_multiplier != 1 else "") + ". No API calls were made to produce this report.", "",
          "## Measured: real 100-review pilot (`cost_100.csv`, unchanged)", "",
          f"- Input checksum: `{runs['ingestion']['file_sha256']}` ({n_in} rows, {texts} distinct texts)",
          f"- Final statuses: {dict(status)}",
          f"- Cold run: empty result cache, 1 worker; wall-clock **{cold['wall_clock_s']:.1f} s**; API cost **${cold_cost:.6f}**",
          f"- Warm run: same settings, saved results; wall-clock **{warm['wall_clock_s']:.1f} s**; new enrichment calls **{warm_enrich}**; "
          f"incremental API cost **${warm_cost:.6f}** (downstream group/memo/verify reuse saved outputs keyed by input hash)",
          f"- Cost per 1,000 input rows (cold, all stages): **${cold_cost / n_in * 1000:.4f}**; per completed record: **${cold_cost / max(1, status['completed']):.6f}**",
          f"- Enrichment throughput (cold, 1 worker): **{fmt(throughput, 2)} texts/s**", "",
          "### By stage (cold and warm)", "",
          "| run | role | model | effort | label_config | requests | failed | retries | items sent | input tok | cached tok | output tok | reasoning tok | summed call s | API cost USD |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for (run, role), s in sorted(st.items()):
        L.append(f"| {run} | {role} | {s['model']} | {s['effort']} | `{s['label_config']}` | {int(s['requests'])} | {int(s['failed'])} | {int(s['retries'])} | "
                 f"{int(s['items_sent'])} | {int(s['input_tokens'])} | {int(s['cached_tokens'])} | {int(s['output_tokens'])} | {int(s['reasoning_tokens'])} | "
                 f"{s['call_seconds']:.1f} | {s['cost']:.6f} |")
    L += ["", "Stage wall-clock seconds (cold): " + ", ".join(f"{k} {v:.2f}" for k, v in cold["stage_seconds"].items()),
          "", "Stage wall-clock seconds (warm): " + ", ".join(f"{k} {v:.2f}" for k, v in warm["stage_seconds"].items()), "",
          "Batch size 50 (enrich), 25 (verify); 1 worker; provider OpenAI Responses API, standard tier (no Batch API discount applied to measured costs).",
          "Summed call seconds are not wall-clock time. Local compute (laptop CPU, SQLite) is not billed: **API spend only**; local electricity/hardware cost is **unknown, not zero**.", "",
          "## Rates used (editable `cost/rates.csv`)", "", "| model | tier | item | USD per unit | quoted | checked | source |", "|---|---|---|---|---|---|---|"]
    for r in rate_meta:
        L.append(f"| {r['model']} | {r['tier']} | {r['item']} | {float(r['price_usd_per_unit']) * a.rate_multiplier:.10f} | {r['price_quoted']} | {r['checked_on']} | {r['source']} |")
    L += ["", "Formula: `item_cost = billed_units x price_per_unit`; items are mutually exclusive (uncached input = input - cached; output includes reasoning). "
          "Per-call items are in `usage.csv`.", ""]
    if refresh_rows:
        L += ["## Refreshed measurements from the main run", "", "| checkpoint (texts) | measured texts | enrich cost USD | cost/text | output tok/text | summed call s |", "|---|---|---|---|---|---|"]
        L += [f"| {r['checkpoint_texts']} | {r['measured_texts']} | {r['enrich_cost_usd']:.6f} | {r['cost_per_text']:.8f} | {r['output_tokens_per_text']:.1f} | {r['summed_call_seconds']:.0f} |" for r in refresh_rows]
        L += [""]
    L += ["## Projections (estimates, not measurements)", "",
          f"Controls: spend limit **${assumptions['spend_limit_usd']:.2f}**, output cap **{cap_out}** tokens/review, max workers **{W}**, "
          f"max fallback fraction **{assumptions['max_fallback_fraction']:.0%}**, verify {assumptions['verify_fraction']:.0%} (min {assumptions['verify_min_sample']}, max {assumptions['verify_max_sample']}).",
          f"Full corpus accounting: {FULL_ROWS:,} rows = {FULL_NONEMPTY:,} nonempty + {FULL_EMPTY} empty-text quarantines; {FULL_DISTINCT:,} distinct nonempty texts.",
          f"Inputs: enrich ${enrich_per_text:.8f}/text ({'refreshed' if refresh_rows else 'pilot'}), verify ${verify_per_item:.8f}/review, fixed group+memo ${fixed:.6f} once, "
          f"measured failed-request rate {fail_rate:.1%}. Time assumes linear speed-up to {W} workers (modeled, not measured).", "",
          "| scope | case | texts | enrich | fallback | verify | group+memo | **total USD** | est. hours | budget |", "|---|---|---|---|---|---|---|---|---|---|"]
    for s in scen:
        L.append(f"| {s['scenario']} | {s['case']} | {s['texts']:,} | {s['enrich']:.4f} | {s['fallback']:.4f} | {s['verify']:.4f} | {s['fixed_group_memo']:.4f} | "
                 f"**{s['total']:.4f}** | {fmt(s['hours'], 2)} | {'**EXCEEDS LIMIT**' if s['over_budget'] else 'within limit'} |")
    L += ["", f"Assumptions file: `cost/assumptions.json`. Changing projected volume does not change the measured pilot section above.", ""]
    Path(a.out).write_text("\n".join(L) + "\n", encoding="utf-8")
    api_total = cold_cost + warm_cost
    print(f"report -> {a.out}; measured pilot API spend ${api_total:.6f} (cold ${cold_cost:.6f}, warm ${warm_cost:.6f}); "
          f"rate multiplier {a.rate_multiplier}; cold wall-clock {cold['wall_clock_s']:.1f}s")
    for s in scen:
        print(f"  {s['scenario']:<48} {s['case']:<12} ${s['total']:.4f} {'EXCEEDS' if s['over_budget'] else 'ok'}")


if __name__ == "__main__":
    main()
