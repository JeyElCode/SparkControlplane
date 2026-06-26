import { Link } from "react-router-dom";
import { api, GpuStatus, NodeStatus } from "../lib/api";
import { usePoll } from "../lib/hooks";
import { boolKind, fmtGib, statusKind } from "../lib/format";
import { Badge, EmptyState, Meter, Spinner } from "../components/ui";

function GpuRow({ g }: { g: GpuStatus }) {
  const memPct = g.mem_total_mib ? (g.mem_used_mib ?? 0) / g.mem_total_mib : 0;
  return (
    <div style={{ padding: "8px 0", borderTop: "1px solid var(--border)" }}>
      <div className="spread" style={{ marginBottom: 4 }}>
        <span className="mono faint">GPU{g.index} {g.name ?? ""}</span>
        <span className="faint">{g.temp_c != null ? `${g.temp_c}°C` : ""} {g.power_w != null ? `· ${g.power_w.toFixed(0)}W` : ""}</span>
      </div>
      <div className="spread gap-sm" style={{ marginBottom: 3 }}>
        <span className="faint" style={{ fontSize: 11, width: 40 }}>util</span>
        <Meter value={g.util_pct ?? 0} max={100} />
        <span className="mono" style={{ fontSize: 11, width: 38, textAlign: "right" }}>{g.util_pct ?? 0}%</span>
      </div>
      <div className="spread gap-sm">
        <span className="faint" style={{ fontSize: 11, width: 40 }}>mem</span>
        <Meter value={g.mem_used_mib ?? 0} max={g.mem_total_mib ?? 1} />
        <span className="mono" style={{ fontSize: 11, width: 80, textAlign: "right" }}>
          {Math.round((g.mem_used_mib ?? 0) / 1024)}/{Math.round((g.mem_total_mib ?? 0) / 1024)}G
        </span>
      </div>
    </div>
  );
}

function NodeCard({ n }: { n: NodeStatus }) {
  return (
    <div className="card">
      <div className="card-head">
        <div className="flex">
          <strong>{n.name}</strong>
          <Badge kind="blue" dot={false}>{n.role}</Badge>
        </div>
        <Badge kind={boolKind(n.reachable)}>{n.reachable ? "online" : "offline"}</Badge>
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
            n.gpus.map((g) => <GpuRow key={g.index} g={g} />)
          )}
          {n.mem_budget_total_gib != null && (
            <div style={{ marginTop: 12 }}>
              <div className="spread" style={{ fontSize: 12, marginBottom: 4 }}>
                <span className="faint">instance memory budget</span>
                <span className="mono">{fmtGib(n.mem_budget_used_gib)} / {fmtGib(n.mem_budget_total_gib)}</span>
              </div>
              <Meter value={n.mem_budget_used_gib ?? 0} max={n.mem_budget_total_gib ?? 1} />
            </div>
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
  const { data, error, loading } = usePoll(() => api.getStatus(), 8000);

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Dashboard</h1>
          <p>Live health of the cluster, GPUs, and running models.</p>
        </div>
      </div>

      {error && <div className="banner banner-warn">⚠ {error}</div>}
      {loading && !data && <div className="card center" style={{ padding: 40 }}><Spinner /></div>}

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
              kind={data.ray.reachable && (data.ray.nodes_alive ?? 0) >= 2 ? "green" : data.ray.reachable ? "amber" : "gray"}
            />
          </div>

          <div className="grid grid-2 mb">
            {data.nodes.map((n) => <NodeCard key={n.node_id} n={n} />)}
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
