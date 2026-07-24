import { useState } from "react";
import { Link } from "react-router-dom";
import { api, InstanceInput, Instance, Topology } from "../lib/api";
import { usePoll } from "../lib/hooks";
import { statusKind } from "../lib/format";
import { Badge, EmptyState, Field, Modal, Spinner } from "../components/ui";
import { JobLogPanel } from "../components/JobLogPanel";
import { LiveLogPanel } from "../components/LiveLogPanel";
import { useToast } from "../components/Toast";

const DEFAULTS: InstanceInput = {
  name: "",
  model_id: 0,
  topology: "cluster",
  node_id: null,
  port: undefined,
  max_model_len: 8192,
  gpu_memory_utilization: 0.85,
  max_num_seqs: null,
  dtype: null,
  enable_tool_choice: true,
  tool_parser: null,
  served_model_names: null,
  trust_remote_code: false,
  kv_cache_dtype: null,
  block_size: null,
  max_num_batched_tokens: null,
  tokenizer_mode: null,
  reasoning_parser: null,
  compilation_config: null,
  advanced_args: null,
  master_port: undefined,
  extra_args: null,
  vllm_image: null,
  api_key: null,
  tls_enabled: false,
  tls_port: 443,
  tls_cert: null,
  tls_key: null,
  autostart: true,
};

const TOPO_HELP: Record<Topology, string> = {
  single: "One node. vLLM runs on a single machine (TP=1). Pick the target node.",
  cluster: "Ray head + worker. vLLM shards the model across both nodes via Ray (TP=2).",
  distributed:
    "Native torch.distributed over the QSFP link — headless workers, no Ray. Uses all registered nodes; the head node's QSFP IP is the master-addr.",
};

function topoLabel(i: Instance): string {
  if (i.topology === "cluster") return "cluster TP=2";
  if (i.topology === "distributed") return "distributed (native)";
  return `single ${i.node_role ?? ""} TP=1`;
}

// ---- helpers for the advanced serialized fields ----

type ArgRow = { flag: string; value: string };

function splitAliases(s?: string | null): string[] {
  return (s ?? "").split(/\s+/).map((x) => x.trim()).filter(Boolean);
}

function parseArgs(s?: string | null): ArgRow[] {
  if (!s) return [];
  try {
    const arr = JSON.parse(s);
    if (Array.isArray(arr)) {
      return arr.map((r: any) => ({ flag: String(r?.flag ?? ""), value: r?.value == null ? "" : String(r.value) }));
    }
  } catch {
    /* ignore malformed stored value; start empty */
  }
  return [];
}

function serializeArgs(rows: ArgRow[]): string | null {
  const clean = rows.filter((r) => r.flag.trim());
  if (!clean.length) return null;
  return JSON.stringify(clean.map((r) => ({ flag: r.flag.trim(), value: r.value.trim() ? r.value.trim() : null })));
}

/** Returns a JSON parse error message, or null if empty/valid. */
function jsonError(s?: string | null): string | null {
  if (!s || !s.trim()) return null;
  try {
    JSON.parse(s);
    return null;
  } catch (e: any) {
    return e?.message ?? "Invalid JSON";
  }
}

// Subset of fields the advanced editor owns. Shared by create + edit forms.
type AdvValues = Pick<
  InstanceInput,
  | "served_model_names"
  | "trust_remote_code"
  | "kv_cache_dtype"
  | "block_size"
  | "max_num_batched_tokens"
  | "tokenizer_mode"
  | "reasoning_parser"
  | "compilation_config"
  | "advanced_args"
  | "extra_args"
  | "vllm_image"
>;

