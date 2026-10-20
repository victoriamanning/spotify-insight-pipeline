"""Model access with budget reservation, a shared rate limiter and bounded retries.

Worker threads call `Caller.call`; it returns every attempt (success or failure) so the single writer
can log them. The spend ledger is shared: before dispatch the orchestrator reserves the worst-case cost
of the call; the reservation is released and replaced by the actual cost when the call returns.
"""

import csv
import hashlib
import os
import random
import re
import threading
import time
import uuid
from dataclasses import dataclass, field

from .common import ROOT, now_iso


class BudgetExceeded(Exception):
    pass


def load_rates(path=None, tier="standard"):
    rates = {}
    with open(path or ROOT / "config" / "rates.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["tier"] == tier:
                rates.setdefault(r["model"], {})[r["item"]] = float(r["price_usd_per_unit"])
    return rates


def cost_of(rates, model, input_tokens, cached_tokens, output_tokens):
    """Mutually exclusive items: uncached input, cached input, output (output already includes reasoning)."""
    r = rates.get(model) or rates.get(model.removeprefix("mock-"))  # mock runs are priced as if real, for control tests
    if r is None:
        return 0.0
    uncached = max(0, input_tokens - cached_tokens)
    return uncached * r["input"] + cached_tokens * r.get("cached_input", r["input"]) + output_tokens * r["output"]


class Ledger:
    """Thread-safe spend ledger shared by every worker of a run."""

    def __init__(self, cap_usd, already_spent=0.0, stage_caps=None, stage_spent=None):
        self.cap = cap_usd
        self.spent = already_spent
        self.reserved = 0.0
        self.stage_caps = stage_caps or {}
        self.stage_spent = dict(stage_spent or {})
        self.stage_reserved = {}
        self.lock = threading.Lock()

    def reserve(self, stage, amount):
        with self.lock:
            if self.spent + self.reserved + amount > self.cap:
                raise BudgetExceeded(f"global cap ${self.cap:.4f}: spent {self.spent:.4f} + reserved {self.reserved:.4f} + next {amount:.4f}")
            cap = self.stage_caps.get(stage)
            s_sp, s_res = self.stage_spent.get(stage, 0.0), self.stage_reserved.get(stage, 0.0)
            if cap is not None and s_sp + s_res + amount > cap:
                raise BudgetExceeded(f"{stage} cap ${cap:.4f}: spent {s_sp:.4f} + reserved {s_res:.4f} + next {amount:.4f}")
            self.reserved += amount
            self.stage_reserved[stage] = s_res + amount

    def settle(self, stage, reserved, actual):
        with self.lock:
            self.reserved -= reserved
            self.stage_reserved[stage] = self.stage_reserved.get(stage, 0.0) - reserved
            self.spent += actual
            self.stage_spent[stage] = self.stage_spent.get(stage, 0.0) + actual


class RateLimiter:
    def __init__(self, per_minute):
        self.interval = 60.0 / per_minute if per_minute else 0
        self.next = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next)
            self.next = t + self.interval
        if t > now:
            time.sleep(t - now)


@dataclass
class Attempt:
    request_id: str
    model: str
    effort: str
    attempt: int
    outcome: str  # succeeded | failed
    error: str = ""
    text: str = ""
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    started_at: str = ""
    duration_s: float = 0.0
    provider_request_id: str = ""
    usage_known: bool = True


TRANSIENT = ("rate limit", "429", "timeout", "timed out", "500", "502", "503", "504", "overloaded", "connection", "temporarily")


