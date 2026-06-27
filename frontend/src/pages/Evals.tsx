import { Fragment, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, CustomTask, CustomTaskInput, EvalRunDetail, EvalRunRequest, EvalRunSummary } from "../lib/api";
import { usePoll } from "../lib/hooks";
import { statusKind, timeAgo } from "../lib/format";
import { Badge, EmptyState, Field, HelpTip, Modal, Spinner } from "../components/ui";
import { BarList, LineChart, PALETTE } from "../components/charts";
import { JobLogPanel } from "../components/JobLogPanel";
import { useToast } from "../components/Toast";

const DEFAULT_CATEGORIES = ["coding", "security", "reasoning", "judging", "tools"];
const pct = (s?: number | null) => (s == null ? "—" : `${Math.round(s * 100)}%`);

function CatGroup({ title, cats, sel, onToggle }: { title: string; cats: string[]; sel: string[]; onToggle: (c: string) => void }) {
  if (cats.length === 0) return null;
  return (
    <div>
      <div className="faint" style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 4 }}>{title}</div>
      <div className="flex wrap gap-sm">
        {cats.map((c) => (
          <label key={c} className="flex gap-sm" style={{ alignItems: "center", cursor: "pointer" }}>
            <input type="checkbox" style={{ width: "auto" }} checked={sel.includes(c)} onChange={() => onToggle(c)} />
            <span style={{ textTransform: "capitalize" }}>{c}</span>
          </label>
        ))}
      </div>
    </div>
  );
}