function VllmAdvanced({
  v,
  patch,
  modelAlias,
}: {
  v: AdvValues;
  patch: (p: Partial<AdvValues>) => void;
  modelAlias?: string;
}) {
  // Chips + rows are seeded once from the serialized props, then drive the
  // serialized value outward on every edit.
  const [aliases, setAliases] = useState<string[]>(() => splitAliases(v.served_model_names));
  const [aliasDraft, setAliasDraft] = useState("");
  const [rows, setRows] = useState<ArgRow[]>(() => parseArgs(v.advanced_args));
  const [expert, setExpert] = useState<boolean>(() => !!v.extra_args);

  const commitAliases = (next: string[]) => {
    setAliases(next);
    patch({ served_model_names: next.length ? next.join(" ") : null });
  };
  const addAlias = () => {
    const parts = splitAliases(aliasDraft);
    if (!parts.length) return;
    const next = Array.from(new Set([...aliases, ...parts]));
    setAliasDraft("");
    commitAliases(next);
  };
  const removeAlias = (a: string) => commitAliases(aliases.filter((x) => x !== a));

  const commitRows = (next: ArgRow[]) => {
    setRows(next);
    patch({ advanced_args: serializeArgs(next) });
  };
  const setRow = (idx: number, k: keyof ArgRow, val: string) =>
    commitRows(rows.map((r, i) => (i === idx ? { ...r, [k]: val } : r)));
  const addRow = () => commitRows([...rows, { flag: "", value: "" }]);
  const removeRow = (idx: number) => commitRows(rows.filter((_, i) => i !== idx));

  const compileErr = jsonError(v.compilation_config);

  return (
    <details className="collapse">
      <summary>Advanced vLLM settings</summary>
      <div className="collapse-body">
        <Field
          label="Served-model-name aliases"
          hint="Names clients use in the OpenAI `model` field (--served-model-name). Type a name and press Enter. Defaults to the registered model name if empty."
        >
          {aliases.length > 0 && (
            <div className="chips">
              {aliases.map((a) => (
                <span key={a} className="chip">
                  {a}
                  <button type="button" aria-label={`Remove ${a}`} onClick={() => removeAlias(a)}>
                    ✕
                  </button>
                </span>
              ))}
            </div>
          )}
          <div className="flex gap-sm">
            <input
              value={aliasDraft}
              placeholder={modelAlias || "my-model"}
              onChange={(e) => setAliasDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === ",") {
                  e.preventDefault();
                  addAlias();
                }
              }}
            />
            <button type="button" className="btn" onClick={addAlias} disabled={!aliasDraft.trim()}>
              Add
            </button>
          </div>
        </Field>

        <label className="checkbox">
          <input
            type="checkbox"
            checked={!!v.trust_remote_code}
            onChange={(e) => patch({ trust_remote_code: e.target.checked })}
          />
          <span>
            <span className="cb-label">Trust remote code</span>
            <div className="cb-sub">Adds --trust-remote-code so models with custom code in their repo can load. Only enable for repos you trust.</div>
          </span>
        </label>

        <div className="row-2">
          <Field
            label="KV cache dtype (optional)"
            help="Precision of the KV cache (--kv-cache-dtype), e.g. auto or fp8. fp8 roughly halves KV-cache memory at a small quality cost."
          >
            <input
              value={v.kv_cache_dtype ?? ""}
              placeholder="auto"
              onChange={(e) => patch({ kv_cache_dtype: e.target.value || null })}
            />
          </Field>
          <Field
            label="Block size (optional)"
            help="Paged-attention KV block size in tokens (--block-size), e.g. 16 or 256. Leave blank for vLLM's default."
          >
            <input
              type="number"
              value={v.block_size ?? ""}
              onChange={(e) => patch({ block_size: e.target.value ? Number(e.target.value) : null })}
            />
          </Field>
        </div>

        <div className="row-2">
          <Field
            label="Max num batched tokens (optional)"
            help="Upper bound on tokens processed together per step (--max-num-batched-tokens). Raise for throughput, lower to cap memory."
          >
            <input
              type="number"
              value={v.max_num_batched_tokens ?? ""}
              onChange={(e) => patch({ max_num_batched_tokens: e.target.value ? Number(e.target.value) : null })}
            />
          </Field>
          <Field
            label="Tokenizer mode (optional)"
            help="Tokenizer selection (--tokenizer-mode), e.g. auto, slow, or a model-specific mode. Leave blank for auto."
          >
            <input
              value={v.tokenizer_mode ?? ""}
              placeholder="auto"
              onChange={(e) => patch({ tokenizer_mode: e.target.value || null })}
            />
          </Field>
        </div>

        <Field
          label="Reasoning parser (optional)"
          help="Parser that extracts reasoning/thinking traces into a separate field (--reasoning-parser), for models that emit them. Leave blank if unused."
        >
          <input
            value={v.reasoning_parser ?? ""}
            placeholder="none"
            onChange={(e) => patch({ reasoning_parser: e.target.value || null })}
          />
        </Field>

        <Field
          label="Compilation config (JSON, optional)"
          help="Passed verbatim to --compilation-config as a single JSON argument. Validated client-side before submit."
        >
          <textarea
            value={v.compilation_config ?? ""}
            placeholder='{"level": 3}'
            spellCheck={false}
            onChange={(e) => patch({ compilation_config: e.target.value || null })}
          />
          {compileErr && <div className="field-err">Invalid JSON: {compileErr}</div>}
        </Field>

        <Field
          label="Advanced args"
          hint="Structured passthrough flags. Add a --flag with an optional value; leave the value blank for a boolean flag."
        >
          {rows.map((r, idx) => (
            <div className="arg-row" key={idx}>
              <input value={r.flag} placeholder="--some-flag" onChange={(e) => setRow(idx, "flag", e.target.value)} />
              <input value={r.value} placeholder="value (optional)" onChange={(e) => setRow(idx, "value", e.target.value)} />
              <button type="button" className="btn btn-sm btn-danger" onClick={() => removeRow(idx)} aria-label="Remove arg">
                ✕
              </button>
            </div>
          ))}
          <button type="button" className="btn btn-sm" onClick={addRow}>
            + Add arg
          </button>
        </Field>

        <Field label="Image override (optional)">
          <input
            value={v.vllm_image ?? ""}
            placeholder="registry/vllm-image:tag — else cluster default"
            onChange={(e) => patch({ vllm_image: e.target.value || null })}
          />
        </Field>

        <label className="checkbox">
          <input type="checkbox" checked={expert} onChange={(e) => setExpert(e.target.checked)} />
          <span>
            <span className="cb-label">Expert: raw extra args</span>
            <div className="cb-sub">Legacy free-text {`--flag`} string appended verbatim. Prefer the structured editor above.</div>
          </span>
        </label>
        {expert && (
          <Field label="Raw extra vllm args (optional)">
            <input
              value={v.extra_args ?? ""}
              placeholder="--enforce-eager"
              onChange={(e) => patch({ extra_args: e.target.value || null })}
            />
          </Field>
        )}
      </div>
    </details>
  );
}

