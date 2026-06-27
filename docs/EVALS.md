# Evaluations & Benchmarking

The **Evals** page benchmarks a model instance two ways and saves every run for
comparison over time:

- **Capability** — how *good* the model is at coding, security, reasoning, and
  judging, scored per task.
- **Performance** — how *fast* it is: TTFT, decode tokens/sec, latency, and peak
  aggregate throughput under a concurrency sweep.

Results live in SQLite and are charted on the page; nothing leaves your cluster
unless you opt into an external judge.

---

## Running an eval

**Evals → New eval**: pick the instance to evaluate, the categories, whether to
run capability and/or performance, the concurrency levels for the throughput
sweep, the judge, and the sandbox image. It runs as a background job whose live
log you can watch ("View log"); results appear when it finishes.

An eval calls the instance's OpenAI-compatible endpoint (the same one the
Playground uses) over the LAN, streaming responses to measure timing.

---

## Capability scoring

Each task declares a **scorer**; the per-category score is the mean of its task
scores (0–1), and the run's overall score is the mean across all tasks.

| Scorer | How it's graded |
|---|---|
| `exact` | the expected answer (normalized) appears in the response |
| `contains` | every required substring appears (case-insensitive) |
| `numeric` | a number within tolerance of the expected value appears |
| `mcq` | the model picks the correct option (A/B/C/… or 1/2/…) |
| `judge` | an LLM judge scores the answer 0–10 against the task's rubric |
| `code_exec` | the model writes code; unit tests run in a sandbox → pass@1 |
| `tool_call` | the model must emit a valid tool call (right function + args), or *refuse* a destructive one |

Built-in categories: **coding** (sandboxed code), **security** (judge + mcq),
**reasoning** (numeric/mcq/contains), **judging** (does it pick the correct
answer), and **tools** (tool-use). Suites are data-driven in
`backend/app/services/eval_suites.py`.

### Tool-use / agent (`tool_call`)

The "ops brain" calls tools, so the `tools` category measures exactly that. A
task ships OpenAI-style function definitions; the model is asked to act, and the
portal checks the returned `tool_calls`:

- **right function** — did it call the expected tool (e.g. `list_files`, not
  `delete_path`)?
- **right arguments** — do the args contain the expected values?
- **refusal** — for a `forbid_tool_call` task (e.g. "run `rm -rf /`"), it passes
  only if the model *declines* to call the destructive tool.

This is where local vLLM models often differ from hosted APIs (tool-call format,
parser, stop tokens), so it's a high-signal check for agentic use.

### Custom tasks

**Evals → Manage tasks** lets you author your own tasks (your repos, prompts,
rubrics, expected answers/tests/tools) in the UI; they're stored in the DB and
run per category alongside the built-ins. Your own suite is usually more
predictive of real usefulness than any public leaderboard. Every scorer above is
available.

### Public benchmark subsets

Selecting **HumanEval**, **GSM8K**, or **MMLU** as a category pulls a configurable
**sample** of real items from the HuggingFace datasets-server at run time and maps
them onto our scorers (HumanEval → `code_exec` pass@1, GSM8K → `numeric`, MMLU →
`mcq`). These are a cheap objective baseline filter — a subset, not a full
leaderboard run, and they need network access from the portal.

### Sandboxed code execution (`code_exec`)

For coding tasks the model is asked to write a function. The portal extracts the
code block, writes it next to the task's `check(candidate)` harness, and runs it
**on a node** in a throwaway container:

```
docker run --rm --network none --memory 512m --cpus 1 --pids-limit 256 \
  -v <tmp>:/work:ro -w /work <sandbox-image> python runner.py
```

`--network none` + resource caps + a hard timeout keep it contained. Scoring is
**pass@1** (binary): all asserts pass or the task fails. The default sandbox
image is `python:3.12-slim` (pulled on the node on first use; configurable per
run). Code execution needs SSH + Docker on the head node; if that's unavailable,
code tasks are skipped (the rest of the run still completes).

### Judge

Open-ended tasks (`judge` scorer) are graded by an LLM that returns
`{"score": 0-10, "reason": "..."}` against the task rubric. The judge is either:

- **a running instance** you pick (self-hosted — the model itself or a peer), or
- **an external** OpenAI-compatible endpoint configured in **Settings → External
  judge** (base URL, model, API key — key stored encrypted).

If a `judge` task runs with no judge configured, it scores 0 and is flagged.

---

## Performance benchmarks

For each performance prompt (coding / reasoning / textgen / judging) the engine
measures, via streaming:

- **TTFT** — time to first token
- **decode tok/s** — completion tokens ÷ (total − TTFT), per stream
- **total latency**, prompt/completion tokens

…then repeats at each **concurrency level** (e.g. 1, 2, 4, 8), running that many
requests at once to find **aggregate throughput** (total completion tokens ÷
wall-clock) — i.e. the cluster's peak tokens/sec. Each measurement is averaged
over the configured repetitions.

---

## Results, comparison & charts

- **Run detail** — a scorecard (overall + per-category), a capability-by-category
  bar chart, peak-throughput-by-category bars, a throughput-vs-concurrency line
  chart, and a per-task table (score, tok/s, TTFT, judge reason / sandbox detail,
  and the model's full response on expand).
- **Comparison** — tick multiple runs to compare overall capability and peak
  throughput side by side.
- **Trend** — overall capability over time, one line per model.

---

## API

See [API.md](API.md#evals) for the endpoints. In short:

- `GET /api/evals/suites` — categories + task counts
- `POST /api/evals` — start a run (returns `{run_id, job_id}`); follow the job log
- `GET /api/evals` / `GET /api/evals/{id}` — list / full detail (results + perf)
- `DELETE /api/evals/{id}` — delete a run

Runs snapshot the model name + instance config, so comparisons stay meaningful
even if the instance is later changed or deleted.
