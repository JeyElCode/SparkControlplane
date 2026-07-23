import { useMemo, useState } from "react";
import { api, Instance, ScheduleEntry } from "../lib/api";
import { usePoll } from "../lib/hooks";
import { EmptyState, Field, Modal, Spinner } from "../components/ui";
import { PALETTE } from "../components/charts";
import { useToast } from "../components/Toast";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const NODE_BUDGET_GIB = 119; // matches SPARK_NODE_MEMORY_GIB default

function minutes(t: string): number {
  const [h, m] = t.split(":").map(Number);
  return h * 60 + m;
}

interface Block {
  sched: ScheduleEntry;
  day: number;      // 0-6 column
  startMin: number; // within the day
  endMin: number;
  color: string;
}

/** Expand schedules into per-day blocks (overnight windows split in two). */
function toBlocks(schedules: ScheduleEntry[], colorOf: (id: number) => string): Block[] {
  const out: Block[] = [];
  for (const s of schedules) {
    if (!s.enabled) continue;
    const st = minutes(s.start_time);
    const en = minutes(s.end_time);
    for (const d of s.days) {
      const color = colorOf(s.instance_id);
      if (st < en) {
        out.push({ sched: s, day: d, startMin: st, endMin: en, color });
      } else {
        out.push({ sched: s, day: d, startMin: st, endMin: 1440, color });
        out.push({ sched: s, day: (d + 1) % 7, startMin: 0, endMin: en, color });
      }
    }
  }
  return out;
}

/** Per-hour scheduled memory (GiB/node) for one day column. */
function hourLoad(blocks: Block[], day: number): number[] {
  const load = new Array(24).fill(0);
  for (const b of blocks) {
    if (b.day !== day) continue;
    for (let h = 0; h < 24; h++) {
      const hs = h * 60, he = hs + 60;
      if (b.startMin < he && b.endMin > hs) load[h] += b.sched.est_gib_per_node;
    }
  }
  return load;
}