// Optional TLS: an on-node nginx sidecar terminates HTTPS on `tls_port` and
// proxies to vLLM (which stays on `port`, internal). Cert/key are write-only PEM.
function TlsConfig({
  v,
  patch,
  editMode,
  hasTlsCert,
}: {
  v: Pick<InstanceInput, "tls_enabled" | "tls_port" | "tls_cert" | "tls_key">;
  patch: (p: Partial<InstanceInput>) => void;
  editMode?: boolean;
  hasTlsCert?: boolean;
}) {
  const on = !!v.tls_enabled;
  return (
    <details className="collapse">
      <summary>Direct-access TLS (rarely needed)</summary>
      <div className="collapse-body">
        <label className="checkbox">
          <input type="checkbox" checked={on} onChange={(e) => patch({ tls_enabled: e.target.checked })} />
          <span>
            <span className="cb-label">Terminate HTTPS with an nginx sidecar</span>
            <div className="cb-sub">Only for clients that connect straight to this instance, bypassing the /v1 gateway — external HTTPS is normally handled by the ingress in front of the portal. When enabled, vLLM binds loopback and an nginx sidecar terminates TLS on the port below (cert rotates without restarting the model).</div>
          </span>
        </label>
        {on && (
          <>
            <Field label="HTTPS port">
              <input
                type="number"
                value={v.tls_port ?? 443}
                onChange={(e) => patch({ tls_port: Number(e.target.value) })}
              />
            </Field>
            <Field
              label={editMode ? "Certificate PEM — leave blank to keep current" : "Certificate (PEM, fullchain)"}
              hint={editMode && hasTlsCert ? "A certificate is already stored." : undefined}
            >
              <textarea
                rows={4}
                value={v.tls_cert ?? ""}
                placeholder="-----BEGIN CERTIFICATE-----"
                onChange={(e) => patch({ tls_cert: e.target.value || null })}
              />
            </Field>
            <Field label={editMode ? "Private key PEM — leave blank to keep current" : "Private key (PEM)"}>
              <textarea
                rows={4}
                value={v.tls_key ?? ""}
                placeholder="-----BEGIN PRIVATE KEY-----"
                onChange={(e) => patch({ tls_key: e.target.value || null })}
              />
            </Field>
          </>
        )}
      </div>
    </details>
  );
}

