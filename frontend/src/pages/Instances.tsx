import { useState } from "react";
import { Link } from "react-router-dom";
import { api, InstanceInput, Instance } from "../lib/api";
import { usePoll } from "../lib/hooks";
import { statusKind } from "../lib/format";
import { Badge, EmptyState, Field, Modal, Spinner } from "../components/ui";
import { JobLogPanel } from "../components/JobLogPanel";
import { useToast } from "../components/Toast";

const DEFAULTS: InstanceInput = {
  name: "",
  model_id: 0,
  topology: "cluster",
  node_id: null,
  port: 8000,
  max_model_len: 8192,
  gpu_memory_utilization: 0.85,
  max_num_seqs: null,
  dtype: null,
  enable_tool_choice: true,
  tool_parser: null,
  extra_args: null,
  api_key: null,
  autostart: true,
};

function CreateForm({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const models = usePoll(() => api.listModels(), 0);
  const nodes = usePoll(() => api.listNodes(), 0);
  const { toast } = useToast();
  const [f, setF] = useState<InstanceInput>(DEFAULTS);
  const [busy, setBusy] = useState(false);
  const set = (k: keyof InstanceInput, v: any) => setF((p) => ({ ...p, [k]: v }));
  const selModel = (models.data ?? []).find((m) => m.id === f.model_id);

  const submit = async () => {
    if (!f.name || !f.model_id) {
      toast("Pick a name and a model", "error");
      return;
    }
    setBusy(true);
    try {
      await api.createInstance({ ...f, port: Number(f.port) });
      toast("Instance created", "success");
      onCreated();
      onClose();
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title="New instance"
      wide
      onClose={onClose}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary" onClick={submit} disabled={busy}>{busy ? <Spinner /> : "Create"}</button>
        </>
      }
    >
      <div className="row-2">
        <Field label="Name"><input value={f.name} placeholder="qwen-main" onChange={(e) => set("name", e.target.value)} /></Field>
        <Field label="Model">
          <select value={f.model_id} onChange={(e) => set("model_id", Number(e.target.value))}>
            <option value={0}>— select —</option>
            {(models.data ?? []).map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
          </select>
        </Field>
      </div>
      <div className="row-2">
        <Field label="Topology" hint="cluster = both nodes (TP=2). single = pinned to one node (TP=1).">
          <select value={f.topology} onChange={(e) => set("topology", e.target.value)}>
            <option value="cluster">cluster (TP=2, both nodes)</option>
            <option value="single">single node (TP=1)</option>
          </select>
        </Field>
        {f.topology === "single" ? (
          <Field label="Target node">
            <select value={f.node_id ?? 0} onChange={(e) => set("node_id", Number(e.target.value) || null)}>
              <option value={0}>— select —</option>
              {(nodes.data ?? []).map((n) => <option key={n.id} value={n.id}>{n.name} ({n.role})</option>)}
            </select>
          </Field>
        ) : (
          <Field label="Port"><input type="number" value={f.port} onChange={(e) => set("port", Number(e.target.value))} /></Field>
        )}
      </div>
      {f.topology === "single" && (
        <Field label="Port"><input type="number" value={f.port} onChange={(e) => set("port", Number(e.target.value))} /></Field>
      )}
      <div className="row-2">
        <Field
          label="Max model length"
          help="Maximum context length in tokens vLLM will serve (--max-model-len). Lower it to shrink KV-cache memory use; leave blank to use the model's default. Cannot exceed the model's trained context window."
        >
          <input type="number" value={f.max_model_len ?? ""} onChange={(e) => set("max_model_len", e.target.value ? Number(e.target.value) : null)} />
        </Field>
        <Field
          label="GPU memory utilization"
          help="Fraction of GPU memory vLLM may use for weights + KV cache (--gpu-memory-utilization, 0–1). Higher allows longer context and more concurrency but leaves less headroom; ~0.85 is typical. Lower it if you co-locate models or hit out-of-memory."
        >
          <input type="number" step="0.05" min="0.1" max="0.99" value={f.gpu_memory_utilization} onChange={(e) => set("gpu_memory_utilization", Number(e.target.value))} />
        </Field>
      </div>
      <div className="row-2">
        <Field
          label="Max num seqs (optional)"
          help="Maximum number of requests vLLM batches at once (--max-num-seqs). Lower it to reduce KV-cache memory pressure; leave blank for vLLM's default."
        >
          <input type="number" value={f.max_num_seqs ?? ""} onChange={(e) => set("max_num_seqs", e.target.value ? Number(e.target.value) : null)} />
        </Field>
        <Field
          label="dtype (optional)"
          help="Weight/compute precision (--dtype): auto, bfloat16, float16, or float32. 'auto' uses the model's native precision (FP8 models are handled via their own config). Usually leave as auto."
        >
          <input value={f.dtype ?? ""} placeholder="auto" onChange={(e) => set("dtype", e.target.value || null)} />
        </Field>
      </div>
      <label className="checkbox">
        <input type="checkbox" checked={f.enable_tool_choice} onChange={(e) => set("enable_tool_choice", e.target.checked)} />
        <span><span className="cb-label">Enable tool calling</span><div className="cb-sub">Adds --enable-auto-tool-choice with the right parser{selModel?.tool_parser ? ` (auto: ${selModel.tool_parser})` : ""}.</div></span>
      </label>
      <Field
        label="Tool parser override (optional)"
        hint="Leave blank to auto-map from the model name."
        help="Overrides the auto-selected --tool-call-parser used for OpenAI tool/function calling (e.g. hermes for Qwen, qwen3_xml for Qwen3-Coder, llama3_json, mistral). Only set this if tool calling misbehaves with the auto-detected parser."
      >
        <input value={f.tool_parser ?? ""} placeholder={selModel?.tool_parser ?? "auto"} onChange={(e) => set("tool_parser", e.target.value || null)} />
      </Field>
      <Field label="Extra vllm args (optional)"><input value={f.extra_args ?? ""} placeholder="--enforce-eager" onChange={(e) => set("extra_args", e.target.value || null)} /></Field>
      <div className="row-2">
        <Field label="API key (optional)" hint="Secures the endpoint with --api-key."><input type="password" value={f.api_key ?? ""} onChange={(e) => set("api_key", e.target.value || null)} /></Field>
        <label className="checkbox" style={{ marginTop: 24 }}>
          <input type="checkbox" checked={f.autostart} onChange={(e) => set("autostart", e.target.checked)} />
          <span><span className="cb-label">Auto-start on boot</span><div className="cb-sub">Enable the systemd unit so it survives reboots.</div></span>
        </label>
      </div>
    </Modal>
  );
}

export default function Instances() {
  const instances = usePoll(() => api.listInstances(), 8000);
  const { toast } = useToast();
  const [creating, setCreating] = useState(false);
  const [job, setJob] = useState<{ id: number; label: string } | null>(null);

  const act = async (p: Promise<{ job_id: number }>, label: string) => {
    try {
      const r = await p;
      setJob({ id: r.job_id, label });
    } catch (e: any) {
      toast(e.message, "error");
    }
  };

  const del = async (i: Instance) => {
    if (!confirm(`Delete instance ${i.name}? Stops it and removes its systemd unit.`)) return;
    act(api.deleteInstance(i.id), `Delete ${i.name}`);
  };

  const copyClient = (i: Instance) => {
    const text = `Base URL: ${i.node_role ? "http://<node-ip>" : ""}:${i.port}/v1\nModel: /models/${i.model_name}`;
    navigator.clipboard?.writeText(text);
    toast("Client config copied", "success");
  };

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Instances</h1>
          <p>Run one or more vLLM servers across the cluster or pinned to a node.</p>
        </div>
        <button className="btn btn-primary" onClick={() => setCreating(true)}>+ New instance</button>
      </div>

      {(instances.data ?? []).length === 0 ? (
        <div className="card"><EmptyState icon="▶" title="No instances yet">Create one once a model is downloaded and synced. See <Link to="/models">Models</Link>.</EmptyState></div>
      ) : (
        <div className="grid grid-2">
          {(instances.data ?? []).map((i) => (
            <div key={i.id} className="card">
              <div className="card-head">
                <div className="flex"><strong>{i.name}</strong><Badge kind={statusKind(i.status)}>{i.status}</Badge></div>
                <Badge kind="blue" dot={false}>{i.topology === "cluster" ? "cluster TP=2" : `single ${i.node_role ?? ""} TP=1`}</Badge>
              </div>
              <dl className="kv">
                <dt>Model</dt><dd>{i.model_name}</dd>
                <dt>Port</dt><dd className="mono">{i.port}</dd>
                <dt>Tool parser</dt><dd>{i.enable_tool_choice ? (i.tool_parser ?? "auto") : "off"}</dd>
                <dt>Context</dt><dd className="mono">{i.max_model_len ?? "default"} · gpu {i.gpu_memory_utilization}</dd>
                <dt>Boot</dt><dd>{i.autostart ? "auto-start" : "manual"}</dd>
              </dl>
              {i.last_error && <div className="banner banner-warn" style={{ marginTop: 10 }}>⚠ {i.last_error}</div>}
              <div className="btn-row mt">
                <button className="btn btn-sm btn-primary" onClick={() => act(api.startInstance(i.id), `Start ${i.name}`)}>Start</button>
                <button className="btn btn-sm" onClick={() => act(api.stopInstance(i.id), `Stop ${i.name}`)}>Stop</button>
                <button className="btn btn-sm" onClick={() => copyClient(i)}>Copy client cfg</button>
                <button className="btn btn-sm btn-danger" onClick={() => del(i)}>Delete</button>
              </div>
            </div>
          ))}
        </div>
      )}

      {creating && <CreateForm onClose={() => setCreating(false)} onCreated={() => instances.reload()} />}
      {job && (
        <Modal title={job.label} wide onClose={() => { setJob(null); instances.reload(); }}>
          <JobLogPanel jobId={job.id} title={job.label} onDone={() => instances.reload()} />
        </Modal>
      )}
    </div>
  );
}
