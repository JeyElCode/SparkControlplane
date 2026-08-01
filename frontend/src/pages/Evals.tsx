import { Fragment, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, EvalRunDetail, EvalRunRequest, EvalRunSummary } from "../lib/api";
import { usePoll } from "../lib/hooks";
import { statusKind, timeAgo } from "../lib/format";
import { Badge, EmptyState, Field, HelpTip, Modal, Spinner } from "../components/ui";
import { BarList, LineChart, PALETTE } from "../components/charts";
import { JobLogPanel } from "../components/JobLogPanel";
import { useToast } from "../components/Toast";

// The predictability ladder — see backend eval_suites.py SPEED_LADDER. These
// three are the default because tok/s is not one number: speculative decoding
// speeds up predictable output and slows down creative output, and only
// measuring all three shows which way an instance actually trades.
const DEFAULT_CATEGORIES = ["predictable", "code", "creative"];
const SPEED_LADDER = ["predictable", "code", "creative"];
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
    perf_reps: 3,
    concurrency: [1, 2, 4],
    temperature: 0.2,
  });
  const [concStr, setConcStr] = useState("1, 2, 4");
  const [busy, setBusy] = useState(false);
  const set = (k: keyof EvalRunRequest, v: any) => setF((p) => ({ ...p, [k]: v }));
  const insts = instances.data ?? [];
  const perfCats = catalog.data?.perf_categories ?? [];

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

      <Field label="Categories" help="Each prompt is measured for tokens/sec and TTFT. The three ladder prompts differ in how PREDICTABLE their output is — speculative decoding speeds up predictable text and slows down creative text, so measuring all three shows which way an instance trades. A single average hides it.">
        <div className="flex-col" style={{ gap: 10 }}>
          <CatGroup title="Predictability ladder (default)" cats={perfCats.filter((c) => SPEED_LADDER.includes(c))} sel={f.categories} onToggle={toggleCat} />
          <CatGroup title="Other prompts" cats={perfCats.filter((c) => !SPEED_LADDER.includes(c))} sel={f.categories} onToggle={toggleCat} />
          
        </div>
      </Field>

      <div className="row-2">
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
        <Field label="Temperature"><input type="number" step="0.1" value={f.temperature} onChange={(e) => set("temperature", Number(e.target.value))} /></Field>
      </div>
      {insts.length === 0 && <div className="banner banner-warn">⚠ No instances yet — start a model on <Link to="/instances">Instances</Link> first.</div>}
    </Modal>
  );
}