// ---------- New eval modal ----------
function NewEval({ onClose, onStarted }: { onClose: () => void; onStarted: (jobId: number, label: string) => void }) {
  const instances = usePoll(() => api.listInstances(), 0);
  const catalog = usePoll(() => api.evalCatalog(), 0);
  const { toast } = useToast();
  const [f, setF] = useState<EvalRunRequest>({
    instance_id: 0,
    name: "",
    categories: [...DEFAULT_CATEGORIES],
    capability: true,
    performance: true,
    perf_reps: 3,
    concurrency: [1, 2, 4],
    temperature: 0.2,
    judge: { type: "instance", instance_id: undefined },
    sandbox_image: "python:3.12-slim",
    benchmark_n: 20,
  });
  const [concStr, setConcStr] = useState("1, 2, 4");
  const [busy, setBusy] = useState(false);
  const set = (k: keyof EvalRunRequest, v: any) => setF((p) => ({ ...p, [k]: v }));
  const insts = instances.data ?? [];
  const builtinCats = (catalog.data?.capability ?? []).map((s) => s.category);
  const benchCats = catalog.data?.benchmarks ?? [];
  const customCats = catalog.data?.custom_categories ?? [];
  const anyBench = f.categories.some((c) => benchCats.includes(c));

  useEffect(() => {
    if (!f.instance_id && insts.length) {
      const running = insts.find((i) => i.status === "running") ?? insts[0];
      setF((p) => ({ ...p, instance_id: running.id, judge: { type: "instance", instance_id: running.id } }));
    }
  }, [insts, f.instance_id]);

  const toggleCat = (c: string) =>
    set("categories", f.categories.includes(c) ? f.categories.filter((x) => x !== c) : [...f.categories, c]);

  const submit = async () => {
    if (!f.instance_id) return toast("Pick an instance to evaluate", "error");
    const concurrency = concStr.split(",").map((s) => parseInt(s.trim(), 10)).filter((n) => n > 0);
    setBusy(true);
    try {
      const r = await api.createEval({ ...f, concurrency: concurrency.length ? concurrency : [1] });
      toast("Eval started", "success");
      onStarted(r.job_id, f.name || "Eval");
      onClose();
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title="New evaluation"
      wide
      onClose={onClose}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary" onClick={submit} disabled={busy}>{busy ? <Spinner /> : "Run eval"}</button>
        </>
      }
    >
      <div className="row-2">
        <Field label="Instance to evaluate">
          <select value={f.instance_id} onChange={(e) => set("instance_id", Number(e.target.value))}>
            <option value={0}>— select —</option>
            {insts.map((i) => <option key={i.id} value={i.id}>{i.name} ({i.model_name}) — {i.status}</option>)}
          </select>
        </Field>
        <Field label="Run name (optional)"><input value={f.name} placeholder="auto" onChange={(e) => set("name", e.target.value)} /></Field>
      </div>

      <Field label="Categories">
        <div className="flex-col" style={{ gap: 10 }}>
          <CatGroup title="Built-in" cats={builtinCats} sel={f.categories} onToggle={toggleCat} />
          <CatGroup title="Public benchmarks" cats={benchCats} sel={f.categories} onToggle={toggleCat} />
          <CatGroup title="Custom" cats={customCats} sel={f.categories} onToggle={toggleCat} />
        </div>
      </Field>
      {anyBench && (
        <Field label="Benchmark sample size" help="How many items to pull per public-benchmark category (HumanEval/GSM8K/MMLU) from the HuggingFace datasets-server. A subset, not the full set.">
          <input type="number" value={f.benchmark_n ?? 20} onChange={(e) => set("benchmark_n", Number(e.target.value))} />
        </Field>
      )}

      <div className="row-2">
        <label className="checkbox"><input type="checkbox" checked={f.capability} onChange={(e) => set("capability", e.target.checked)} /><span><span className="cb-label">Capability scoring</span><div className="cb-sub">Correctness via deterministic checks, judge, and sandboxed code.</div></span></label>
        <label className="checkbox"><input type="checkbox" checked={f.performance} onChange={(e) => set("performance", e.target.checked)} /><span><span className="cb-label">Performance</span><div className="cb-sub">TTFT, tokens/sec, latency + concurrency sweep.</div></span></label>
      </div>

      <div className="row-2">
        <Field label="Concurrency levels" help="Comma-separated concurrent-request counts for the throughput sweep, e.g. 1, 2, 4, 8. Peak tokens/sec is found across these.">
          <input value={concStr} onChange={(e) => setConcStr(e.target.value)} placeholder="1, 2, 4" />
        </Field>
        <Field label="Perf repetitions" help="How many times each performance prompt is run per concurrency level; results are averaged.">
          <input type="number" value={f.perf_reps} onChange={(e) => set("perf_reps", Number(e.target.value))} />
        </Field>
      </div>

      <div className="row-2">
        <Field label="Judge">
          <select
            value={f.judge?.type ?? "none"}
            onChange={(e) => set("judge", { type: e.target.value, instance_id: f.judge?.instance_id })}
          >
            <option value="instance">A running instance</option>
            <option value="external">External API (Settings)</option>
            <option value="none">No judge</option>
          </select>
        </Field>
        {f.judge?.type === "instance" ? (
          <Field label="Judge instance" help="The model that grades open-ended (judge-scored) answers 0–10 against each task's rubric. Can be the same model or a peer.">
            <select value={f.judge?.instance_id ?? 0} onChange={(e) => set("judge", { type: "instance", instance_id: Number(e.target.value) })}>
              <option value={0}>— select —</option>
              {insts.map((i) => <option key={i.id} value={i.id}>{i.name} ({i.model_name})</option>)}
            </select>
          </Field>
        ) : f.judge?.type === "external" ? (
          <Field label="External judge"><div className="faint" style={{ fontSize: 12, paddingTop: 8 }}>Configure the endpoint + key on <Link to="/settings">Settings</Link>.</div></Field>
        ) : (
          <div />
        )}
      </div>

      <div className="row-2">
        <Field label="Temperature"><input type="number" step="0.1" value={f.temperature} onChange={(e) => set("temperature", Number(e.target.value))} /></Field>
        <Field label="Sandbox image" help="Container image used to run model-written code against unit tests, with --network none. Pulled on the node on first use.">
          <input value={f.sandbox_image} onChange={(e) => set("sandbox_image", e.target.value)} />
        </Field>
      </div>
      {insts.length === 0 && <div className="banner banner-warn">⚠ No instances yet — start a model on <Link to="/instances">Instances</Link> first.</div>}
    </Modal>
  );
}