function ScheduleForm({
  instances, initial, onClose, onSaved,
}: {
  instances: Instance[];
  initial: ScheduleEntry | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { toast } = useToast();
  const [instanceId, setInstanceId] = useState<number>(initial?.instance_id ?? instances[0]?.id ?? 0);
  const [days, setDays] = useState<number[]>(initial?.days ?? [0, 1, 2, 3, 4]);
  const [start, setStart] = useState(initial?.start_time ?? "08:00");
  const [end, setEnd] = useState(initial?.end_time ?? "17:00");
  const [busy, setBusy] = useState(false);

  const toggleDay = (d: number) =>
    setDays((p) => (p.includes(d) ? p.filter((x) => x !== d) : [...p, d].sort()));

  const save = async () => {
    setBusy(true);
    try {
      if (initial) {
        await api.updateSchedule(initial.id, { days, start_time: start, end_time: end });
      } else {
        await api.createSchedule({ instance_id: instanceId, days, start_time: start, end_time: end, enabled: true });
      }
      onSaved();
      onClose();
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title={initial ? `Edit window — ${initial.instance_name}` : "New live window"} onClose={onClose}>
      {!initial && (
        <Field label="Instance">
          <select value={instanceId} onChange={(e) => setInstanceId(Number(e.target.value))}>
            {instances.map((i) => (
              <option key={i.id} value={i.id}>{i.name} ({i.topology})</option>
            ))}
          </select>
        </Field>
      )}
      <Field label="Days">
        <div className="btn-row">
          {DAYS.map((d, i) => (
            <button key={d} type="button"
                    className={`btn btn-sm ${days.includes(i) ? "btn-primary" : ""}`}
                    onClick={() => toggleDay(i)}>{d}</button>
          ))}
        </div>
      </Field>
      <div className="row-2">
        <Field label="Start"><input type="time" value={start} onChange={(e) => setStart(e.target.value)} /></Field>
        <Field label="End" hint="an end at or before the start wraps past midnight">
          <input type="time" value={end} onChange={(e) => setEnd(e.target.value)} />
        </Field>
      </div>
      <div className="btn-row">
        <button className="btn btn-primary" onClick={save} disabled={busy || days.length === 0}>
          {busy ? <Spinner /> : "Save window"}
        </button>
        <button className="btn" onClick={onClose}>Cancel</button>
      </div>
    </Modal>
  );
}

export default function SchedulePage() {
  const { toast } = useToast();
  const schedules = usePoll(() => api.listSchedules(), 15000);
  const instances = usePoll(() => api.listInstances(), 0);
  const nowInfo = usePoll(() => api.schedulesNow(), 30000);
  const [form, setForm] = useState<{ initial: ScheduleEntry | null } | null>(null);

  const all = schedules.data ?? [];
  const instanceIds = useMemo(
    () => Array.from(new Set(all.map((s) => s.instance_id))),
    [all]
  );
  const colorOf = (id: number) => PALETTE[instanceIds.indexOf(id) % PALETTE.length];
  const blocks = useMemo(() => toBlocks(all, colorOf), [all]);  // eslint-disable-line react-hooks/exhaustive-deps

  const toggle = async (s: ScheduleEntry) => {
    await api.updateSchedule(s.id, { enabled: !s.enabled });
    schedules.reload();
  };
  const del = async (s: ScheduleEntry) => {
    if (!confirm(`Delete the ${s.instance_name} window ${s.start_time}–${s.end_time}?`)) return;
    await api.deleteSchedule(s.id);
    schedules.reload();
    toast("Window deleted", "success");
  };

  const H = 480; // px for 24h

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Schedule</h1>
          <p>
            Time-share your memory: models start when their window opens and stop when it closes.
            Manual starts/stops are respected until the next window edge.
            {nowInfo.data && <> Scheduler clock: <span className="mono">{nowInfo.data.tz}</span>.</>}
          </p>
        </div>
        <button className="btn btn-primary" disabled={(instances.data ?? []).length === 0}
                onClick={() => setForm({ initial: null })}>+ Live window</button>
      </div>

      {all.length === 0 ? (
        <EmptyState icon="◷" title="No live windows yet">
          Add a window to start/stop an instance automatically — e.g. a coding model on
          weekdays 08:00–17:00 and a batch model overnight.
        </EmptyState>
      ) : (
        <>
          <div className="card mb">
            <div className="table-wrap">
              <div style={{ display: "grid", gridTemplateColumns: "38px repeat(7, 1fr)", gap: 4, minWidth: 700 }}>
                <div />
                {DAYS.map((d, di) => (
                  <div key={d} className="faint" style={{ textAlign: "center", fontSize: 12, fontWeight: nowInfo.data?.weekday === di ? 700 : 400 }}>
                    {d}{nowInfo.data?.weekday === di ? " ·" : ""}
                  </div>
                ))}
                <div style={{ position: "relative", height: H }}>
                  {[0, 6, 12, 18, 24].map((h) => (
                    <div key={h} className="faint mono" style={{ position: "absolute", top: (h / 24) * H - 7, right: 4, fontSize: 10 }}>{String(h).padStart(2, "0")}</div>
                  ))}
                </div>
                {DAYS.map((_, di) => {
                  const load = hourLoad(blocks, di);
                  return (
                    <div key={di} style={{ position: "relative", height: H, background: "var(--bg-elev-2)", borderRadius: 6, overflow: "hidden" }}>
                      {[6, 12, 18].map((h) => (
                        <div key={h} style={{ position: "absolute", top: (h / 24) * H, left: 0, right: 0, borderTop: "1px solid var(--border)" }} />
                      ))}
                      {load.map((g, h) =>
                        g > NODE_BUDGET_GIB ? (
                          <div key={`ob-${h}`} title={`~${g.toFixed(0)} GiB/node scheduled — over the ~${NODE_BUDGET_GIB} GiB budget`}
                               style={{ position: "absolute", top: (h / 24) * H, height: H / 24, left: 0, right: 0, background: "rgba(248,113,113,0.25)" }} />
                        ) : null
                      )}
                      {blocks.filter((b) => b.day === di).map((b, i) => (
                        <div key={i}
                             title={`${b.sched.instance_name} (${b.sched.model_name})\n${b.sched.start_time}–${b.sched.end_time} · ~${b.sched.est_gib_per_node} GiB/node on ${b.sched.node_scope}`}
                             onClick={() => setForm({ initial: b.sched })}
                             style={{
                               position: "absolute",
                               top: (b.startMin / 1440) * H,
                               height: Math.max(14, ((b.endMin - b.startMin) / 1440) * H),
                               left: 3, right: 3,
                               background: b.color, opacity: 0.85, borderRadius: 4,
                               fontSize: 10, color: "#06101f", padding: "2px 5px",
                               overflow: "hidden", cursor: "pointer", fontWeight: 600,
                             }}>
                          {b.sched.instance_name} · {b.sched.est_gib_per_node}G
                        </div>
                      ))}
                      {nowInfo.data?.weekday === di && (
                        <div style={{ position: "absolute", top: (nowInfo.data.minutes / 1440) * H, left: 0, right: 0, borderTop: "2px solid var(--red)" }} />
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
            <div className="faint" style={{ fontSize: 11, marginTop: 8 }}>
              Red shading = scheduled instances would need more than ~{NODE_BUDGET_GIB} GiB on a node at that hour.
              The red line is now. Click a block to edit its window.
            </div>
          </div>

          <div className="card">
            <h2>Windows</h2>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr><th>Instance</th><th>Model</th><th>Days</th><th>Window</th><th>Est. memory</th><th>Where</th><th>Status</th><th /></tr>
                </thead>
                <tbody>
                  {all.map((s) => (
                    <tr key={s.id} style={{ opacity: s.enabled ? 1 : 0.5 }}>
                      <td>
                        <span style={{ display: "inline-block", width: 10, height: 10, borderRadius: 2, background: colorOf(s.instance_id), marginRight: 6 }} />
                        <strong>{s.instance_name}</strong>
                      </td>
                      <td className="mono faint">{s.model_name}</td>
                      <td className="mono">{s.days.map((d) => DAYS[d]).join(" ")}</td>
                      <td className="mono">{s.start_time}–{s.end_time}{minutes(s.end_time) <= minutes(s.start_time) ? " (+1d)" : ""}</td>
                      <td className="mono">~{s.est_gib_per_node} GiB/node</td>
                      <td className="faint">{s.node_scope}</td>
                      <td className="mono faint">{s.status}</td>
                      <td>
                        <div className="btn-row">
                          <button className="btn btn-sm" onClick={() => setForm({ initial: s })}>Edit</button>
                          <button className="btn btn-sm" onClick={() => toggle(s)}>{s.enabled ? "Disable" : "Enable"}</button>
                          <button className="btn btn-sm btn-danger" onClick={() => del(s)}>Delete</button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}

      {form && (
        <ScheduleForm
          instances={instances.data ?? []}
          initial={form.initial}
          onClose={() => setForm(null)}
          onSaved={() => schedules.reload()}
        />
      )}
    </div>
  );
}
