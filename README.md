# litellm-gemini-laya-router: route Antigravity's asks to the right Gemini model on Vertex AI

> **Experimental.** Tested by hand on a handful of real asks, not an eval. Use the numbers below as anecdotes, not benchmarks.

Google has [expanded Antigravity for enterprise customers](https://cloud.google.com/blog/products/ai-machine-learning/expanding-google-antigravity-for-enterprise-customers) (21 Aug 2026). It's now part of Gemini Enterprise, with Application Default Credentials, budget caps, pooled token quotas and audit logging. So more Google Cloud teams will run a coding agent on Gemini all day. A coding agent is the biggest token user in most teams, and it sends every ask to whichever model you picked: a variable rename gets the same model as a production deadlock.

This project puts a model router in between. A [LiteLLM](https://docs.litellm.ai/) gateway sits in front of the agent and sends each ask to the Gemini model it needs, on **Vertex AI in your own Google Cloud project**:

| The ask | Model |
|---|---|
| Trivial: a rename, a typo, a one-line edit, a quick question about the code | **3.5 Flash-Lite** |
| Everything else: docstrings, unit tests, ordinary changes | **3.8 Flash** |
| Hard: debugging a production issue, a feature design, cross-file architecture | **3.1 Pro** |

The router is a LiteLLM pre-call hook (`laya_hook.py`). It's a plain `CustomLogger`, so it works for any client on any LiteLLM route: Gemini `generateContent`/`streamGenerateContent`, OpenAI `/v1/chat/completions` and Anthropic `/v1/messages`. **The only client tested so far is the [Antigravity CLI](https://antigravity.google/) (`agy`)**, through its undocumented `AGY_LLM_GATEWAY_URL` gateway mode.

```
agy ──(Gemini API)──► LiteLLM :4000 ──► laya_hook.py ──► laya-serve :8701 (Laya, on your machine)
 AGY_LLM_GATEWAY_URL     │  rewrites model:  trivial → 3.5 Flash-Lite
                         │                   hard    → 3.1 Pro
                         │                   else    → 3.8 Flash
                         ▼
             Vertex AI (your project, global) · cost per request in the LiteLLM log
```

Built for the talk [*Token-Maxxing Is Not a Strategy: Getting More Value per Token on Google Cloud*](https://gdg-melbourne-devfest-2026.sessionize.com/session/1321310) (GDG DevFest Melbourne, 3 Oct 2026). For a measured version of the same idea, an ADK agent with an eval scored by cost per successful task, see [google-adk-laya-router](https://github.com/derrickqin/google-adk-laya-router).

## Laya: the router runs on your laptop

[Laya](https://pypi.org/project/laya/) is a small open model (421M parameters, Apache-2.0) that answers typed questions about a prompt in one pass. Here it gets two yes/no questions: "is this a small coding request?" and "does it need deep reasoning across many files?". `start.sh` runs it locally as `laya-serve`.

- **Fast on a MacBook.** Laya runs on the Mac's GPU (`mps`, picked automatically): **~160 ms per decision** once warm in testing, including the HTTP hop from LiteLLM. That's small next to a Gemini call, which takes seconds. And only the first request of each ask pays it: an ask's later agent requests reuse the decision. The first decision after start-up took ~7 s, which is why `start.sh` warms it up. CPU-only works too: ~1 s per decision on a 4 vCPU Cloud Run instance, measured in the [sibling project](https://github.com/derrickqin/google-adk-laya-router).
- **Prompts stay with you.** The routing decision happens on your machine, so no prompt text goes to a third-party classifier. The Gemini calls themselves go to Vertex AI in your own project.
- **No fine-tuning.** Change the questions in `laya_hook.py` and you've changed the router.

### Why not Jev?

The idea came from **Jev**, TypeSafe's hosted routing classifier: a cheap classifier decides, then the expensive model does the work. LiteLLM even has it built in (`classifier_type: jev` in the Auto Router). So why not just use it?

- **Your prompts go to TypeSafe.** Hosted Jev runs on TypeSafe's API, outside Google Cloud, and every routing decision sends it a summary of the request. For a coding agent, that request is your code, your file names and your bug reports, on every ask. If you're on Antigravity through Google Cloud because you want your data to stay there, a router that sends it somewhere else undoes that.
- **Local Laya is enough for this job.** The router only has to answer two yes/no questions per ask. Laya did that on a laptop in ~160 ms. Published figures for hosted Jev are a 236–276 ms median (vendor and third-party numbers, not measured here), so the local option isn't slower either. In testing it sent each ask to the expected tier: a rename to Flash-Lite, docstrings and tests to Flash, a production deadlock to Pro. That's a handful of asks, not an eval. For a measured result on a different task, the sibling project's eval of conference-schedule questions had Laya routing at 100% on reworded questions and −37% cost vs always-Pro.
- **Same protocol, so it's not a dead end.** `laya-serve` speaks Jev's `/v1/systemone` protocol, so anything built for Jev can point at it. LiteLLM's built-in Jev classifier ran against local Laya just by changing `api_base` (see [What was tried](#what-was-tried-1-oct-2026-agy-1214-litellm-1103)).

One honest caveat: Laya is zero-shot, so the question has to suit it. LiteLLM's built-in Jev question asks for one of four tiers at once, and Laya got that wrong: a rename and a deadlock both came out COMPLEX. Two yes/no questions work, and that's what this hook uses. The open-weights alternative, OpenJev, isn't a laptop option: it's 27B parameters, needs an H100 and has a non-commercial license (CC BY-NC).

## How it decides

- Only requests for `gemini-3.8-flash` are routed (`ROUTED` in `laya_hook.py`). Anything else passes through unchanged, such as AGY's background title generation on `gemini-3.1-flash-lite`. To route requests for other models, edit `ROUTED`.
- The hook classifies the **latest human text** (Gemini `contents` or OpenAI `messages`, skipping tool results), not the whole conversation.
- Two yes/no questions: `hard ≥ 0.5` → Pro, else `trivial ≥ 0.5` → Flash-Lite, else Flash. Thresholds are `HARD_AT` / `TRIVIAL_AT`.
- **Same ask → same tier.** Decisions are memoised per ask, so the many requests of one agent turn stay on one model and keep its prompt cache. Each new ask can switch models, which throws that cache away.
- **If Laya is down or slow**, the hook keeps the model the client asked for. Routing degrades, but the request never fails.

## Costs

- **Laya:** free; it runs locally (first run downloads ~800 MB).
- **Gemini:** every request bills to your Google Cloud project on Vertex AI, at the routed tier's price. A coding agent makes many requests per ask (7–10 in testing). LiteLLM logs each request's cost, and the hook prints a running total per ask.

## Run it (with the Antigravity CLI)

You need [uv](https://docs.astral.sh/uv/), the gcloud CLI, a Google Cloud project with Vertex AI enabled, and `agy`.

```bash
gcloud auth application-default login          # LiteLLM calls Vertex AI with your ADC
export GOOGLE_CLOUD_PROJECT=<your-project-id>  # required (or: cp .env.example .env, edit, source .env)
source start.sh                                # starts laya-serve + LiteLLM, exports AGY_LLM_GATEWAY_*

tail -F /tmp/litellm.log | grep --line-buffered -E "laya-(route|cost)"   # second terminal: tier + $ per call

cd <some repo>
agy --model "Gemini 3.8 Flash (Low)"           # interactive; pick Flash, the gateway decides per ask
agy -p "Rename the variable tmp to total in utils.py" --model "Gemini 3.8 Flash (Low)"

source stop.sh                                 # stop both services
```

Source `start.sh`; don't run it. It exports `AGY_LLM_GATEWAY_URL` into your shell. It works in bash and zsh, leaves your shell options alone, and gives up with an error if laya-serve or LiteLLM isn't up in time (`LAYA_START_TIMEOUT`, default 600 s for the first-run download; `LITELLM_START_TIMEOUT`, default 180 s). Logs go to `/tmp/laya-serve.log` and `/tmp/litellm.log`.

**Other clients:** point them at `http://127.0.0.1:4000` and ask for `gemini-3.8-flash`. This hasn't been tested with anything but `agy` yet. Reports welcome.

**The documented setting:** [Antigravity's docs](https://antigravity.google/docs/cli/install/#point-the-cli-to-a-custom-endpoint) describe `GOOGLE_GEMINI_BASE_URL` for sending requests to a Gemini-compatible endpoint. Pointing it at the gateway should work the same way, but it hasn't been tested here. `AGY_LLM_GATEWAY_URL` is the mode that was tested; it also skips the Antigravity sign-in.

## What was tried (1 Oct 2026, AGY 1.2.14, LiteLLM 1.103)

| Step | Result |
|---|---|
| AGY → LiteLLM via `AGY_LLM_GATEWAY_URL` | ✅ No Antigravity sign-in needed ("authenticated via gateway"). AGY sends `POST /v1beta/models/<model>:streamGenerateContent?alt=sse`. This mode isn't in AGY's docs and could change in any release. |
| AGY answers a prompt through the gateway | ✅ `agy -p "Reply with the single word OK"` → `OK` via `gemini-3.8-flash` |
| Laya hook rewrites the model on the Gemini route | ✅ rename → Flash-Lite (trivial 0.90), docstring → Flash, unit tests → Flash, production deadlock → Pro (hard 0.80). ~160 ms per decision once warm |
| AGY doing real edits end to end (tool calls, streaming, model switches) | ✅ Interactive session, 3 asks: rename → Flash-Lite (7 agent requests), docstrings → Flash (9), both edits applied; the hard `mean()` ask ran 10 requests on Flash but changed nothing. Tool calls worked on every tier, and each ask stayed on one model |
| AGY's prompt wrapper | ⚠️→✅ AGY sends `<USER_REQUEST>…</USER_REQUEST><ADDITIONAL_METADATA>…`. Classifying all of it diluted Laya (the hard ask: hard 0.70 bare vs 0.22 wrapped, so it went to Flash instead of Pro). The hook now classifies only the `<USER_REQUEST>` text: rename 0.90 trivial → Flash-Lite, hard ask 0.70 hard → Pro |
| LiteLLM Auto Router behind AGY | ❌ The Auto Router (`auto_router/...`) only runs on `/v1/chat/completions` and `/v1/messages`. On the Gemini route LiteLLM says "strategy router marker… not a callable model". That's why this uses a pre-call hook |
| LiteLLM's built-in JEV classifier → Laya | ✅ Works on `/v1/chat/completions` (`api_base` pointed at laya-serve, `cause=jev_classifier` in the log; the `smart-router` entry in `litellm_config.yaml`). But zero-shot Laya can't do its 4-way tier question: a rename and a deadlock both came out COMPLEX |
| `AGY_LLM_GATEWAY_MODELS=smart-router` | ❌ AGY panics (`unknown model key MODEL_PLACEHOLDER_M50`): the gateway passes through AGY's own model names |

That's the whole test: a few asks in one repo, by hand. Nobody has measured how often Laya's tier is the right one for coding asks, or whether the Flash-Lite/Pro split saves money once failed attempts are counted. Don't route production traffic on this without an eval.

## Things to know

- **The master key is off** (`litellm_config.yaml`), because the gateway listens on 127.0.0.1 only. Turn it on, and set `AGY_LLM_GATEWAY_API_KEY` to match, before exposing it anywhere.
- **The memo is unbounded (demo-grade).** `LayaTierHook.memo` and `.totals` keep every distinct ask for the life of the proxy, so a long-running gateway grows without limit. Fine for one developer's session; use an LRU (or a TTL cache) before sharing it.
- **Tool calls across tiers** worked in testing (Flash-Lite included). If Flash-Lite struggles on harder edits, raise `TRIVIAL_AT` or drop the Flash-Lite branch.

## Files

| File | What it does |
|---|---|
| `laya_hook.py` | The LiteLLM pre-call hook: asks Laya, rewrites the model, and logs `[laya-route]` / `[laya-cost]` lines |
| `litellm_config.yaml` | The three Gemini tiers on Vertex AI (`global`), AGY's background model, the hook, and an optional Auto Router entry (`smart-router`) for `/v1/chat/completions` |
| `start.sh` / `stop.sh` | Start/stop laya-serve (:8701) and LiteLLM (:4000) in the background; source them |
| `.env.example` | The environment variables `start.sh` reads |

## License

MIT. See [LICENSE](LICENSE).
