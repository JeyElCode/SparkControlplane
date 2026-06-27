import { useState } from "react";
import { api, Model } from "../lib/api";
import { usePoll } from "../lib/hooks";
import { fmtBytes, statusKind } from "../lib/format";
import { Badge, EmptyState, HelpTip, Meter, Modal, Spinner } from "../components/ui";
import { JobLogPanel } from "../components/JobLogPanel";
import { useToast } from "../components/Toast";

export default function Models() {
  const models = usePoll(() => api.listModels(), 8000);
  const suggestions = usePoll(() => api.suggestions(), 0);
  const { toast } = useToast();
  const [repo, setRepo] = useState("");
  const [validating, setValidating] = useState(false);
  const [valInfo, setValInfo] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [job, setJob] = useState<{ id: number; label: string } | null>(null);

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
        setValInfo(`✓ Found · ${r.size_bytes ? fmtBytes(r.size_bytes) : "size n/a"} · parser: ${r.tool_parser ?? "none"}${r.gated ? " · gated (accept license on HF)" : ""}`);
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
                {(models.data ?? []).map((m) => (
                  <tr key={m.id}>
                    <td><strong>{m.name}</strong><div className="faint mono" style={{ fontSize: 11 }}>{m.repo_id}</div></td>
                    <td className="mono">{fmtBytes(m.size_bytes)}</td>
                    <td><span className="tag">{m.tool_parser ?? "—"}</span></td>
                    <td>
                      <div className="flex-col gap-sm">
                        {m.node_states.map((s) => (
                          <div key={s.node_id} className="flex-col" style={{ gap: 3, minWidth: 160 }}>
                            <Badge kind={s.present && s.checksum_ok === false ? "amber" : statusKind(s.status)}>
                              {s.node_name}: {s.present ? "✓" : s.status}
                              {s.present && s.checksum_ok === false ? " ⚠ checksum" : ""}
                            </Badge>
                            {(s.status === "downloading" || s.status === "syncing") && s.progress != null && (
                              <div className="progress-row" style={{ margin: 0 }}>
                                <Meter value={s.progress} max={1} />
                                <span className="pct">{Math.round(s.progress * 100)}%</span>
                              </div>
                            )}
                          </div>
                        ))}
                      </div>
                    </td>
                    <td><Badge kind={statusKind(m.status)}>{m.status}</Badge></td>
                    <td>
                      <div className="btn-row" style={{ justifyContent: "flex-end" }}>
                        <button className="btn btn-sm" onClick={() => startJob(api.downloadModel(m.id, true), `Download ${m.name}`)}>Download</button>
                        <button className="btn btn-sm" onClick={() => startJob(api.syncModel(m.id), `Sync ${m.name}`)}>Sync</button>
                        <button className="btn btn-sm btn-danger" onClick={() => del(m)}>Delete</button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {job && (
        <Modal title={job.label} wide onClose={() => { setJob(null); models.reload(); }}>
          <JobLogPanel jobId={job.id} title={job.label} onDone={() => models.reload()} />
        </Modal>
      )}
    </div>
  );
}
