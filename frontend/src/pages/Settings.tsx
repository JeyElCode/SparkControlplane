import { useState } from "react";
import { api, ClusterConfig } from "../lib/api";
import { usePoll } from "../lib/hooks";
import { Field, Spinner } from "../components/ui";
import { useToast } from "../components/Toast";

export default function SettingsPage() {
  const config = usePoll(() => api.getConfig(), 0);
  const settings = usePoll(() => api.getSettings(), 0);
  const { toast } = useToast();
  const [draft, setDraft] = useState<Partial<ClusterConfig>>({});
  const [hfToken, setHfToken] = useState("");
  const [poll, setPoll] = useState<number | "">("");
  const [busy, setBusy] = useState(false);
  const [judgeUrl, setJudgeUrl] = useState<string | null>(null);
  const [judgeModel, setJudgeModel] = useState<string | null>(null);
  const [judgeKey, setJudgeKey] = useState("");

  const saveJudge = async () => {
    await api.updateSettings({
      ...(judgeUrl != null ? { judge_base_url: judgeUrl } : {}),
      ...(judgeModel != null ? { judge_model: judgeModel } : {}),
      ...(judgeKey ? { judge_api_key: judgeKey } : {}),
    });
    setJudgeUrl(null);
    setJudgeModel(null);
    setJudgeKey("");
    settings.reload();
    toast("External judge saved", "success");
  };

  const cfg = { ...config.data, ...draft } as ClusterConfig;
  const set = (k: keyof ClusterConfig, v: any) => setDraft((p) => ({ ...p, [k]: v }));

  const saveConfig = async () => {
    setBusy(true);
    try {
      await api.updateConfig(draft);
      setDraft({});
      config.reload();
      toast("Cluster config saved", "success");
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setBusy(false);
    }
  };

  const saveToken = async () => {
    await api.updateSettings({ hf_token: hfToken });
    setHfToken("");
    settings.reload();
    toast("HuggingFace token saved", "success");
  };

  const savePoll = async () => {
    if (poll === "") return;
    await api.updateSettings({ status_poll_seconds: Number(poll) });
    settings.reload();
    toast("Saved", "success");
  };

  if (!config.data) return <div className="card center" style={{ padding: 40 }}><Spinner /></div>;

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Settings</h1>
          <p>Cluster-wide configuration and secrets.</p>
        </div>
      </div>

      <div className="grid grid-2">
        <div className="card">
          <h2>Cluster config</h2>
          <Field label="Cluster name"><input value={cfg.cluster_name} onChange={(e) => set("cluster_name", e.target.value)} /></Field>
          <Field label="vLLM image"><input value={cfg.vllm_image} onChange={(e) => set("vllm_image", e.target.value)} /></Field>
          <div className="row-2">
            <Field label="QSFP netmask"><input type="number" value={cfg.qsfp_netmask} onChange={(e) => set("qsfp_netmask", Number(e.target.value))} /></Field>
            <Field
              label="Container shm size"
              help="Shared memory (/dev/shm) for the Ray & vLLM containers — Docker's --shm-size. Docker's 64MB default is too small for vLLM's tensor-parallel IPC and causes crashes. Default 10.24gb; accepts values like 16g or 2048m. It's a cap on a tmpfs, not a reservation, so keep it below a node's RAM."
            >
              <input value={cfg.shm_size} onChange={(e) => set("shm_size", e.target.value)} />
            </Field>
          </div>
          <div className="row-2">
            <Field label="Models subdir" hint="relative to the SSH user's home"><input value={cfg.models_subdir} onChange={(e) => set("models_subdir", e.target.value)} /></Field>
            <Field label="HF cache subdir"><input value={cfg.hf_cache_subdir} onChange={(e) => set("hf_cache_subdir", e.target.value)} /></Field>
          </div>
          <div className="kv mb">
            <dt>models in container</dt><dd className="mono">{cfg.models_container_path}</dd>
            <dt>hf cache in container</dt><dd className="mono">{cfg.hf_cache_container_path}</dd>
            <dt>ray port</dt><dd className="mono">{cfg.ray_port}</dd>
          </div>
          <button className="btn btn-primary" onClick={saveConfig} disabled={busy || Object.keys(draft).length === 0}>{busy ? <Spinner /> : "Save config"}</button>
        </div>

        <div className="flex-col" style={{ gap: 16 }}>
          <div className="card">
            <h2>HuggingFace token</h2>
            <Field label="Token" hint={settings.data?.has_hf_token ? "A token is stored. Enter a new one to replace it." : "Required for gated/private downloads. Stored encrypted."}>
              <div className="flex gap-sm">
                <input type="password" placeholder={settings.data?.has_hf_token ? "•••••• (stored)" : "hf_..."} value={hfToken} onChange={(e) => setHfToken(e.target.value)} />
                <button className="btn" onClick={saveToken} disabled={!hfToken}>Save</button>
              </div>
            </Field>
          </div>

          <div className="card">
            <h2>External judge (evals)</h2>
            <p className="faint" style={{ fontSize: 12, marginTop: -6 }}>
              Optional OpenAI-compatible endpoint used to grade eval answers when you pick the "external" judge.
            </p>
            <Field label="Base URL">
              <input
                placeholder={settings.data?.judge_base_url ?? "https://api.example.com/v1"}
                value={judgeUrl ?? settings.data?.judge_base_url ?? ""}
                onChange={(e) => setJudgeUrl(e.target.value)}
              />
            </Field>
            <Field label="Model">
              <input
                placeholder={settings.data?.judge_model ?? "model-id"}
                value={judgeModel ?? settings.data?.judge_model ?? ""}
                onChange={(e) => setJudgeModel(e.target.value)}
              />
            </Field>
            <Field label="API key" hint={settings.data?.has_judge_api_key ? "A key is stored. Enter a new one to replace it." : "Stored encrypted."}>
              <div className="flex gap-sm">
                <input type="password" placeholder={settings.data?.has_judge_api_key ? "•••••• (stored)" : "sk-..."} value={judgeKey} onChange={(e) => setJudgeKey(e.target.value)} />
                <button className="btn" onClick={saveJudge} disabled={judgeUrl == null && judgeModel == null && !judgeKey}>Save</button>
              </div>
            </Field>
          </div>

          <div className="card">
            <h2>Status polling</h2>
            <Field label="Dashboard poll interval (seconds)">
              <div className="flex gap-sm">
                <input type="number" placeholder={String(settings.data?.status_poll_seconds ?? 10)} value={poll} onChange={(e) => setPoll(e.target.value ? Number(e.target.value) : "")} />
                <button className="btn" onClick={savePoll} disabled={poll === ""}>Save</button>
              </div>
            </Field>
          </div>

          <div className="card">
            <h2>Security</h2>
            <p className="faint" style={{ fontSize: 13 }}>
              SSH passwords, private keys, sudo passwords and the HuggingFace token are encrypted at rest with a Fernet key
              (from <span className="tag">SPARK_SECRET_KEY</span> or generated to <span className="tag">/data/secret.key</span>).
              Back up that key — without it, stored secrets can't be decrypted. Portal login is not enabled in this build; run it
              only on a trusted network.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
