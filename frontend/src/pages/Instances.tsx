import { useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { api, InstanceInput, Instance, Plan, Topology } from "../lib/api";
import { usePoll } from "../lib/hooks";
import { clientSnippet, gatewayBaseUrl, gatewayModelName } from "../lib/gateway";
import { loadProgress, statusKind } from "../lib/format";
import { Badge, EmptyState, Field, Modal, Spinner, LoadError } from "../components/ui";
import { JobLogPanel } from "../components/JobLogPanel";
import { LiveLogPanel } from "../components/LiveLogPanel";
import { PlanDetails } from "../components/QuickLaunch";
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
  env_vars: null,
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
  | "env_vars"
  | "extra_args"
  | "vllm_image"
>;

function VllmAdvanced({
  v,
  patch,
  modelAlias,
  topology,
}: {
  v: AdvValues;
  patch: (p: Partial<AdvValues>) => void;
  modelAlias?: string;
  topology?: Topology;
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

        <EnvEditor
          value={v.env_vars}
          onChange={(env) => patch({ env_vars: env })}
          topology={topology}
        />

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

/** KEY=VALUE lines <-> the env map. A textarea rather than a row editor: this
 *  is something operators paste from a runbook, and the point of the feature is
 *  that setting one variable should not require building a custom image. */
function envToText(env?: Record<string, string> | null): string {
  if (!env) return "";
  return Object.entries(env).map(([k, v]) => `${k}=${v}`).join("\n");
}

function textToEnv(text: string): { env: Record<string, string> | null; error: string | null } {
  const env: Record<string, string> = {};
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq <= 0) return { env: null, error: `Not a KEY=VALUE line: "${line}"` };
    const key = line.slice(0, eq).trim();
    // Mirrors the server rule, so the error arrives while typing rather than as
    // a 422 after pressing Create.
    if (!/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(key)) {
      return { env: null, error: `Invalid variable name "${key}"` };
    }
    env[key] = line.slice(eq + 1).trim();
  }
  return { env: Object.keys(env).length ? env : null, error: null };
}

function EnvEditor({
  value,
  onChange,
  topology,
}: {
  value?: Record<string, string> | null;
  onChange: (env: Record<string, string> | null) => void;
  topology?: Topology;
}) {
  const [text, setText] = useState(() => envToText(value));
  const [error, setError] = useState<string | null>(null);
  const clusterBlocked = topology === "cluster";

  return (
    <Field
      label="Environment variables (optional)"
      hint={
        clusterBlocked
          ? "Not available on cluster topology — the instance runs inside the shared Ray container, so variables would reach only the driver, not the workers."
          : "One KEY=VALUE per line, passed to the container with docker -e."
      }
      help="For settings that live in the environment rather than in a vLLM flag — NCCL tuning, VLLM_* switches, HF_* endpoints. Multi-node RoCE (NCCL_IB_HCA / NCCL_IB_GID_INDEX) is detected and set automatically; anything set here takes precedence."
    >
      <textarea
        rows={4}
        disabled={clusterBlocked}
        value={text}
        placeholder={"NCCL_DEBUG=INFO"}
        onChange={(e) => {
          setText(e.target.value);
          const parsed = textToEnv(e.target.value);
          setError(parsed.error);
          if (!parsed.error) onChange(parsed.env);
        }}
      />
      {error && <div className="badge-note" style={{ color: "var(--red)" }}>{error}</div>}
    </Field>
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

function CreateForm({
  onClose,
  onCreated,
  initial,
  initialPlan,
}: {
  onClose: () => void;
  onCreated: () => void;
  /** Pre-filled values, e.g. handed over by Quick launch's "Customize". */
  initial?: Partial<InstanceInput>;
  /** The reasoning behind `initial`, shown so a pre-filled form is auditable
   *  rather than a set of numbers that appeared from nowhere. */
  initialPlan?: Plan;
}) {
  const models = usePoll(() => api.listModels(), 0);
  const nodes = usePoll(() => api.listNodes(), 0);
  const profiles = usePoll(() => api.listProfiles(), 0);
  const [appliedProfile, setAppliedProfile] = useState<string | null>(null);
  const { toast } = useToast();
  const [f, setF] = useState<InstanceInput>({ ...DEFAULTS, ...initial });
  const [busy, setBusy] = useState(false);
  const [plan, setPlan] = useState<Plan | undefined>(initialPlan);
  const [planning, setPlanning] = useState(false);
  const set = (k: keyof InstanceInput, v: any) => setF((p) => ({ ...p, [k]: v }));
  const patch = (p: Partial<InstanceInput>) => setF((prev) => ({ ...prev, ...p }));
  const selModel = (models.data ?? []).find((m) => m.id === f.model_id);

  // Derive settings for whatever model is selected, honouring a topology the
  // operator has already picked — the plan works around their decision rather
  // than overwriting it.
  const recommend = async () => {
    if (!f.model_id) {
      toast("Pick a model first — the recommendation is derived from it", "error");
      return;
    }
    setPlanning(true);
    try {
      const p = await api.planInstance({
        model_id: f.model_id,
        topology: f.topology,
        node_id: f.node_id,
      });
      setPlan(p);
      patch({ ...(p.settings as Partial<InstanceInput>), name: f.name || p.name });
      setAppliedProfile(null);
      toast("Settings derived from your cluster — review them below", "success");
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setPlanning(false);
    }
  };

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
        {(profiles.data ?? []).length > 0 && (
          <Field
            label="Start from a profile"
            hint="Known-good serve settings. Everything stays editable below — a profile is a starting point, not a lock."
          >
            <select
              value=""
              onChange={(e) => {
                const p = (profiles.data ?? []).find((x) => String(x.id) === e.target.value);
                if (!p) return;
                // Only the serve settings; name/model/node stay whatever the
                // operator has already chosen.
                patch(p.settings as Partial<InstanceInput>);
                setAppliedProfile(p.name);
                toast(`Applied "${p.name}"`, "success");
              }}
            >
              <option value="">— none —</option>
              {(profiles.data ?? []).map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}{p.builtin ? " (built-in)" : ""}{p.repo_id ? ` — ${p.repo_id}` : ""}
                </option>
              ))}
            </select>
          </Field>
        )}
        {appliedProfile && (
          <div className="banner" style={{ marginBottom: 12 }}>
            Settings from <strong>{appliedProfile}</strong> applied — review them below before creating.
          </div>
        )}
        <Field label="Name"><input value={f.name} placeholder="main" onChange={(e) => set("name", e.target.value)} /></Field>
        <Field label="Model">
          <select value={f.model_id} onChange={(e) => set("model_id", Number(e.target.value))}>
            <option value={0}>— select —</option>
            {(models.data ?? []).map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
          </select>
        </Field>
      </div>

      <div className="plan-cta">
        <button className="btn" onClick={recommend} disabled={planning || !f.model_id}>
          {planning ? <Spinner /> : "✨ Work it out for me"}
        </button>
        <span className="badge-note">
          Fills in topology, memory fraction and context length from this model's
          shape and what your nodes have free — then tells you why. Everything
          stays editable.
        </span>
      </div>

      {plan && (
        <details className="collapse" open>
          <summary>Why these settings</summary>
          <div className="collapse-body">
            <PlanDetails plan={plan} />
          </div>
        </details>
      )}

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

      <VllmAdvanced v={f} patch={patch} modelAlias={selModel?.name} topology={f.topology} />
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
  | "env_vars"
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
    env_vars: inst.env_vars ?? null,
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

      <VllmAdvanced v={f} patch={patch} modelAlias={inst.model_name} topology={inst.topology} />
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
  const gw = usePoll(() => api.gatewayRoutes(), 10000);
  const profiles = usePoll(() => api.listProfiles(), 0);
  const [saveProfileFor, setSaveProfileFor] = useState<Instance | null>(null);
  const [profileName, setProfileName] = useState("");
  const { toast } = useToast();
  // Quick launch's "Customize" navigates here carrying its derived plan, so
  // the form opens already filled in with the reasoning attached.
  const handoff = (useLocation().state ?? null) as { plan?: Plan; modelId?: number } | null;
  const [creating, setCreating] = useState(!!handoff?.plan);
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
    // Clients go through the portal's /v1 gateway, not the instance port — the
    // origin is whatever the browser is on, which also works behind the ingress.
    // (The old version emitted ":8001/v1" for cluster/distributed instances,
    // whose node_role is null, and never mentioned the gateway or the token.)
    navigator.clipboard?.writeText(clientSnippet(gatewayModelName(i), gw.data));
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

      <LoadError error={instances.error} what="instances" />

      {(gw.data?.routes.length ?? 0) > 0 && (
        <div className="card">
          <div className="card-head">
            <div>
              <h2>API gateway</h2>
              <p className="muted">
                One endpoint for every model. Clients send the model name below —
                they never need instance ports or node addresses.
              </p>
            </div>
            <button
              className="btn btn-sm"
              onClick={() => {
                navigator.clipboard?.writeText(gatewayBaseUrl());
                toast("Gateway URL copied", "success");
              }}
            >
              Copy base URL
            </button>
          </div>
          <div className="mono" style={{ marginBottom: 10 }}>{gatewayBaseUrl()}</div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr><th>Model name</th><th>Serves from</th><th>Node</th><th /></tr>
              </thead>
              <tbody>
                {gw.data!.routes.map((r) => (
                  <tr key={r.model_name}>
                    <td className="mono">
                      {r.model_name}
                      {r.confirmed_upstream === false && (
                        <Badge kind="amber">not served upstream</Badge>
                      )}
                    </td>
                    <td>{r.instance}</td>
                    <td>{r.node ?? "—"}</td>
                    <td style={{ textAlign: "right" }}>
                      <button
                        className="btn btn-sm"
                        onClick={() => {
                          navigator.clipboard?.writeText(clientSnippet(r.model_name, gw.data));
                          toast("Client config copied", "success");
                        }}
                      >
                        Copy client cfg
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {gw.data!.auth_required && !gw.data!.token_configured && (
            <div className="banner banner-warn mt">
              ⚠ Portal auth is on but no gateway token is set — external clients
              can't authenticate. Set one in <Link to="/settings">Settings → API gateway</Link>.
            </div>
          )}
          {(gw.data?.unavailable.length ?? 0) > 0 && (
            <p className="muted mt">
              Not servable right now:{" "}
              {gw.data!.unavailable.map((r) => `${r.model_name} (${r.status})`).join(", ")}
            </p>
          )}
        </div>
      )}

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
                  <div className="flex">
                    <strong>{i.name}</strong>
                    <Badge kind={statusKind(i.status)}>{i.status}</Badge>
                    {i.status === "starting" && (
                      <span className="badge-note">{loadProgress(i.started_at, i.last_load_seconds)}</span>
                    )}
                  </div>
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
                  <button className="btn btn-sm" title="Save these serve settings as a reusable profile"
                          onClick={() => setSaveProfileFor(i)}>Save as profile</button>
                  <button className="btn btn-sm" onClick={() => setLogsFor(i.name)} title="Live journalctl tail">Logs</button>
                  <button className="btn btn-sm btn-danger" onClick={() => del(i)}>Delete</button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {(profiles.data ?? []).length > 0 && (
        <div className="card">
          <div className="card-head">
            <div>
              <h2>Serve profiles</h2>
              <p className="muted">
                Known-good vLLM settings you can apply when creating an instance,
                so a model's flags stop being something you rediscover.
              </p>
            </div>
            <div className="btn-row">
              <button className="btn btn-sm" onClick={async () => {
                const doc = await api.exportProfiles();
                navigator.clipboard?.writeText(JSON.stringify(doc, null, 2));
                toast(`Copied ${doc.profiles.length} profile(s) as JSON`, "success");
              }}>Export</button>
              <button className="btn btn-sm" onClick={async () => {
                const raw = prompt("Paste a serve-profile JSON document:");
                if (!raw) return;
                try {
                  const res = await api.importProfiles(JSON.parse(raw));
                  const bits = [`${res.imported.length} imported`];
                  if (res.skipped.length) bits.push(`${res.skipped.length} already existed`);
                  if (res.dropped_fields.length) {
                    bits.push(`dropped ${res.dropped_fields.join(", ")} (an imported profile can't choose the container image or raw flags)`);
                  }
                  toast(bits.join(" · "), res.imported.length ? "success" : "error");
                  profiles.reload();
                } catch (e: any) { toast(`Import failed: ${e.message}`, "error"); }
              }}>Import</button>
            </div>
          </div>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Name</th><th>For</th><th>Settings</th><th /></tr></thead>
              <tbody>
                {(profiles.data ?? []).map((p) => (
                  <tr key={p.id}>
                    <td>
                      {p.name}{p.builtin && <Badge kind="blue">built-in</Badge>}
                      {p.description && <div className="badge-note">{p.description}</div>}
                    </td>
                    <td className="mono faint">{p.repo_id ?? "any model"}</td>
                    <td className="mono badge-note">
                      {Object.entries(p.settings).slice(0, 4).map(([k, v]) => `${k}=${v}`).join(" · ")}
                      {Object.keys(p.settings).length > 4 ? ` +${Object.keys(p.settings).length - 4}` : ""}
                    </td>
                    <td style={{ textAlign: "right" }}>
                      {!p.builtin && (
                        <button className="btn btn-sm btn-danger" onClick={async () => {
                          if (!confirm(`Delete profile "${p.name}"?`)) return;
                          await api.deleteProfile(p.id);
                          profiles.reload();
                        }}>Delete</button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {saveProfileFor && (
        <Modal title={`Save "${saveProfileFor.name}" as a profile`} onClose={() => { setSaveProfileFor(null); setProfileName(""); }}>
          <p className="faint">
            Captures the serve settings only — context length, memory fraction,
            batch limits, parsers. Not the name, port, node or API key.
          </p>
          <Field label="Profile name">
            <input autoFocus value={profileName} placeholder={`${saveProfileFor.name}-settings`}
                   onChange={(e) => setProfileName(e.target.value)} />
          </Field>
          <div className="btn-row">
            <button className="btn btn-primary" disabled={!profileName.trim()}
              onClick={async () => {
                try {
                  await api.profileFromInstance(saveProfileFor.id, {
                    name: profileName.trim(),
                    description: `Captured from instance "${saveProfileFor.name}"`,
                    repo_id: saveProfileFor.model_repo_id || null,
                  });
                  toast("Profile saved", "success");
                  setSaveProfileFor(null); setProfileName(""); profiles.reload();
                } catch (e: any) { toast(e.message, "error"); }
              }}>Save profile</button>
            <button className="btn btn-ghost" onClick={() => { setSaveProfileFor(null); setProfileName(""); }}>Cancel</button>
          </div>
        </Modal>
      )}

      {creating && (
        <CreateForm
          onClose={() => setCreating(false)}
          onCreated={() => instances.reload()}
          initial={
            handoff?.plan
              ? { ...(handoff.plan.settings as Partial<InstanceInput>), name: handoff.plan.name, model_id: handoff.modelId ?? 0 }
              : undefined
          }
          initialPlan={handoff?.plan}
        />
      )}
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