// ---------- Run detail ----------
function RunDetail({ id }: { id: number }) {
  const [d, setD] = useState<EvalRunDetail | null>(null);
  const [err, setErr] = useState<string>();
  const [openTask, setOpenTask] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setD(null);
    setErr(undefined);
    api.getEval(id).then((x) => active && setD(x)).catch((e) => active && setErr(e.message));
    return () => { active = false; };
  }, [id]);

  if (err) return <div className="banner banner-warn">⚠ {err}</div>;
  if (!d) return <div className="card center" style={{ padding: 30 }}><Spinner /></div>;

  const catScores: Record<string, number> = (d.summary?.category_scores as any) ?? {};
  // peak throughput per category + throughput-vs-concurrency series
  const byCat: Record<string, { c: number; tput: number }[]> = {};
  for (const p of d.perf) {
    if (p.throughput_tps == null) continue;
    (byCat[p.category] ??= []).push({ c: p.concurrency, tput: p.throughput_tps });
  }
  const peakByCat = Object.entries(byCat).map(([cat, pts]) => ({ label: cat, value: Math.max(...pts.map((x) => x.tput)) }));
  const tputSeries = Object.entries(byCat).map(([cat, pts], i) => ({
    label: cat, color: PALETTE[i % PALETTE.length], points: pts.map((x) => [x.c, x.tput] as [number, number]),
  }));

  return (
    <div className="card">
      <div className="card-head">
        <div>
          <h2 style={{ margin: 0 }}>{d.name}</h2>
          <div className="faint" style={{ fontSize: 12 }}>{d.model_name} · {d.instance_label} · {timeAgo(d.created_at)}{d.judge_desc ? ` · judge: ${d.judge_desc}` : ""}</div>
        </div>
        <Badge kind={statusKind(d.status)}>{d.status}</Badge>
      </div>

      <div className="scorecard mb">
        <div className="sc"><div className="v">{pct(d.overall_score)}</div><div className="k">overall</div></div>
        {Object.entries(catScores).map(([c, s]) => <div className="sc" key={c}><div className="v">{pct(s)}</div><div className="k">{c}</div></div>)}
        {d.peak_throughput_tps != null && <div className="sc"><div className="v">{Math.round(d.peak_throughput_tps)}</div><div className="k">peak tok/s</div></div>}
      </div>

      {d.capability && Object.keys(catScores).length > 0 && (
        <div className="mb">
          <h3>Capability by category</h3>
          <BarList data={Object.entries(catScores).map(([c, s]) => ({ label: c, value: s, valueLabel: pct(s) }))} max={1} />
        </div>
      )}

      {d.performance && peakByCat.length > 0 && (
        <div className="grid grid-2 mb">
          <div><h3>Peak throughput by category</h3><BarList data={peakByCat} unit="tok/s" /></div>
          <div><h3>Throughput vs concurrency</h3><LineChart series={tputSeries} xLabel="concurrency" yLabel="tok/s" fmtX={(n) => `C=${n}`} /></div>
        </div>
      )}

      {d.capability && d.results.length > 0 && (
        <div className="mb">
          <h3>Tasks</h3>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Category</th><th>Task</th><th>Scorer</th><th>Score</th><th>tok/s</th><th>TTFT</th><th>Notes</th></tr></thead>
              <tbody>
                {d.results.map((r) => (
                  <Fragment key={r.task_id}>
                    <tr style={{ cursor: "pointer" }} onClick={() => setOpenTask(openTask === r.task_id ? null : r.task_id)}>
                      <td className="faint">{r.category}</td>
                      <td><strong>{r.task_name}</strong></td>
                      <td><span className="tag">{r.scorer}</span></td>
                      <td><Badge kind={r.score >= 0.999 ? "green" : r.score > 0 ? "amber" : "red"}>{pct(r.score)}</Badge></td>
                      <td className="mono faint">{r.tokens_per_sec ? Math.round(r.tokens_per_sec) : "—"}</td>
                      <td className="mono faint">{r.ttft_ms ? `${Math.round(r.ttft_ms)}ms` : "—"}</td>
                      <td className="faint" style={{ maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.error ? `⚠ ${r.error}` : r.judge_reason}</td>
                    </tr>
                    {openTask === r.task_id && (
                      <tr><td colSpan={7} style={{ background: "var(--bg)" }}>
                        {r.judge_reason && <div className="faint" style={{ fontSize: 12, marginBottom: 6 }}>{r.judge_reason}</div>}
                        <div className="faint" style={{ fontSize: 11, margin: "0 0 4px" }}>RESPONSE</div>
                        <div className="logs" style={{ maxHeight: 280 }}>{r.response || "(no response)"}</div>
                      </td></tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------- Custom tasks ----------
const SCORERS = ["judge", "contains", "numeric", "mcq", "exact", "tool_call", "code_exec"];
const EMPTY_TASK: CustomTaskInput = {
  category: "custom", name: "", prompt: "", scorer: "judge", system: null, answer: null,
  contains: [], numeric_answer: null, numeric_tol: 0.01, choices: [], correct: null, rubric: null,
  entry_point: null, test_code: null, code_prefix: null, tools: [], expected_tool: null,
  expected_args: {}, forbid_tool_call: false, max_tokens: 1024, enabled: true,
};

function TaskForm({ initial, onSave, onCancel }: { initial: CustomTaskInput; onSave: (t: CustomTaskInput) => void; onCancel: () => void }) {
  const [t, setT] = useState<CustomTaskInput>(initial);
  const [containsStr, setContainsStr] = useState(initial.contains.join(", "));
  const [choicesStr, setChoicesStr] = useState(initial.choices.join(", "));
  const [toolsStr, setToolsStr] = useState(initial.tools.length ? JSON.stringify(initial.tools, null, 2) : "");
  const [argsStr, setArgsStr] = useState(Object.keys(initial.expected_args).length ? JSON.stringify(initial.expected_args, null, 2) : "");
  const [err, setErr] = useState<string>();
  const set = (k: keyof CustomTaskInput, v: any) => setT((p) => ({ ...p, [k]: v }));

  const save = () => {
    try {
      onSave({
        ...t,
        contains: containsStr.split(",").map((s) => s.trim()).filter(Boolean),
        choices: choicesStr.split(",").map((s) => s.trim()).filter(Boolean),
        tools: toolsStr.trim() ? JSON.parse(toolsStr) : [],
        expected_args: argsStr.trim() ? JSON.parse(argsStr) : {},
      });
    } catch (e: any) {
      setErr("Invalid JSON in tools / expected args: " + e.message);
    }
  };

  return (
    <div>
      <div className="row-2">
        <Field label="Category" help="A built-in category (coding/security/reasoning/judging/tools) to extend it, or your own name (e.g. 'myrepo').">
          <input value={t.category} onChange={(e) => set("category", e.target.value)} />
        </Field>
        <Field label="Name"><input value={t.name} onChange={(e) => set("name", e.target.value)} /></Field>
      </div>
      <Field label="Prompt"><textarea value={t.prompt} onChange={(e) => set("prompt", e.target.value)} style={{ minHeight: 80 }} /></Field>
      <Field label="System prompt (optional)"><input value={t.system ?? ""} onChange={(e) => set("system", e.target.value || null)} /></Field>
      <div className="row-2">
        <Field label="Scorer">
          <select value={t.scorer} onChange={(e) => set("scorer", e.target.value)}>
            {SCORERS.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </Field>
        <Field label="Max tokens"><input type="number" value={t.max_tokens} onChange={(e) => set("max_tokens", Number(e.target.value))} /></Field>
      </div>

      {t.scorer === "exact" && <Field label="Expected answer (substring match)"><input value={t.answer ?? ""} onChange={(e) => set("answer", e.target.value || null)} /></Field>}
      {t.scorer === "contains" && <Field label="Must contain (comma-separated)"><input value={containsStr} onChange={(e) => setContainsStr(e.target.value)} /></Field>}
      {t.scorer === "numeric" && (
        <div className="row-2">
          <Field label="Expected number"><input type="number" value={t.numeric_answer ?? ""} onChange={(e) => set("numeric_answer", e.target.value === "" ? null : Number(e.target.value))} /></Field>
          <Field label="Tolerance"><input type="number" step="0.001" value={t.numeric_tol} onChange={(e) => set("numeric_tol", Number(e.target.value))} /></Field>
        </div>
      )}
      {t.scorer === "mcq" && (
        <div className="row-2">
          <Field label="Choices (comma-separated)"><input value={choicesStr} onChange={(e) => setChoicesStr(e.target.value)} placeholder="A, B, C, D" /></Field>
          <Field label="Correct"><input value={t.correct ?? ""} onChange={(e) => set("correct", e.target.value || null)} /></Field>
        </div>
      )}
      {t.scorer === "judge" && <Field label="Rubric" help="What full marks require; the judge grades 0–10 against this."><textarea value={t.rubric ?? ""} onChange={(e) => set("rubric", e.target.value || null)} /></Field>}
      {t.scorer === "code_exec" && (
        <>
          <Field label="Entry point (function name)"><input value={t.entry_point ?? ""} onChange={(e) => set("entry_point", e.target.value || null)} /></Field>
          <Field label="Test code" help="Python defining check(candidate) that asserts; runs in a sandbox. pass@1.">
            <textarea value={t.test_code ?? ""} onChange={(e) => set("test_code", e.target.value || null)} style={{ minHeight: 90 }} placeholder={"def check(candidate):\n    assert candidate(2) == 4"} />
          </Field>
          <Field label="Code prefix (optional)" help="Prepended to the model's code before running (e.g. a function signature)."><textarea value={t.code_prefix ?? ""} onChange={(e) => set("code_prefix", e.target.value || null)} /></Field>
        </>
      )}
      {t.scorer === "tool_call" && (
        <>
          <Field label="Tools (JSON array of OpenAI tool defs)"><textarea value={toolsStr} onChange={(e) => setToolsStr(e.target.value)} style={{ minHeight: 90 }} placeholder='[{"type":"function","function":{"name":"get_weather","parameters":{...}}}]' /></Field>
          <div className="row-2">
            <Field label="Expected tool (function name)"><input value={t.expected_tool ?? ""} onChange={(e) => set("expected_tool", e.target.value || null)} /></Field>
            <label className="checkbox" style={{ marginTop: 22 }}><input type="checkbox" checked={t.forbid_tool_call} onChange={(e) => set("forbid_tool_call", e.target.checked)} /><span><span className="cb-label">Forbid (must refuse)</span><div className="cb-sub">Pass only if the model declines to call any tool.</div></span></label>
          </div>
          <Field label="Expected args (JSON: arg → required substring)"><textarea value={argsStr} onChange={(e) => setArgsStr(e.target.value)} placeholder='{"location": "oslo"}' /></Field>
        </>
      )}

      <label className="checkbox"><input type="checkbox" checked={t.enabled} onChange={(e) => set("enabled", e.target.checked)} /><span className="cb-label">Enabled</span></label>
      {err && <div className="banner banner-warn">⚠ {err}</div>}
      <div className="modal-foot" style={{ paddingRight: 0 }}>
        <button className="btn btn-ghost" onClick={onCancel}>Cancel</button>
        <button className="btn btn-primary" onClick={save} disabled={!t.name || !t.prompt}>Save task</button>
      </div>
    </div>
  );
}

function CustomTasksModal({ onClose }: { onClose: () => void }) {
  const tasks = usePoll(() => api.listEvalTasks(), 0);
  const { toast } = useToast();
  const [editing, setEditing] = useState<CustomTaskInput | null>(null);
  const [editId, setEditId] = useState<number | null>(null);

  const save = async (payload: CustomTaskInput) => {
    try {
      if (editId != null) await api.updateEvalTask(editId, payload);
      else await api.createEvalTask(payload);
      toast("Task saved", "success");
      setEditing(null);
      setEditId(null);
      tasks.reload();
    } catch (e: any) {
      toast(e.message, "error");
    }
  };
  const del = async (t: CustomTask) => {
    if (!confirm(`Delete task "${t.name}"?`)) return;
    await api.deleteEvalTask(t.id);
    tasks.reload();
  };
  const edit = (t: CustomTask) => {
    const { id, ...rest } = t;
    setEditing(rest as CustomTaskInput);
    setEditId(id);
  };

  return (
    <Modal title="Custom eval tasks" wide onClose={onClose}>
      {editing ? (
        <TaskForm initial={editing} onSave={save} onCancel={() => { setEditing(null); setEditId(null); }} />
      ) : (
        <>
          <div className="spread mb">
            <span className="faint">Your own tasks, run per category alongside the built-ins.</span>
            <button className="btn btn-sm btn-primary" onClick={() => { setEditing({ ...EMPTY_TASK }); setEditId(null); }}>+ Add task</button>
          </div>
          {(tasks.data ?? []).length === 0 ? (
            <EmptyState icon="✎" title="No custom tasks yet">Add tasks from your own repos/prompts.</EmptyState>
          ) : (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Name</th><th>Category</th><th>Scorer</th><th>Enabled</th><th></th></tr></thead>
                <tbody>
                  {(tasks.data ?? []).map((t) => (
                    <tr key={t.id}>
                      <td><strong>{t.name}</strong></td>
                      <td><span className="tag">{t.category}</span></td>
                      <td><span className="tag">{t.scorer}</span></td>
                      <td><Badge kind={t.enabled ? "green" : "gray"}>{t.enabled ? "on" : "off"}</Badge></td>
                      <td><div className="btn-row" style={{ justifyContent: "flex-end" }}>
                        <button className="btn btn-sm" onClick={() => edit(t)}>Edit</button>
                        <button className="btn btn-sm btn-danger" onClick={() => del(t)}>✕</button>
                      </div></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </Modal>
  );
}

// ---------- Page ----------
export default function Evals() {
  const evals = usePoll(() => api.listEvals(), 5000);
  const { toast } = useToast();
  const [creating, setCreating] = useState(false);
  const [managing, setManaging] = useState(false);
  const [job, setJob] = useState<{ id: number; label: string } | null>(null);
  const [detailId, setDetailId] = useState<number | null>(null);
  const [compare, setCompare] = useState<Set<number>>(new Set());

  const runs = evals.data ?? [];
  const toggleCompare = (id: number) => setCompare((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const compareRuns = runs.filter((r) => compare.has(r.id));

  const del = async (r: EvalRunSummary) => {
    if (!confirm(`Delete eval run "${r.name}"?`)) return;
    await api.deleteEval(r.id);
    if (detailId === r.id) setDetailId(null);
    evals.reload();
  };

  // trend over time: overall % per model
  const byModel: Record<string, [number, number][]> = {};
  for (const r of runs) {
    if (r.overall_score == null) continue;
    (byModel[r.model_name] ??= []).push([Date.parse(r.created_at), r.overall_score * 100]);
  }
  const trend = Object.entries(byModel).filter(([, p]) => p.length >= 2).map(([m, p], i) => ({ label: m, color: PALETTE[i % PALETTE.length], points: p }));

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Evals</h1>
          <p>Benchmark model capability (coding / security / reasoning / judging) and throughput, and compare runs over time.</p>
        </div>
        <div className="btn-row">
          <button className="btn" onClick={() => setManaging(true)}>Manage tasks</button>
          <button className="btn btn-primary" onClick={() => setCreating(true)}>+ New eval</button>
        </div>
      </div>

      {trend.length > 0 && (
        <div className="card mb">
          <h3>Overall capability over time</h3>
          <LineChart series={trend} yLabel="overall %" fmtX={(n) => new Date(n).toLocaleDateString()} fmtY={(n) => `${Math.round(n)}%`} />
        </div>
      )}

      {compareRuns.length >= 2 && (
        <div className="card mb">
          <div className="card-head"><h2 style={{ margin: 0 }}>Comparison ({compareRuns.length})</h2><button className="btn btn-sm btn-ghost" onClick={() => setCompare(new Set())}>Clear</button></div>
          <div className="grid grid-2">
            <div><h3>Overall capability</h3><BarList data={compareRuns.map((r) => ({ label: `${r.model_name} #${r.id}`, value: r.overall_score ?? 0, valueLabel: pct(r.overall_score) }))} max={1} /></div>
            <div><h3>Peak throughput</h3><BarList data={compareRuns.map((r) => ({ label: `${r.model_name} #${r.id}`, value: r.peak_throughput_tps ?? 0 }))} unit="tok/s" /></div>
          </div>
        </div>
      )}

      <div className="card">
        <div className="card-head"><h2 style={{ margin: 0 }}>Runs</h2><button className="btn btn-sm" onClick={() => evals.reload()}>Refresh</button></div>
        {runs.length === 0 ? (
          <EmptyState icon="✦" title="No eval runs yet">Run one against a model instance to see scores and throughput.</EmptyState>
        ) : (
          <div className="table-wrap">
            <table>
              <thead><tr><th></th><th>Run</th><th>Model</th><th>Status</th><th>Overall</th><th>Peak tok/s</th><th>When</th><th></th></tr></thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.id}>
                    <td><input type="checkbox" style={{ width: "auto" }} checked={compare.has(r.id)} onChange={() => toggleCompare(r.id)} title="Add to comparison" /></td>
                    <td><strong>{r.name}</strong><div className="faint" style={{ fontSize: 11 }}>{r.categories.join(", ")}</div></td>
                    <td>{r.model_name}</td>
                    <td><Badge kind={statusKind(r.status)}>{r.status}</Badge></td>
                    <td><strong>{pct(r.overall_score)}</strong></td>
                    <td className="mono">{r.peak_throughput_tps ? Math.round(r.peak_throughput_tps) : "—"}</td>
                    <td className="faint">{timeAgo(r.created_at)}</td>
                    <td>
                      <div className="btn-row" style={{ justifyContent: "flex-end" }}>
                        {r.status === "running" && r.job_id ? (
                          <button className="btn btn-sm btn-primary" onClick={() => setJob({ id: r.job_id!, label: r.name })}>View log</button>
                        ) : (
                          <button className="btn btn-sm" onClick={() => setDetailId(r.id)}>View</button>
                        )}
                        <button className="btn btn-sm btn-danger" onClick={() => del(r)}>✕</button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {detailId != null && <div className="mt"><RunDetail id={detailId} /></div>}

      {creating && <NewEval onClose={() => setCreating(false)} onStarted={(id, label) => { setJob({ id, label }); evals.reload(); }} />}
      {managing && <CustomTasksModal onClose={() => setManaging(false)} />}
      {job && (
        <Modal title={`Eval: ${job.label}`} wide onClose={() => { setJob(null); evals.reload(); }}>
          <JobLogPanel jobId={job.id} title={job.label} onDone={() => evals.reload()} />
        </Modal>
      )}
    </div>
  );
}
