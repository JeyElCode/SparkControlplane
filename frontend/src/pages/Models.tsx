import { useState } from "react";
import { api, Model, NodeStorage } from "../lib/api";
import { usePoll } from "../lib/hooks";
import { fmtBytes, statusKind } from "../lib/format";
import { Badge, EmptyState, HelpTip, Meter, Modal, Spinner } from "../components/ui";
import { JobLogPanel } from "../components/JobLogPanel";
import { useToast } from "../components/Toast";

export default function Models() {
  const models = usePoll(() => api.listModels(), 8000);
  const suggestions = usePoll(() => api.suggestions(), 0);
  // Node reachability comes from the status snapshot (the authoritative live
  // probe). The model registry stores only last-known presence, so when a node
  // is offline we must not keep showing a stale "present ✓" for it.
  const status = usePoll(() => api.getStatus(), 8000);
  const { toast } = useToast();
  const [repo, setRepo] = useState("");
  const [validating, setValidating] = useState(false);
  const [valInfo, setValInfo] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [job, setJob] = useState<{ id: number; label: string } | null>(null);
  const [storage, setStorage] = useState<NodeStorage[] | null>(null);
  const [storageBusy, setStorageBusy] = useState(false);

  const scanStorage = async () => {
    setStorageBusy(true);
    try {
      setStorage(await api.getStorage());
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setStorageBusy(false);
    }
  };

  // Free-space projection for downloads: the download lands on the head first.
  const headDisk = (status.data?.nodes ?? []).find((n) => n.role === "head")?.disk;

  const scan = async () => {
    setScanning(true);
    try {
      const before = (models.data ?? []).length;
      const after = await api.scanModels();
      const added = after.length - before;
      toast(added > 0 ? `Imported ${added} model(s) found on disk` : "No new models on disk", "success");
      models.reload();
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setScanning(false);
    }
  };

  const validate = async () => {
    if (!repo) return;
    setValidating(true);
    setValInfo(null);
    try {
      const r = await api.validateRepo(repo.trim());
      if (r.ok) {
        const fits =
          r.size_bytes && headDisk?.free_bytes != null
            ? r.size_bytes > headDisk.free_bytes
              ? ` · ⚠ head has only ${fmtBytes(headDisk.free_bytes)} free`
              : ` · head has ${fmtBytes(headDisk.free_bytes)} free`
            : "";
        setValInfo(`✓ Found · ${r.size_bytes ? fmtBytes(r.size_bytes) : "size n/a"} · parser: ${r.tool_parser ?? "none"}${r.gated ? " · gated (accept license on HF)" : ""}${fits}`);
      } else {
        setValInfo(`✗ ${r.error}`);
      }
    } finally {
      setValidating(false);
    }
  };

  const add = async () => {
    if (!repo.trim()) return;
    setAdding(true);
    try {
      await api.addModel(repo.trim());
      toast("Model added to registry", "success");
      setRepo("");
      setValInfo(null);
      models.reload();
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setAdding(false);
    }
  };

  const startJob = async (p: Promise<{ job_id: number }>, label: string) => {
    try {
      const r = await p;
      setJob({ id: r.job_id, label });
    } catch (e: any) {
      toast(e.message, "error");
    }
  };

  const stop = async (m: Model) => {
    if (
      !confirm(
        `Stop the transfer for ${m.name}? This kills the download/sync on the node ` +
          `and clears any stale locks. Partial files are kept, so Download resumes from where it left off.`,
      )
    )
      return;
    try {
      const r = await api.cancelModel(m.id);
      toast("Stopping transfer…", "success");
      // Open the cleanup job's log so it's visible even for an orphaned transfer
      // (which has no active_job_id to surface a "View log" button).
      setJob({ id: r.job_id, label: `Stop ${m.name}` });
      models.reload();
    } catch (e: any) {
      toast(e.message, "error");
    }
  };

  // node_id -> reachable (only for nodes present in the latest snapshot). A node
  // missing from the map (snapshot still loading, or node not configured) is
  // treated as "unknown" and rendered normally rather than falsely "offline".
  const nodeReach = new Map<number, boolean>();
  (status.data?.nodes ?? []).forEach((n) => nodeReach.set(n.node_id, n.reachable));
  const offlineNodes = (status.data?.nodes ?? []).filter((n) => !n.reachable).map((n) => n.name);

  const del = (m: Model) => {
    if (
      !confirm(
        `Delete ${m.name}? This removes its files from all nodes (${fmtBytes(m.size_bytes)}) ` +
          `and the registry entry. A re-download is required to restore it.`,
      )
    )
      return;
    // drop_row=true so it's gone for good — otherwise startup/scan discovery
    // would just re-import the leftover directory.
    startJob(api.deleteModelFiles(m.id, null, true), `Delete ${m.name}`);
  };

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Models</h1>
          <p>Download to the head node, then auto-sync to the worker with checksum verification.</p>
        </div>
      </div>

      <div className="card mb">
        <h2>Add a model</h2>
        <div className="flex gap-sm wrap mb">
          <input style={{ flex: 1, minWidth: 280 }} placeholder="HuggingFace repo id, e.g. Qwen/Qwen3-30B-A3B-FP8" value={repo} onChange={(e) => { setRepo(e.target.value); setValInfo(null); }} />
          <button className="btn" onClick={validate} disabled={!repo || validating}>{validating ? <Spinner /> : "Validate"}</button>
          <button className="btn btn-primary" onClick={add} disabled={!repo || adding}>{adding ? <Spinner /> : "Add to registry"}</button>
        </div>
        {valInfo && <div className={`banner ${valInfo.startsWith("✓") ? "banner-info" : "banner-warn"}`}>{valInfo}</div>}
        <div className="flex wrap gap-sm">
          {(suggestions.data ?? []).map((s) => (
            <button key={s.repo_id} className="btn btn-sm" title={s.note ?? ""} onClick={() => { setRepo(s.repo_id); setValInfo(null); }}>
              {s.label} <span className="faint">· {s.approx_size_gb ? `${s.approx_size_gb}GB` : ""} {s.tool_parser ?? ""}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <h2 style={{ margin: 0 }}>Registry</h2>
          <div className="btn-row">
            <button className="btn btn-sm" onClick={scan} disabled={scanning} title="Import any models already on the nodes' disks into the registry">
              {scanning ? <Spinner /> : "Scan nodes"}
            </button>
            <button className="btn btn-sm" onClick={() => models.reload()}>Refresh</button>
          </div>
        </div>
        {offlineNodes.length > 0 && (
          <div className="banner banner-warn">
            ⚠ {offlineNodes.join(", ")} {offlineNodes.length > 1 ? "are" : "is"} unreachable —
            per-node presence below reflects the last known state, not live disk contents.
          </div>
        )}
        {(models.data ?? []).length === 0 ? (
          <EmptyState icon="◈" title="No models yet">Add a model above to get started.</EmptyState>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Model</th>
                  <th>Size</th>
                  <th>
                    Parser
                    <HelpTip text="The vLLM tool-call parser (--tool-call-parser) for OpenAI tool/function calling — e.g. hermes (Qwen), qwen3_xml (Qwen3-Coder), llama3_json, mistral. Auto-detected from the model name; required for tool_choice:auto to work. Override it per instance when creating one." />
                  </th>
                  <th>Per-node</th>
                  <th>Status</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {(models.data ?? []).map((m) => {
                  const activeJob = m.active_job_id ?? null;
                  // "busy" covers an orphaned transfer too: after a control-plane
                  // restart there's no in-memory job, but a node may still read
                  // downloading/syncing — Stop reaps it and resets the state.
                  const busy =
                    activeJob != null ||
                    m.node_states.some((s) => ["downloading", "syncing", "verifying"].includes(s.status));
                  return (
                  <tr key={m.id}>
                    <td><strong>{m.name}</strong><div className="faint mono" style={{ fontSize: 11 }}>{m.repo_id}</div></td>
                    <td className="mono">{fmtBytes(m.size_bytes)}</td>
                    <td><span className="tag">{m.tool_parser ?? "—"}</span></td>
                    <td>
                      <div className="flex-col gap-sm">
                        {m.node_states.map((s) => {
                          // Confirmed offline (in the snapshot and unreachable) — don't
                          // present stale registry state as live truth.
                          const offline = nodeReach.get(s.node_id) === false;
                          return (
                          <div key={s.node_id} className="flex-col" style={{ gap: 3, minWidth: 160 }}>
                            {offline ? (
                              <Badge kind="gray">
                                <span title={`Node unreachable — last known: ${s.present ? "present" : s.status}`}>
                                  {s.node_name}: offline
                                </span>
                              </Badge>
                            ) : (
                              <Badge kind={s.present && s.checksum_ok === false ? "amber" : statusKind(s.status)}>
                                {s.node_name}: {s.present ? "✓" : s.status}
                                {s.present && s.checksum_ok === false ? " ⚠ checksum" : ""}
                              </Badge>
                            )}
                            {!offline && (s.status === "downloading" || s.status === "syncing") && s.progress != null && (
                              <div className="progress-row" style={{ margin: 0 }}>
                                <Meter value={s.progress} max={1} />
                                <span className="pct">{Math.round(s.progress * 100)}%</span>
                              </div>
                            )}
                          </div>
                          );
                        })}
                      </div>
                    </td>
                    <td><Badge kind={statusKind(m.status)}>{m.status}</Badge></td>
                    <td>
                      <div className="btn-row" style={{ justifyContent: "flex-end" }}>
                        {activeJob != null && (
                          <button className="btn btn-sm btn-primary" onClick={() => setJob({ id: activeJob, label: `${m.name} (in progress)` })}>
                            View log
                          </button>
                        )}
                        {busy ? (
                          <button className="btn btn-sm btn-danger" onClick={() => stop(m)} title="Stop the transfer and clear stale locks (partial files are kept)">Stop</button>
                        ) : (
                          <>
                            <button className="btn btn-sm" onClick={() => startJob(api.downloadModel(m.id, true), `Download ${m.name}`)}>Download</button>
                            <button className="btn btn-sm" onClick={() => startJob(api.syncModel(m.id), `Sync ${m.name}`)}>Sync</button>
                            <button className="btn btn-sm btn-danger" onClick={() => del(m)}>Delete</button>
                          </>
                        )}
                      </div>
                    </td>
                  </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <div className="card-head">
          <h2 style={{ margin: 0 }}>Storage</h2>
          <button className="btn btn-sm" onClick={scanStorage} disabled={storageBusy}>
            {storageBusy ? <Spinner /> : storage ? "Re-scan" : "Scan storage"}
          </button>
        </div>
        {!storage ? (
          <div className="faint">Scan to see per-node disk usage, orphaned model directories, and the HuggingFace cache.</div>
        ) : (
          <div className="grid grid-2">
            {storage.map((n) => (
              <div key={n.node_id}>
                <div className="spread mb">
                  <strong>{n.node_name}</strong>
                  {n.disk && (
                    <span className="mono faint" style={{ fontSize: 12 }}>
                      {fmtBytes(n.disk.used_bytes)} / {fmtBytes(n.disk.total_bytes)} · {fmtBytes(n.disk.free_bytes)} free
                    </span>
                  )}
                </div>
                {n.disk && <Meter value={n.disk.used_bytes} max={n.disk.total_bytes} />}
                {!n.reachable ? (
                  <div className="faint" style={{ marginTop: 8 }}>{n.error ?? "Unreachable."}</div>
                ) : (
                  <table style={{ width: "100%", fontSize: 12, marginTop: 8 }}>
                    <tbody>
                      {n.models.map((m) => (
                        <tr key={m.name}>
                          <td className="mono">{m.name}</td>
                          <td className="mono faint" style={{ textAlign: "right" }}>{fmtBytes(m.size_bytes)}</td>
                          <td style={{ width: 70 }} />
                        </tr>
                      ))}
                      {n.orphans.map((o) => (
                        <tr key={o.name}>
                          <td className="mono" style={{ color: "var(--amber)" }}>{o.name} <span className="faint">(orphan)</span></td>
                          <td className="mono faint" style={{ textAlign: "right" }}>{fmtBytes(o.size_bytes)}</td>
                          <td style={{ textAlign: "right" }}>
                            <button className="btn btn-sm btn-danger" onClick={() => {
                              if (confirm(`Delete orphaned directory '${o.name}' (${fmtBytes(o.size_bytes)}) on ${n.node_name}? No registry model references it.`))
                                startJob(api.deleteOrphan(n.node_id, o.name), `Delete orphan ${o.name}`);
                            }}>Delete</button>
                          </td>
                        </tr>
                      ))}
                      <tr>
                        <td className="faint">HuggingFace cache</td>
                        <td className="mono faint" style={{ textAlign: "right" }}>{n.hf_cache_bytes != null ? fmtBytes(n.hf_cache_bytes) : "—"}</td>
                        <td style={{ textAlign: "right" }}>
                          {(n.hf_cache_bytes ?? 0) > 0 && (
                            <button className="btn btn-sm" onClick={() => {
                              if (confirm(`Clear the HuggingFace cache on ${n.node_name} (${fmtBytes(n.hf_cache_bytes)})? Only cached downloads are removed — models are untouched.`))
                                startJob(api.clearHfCache([n.node_id]), `Clear HF cache on ${n.node_name}`);
                            }}>Clear</button>
                          )}
                        </td>
                      </tr>
                    </tbody>
                  </table>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      {job && (
        <Modal title={job.label} wide onClose={() => { setJob(null); models.reload(); setStorage(null); }}>
          <JobLogPanel jobId={job.id} title={job.label} onDone={() => models.reload()} />
        </Modal>
      )}
    </div>
  );
}