class Caller:
    def __init__(self, provider, rates, ledger, limiter, retry_cfg, fault_plan=None):
        self.provider = provider
        self.rates = rates
        self.ledger = ledger
        self.limiter = limiter
        self.retry = retry_cfg
        self.fault_plan = fault_plan or {}  # test hook: {"transient_first_n": k} injects k fake 503s
        self._faults_left = int(self.fault_plan.get("transient_first_n", 0))
        self._fault_lock = threading.Lock()
        self.client = None
        if provider == "openai":
            from openai import OpenAI
            if not os.environ.get("OPENAI_API_KEY"):
                raise SystemExit("OPENAI_API_KEY is not set. Copy .env.example to .env and add your key, or use --provider mock.")
            self.client = OpenAI(timeout=retry_cfg.get("timeout_s", 90), max_retries=0)

    def worst_case(self, model, est_input_tokens, max_output_tokens):
        return cost_of(self.rates, model, est_input_tokens, 0, max_output_tokens)

    def call(self, stage, model, effort, instructions, user_text, max_output_tokens, json_schema=None, cache_key=None,
             est_input_tokens=None):
        """Returns (attempts, final_attempt_or_None). Transient errors retry with backoff+jitter."""
        attempts = []
        max_attempts = int(self.retry.get("transient_max_attempts", 4))
        est = est_input_tokens or (len(instructions) + len(user_text)) // 3 + 50
        for n in range(1, max_attempts + 1):
            reservation = self.worst_case(model, est, max_output_tokens)
            self.ledger.reserve(stage, reservation)  # raises BudgetExceeded -> caller stops admitting work
            self.limiter.wait()
            a = Attempt(request_id=f"{stage}-{uuid.uuid4().hex[:16]}", model=model, effort=effort, attempt=n,
                        outcome="failed", started_at=now_iso())
            t0 = time.perf_counter()
            try:
                self._maybe_inject_fault()
                if self.provider == "mock":
                    self._mock(a, instructions, user_text, json_schema)
                else:
                    self._openai(a, model, effort, instructions, user_text, max_output_tokens, json_schema, cache_key)
                a.outcome = "succeeded"
            except Exception as e:  # noqa: BLE001 - every failure is logged
                a.error = f"{type(e).__name__}: {str(e)[:300]}"
            a.duration_s = round(time.perf_counter() - t0, 3)
            a.cost_usd = cost_of(self.rates, model, a.input_tokens, a.cached_tokens, a.output_tokens)
            self.ledger.settle(stage, reservation, a.cost_usd)
            attempts.append(a)
            if a.outcome == "succeeded":
                return attempts, a
            if not any(t in a.error.lower() for t in TRANSIENT) or n == max_attempts:
                return attempts, None
            delay = min(self.retry.get("backoff_max_s", 30), self.retry.get("backoff_base_s", 2) * 2 ** (n - 1))
            time.sleep(delay * (0.5 + random.random()))
        return attempts, None

    def _maybe_inject_fault(self):
        with self._fault_lock:
            if self._faults_left > 0:
                self._faults_left -= 1
                raise RuntimeError("503 injected temporary failure (test fault plan)")

    def _openai(self, a, model, effort, instructions, user_text, max_output_tokens, json_schema, cache_key):
        kwargs = dict(model=model, instructions=instructions, input=user_text, max_output_tokens=max_output_tokens,
                      reasoning={"effort": effort}, store=False)
        if cache_key:
            kwargs["prompt_cache_key"] = cache_key
        if json_schema:
            kwargs["text"] = {"format": {"type": "json_schema", "name": json_schema["name"], "schema": json_schema["schema"], "strict": True}}
        resp = self.client.responses.create(**kwargs)
        a.provider_request_id = getattr(resp, "_request_id", "") or getattr(resp, "id", "")
        u = resp.usage
        if u is not None:
            a.input_tokens = u.input_tokens or 0
            a.output_tokens = u.output_tokens or 0
            a.cached_tokens = getattr(u.input_tokens_details, "cached_tokens", 0) or 0
            a.reasoning_tokens = getattr(u.output_tokens_details, "reasoning_tokens", 0) or 0
        else:
            a.usage_known = False
        if resp.status == "incomplete":
            reason = getattr(resp.incomplete_details, "reason", "unknown")
            a.text = resp.output_text or ""
            if reason == "max_output_tokens" and a.text.strip():
                # The request itself succeeded and was billed; code validates each returned line and retries the rest.
                a.error = "incomplete:max_output_tokens (partial output parsed)"
                return
            raise RuntimeError(f"incomplete response: {reason}")
        a.text = resp.output_text or ""
        if not a.text.strip():
            raise RuntimeError("empty or refused response")

    def _mock(self, a, instructions, user_text, json_schema):
        """Deterministic offline stand-in used for tests and for the no-key smoke run. Never used for graded labels."""
        time.sleep(0.01)
        a.input_tokens = (len(instructions) + len(user_text)) // 4
        a.cached_tokens = 0
        if json_schema:
            a.text = MOCK_JSON.get(json_schema["name"], lambda u: "{}")(user_text)
        else:
            lines = []
            for m in re.finditer(r"^(r\d+): (.*)$", user_text, re.M):
                ref, text = m.group(1), m.group(2)
                h = int(hashlib.sha256(text.encode()).hexdigest(), 16)
                topic = ["playback", "usability", "billing", "other"][h % 4]
                sub = {"playback": "crash_wont_open", "usability": "ads", "billing": "free_tier_limits", "other": "general"}[topic]
                intent = "complaint" if topic != "other" else "praise"
                sev = 1 if intent == "praise" else 2 + h % 3
                q = " ".join(text.split("\n")[0].split()[:8]) or text
                if "verify" in instructions[:80].lower():
                    lines.append(f"{ref}|{topic}|{intent}|{sev}")
                else:
                    lines.append(f"{ref}|{topic}|{sub}|{intent}|{sev}|{-1 if intent != 'praise' else 1}|0|-|{q}")
            a.text = "\n".join(lines)
        a.output_tokens = len(a.text) // 4


def _mock_group(user_text):
    import json
    ids = re.findall(r"^ISSUE (\S+)", user_text, re.M)
    out = []
    for iid in ids:
        block = user_text.split(f"ISSUE {iid}", 1)[1].split("ISSUE ", 1)[0]
        ex = re.findall(r"\[([0-9a-f-]{36})\]", block)[:1]
        out.append({"issue_id": iid, "name": iid.replace(".", " ").replace("_", " ").title(), "description": "Mock description.", "example_review_ids": ex})
    return json.dumps({"issues": out})


def _mock_memo(user_text):
    import json
    m = re.search(r"^1,(\S+?),(\d+),(\d+),([\d.]+),(\d+),", user_text, re.M)
    iid, cnt, ssum, mean, score = m.groups() if m else ("other.general", "0", "0", "0", "0")
    return json.dumps({"priority_area": iid.split(".")[0], "headline": f"Mock memo: prioritise {iid}",
                       "recommendation": f"Mock recommendation for {iid} with {cnt} complaints [C1] and priority score {score} [C2].",
                       "first_issues": [iid],
                       "alternatives": [{"area": "billing", "why_lower": "Mock."}, {"area": "access", "why_lower": "Mock."}],
                       "limitations": ["Mock output - not a real model."],
                       "claims": [{"claim_id": "C1", "issue_id": iid, "metric": "complaint_count", "value": cnt},
                                  {"claim_id": "C2", "issue_id": iid, "metric": "priority_score", "value": score}],
                       "cited_review_ids": []})


MOCK_JSON = {"issue_names": _mock_group, "memo": _mock_memo}