function CreateForm({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const models = usePoll(() => api.listModels(), 0);
  const nodes = usePoll(() => api.listNodes(), 0);
  const { toast } = useToast();
  const [f, setF] = useState<InstanceInput>(DEFAULTS);
  const [busy, setBusy] = useState(false);
  const set = (k: keyof InstanceInput, v: any) => setF((p) => ({ ...p, [k]: v }));
  const patch = (p: Partial<InstanceInput>) => setF((prev) => ({ ...prev, ...p }));
  const selModel = (models.data ?? []).find((m) => m.id === f.model_id);

  const submit = async () => {
    if (!f.name || !f.model_id) {
      toast("Pick a name and a model", "error");
      return;
    }
    if (f.topology === "single" && !f.node_id) {
      toast("Pick a target node for a single-node instance", "error");
      return;
    }
    const compileErr = jsonError(f.compilation_config);
    if (compileErr) {
      toast(`Compilation config is not valid JSON: ${compileErr}`, "error");
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
        <Field label="Name"><input value={f.name} placeholder="main" onChange={(e) => set("name", e.target.value)} /></Field>
        <Field label="Model">
          <select value={f.model_id} onChange={(e) => set("model_id", Number(e.target.value))}>
            <option value={0}>— select —</option>
            {(models.data ?? []).map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
          </select>
        </Field>
      </div>

      <Field label="Topology" hint={TOPO_HELP[f.topology]}>
        <select value={f.topology} onChange={(e) => set("topology", e.target.value as Topology)}>
          <option value="single">single</option>
          <option value="cluster">cluster (Ray)</option>
          <option value="distributed">distributed (native multi-node)</option>
        </select>
      </Field>

      {f.topology === "single" && (
        <div className="row-2">
          <Field label="Target node">
            <select value={f.node_id ?? 0} onChange={(e) => set("node_id", Number(e.target.value) || null)}>
              <option value={0}>— select —</option>
              {(nodes.data ?? []).map((n) => <option key={n.id} value={n.id}>{n.name} ({n.role})</option>)}
            </select>
          </Field>
          <Field label="Port" hint="empty = auto — clients use the /v1 gateway"><input type="number" placeholder="auto" value={f.port ?? ""} onChange={(e) => set("port", e.target.value === "" ? undefined : Number(e.target.value))} /></Field>
        </div>
      )}
      {f.topology === "cluster" && (
        <Field label="Port" hint="empty = auto — clients use the /v1 gateway"><input type="number" placeholder="auto" value={f.port ?? ""} onChange={(e) => set("port", e.target.value === "" ? undefined : Number(e.target.value))} /></Field>
      )}
      {f.topology === "distributed" && (
        <>
          <div className="banner banner-info">
            Uses every registered node that has a QSFP IP. The head node is rank 0 and serves the API; workers run headless.
            Master-addr = the head node's QSFP IP.
          </div>
          <div className="row-2">
            <Field label="Port" help="API port on the head node." hint="empty = auto"><input type="number" placeholder="auto" value={f.port ?? ""} onChange={(e) => set("port", e.target.value === "" ? undefined : Number(e.target.value))} /></Field>
            <Field label="Master port" help="torch.distributed rendezvous port on the head node (--master-port).">
              <input type="number" placeholder="auto" value={f.master_port ?? ""} onChange={(e) => set("master_port", e.target.value === "" ? undefined : Number(e.target.value))} />
            </Field>
          </div>
        </>
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
        help="Overrides the auto-selected --tool-call-parser used for OpenAI tool/function calling (e.g. hermes, qwen3_xml, llama3_json, mistral). Only set this if tool calling misbehaves with the auto-detected parser."
      >
        <input value={f.tool_parser ?? ""} placeholder={selModel?.tool_parser ?? "auto"} onChange={(e) => set("tool_parser", e.target.value || null)} />
      </Field>

      <VllmAdvanced v={f} patch={patch} modelAlias={selModel?.name} />
      <TlsConfig v={f} patch={patch} />

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

// Fields editable after creation (mirrors the backend InstanceUpdate schema).
// Name / model / topology / node are fixed for an existing instance — changing
// them would make it a different instance, so those are shown read-only.
type EditFields = Pick<
  InstanceInput,
  | "port"
  | "max_model_len"
  | "gpu_memory_utilization"
  | "max_num_seqs"
  | "dtype"
  | "enable_tool_choice"
  | "tool_parser"
  | "served_model_names"
  | "trust_remote_code"
  | "kv_cache_dtype"
  | "block_size"
  | "max_num_batched_tokens"
  | "tokenizer_mode"
  | "reasoning_parser"
  | "compilation_config"
  | "advanced_args"
  | "master_port"
  | "extra_args"
  | "vllm_image"
  | "tls_enabled"
  | "tls_port"
  | "tls_cert"
  | "tls_key"
  | "autostart"
>;

function EditForm({ inst, onClose, onSaved }: { inst: Instance; onClose: () => void; onSaved: () => void }) {
  const { toast } = useToast();
  const [f, setF] = useState<EditFields>({
    port: inst.port,
    max_model_len: inst.max_model_len ?? null,
    gpu_memory_utilization: inst.gpu_memory_utilization,
    max_num_seqs: inst.max_num_seqs ?? null,
    dtype: inst.dtype ?? null,
    enable_tool_choice: inst.enable_tool_choice,
    tool_parser: inst.tool_parser ?? null,
    served_model_names: inst.served_model_names ?? null,
    trust_remote_code: inst.trust_remote_code,
    kv_cache_dtype: inst.kv_cache_dtype ?? null,
    block_size: inst.block_size ?? null,
    max_num_batched_tokens: inst.max_num_batched_tokens ?? null,
    tokenizer_mode: inst.tokenizer_mode ?? null,
    reasoning_parser: inst.reasoning_parser ?? null,
    compilation_config: inst.compilation_config ?? null,
    advanced_args: inst.advanced_args ?? null,
    master_port: inst.master_port ?? null,
    extra_args: inst.extra_args ?? null,
    vllm_image: inst.vllm_image ?? null,
    tls_enabled: inst.tls_enabled,
    tls_port: inst.tls_port,
    tls_cert: null, // write-only; blank keeps the stored cert
    tls_key: null,
    autostart: inst.autostart,
  });
  const [busy, setBusy] = useState(false);
  const set = (k: keyof EditFields, v: any) => setF((p) => ({ ...p, [k]: v }));
  const patch = (p: Partial<EditFields>) => setF((prev) => ({ ...prev, ...p }));

  const submit = async () => {
    const compileErr = jsonError(f.compilation_config);
    if (compileErr) {
      toast(`Compilation config is not valid JSON: ${compileErr}`, "error");
      return;
    }
    setBusy(true);
    try {
      // Blank cert/key mean "keep the stored one" — drop them so we don't clear it.
      const payload: Partial<InstanceInput> = { ...f, port: Number(f.port) };
      if (!payload.tls_cert) delete payload.tls_cert;
      if (!payload.tls_key) delete payload.tls_key;
      await api.updateInstance(inst.id, payload);
      toast("Instance updated", "success");
      onSaved();
      onClose();
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title={`Edit ${inst.name}`}
      wide
      onClose={onClose}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary" onClick={submit} disabled={busy}>{busy ? <Spinner /> : "Save"}</button>
        </>
      }
    >
      <div className="banner banner-info mb">
        Editing {inst.model_name} · {topoLabel(inst)}.
        Changes apply the next time this instance is started.
      </div>
      <div className="row-2">
        <Field label="Port" hint="empty = auto — clients use the /v1 gateway"><input type="number" placeholder="auto" value={f.port ?? ""} onChange={(e) => set("port", e.target.value === "" ? undefined : Number(e.target.value))} /></Field>
        <Field
          label="GPU memory utilization"
          help="Fraction of GPU memory vLLM may use for weights + KV cache (--gpu-memory-utilization, 0–1). Higher allows longer context and more concurrency but leaves less headroom; ~0.85 is typical."
        >
          <input type="number" step="0.05" min="0.1" max="0.99" value={f.gpu_memory_utilization} onChange={(e) => set("gpu_memory_utilization", Number(e.target.value))} />
        </Field>
      </div>
      <div className="row-2">
        <Field
          label="Max model length"
          help="Maximum context length in tokens vLLM will serve (--max-model-len). Lower it to shrink KV-cache memory use; leave blank to use the model's default."
        >
          <input type="number" value={f.max_model_len ?? ""} onChange={(e) => set("max_model_len", e.target.value ? Number(e.target.value) : null)} />
        </Field>
        <Field
          label="Max num seqs (optional)"
          help="Maximum number of requests vLLM batches at once (--max-num-seqs). Lower it to reduce KV-cache memory pressure; leave blank for vLLM's default."
        >
          <input type="number" value={f.max_num_seqs ?? ""} onChange={(e) => set("max_num_seqs", e.target.value ? Number(e.target.value) : null)} />
        </Field>
      </div>
      <div className="row-2">
        <Field
          label="dtype (optional)"
          help="Weight/compute precision (--dtype): auto, bfloat16, float16, or float32. Usually leave as auto."
        >
          <input value={f.dtype ?? ""} placeholder="auto" onChange={(e) => set("dtype", e.target.value || null)} />
        </Field>
        <Field
          label="Tool parser override (optional)"
          hint="Leave blank to auto-map from the model name."
          help="Overrides the auto-selected --tool-call-parser used for OpenAI tool/function calling (e.g. hermes, qwen3_xml, llama3_json, mistral)."
        >
          <input value={f.tool_parser ?? ""} placeholder="auto" onChange={(e) => set("tool_parser", e.target.value || null)} />
        </Field>
      </div>
      {inst.topology === "distributed" && (
        <Field label="Master port" help="torch.distributed rendezvous port on the head node (--master-port).">
          <input type="number" placeholder="auto" value={f.master_port ?? ""} onChange={(e) => set("master_port", e.target.value === "" ? undefined : Number(e.target.value))} />
        </Field>
      )}
      <label className="checkbox">
        <input type="checkbox" checked={f.enable_tool_choice} onChange={(e) => set("enable_tool_choice", e.target.checked)} />
        <span><span className="cb-label">Enable tool calling</span><div className="cb-sub">Adds --enable-auto-tool-choice with the right parser.</div></span>
      </label>

      <VllmAdvanced v={f} patch={patch} modelAlias={inst.model_name} />
      <TlsConfig v={f} patch={patch} editMode hasTlsCert={inst.has_tls_cert} />

      <label className="checkbox">
        <input type="checkbox" checked={f.autostart} onChange={(e) => set("autostart", e.target.checked)} />
        <span><span className="cb-label">Auto-start on boot</span><div className="cb-sub">Enable the systemd unit so it survives reboots.</div></span>
      </label>
    </Modal>
  );
}

// Serve settings are baked into the unit at start time, so editing only makes
// sense while the instance is not live.
const EDITABLE_STATUSES = ["stopped", "error"];

export default function Instances() {
  const instances = usePoll(() => api.listInstances(), 8000);
  const { toast } = useToast();
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<Instance | null>(null);
  const [logsFor, setLogsFor] = useState<string | null>(null);
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
    const model = splitAliases(i.served_model_names)[0] ?? `/models/${i.model_name}`;
    const text = `Base URL: ${i.node_role ? "http://<node-ip>" : ""}:${i.port}/v1\nModel: ${model}`;
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
          {(instances.data ?? []).map((i) => {
            const aliases = splitAliases(i.served_model_names);
            const advCount = parseArgs(i.advanced_args).length;
            return (
              <div key={i.id} className="card">
                <div className="card-head">
                  <div className="flex"><strong>{i.name}</strong><Badge kind={statusKind(i.status)}>{i.status}</Badge></div>
                  <Badge kind="blue" dot={false}>{topoLabel(i)}</Badge>
                </div>
                <dl className="kv">
                  <dt>Model</dt><dd>{i.model_name}</dd>
                  {aliases.length > 0 && (<><dt>Aliases</dt><dd className="mono">{aliases.join(", ")}</dd></>)}
                  <dt>Port</dt><dd className="mono">{i.port}{i.topology === "distributed" && i.master_port ? ` · master ${i.master_port}` : ""}</dd>
                  <dt>Tool parser</dt><dd>{i.enable_tool_choice ? (i.tool_parser ?? "auto") : "off"}</dd>
                  <dt>Context</dt><dd className="mono">{i.max_model_len ?? "default"} · gpu {i.gpu_memory_utilization}</dd>
                  {(i.kv_cache_dtype || i.block_size != null) && (
                    <><dt>KV cache</dt><dd className="mono">{i.kv_cache_dtype ?? "auto"}{i.block_size != null ? ` · block ${i.block_size}` : ""}</dd></>
                  )}
                  {i.reasoning_parser && (<><dt>Reasoning</dt><dd className="mono">{i.reasoning_parser}</dd></>)}
                  {(i.trust_remote_code || advCount > 0) && (
                    <><dt>Extra</dt><dd>{i.trust_remote_code ? "trust-remote-code" : ""}{i.trust_remote_code && advCount > 0 ? " · " : ""}{advCount > 0 ? `${advCount} adv arg${advCount === 1 ? "" : "s"}` : ""}</dd></>
                  )}
                  <dt>Boot</dt><dd>{i.autostart ? "auto-start" : "manual"}</dd>
                </dl>
                {i.last_error && <div className="banner banner-warn" style={{ marginTop: 10 }}>⚠ {i.last_error}</div>}
                <div className="btn-row mt">
                  <button className="btn btn-sm btn-primary" onClick={() => act(api.startInstance(i.id), `Start ${i.name}`)}>Start</button>
                  <button className="btn btn-sm" onClick={() => act(api.stopInstance(i.id), `Stop ${i.name}`)}>Stop</button>
                  {EDITABLE_STATUSES.includes(i.status) && (
                    <button className="btn btn-sm" onClick={() => setEditing(i)} title="Edit serve settings (applies on next start)">Edit</button>
                  )}
                  <button className="btn btn-sm" onClick={() => copyClient(i)}>Copy client cfg</button>
                  <button className="btn btn-sm" onClick={() => setLogsFor(i.name)} title="Live journalctl tail">Logs</button>
                  <button className="btn btn-sm btn-danger" onClick={() => del(i)}>Delete</button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {creating && <CreateForm onClose={() => setCreating(false)} onCreated={() => instances.reload()} />}
      {editing && <EditForm inst={editing} onClose={() => setEditing(null)} onSaved={() => instances.reload()} />}
      {logsFor && (
        <Modal title="Live logs" wide onClose={() => setLogsFor(null)}>
          <LiveLogPanel filter={logsFor} />
        </Modal>
      )}
      {job && (
        <Modal title={job.label} wide onClose={() => { setJob(null); instances.reload(); }}>
          <JobLogPanel jobId={job.id} title={job.label} onDone={() => instances.reload()} />
        </Modal>
      )}
    </div>
  );
}
