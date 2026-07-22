import { Link } from "react-router-dom";
import { api, GpuStatus, HistoryPoint, NodeHistory, NodeStatus } from "../lib/api";
import { usePoll, useStatusStream } from "../lib/hooks";
import { boolKind, fmtBytes, fmtGib, fmtRate, fmtUptime, statusKind } from "../lib/format";
import { Badge, EmptyState, Meter, Spinner } from "../components/ui";
import { Sparkline } from "../components/charts";

const CLR = { gpu: "#4f9cff", cpu: "#a78bfa", mem: "#34d399", rx: "#38bdf8", tx: "#fb923c" };

function pts(points: HistoryPoint[], pick: (p: HistoryPoint) => number | null | undefined): [number, number][] {
  const out: [number, number][] = [];
  for (const p of points) {
    const v = pick(p);
    if (v != null) out.push([p.ts, v]);
  }
  return out;
}

function MetricRow({
  label,
  value,
  meterValue,
  meterMax,
  spark,
  sparkMax,
}: {
  label: string;
  value: string;
  meterValue?: number;
  meterMax?: number;
  spark?: { color: string; points: [number, number][] }[];
  sparkMax?: number;
}) {
  return (
    <div style={{ padding: "7px 0", borderTop: "1px solid var(--border)" }}>
      <div className="spread" style={{ marginBottom: 3 }}>
        <span className="faint" style={{ fontSize: 11 }}>{label}</span>
        <span className="mono" style={{ fontSize: 11 }}>{value}</span>
      </div>
      {meterValue != null && meterMax != null && <Meter value={meterValue} max={meterMax} />}
      {spark && spark.some((s) => s.points.length > 1) && (
        <div style={{ marginTop: 4 }}>
          <Sparkline series={spark} height={30} max={sparkMax} />
        </div>
      )}
    </div>
  );
}

function GpuRow({ g, spark }: { g: GpuStatus; spark?: [number, number][] }) {
  return (
    <div style={{ padding: "7px 0", borderTop: "1px solid var(--border)" }}>
      <div className="spread" style={{ marginBottom: 4 }}>
        <span className="mono faint">GPU{g.index} {g.name ?? ""}</span>
        <span className="faint">{g.temp_c != null ? `${g.temp_c}°C` : ""} {g.power_w != null ? `· ${g.power_w.toFixed(0)}W` : ""}</span>
      </div>
      <div className="spread gap-sm">
        <span className="faint" style={{ fontSize: 11, width: 40 }}>util</span>
        <Meter value={g.util_pct ?? 0} max={100} />
        <span className="mono" style={{ fontSize: 11, width: 38, textAlign: "right" }}>{g.util_pct ?? 0}%</span>
      </div>
      {g.mem_total_mib != null && (
        <div className="spread gap-sm" style={{ marginTop: 3 }}>
          <span className="faint" style={{ fontSize: 11, width: 40 }}>mem</span>
          <Meter value={g.mem_used_mib ?? 0} max={g.mem_total_mib} />
          <span className="mono" style={{ fontSize: 11, width: 80, textAlign: "right" }}>
            {Math.round((g.mem_used_mib ?? 0) / 1024)}/{Math.round(g.mem_total_mib / 1024)}G
          </span>
        </div>
      )}
      {spark && spark.length > 1 && (
        <div style={{ marginTop: 4 }}>
          <Sparkline series={[{ color: CLR.gpu, points: spark }]} height={30} max={100} />
        </div>
      )}
    </div>
  );
}

function NetRow({ n, hist }: { n: NodeStatus; hist?: HistoryPoint[] }) {
  const qsfp = (n.net ?? []).find((r) => r.kind === "qsfp");
  const lan = (n.net ?? []).find((r) => r.kind === "lan");
  if (!qsfp && !lan) return null;
  const h = hist ?? [];
  return (
    <>
      {qsfp && (
        <MetricRow
          label={`network · QSFP (${qsfp.iface})`}
          value={`↓ ${fmtRate(qsfp.rx_bps)} · ↑ ${fmtRate(qsfp.tx_bps)}`}
          spark={[
            { color: CLR.rx, points: pts(h, (p) => p.qsfp_rx_bps) },
            { color: CLR.tx, points: pts(h, (p) => p.qsfp_tx_bps) },
          ]}
        />
      )}
      {lan && (
        <MetricRow
          label={`network · LAN (${lan.iface})`}
          value={`↓ ${fmtRate(lan.rx_bps)} · ↑ ${fmtRate(lan.tx_bps)}`}
          spark={[
            { color: CLR.rx, points: pts(h, (p) => p.lan_rx_bps) },
            { color: CLR.tx, points: pts(h, (p) => p.lan_tx_bps) },
          ]}
        />
      )}
    </>
  );
}

