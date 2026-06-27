# Evaluations & Benchmarking

The **Evals** page benchmarks a model instance two ways and saves every run for
comparison over time:

- **Performance** — how *fast* it is: TTFT, decode tokens/sec, latency, and peak
  aggregate throughput under a concurrency sweep.
- **Capability** — how *good* it is, measured with **your own custom tasks**
  (coding, tool-use, reasoning, judging — whatever matters for your use), scored
  per task.

Everything runs against the instance's OpenAI-compatible endpoint over the LAN
and stays on your cluster (unless you opt into an external judge). There's no
public-leaderboard integration — for standardized benchmarks (MMLU/HumanEval/…)
read the datasets from HuggingFace directly; the value here is *your* tasks and
*your* throughput.

---

## Running an eval

**Evals → New eval**: pick the instance, the categories, whether to run
performance and/or capability, the concurrency levels for the throughput sweep,
the judge, and the sandbox image. It runs as a background job whose live log you
can watch ("View log"); results appear when it finishes. **Re-run** repeats a run
with the same instance + config.

---

## Performance benchmarks (tokens/sec)

For each built-in performance prompt (categories **coding / reasoning / textgen /
judging**) the engine measures, via streaming:

- **TTFT** — time to first token
- **decode tok/s** — completion tokens ÷ (total − TTFT), per stream
- **total latency**, prompt/completion tokens

…then repeats at each **concurrency level** (e.g. 1, 2, 4, 8), running that many
requests at once to find **aggregate throughput** (total completion tokens ÷
wall-clock) — the cluster's peak tokens/sec. Each measurement is averaged over
the configured repetitions.

---

## Custom capability tasks

**Evals → Manage tasks** lets you author tasks (your repos, prompts, rubrics,
expected answers/tests/tools), stored in the DB and run per category. Each task
declares a **scorer**:

| Scorer | How it's graded |
|---|---|
| `exact` | the expected answer (normalized) appears in the response |
| `contains` | every required substring appears (case-insensitive) |
| `numeric` | a number within tolerance of the expected value appears |
| `mcq` | the model picks the right option from your choices |
| `judge` | an LLM judge scores the answer 0–10 against your rubric |
| `code_exec` | the model writes code; your `check(candidate)` tests run in a sandbox → pass@1 |
| `tool_call` | the model must emit the right tool call (function + args), or *refuse* a destructive one |

### Sandboxed code execution (`code_exec`)

The model writes a function; the portal extracts the code, writes it next to your
`check(candidate)` harness, and runs it **on a node** in a throwaway container
(`--network none`, memory/CPU/PID-capped, hard timeout). Scoring is **pass@1**
(all asserts pass or fail). Default sandbox image `python:3.12-slim`; needs SSH +
Docker on the head node (code tasks are skipped if unavailable, the rest of the
run continues).

### Tool-use (`tool_call`)

Provide OpenAI tool/function definitions on the task; the portal checks the
model's `tool_calls` — right function, right arguments, or (with "forbid") that
it *refuses* to call a destructive tool. This is the high-signal check for an
agentic / ops-automation model.

### Judge

`judge`-scored tasks are graded by an LLM returning `{"score": 0-10, "reason":
"…"}` against your rubric. The judge is either a **running instance** you pick or
an **external** OpenAI-compatible endpoint set in **Settings → External judge**
(key encrypted).

---

## Results, comparison & charts

- **Run detail** — scorecard (overall + per-category), capability-by-category
  bars, peak-throughput-by-category bars, a throughput-vs-concurrency line chart,
  and a per-task table (score, tok/s, TTFT, judge reason / sandbox detail, and
  the model's full response on expand).
- **Comparison** — tick multiple runs to compare overall capability and peak
  throughput side by side.
- **Trend** — overall capability over time, one line per model.

Runs snapshot the model + config so comparisons stay meaningful even if the
instance is later changed or deleted.

---

## API

See [API.md](API.md#evals). In short: `GET /api/evals/catalog`
(perf + custom categories), `POST /api/evals` (start; returns `{run_id, job_id}`),
`GET /api/evals` / `GET /api/evals/{id}`, `DELETE /api/evals/{id}`, and
`GET/POST/PATCH/DELETE /api/evals/tasks` for custom tasks.
