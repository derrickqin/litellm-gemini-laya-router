"""LiteLLM pre-call hook: Laya picks the Gemini tier for every coding-agent request.

Why a hook and not the Auto Router: LiteLLM's Auto Router only runs on /v1/chat/completions and
/v1/messages. A pre-call hook runs on every route (including the Gemini API's streamGenerateContent,
which the Antigravity CLI uses) and may rewrite the model, so it works for any client as-is.

Two yes/no questions to self-hosted Laya (laya-serve, the Jev /v1/systemone protocol):
  trivial?  -> Flash-Lite      hard?  -> Pro      otherwise -> Flash
Yes/no questions, because zero-shot Laya is weak at multi-way choices.
"""

import os
import re
import time

import httpx
from litellm.integrations.custom_logger import CustomLogger

LAYA_URL = os.environ.get("LAYA_URL", "http://127.0.0.1:8701") + "/v1/systemone"
LAYA_KEY = os.environ.get("LAYA_API_KEY", "")

# Requests for these models get routed; everything else (e.g. background title generation) passes through.
ROUTED = {"gemini-3.8-flash"}
TIERS = {"lite": "gemini-3.5-flash-lite", "flash": "gemini-3.8-flash", "pro": "gemini-3.1-pro-preview"}

QUESTIONS = {
    "trivial": {
        "type": "noul",
        "instructions": "Is this a small coding request, such as renaming something, fixing a typo, "
                        "a one-line edit, or a quick factual question about the code?",
    },
    "hard": {
        "type": "noul",
        "instructions": "Does this coding request need deep reasoning across many files, such as debugging "
                        "a production issue, designing a feature, or changing the architecture?",
    },
}
HARD_AT, TRIVIAL_AT = 0.5, 0.5


# AGY wraps each ask as <USER_REQUEST>…</USER_REQUEST><ADDITIONAL_METADATA>…</ADDITIONAL_METADATA>.
# The metadata (time, workspace, open files) dilutes Laya's signal: measured on one hard ask,
# hard = 0.70 on the bare request vs 0.22 with the wrapper. So classify only the request itself.
USER_REQUEST = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)


def clean(text: str) -> str:
    m = USER_REQUEST.search(text)
    return (m.group(1) if m else text)[-4000:]


def last_human_text(data: dict) -> str:
    """The latest text a person typed: Gemini `contents` or OpenAI `messages`, skipping tool results."""
    for c in reversed(data.get("contents") or []):
        if c.get("role", "user") != "user":
            continue
        texts = [p["text"] for p in c.get("parts", []) if isinstance(p, dict) and p.get("text")]
        if texts:
            return clean("\n".join(texts))
    for m in reversed(data.get("messages") or []):
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, str):
                return clean(content)
            texts = [p.get("text", "") for p in content or [] if isinstance(p, dict) and p.get("type") == "text"]
            if texts:
                return clean("\n".join(texts))
    return ""


class LayaTierHook(CustomLogger):
    def __init__(self):
        super().__init__()
        self.client = httpx.AsyncClient(timeout=15.0)  # first Laya call after start-up is slow
        self.memo: dict[str, tuple[str, dict, float]] = {}  # same ask -> same tier: keeps one turn on one model (and its cache)
        self.totals: dict[str, list] = {}  # ask -> [calls, dollars, tier], for the running total per ask

    async def decide(self, text: str) -> tuple[str, dict, float]:
        if text in self.memo:
            return self.memo[text]
        t0 = time.perf_counter()
        r = await self.client.post(LAYA_URL, json={"model": "laya", "state": text, "questions": QUESTIONS},
                                   headers={"Authorization": f"Bearer {LAYA_KEY}"} if LAYA_KEY else {})
        r.raise_for_status()
        a = r.json()["answers"]
        p = {k: round(a[k]["noul"], 3) for k in QUESTIONS}
        tier = "pro" if p["hard"] >= HARD_AT else "lite" if p["trivial"] >= TRIVIAL_AT else "flash"
        out = (tier, p, time.perf_counter() - t0)
        self.memo[text] = out
        return out

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if data.get("model") not in ROUTED:
            return data
        text = last_human_text(data)
        if not text:
            return data
        try:
            tier, p, secs = await self.decide(text)
        except Exception as e:  # Laya down: don't fail the request, keep the model the client asked for
            print(f"[laya-route] Laya unavailable ({e!r}); keeping {data['model']}", flush=True)
            return data
        print(f"[laya-route] {tier:<5} -> {TIERS[tier]:<24} trivial={p['trivial']:.2f} hard={p['hard']:.2f} "
              f"({secs * 1000:.0f} ms)  {text[:70]!r}", flush=True)
        data["model"] = TIERS[tier]
        data.setdefault("metadata", {}).update(laya_tier=tier, laya_ask=text[:200])
        return data


    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        """One cost line per model call, next to the routing line: what each ask actually cost."""
        cost = kwargs.get("response_cost") or 0.0
        model = kwargs.get("model", "?")
        secs = (end_time - start_time).total_seconds()
        meta = (kwargs.get("litellm_params") or {}).get("metadata") or {}
        ask = meta.get("laya_ask")
        line = f"[laya-cost]  {model:<24} ${cost:.5f}  {secs:.1f}s"
        if ask:  # only routed calls count towards an ask's total
            t = self.totals.setdefault(ask, [0, 0.0, meta.get("laya_tier", "?")])
            t[0] += 1
            t[1] += cost
            line += f"   ask total: {t[0]} call{'s' if t[0] > 1 else ''}, ${t[1]:.5f} on {t[2]}"
        print(line, flush=True)


proxy_handler_instance = LayaTierHook()