function GpuProcs({ n }: { n: NodeStatus }) {
  const procs = (n.gpu_procs ?? []).slice(0, 4);
  if (procs.length === 0) return null;
  return (
    <div style={{ padding: "7px 0", borderTop: "1px solid var(--border)" }}>
      <div className="faint" style={{ fontSize: 11, marginBottom: 4 }}>GPU processes</div>
      <table style={{ width: "100%", fontSize: 11 }}>
        <tbody>
          {procs.map((p) => (
            <tr key={p.pid}>
              <td className="mono faint" style={{ width: 60 }}>{p.pid}</td>
              <td className="mono" style={{ overflow: "hidden", textOverflow: "ellipsis", maxWidth: 160 }}>{p.name}</td>
              <td className="mono faint" style={{ textAlign: "right" }}>{p.mem_mib != null ? `${(p.mem_mib / 1024).toFixed(1)}G` : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function NodeCard({ n, hist }: { n: NodeStatus; hist?: HistoryPoint[] }) {
  const h = hist ?? [];
  return (
    <div className="card">
      <div className="card-head">
        <div className="flex">
          <strong>{n.name}</strong>
          <Badge kind="blue" dot={false}>{n.role}</Badge>
        </div>
        <div className="flex gap-sm">
          {n.uptime_seconds != null && <span className="faint" style={{ fontSize: 11 }}>up {fmtUptime(n.uptime_seconds)}</span>}
          <Badge kind={boolKind(n.reachable)}>{n.reachable ? "online" : "offline"}</Badge>
        </div>
      </div>
      {!n.reachable ? (
        <div className="faint">{n.detail ?? "Unreachable over SSH."}</div>
      ) : (
        <>
          <div className="flex wrap mb">
            <Badge kind={boolKind(n.docker_ok)}>docker</Badge>
            <Badge kind={boolKind(n.ray_container_up)}>ray container</Badge>
          </div>
          {n.gpus.length === 0 ? (
            <div className="faint">No GPU telemetry (nvidia-smi unavailable).</div>
          ) : (
            n.gpus.map((g) => (
              <GpuRow key={g.index} g={g} spark={g.index === 0 ? pts(h, (p) => p.gpu_util_pct) : undefined} />
            ))
          )}
          {n.cpu_pct != null && (
            <MetricRow
              label={`cpu${n.cpu_count ? ` · ${n.cpu_count} cores` : ""}${n.loadavg_1m != null ? ` · load ${n.loadavg_1m.toFixed(2)}` : ""}`}
              value={`${n.cpu_pct.toFixed(0)}%`}
              meterValue={n.cpu_pct}
              meterMax={100}
              spark={[{ color: CLR.cpu, points: pts(h, (p) => p.cpu_pct) }]}
              sparkMax={100}
            />
          )}
          {n.sys_mem_total_mib != null && (
            <MetricRow
              label="memory (unified)"
              value={`${Math.round((n.sys_mem_used_mib ?? 0) / 1024)} / ${Math.round(n.sys_mem_total_mib / 1024)} GiB`}
              meterValue={n.sys_mem_used_mib ?? 0}
              meterMax={n.sys_mem_total_mib}
              spark={[{ color: CLR.mem, points: pts(h, (p) => (p.mem_used_mib != null ? p.mem_used_mib / 1024 : null)) }]}
              sparkMax={n.sys_mem_total_mib / 1024}
            />
          )}
          <NetRow n={n} hist={h} />
          {n.disk?.total_bytes != null && (
            <MetricRow
              label={`models disk (${n.disk.path})`}
              value={`${fmtBytes(n.disk.used_bytes)} / ${fmtBytes(n.disk.total_bytes)} · ${fmtBytes(n.disk.free_bytes)} free`}
              meterValue={n.disk.used_bytes ?? 0}
              meterMax={n.disk.total_bytes}
            />
          )}
          <GpuProcs n={n} />
          {n.mem_budget_total_gib != null && (
            <MetricRow
              label="instance memory budget"
              value={`${fmtGib(n.mem_budget_used_gib)} / ${fmtGib(n.mem_budget_total_gib)}`}
              meterValue={n.mem_budget_used_gib ?? 0}
              meterMax={n.mem_budget_total_gib ?? 1}
            />
          )}
        </>
      )}
    </div>
  );
}

function Tile({ label, value, kind }: { label: string; value: string; kind?: any }) {
  return (
    <div className="card stat">
      <span className="k">{label}</span>
      <span className="v">{kind ? <Badge kind={kind}>{value}</Badge> : value}</span>
    </div>
  );
}

export default function Dashboard() {
  const { data, error, connected } = useStatusStream(3);
  const history = usePoll(() => api.getStatusHistory(15), 10000);
  const histByNode = new Map<number, NodeHistory>((history.data ?? []).map((h) => [h.node_id, h]));

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Dashboard</h1>
          <p>Live health of the cluster, GPUs, and running models.</p>
        </div>
        <Badge kind={connected ? "green" : "amber"}>{connected ? "live" : "polling"}</Badge>
      </div>

      {error && <div className="banner banner-warn">⚠ {error}</div>}
      {!data && !error && <div className="card center" style={{ padding: 40 }}><Spinner /></div>}

      {data && (
        <>
          {!data.setup_complete && (
            <div className="banner banner-info">
              ◆ Cluster setup is not complete. Head to <Link to="/setup">Setup</Link> to provision the nodes.
            </div>
          )}
          {data.overcommit_warnings.map((w, i) => (
            <div key={i} className="banner banner-warn">⚠ {w}</div>
          ))}

          <div className="grid grid-3 mb">
            <Tile label="Setup" value={data.setup_complete ? "complete" : "incomplete"} kind={data.setup_complete ? "green" : "amber"} />
            <Tile label="QSFP link" value={data.qsfp_ok == null ? "unknown" : data.qsfp_ok ? "up" : "down"} kind={data.qsfp_ok ? "green" : data.qsfp_ok === false ? "red" : "gray"} />
            <Tile
              label="Ray nodes"
              value={data.ray.reachable ? `${data.ray.nodes_alive ?? 0} alive · ${data.ray.gpus_total ?? 0} GPU` : "offline"}
              kind={data.ray.reachable && (data.ray.nodes_alive ?? 0) >= data.nodes.length ? "green" : data.ray.reachable ? "amber" : "gray"}
            />
          </div>

          <div className="grid grid-2 mb">
            {data.nodes.map((n) => (
              <NodeCard key={n.node_id} n={n} hist={histByNode.get(n.node_id)?.points} />
            ))}
          </div>

          <div className="card">
            <div className="card-head"><h2 style={{ margin: 0 }}>Instances</h2><Link className="btn btn-sm" to="/instances">Manage</Link></div>
            {data.instances.length === 0 ? (
              <EmptyState icon="▶" title="No model instances yet">
                <Link to="/instances">Create one</Link> once models are downloaded.
              </EmptyState>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr><th>Name</th><th>Status</th><th>Health</th><th>Service</th><th>Served model</th><th>Endpoint</th></tr>
                  </thead>
                  <tbody>
                    {data.instances.map((i) => (
                      <tr key={i.instance_id}>
                        <td><strong>{i.name}</strong></td>
                        <td><Badge kind={statusKind(i.status)}>{i.status}</Badge></td>
                        <td><Badge kind={boolKind(i.health_ok)}>{i.health_ok == null ? "—" : i.health_ok ? "healthy" : "down"}</Badge></td>
                        <td><Badge kind={boolKind(i.systemd_active)}>{i.systemd_active == null ? "—" : i.systemd_active ? "active" : "inactive"}</Badge></td>
                        <td className="mono faint">{i.served_model ?? "—"}</td>
                        <td className="mono faint">{i.endpoint ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
