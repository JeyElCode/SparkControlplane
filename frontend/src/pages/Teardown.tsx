import { useState } from "react";
import { api, TeardownRequest } from "../lib/api";
import { Modal } from "../components/ui";
import { JobLogPanel } from "../components/JobLogPanel";
import { useToast } from "../components/Toast";

const ITEMS: { key: keyof TeardownRequest; label: string; sub: string; danger?: boolean }[] = [
  { key: "stop_instances", label: "Stop vLLM instances", sub: "Stop every running model instance." },
  { key: "stop_ray", label: "Stop & disable Ray cluster", sub: "Stop the Ray head/worker services and remove their containers." },
  { key: "remove_network", label: "Remove QSFP network config", sub: "Delete the nmcli qsfp-vllm connection and flush the static IP." },
  { key: "remove_inter_node_ssh", label: "Remove inter-node SSH trust", sub: "Delete the generated key and the head→worker SSH config." },
  { key: "remove_hosts_entries", label: "Remove /etc/hosts entries", sub: "Strip the spark-01/spark-02 host entries on both nodes." },
  { key: "delete_models", label: "Delete downloaded models", sub: "Remove ~40GB+ of model files from BOTH nodes. Irreversible.", danger: true },
];

export default function Teardown() {
  const { toast } = useToast();
  const [req, setReq] = useState<TeardownRequest>({
    stop_instances: true,
    stop_ray: true,
    remove_network: false,
    remove_inter_node_ssh: false,
    remove_hosts_entries: false,
    delete_models: false,
  });
  const [job, setJob] = useState<number | null>(null);

  const set = (k: keyof TeardownRequest, v: boolean) => setReq((p) => ({ ...p, [k]: v }));

  const run = async () => {
    const selected = ITEMS.filter((i) => req[i.key]).map((i) => i.label);
    if (selected.length === 0) {
      toast("Select at least one action", "error");
      return;
    }
    const warn = req.delete_models ? "\n\n⚠ This will DELETE all downloaded model files on both nodes." : "";
    if (!confirm(`Run teardown?\n\n- ${selected.join("\n- ")}${warn}`)) return;
    try {
      const r = await api.teardown(req);
      setJob(r.job_id);
    } catch (e: any) {
      toast(e.message, "error");
    }
  };

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Teardown / Reset</h1>
          <p>Selectively undo the setup. Model deletion is off by default so you don't lose downloads.</p>
        </div>
        <button className="btn btn-danger" onClick={run}>Run teardown</button>
      </div>

      <div className="card" style={{ maxWidth: 640 }}>
        {ITEMS.map((it) => (
          <label key={it.key} className="checkbox" style={{ borderTop: "1px solid var(--border)", paddingTop: 12 }}>
            <input type="checkbox" checked={req[it.key]} onChange={(e) => set(it.key, e.target.checked)} />
            <span>
              <span className="cb-label" style={it.danger ? { color: "var(--red)" } : undefined}>{it.label}</span>
              <div className="cb-sub">{it.sub}</div>
            </span>
          </label>
        ))}
      </div>

      {job && (
        <Modal title="Teardown" wide onClose={() => setJob(null)}>
          <JobLogPanel jobId={job} title="Teardown" onDone={() => toast("Teardown finished", "success")} />
        </Modal>
      )}
    </div>
  );
}
