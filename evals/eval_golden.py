"""Compare pipeline predictions with the human-labelled golden 50 (offline, no model calls).

  python evals/eval_golden.py --records runs/final/grading/records.jsonl.gz

Predeclared rules: topic/intent agreement counts a prediction as correct if it equals the primary human label or a
listed alternative (ambiguous cases); severity exact agreement and mean absolute error vs the closest accepted level;
sentiment tolerance |pred - human| <= 0.5 on the -1..1 scale (and MAE); evidence_quote must be an exact substring
(checked in code) and is listed for human inspection of support; entities not present in the text are unsupported
additions; needs_review is scored as a prediction of the human 'ambiguous' flag. Missing/quarantined predictions
count as incorrect.
"""

import argparse
import csv
import gzip
import json
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOPICS = ("access", "usability", "playback", "downloads", "catalog", "billing", "support", "other")
INTENTS = ("cancellation", "complaint", "request", "praise", "unclear")


def read_records(path):
    op = gzip.open if str(path).endswith(".gz") else open
    with op(path, "rt", encoding="utf-8") as f:
        return {r["review_id"]: r for r in map(json.loads, f) if r}


def split(v):
    return [x.strip().lower() for x in (v or "").replace(",", ";").split(";") if x.strip()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default=HERE / "golden" / "golden_50_labels_v1.csv")
    ap.add_argument("--records", required=True)
    ap.add_argument("--out", default=HERE / "golden")
    a = ap.parse_args()
    labels = list(csv.DictReader(open(a.labels, encoding="utf-8-sig")))
    missing_labels = [r["n"] for r in labels if not (r["topic"] and r["intent"] and r["severity"])]
    if missing_labels:
        raise SystemExit(f"Human labels incomplete for rows {missing_labels[:10]} - finish labelling first.")
    preds = read_records(a.records)
    out = Path(a.out)
    cases, tot = [], Counter()
    conf_topic, conf_intent = Counter(), Counter()
    per_topic = defaultdict(Counter)
    gold_cases = []
    for h in labels:
        rid, text = h["review_id"], h["review_text"]
        acc_topic = [h["topic"].strip()] + split(h["alt_topic"])
        acc_intent = [h["intent"].strip()] + split(h["alt_intent"])
        acc_sev = [int(h["severity"])] + [int(x) for x in split(h["alt_severity"])]
        for t in acc_topic:
            assert t in TOPICS, f"row {h['n']}: bad topic {t}"
        for t in acc_intent:
            assert t in INTENTS, f"row {h['n']}: bad intent {t}"
        ambiguous = h["ambiguous"].strip().lower() in ("1", "y", "yes", "true")
        p = preds.get(rid, {})
        ok = p.get("status") == "completed"
        c = {"n": h["n"], "review_id": rid, "review_text": text[:200], "ambiguous": ambiguous, "prediction_status": p.get("status", "missing"),
             "human_topic": "/".join(acc_topic), "pred_topic": p.get("topic"), "human_intent": "/".join(acc_intent), "pred_intent": p.get("intent"),
             "human_severity": "/".join(map(str, acc_sev)), "pred_severity": p.get("severity")}
        c["topic_ok"] = ok and p["topic"] in acc_topic
        c["intent_ok"] = ok and p["intent"] in acc_intent
        c["severity_exact"] = ok and p["severity"] in acc_sev
        c["severity_abs_err"] = min(abs(p["severity"] - s) for s in acc_sev) if ok else None
        hs = float(h["sentiment"]) if h["sentiment"].strip() else None
        c["human_sentiment"], c["pred_sentiment"] = hs, p.get("sentiment")
        c["sentiment_abs_err"] = abs(p["sentiment"] - hs) if ok and hs is not None else None
        c["sentiment_within_0.5"] = c["sentiment_abs_err"] is not None and c["sentiment_abs_err"] <= 0.5
        c["quote_is_substring"] = ok and p["evidence_quote"] in text
        c["pred_quote"] = p.get("evidence_quote", "")[:160]
        c["human_quote"] = h["evidence_quote"][:160]
        unsupported = [e for e in p.get("entities", []) if e.lower() not in text.lower()] if ok else []
        c["pred_entities"] = ";".join(p.get("entities", [])) if ok else ""
        c["unsupported_entities"] = ";".join(unsupported)
        c["pred_needs_review"] = p.get("needs_review") if ok else None
        c["all_core_ok"] = c["topic_ok"] and c["intent_ok"] and c["severity_exact"]
        cases.append(c)
        for k in ("topic_ok", "intent_ok", "severity_exact", "sentiment_within_0.5", "quote_is_substring", "all_core_ok"):
            tot[k] += bool(c[k])
        tot["ambiguous"] += ambiguous
        tot["valid"] += ok
        tot["unsupported_entities"] += len(unsupported)
        if ok:
            tot["sev_err"] += c["severity_abs_err"]
            if c["sentiment_abs_err"] is not None:
                tot["sent_err"] += c["sentiment_abs_err"]; tot["sent_n"] += 1
            nr = bool(p["needs_review"])
            tot["nr_tp"] += nr and ambiguous; tot["nr_fp"] += nr and not ambiguous; tot["nr_fn"] += (not nr) and ambiguous
        conf_topic[(acc_topic[0], p.get("topic", "<missing>"))] += 1
        conf_intent[(acc_intent[0], p.get("intent", "<missing>"))] += 1
        per_topic[acc_topic[0]]["support"] += 1
        per_topic[acc_topic[0]]["correct"] += bool(c["topic_ok"])
        gold_cases.append({"review_id": rid, "status": "approved", "reviewer": "student-labeller",
                           "accepted_topic": acc_topic, "accepted_intent": acc_intent, "accepted_severity": acc_sev})
    n = len(cases)
    summary = {
        "cases": n, "valid_predictions": tot["valid"], "ambiguous_cases": tot["ambiguous"],
        "topic_agreement": round(tot["topic_ok"] / n, 4), "intent_agreement": round(tot["intent_ok"] / n, 4),
        "severity_exact_agreement": round(tot["severity_exact"] / n, 4),
        "severity_mae_valid": round(tot["sev_err"] / max(1, tot["valid"]), 4),
        "sentiment_within_0.5": round(tot["sentiment_within_0.5"] / n, 4),
        "sentiment_mae": round(tot["sent_err"] / max(1, tot["sent_n"]), 4),
        "joint_topic_intent_severity": round(tot["all_core_ok"] / n, 4),
        "evidence_quote_exact_substring": f"{tot['quote_is_substring']}/{n}",
        "unsupported_entity_additions": tot["unsupported_entities"],
        "needs_review_vs_ambiguous": {"tp": tot["nr_tp"], "fp": tot["nr_fp"], "fn": tot["nr_fn"]},
        "per_topic_recall": {t: f"{v['correct']}/{v['support']}" for t, v in sorted(per_topic.items())},
        "topic_confusion_human_to_pred": {f"{a}->{b}": k for (a, b), k in sorted(conf_topic.items())},
        "intent_confusion_human_to_pred": {f"{a}->{b}": k for (a, b), k in sorted(conf_intent.items())},
        "rules": __doc__.strip().split("\n\n", 1)[1],
    }
    json.dump(summary, open(out / "golden_results.json", "w"), indent=2)
    with open(out / "golden_per_case.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(cases[0].keys())); w.writeheader(); w.writerows(cases)
    json.dump({"version": "student-golden-v1", "cases": gold_cases}, open(out / "gold_benchmark.json", "w"), indent=2)
    print(json.dumps({k: v for k, v in summary.items() if k != "rules"}, indent=2))


if __name__ == "__main__":
    main()