// ---------- Run detail ----------
function RunDetail({ id, onRerun }: { id: number; onRerun?: () => void }) {
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
        <div className="flex gap-sm">
          {onRerun && <button className="btn btn-sm" onClick={onRerun}>Re-run</button>}
          <Badge kind={statusKind(d.status)}>{d.status}</Badge>
        </div>
      </div>

      <div className="scorecard mb">
        {d.overall_score != null && (
          <div className="sc"><div className="v">{pct(d.overall_score)}</div><div className="k">overall</div></div>
        )}
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

// ---------- Page ----------
export default function Evals() {
  const evals = usePoll(() => api.listEvals(), 5000);
  const { toast } = useToast();
  const [creating, setCreating] = useState(false);
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

  // Re-run an eval with the same instance + config.
  const rerun = async (runId: number) => {
    try {
      const d = await api.getEval(runId);
      const cfg = d.config ?? {};
      const instance_id = (cfg.instance_id as number) ?? d.instance_id ?? 0;
      if (!instance_id) {
        toast("The original instance no longer exists — create a new eval", "error");
        return;
      }
      const r = await api.createEval({
        instance_id,
        name: d.name,
        categories: d.categories,
        perf_reps: cfg.perf_reps ?? 3,
        concurrency: cfg.concurrency ?? [1, 2, 4],
        temperature: cfg.temperature ?? 0.2,
      });
      toast("Re-run started", "success");
      setJob({ id: r.job_id, label: d.name });
      evals.reload();
    } catch (e: any) {
      toast(e.message, "error");
    }
  };

  // trend over time: tok/s per predictability regime
  const byModel: Record<string, [number, number][]> = {};
  for (const r of runs ?? []) {
    for (const rung of SPEED_LADDER) {
      const v = r.ladder_tps?.[rung];
      if (v == null) continue;
      (byModel[`${r.model_name} · ${rung}`] ??= []).push([Date.parse(r.created_at), v]);
    }
  }
  const trend = Object.entries(byModel).filter(([, p]) => p.length >= 2).map(([m, p], i) => ({ label: m, color: PALETTE[i % PALETTE.length], points: p }));

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Evals</h1>
          <p>Measure serving speed across the predictability ladder — predictable, code and creative output are decoded at very different rates — and compare instances over time.</p>
        </div>
        <div className="btn-row">
          <button className="btn btn-primary" onClick={() => setCreating(true)}>+ New eval</button>
        </div>
      </div>

      {trend.length > 0 && (
        <div className="card mb">
          <h3>Speed over time (tok/s per regime)</h3>
          <LineChart series={trend} yLabel="overall %" fmtX={(n) => new Date(n).toLocaleDateString()} fmtY={(n) => `${Math.round(n)}%`} />
        </div>
      )}

      {compareRuns.length >= 2 && (
        <div className="card mb">
          <div className="card-head"><h2 style={{ margin: 0 }}>Comparison ({compareRuns.length})</h2><button className="btn btn-sm btn-ghost" onClick={() => setCompare(new Set())}>Clear</button></div>
          <div className="grid grid-2">
            {SPEED_LADDER.map((rung) => {
              const rows = compareRuns
                .filter((r) => r.ladder_tps?.[rung] != null)
                .map((r) => ({ label: `${r.model_name} #${r.id}`, value: r.ladder_tps![rung], valueLabel: `${Math.round(r.ladder_tps![rung])}` }));
              if (!rows.length) return null;
              return (
                <div key={rung}>
                  <h3>{rung} — tok/s</h3>
                  <BarList data={rows} max={Math.max(...rows.map((x) => x.value))} />
                </div>
              );
            })}
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
              <thead><tr><th></th><th>Run</th><th>Model</th><th>Status</th><th title="predictable / code / creative tok/s">Speed (p/c/c)</th><th>Peak tok/s</th><th>When</th><th></th></tr></thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.id}>
                    <td><input type="checkbox" style={{ width: "auto" }} checked={compare.has(r.id)} onChange={() => toggleCompare(r.id)} title="Add to comparison" /></td>
                    <td><strong>{r.name}</strong><div className="faint" style={{ fontSize: 11 }}>{r.categories.join(", ")}</div></td>
                    <td>{r.model_name}</td>
                    <td><Badge kind={statusKind(r.status)}>{r.status}</Badge></td>
                    <td className="mono">
                      {SPEED_LADDER.some((k) => r.ladder_tps?.[k] != null)
                        ? SPEED_LADDER.map((k) => (r.ladder_tps?.[k] != null ? Math.round(r.ladder_tps[k]) : "—")).join(" / ")
                        : "—"}
                    </td>
                    <td className="mono">{r.peak_throughput_tps ? Math.round(r.peak_throughput_tps) : "—"}</td>
                    <td className="faint">{timeAgo(r.created_at)}</td>
                    <td>
                      <div className="btn-row" style={{ justifyContent: "flex-end" }}>
                        {r.status === "running" && r.job_id ? (
                          <button className="btn btn-sm btn-primary" onClick={() => setJob({ id: r.job_id!, label: r.name })}>View log</button>
                        ) : (
                          <>
                            <button className="btn btn-sm" onClick={() => setDetailId(r.id)}>View</button>
                            <button className="btn btn-sm" onClick={() => rerun(r.id)} title="Run again with the same instance + config">Re-run</button>
                          </>
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

      {detailId != null && <div className="mt"><RunDetail id={detailId} onRerun={() => rerun(detailId)} /></div>}

      {creating && <NewEval onClose={() => setCreating(false)} onStarted={(id, label) => { setJob({ id, label }); evals.reload(); }} />}
      {job && (
        <Modal title={`Eval: ${job.label}`} wide onClose={() => { setJob(null); evals.reload(); }}>
          <JobLogPanel jobId={job.id} title={job.label} onDone={() => evals.reload()} />
        </Modal>
      )}
    </div>
  );
}
