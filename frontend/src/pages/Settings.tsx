import { useState } from "react";
import { AlertConfig, api, ClusterConfig, ImageTags } from "../lib/api";
import { usePoll } from "../lib/hooks";
import { Badge, Field, Modal, Spinner } from "../components/ui";
import { JobLogPanel } from "../components/JobLogPanel";
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
  const [tags, setTags] = useState<ImageTags | null>(null);
  const [checking, setChecking] = useState(false);
  const [selTag, setSelTag] = useState("");
  const [restartRay, setRestartRay] = useState(true);
  const [restartInstances, setRestartInstances] = useState(true);
  const [updateJob, setUpdateJob] = useState<number | null>(null);
  const [alertDraft, setAlertDraft] = useState<Partial<AlertConfig>>({});
  const [webhookUrl, setWebhookUrl] = useState("");
  const [alertBusy, setAlertBusy] = useState(false);

  const [bk, setBk] = useState<Record<string, any>>({});
  const [bkSecret, setBkSecret] = useState("");
  const [bkBusy, setBkBusy] = useState(false);
  const [restoreSummary, setRestoreSummary] = useState<import("../lib/api").RestoreSummary | null>(null);
  const s3list = usePoll(
    () => (settings.data?.has_backup_s3_secret ? api.listS3Backups().catch(() => []) : Promise.resolve([])),
    0
  );
  const bkv = (k: string) => bk[k] ?? (settings.data as any)?.[k];

  const saveBackup = async () => {
    setBkBusy(true);
    try {
      await api.updateSettings({ ...bk, ...(bkSecret ? { backup_s3_secret: bkSecret } : {}) });
      setBk({});
      setBkSecret("");
      settings.reload();
      s3list.reload();
      toast("Backup settings saved", "success");
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setBkBusy(false);
    }
  };

  const backupNow = async () => {
    setBkBusy(true);
    try {
      const r = await api.runBackup();
      toast(`Backup uploaded: ${r.key}`, "success");
      s3list.reload();
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setBkBusy(false);
    }
  };

  const restoreFile = async (file: File) => {
    if (!confirm("Restore this bundle? ALL current config (nodes, instances, schedules, settings) will be replaced.")) return;
    try {
      const bundle = JSON.parse(await file.text());
      setRestoreSummary(await api.importBackup(bundle));
      config.reload();
      settings.reload();
      toast("Backup restored", "success");
    } catch (e: any) {
      toast(e.message, "error");
    }
  };

  const restoreS3 = async (key: string) => {
    if (!confirm(`Restore ${key}? ALL current config will be replaced.`)) return;
    try {
      setRestoreSummary(await api.restoreS3Backup(key));
      config.reload();
      settings.reload();
      toast("Backup restored", "success");
    } catch (e: any) {
      toast(e.message, "error");
    }
  };

  const alertCfg = { ...(settings.data?.alerts ?? {}), ...alertDraft } as AlertConfig;
  const setAlert = (k: keyof AlertConfig, v: any) => setAlertDraft((p) => ({ ...p, [k]: v }));

  const saveAlerts = async () => {
    setAlertBusy(true);
    try {
      await api.updateSettings({
        ...(Object.keys(alertDraft).length ? { alerts: alertDraft } : {}),
        ...(webhookUrl ? { alert_webhook_url: webhookUrl } : {}),
      });
      setAlertDraft({});
      setWebhookUrl("");
      settings.reload();
      toast("Alert settings saved", "success");
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setAlertBusy(false);
    }
  };

  const clearWebhook = async () => {
    await api.updateSettings({ alert_webhook_url: "" });
    settings.reload();
    toast("Webhook removed", "success");
  };

  const testWebhook = async () => {
    try {
      const r = await api.testAlertWebhook();
      toast(r.message, "success");
    } catch (e: any) {
      toast(e.message, "error");
    }
  };

  const checkTags = async () => {
    setChecking(true);
    try {
      const t = await api.getImageTags();
      setTags(t);
      const newer = t.tags.find((x) => x !== t.current_tag);
      setSelTag(newer ?? t.tags[0] ?? "");
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setChecking(false);
    }
  };

  const runImageUpdate = async () => {
    if (!tags || !selTag) return;
    const image = `${tags.repository.replace(/^registry-1\.docker\.io\//, "")}:${selTag}`;
    const parts = [
      `Pull ${image} on every node`,
      restartRay ? "restart the Ray cluster" : null,
      restartInstances ? "rolling-restart running instances (brief downtime each)" : null,
    ].filter(Boolean);
    if (!confirm(`Update the cluster image?\n\nThis will: ${parts.join(", ")}.`)) return;
    try {
      const r = await api.updateImage({ image, restart_ray: restartRay, restart_instances: restartInstances });
      setUpdateJob(r.job_id);
    } catch (e: any) {
      toast(e.message, "error");
    }
  };

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
      {updateJob && (
        <Modal title="Cluster image update" onClose={() => { setUpdateJob(null); config.reload(); setTags(null); }}>
          <JobLogPanel jobId={updateJob} title="Pull + restart" onDone={() => config.reload()} />
        </Modal>
      )}
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
          <Field label="vLLM image">
            <div className="flex gap-sm">
              <input value={cfg.vllm_image} onChange={(e) => set("vllm_image", e.target.value)} />
              <button className="btn" onClick={checkTags} disabled={checking}>{checking ? <Spinner /> : "Check updates"}</button>
            </div>
          </Field>
          {tags && (
            <div className="banner banner-info" style={{ marginBottom: 14 }}>
              <div className="flex-col" style={{ gap: 8 }}>
                <div>
                  <strong>{tags.repository}</strong> — current: <span className="mono">{tags.current_tag ?? "?"}</span>
                  {tags.tags[0] && tags.tags[0] !== tags.current_tag
                    ? <> · newest: <span className="mono">{tags.tags[0]}</span></>
                    : <> · up to date</>}
                </div>
                <div className="flex wrap gap-sm" style={{ alignItems: "center" }}>
                  <select value={selTag} onChange={(e) => setSelTag(e.target.value)} style={{ width: "auto" }}>
                    {tags.tags.map((t) => (
                      <option key={t} value={t}>{t}{t === tags.current_tag ? " (current)" : ""}</option>
                    ))}
                  </select>
                  <label className="flex gap-sm" style={{ alignItems: "center", fontSize: 12 }}>
                    <input type="checkbox" checked={restartRay} onChange={(e) => setRestartRay(e.target.checked)} /> restart Ray
                  </label>
                  <label className="flex gap-sm" style={{ alignItems: "center", fontSize: 12 }}>
                    <input type="checkbox" checked={restartInstances} onChange={(e) => setRestartInstances(e.target.checked)} /> restart instances
                  </label>
                  <button className="btn btn-primary btn-sm" onClick={runImageUpdate} disabled={!selTag || selTag === tags.current_tag}>
                    Update cluster
                  </button>
                </div>
              </div>
            </div>
          )}
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
            <h2>Backups</h2>
            <p className="faint" style={{ marginTop: -6 }}>
              Config snapshot (nodes, instances, schedules, models, settings — secrets stay encrypted;
              restoring needs the same SPARK_SECRET_KEY). Point it at any S3-compatible store
              (MinIO, R2, AWS) for scheduled off-box copies, or just download a file.
            </p>
            {restoreSummary && (
              <div className={`banner ${restoreSummary.cleared_secrets.length ? "banner-warn" : "banner-info"}`} style={{ marginBottom: 12 }}>
                Restored {Object.entries(restoreSummary.restored).map(([t, n]) => `${n} ${t}`).join(", ")}
                {restoreSummary.cleared_secrets.length > 0 && (
                  <> — ⚠ different secret key: re-enter {restoreSummary.cleared_secrets.join(", ")}</>
                )}
              </div>
            )}
            <div className="btn-row mb">
              <a className="btn" href="/api/backup/export">Download export</a>
              <label className="btn" style={{ cursor: "pointer" }}>
                Restore from file
                <input type="file" accept=".json" style={{ display: "none" }}
                       onChange={(e) => e.target.files?.[0] && restoreFile(e.target.files[0])} />
              </label>
            </div>
            <div className="row-2">
              <Field label="S3 endpoint" hint="e.g. https://minio.lab:9000">
                <input value={bkv("backup_s3_endpoint") ?? ""} onChange={(e) => setBk((p) => ({ ...p, backup_s3_endpoint: e.target.value }))} />
              </Field>
              <Field label="Bucket">
                <input value={bkv("backup_s3_bucket") ?? ""} onChange={(e) => setBk((p) => ({ ...p, backup_s3_bucket: e.target.value }))} />
              </Field>
            </div>
            <div className="row-2">
              <Field label="Access key">
                <input value={bkv("backup_s3_access_key") ?? ""} onChange={(e) => setBk((p) => ({ ...p, backup_s3_access_key: e.target.value }))} />
              </Field>
              <Field label="Secret key" hint={settings.data?.has_backup_s3_secret ? "Stored. Enter a new one to replace." : "stored encrypted"}>
                <input type="password" placeholder={settings.data?.has_backup_s3_secret ? "•••••• (stored)" : ""} value={bkSecret} onChange={(e) => setBkSecret(e.target.value)} />
              </Field>
            </div>
            <div className="row-2">
              <Field label="Interval (hours)">
                <input type="number" value={bkv("backup_interval_hours") ?? 24} onChange={(e) => setBk((p) => ({ ...p, backup_interval_hours: Number(e.target.value) }))} />
              </Field>
              <Field label="Keep newest N">
                <input type="number" value={bkv("backup_retention") ?? 14} onChange={(e) => setBk((p) => ({ ...p, backup_retention: Number(e.target.value) }))} />
              </Field>
            </div>
            <div className="btn-row">
              <label className="flex gap-sm" style={{ alignItems: "center", fontSize: 13 }}>
                <input type="checkbox" checked={!!bkv("backup_enabled")} onChange={(e) => setBk((p) => ({ ...p, backup_enabled: e.target.checked }))} />
                scheduled backups
              </label>
              <button className="btn btn-primary" onClick={saveBackup} disabled={bkBusy || (Object.keys(bk).length === 0 && !bkSecret)}>
                {bkBusy ? <Spinner /> : "Save backup config"}
              </button>
              {settings.data?.has_backup_s3_secret && (
                <button className="btn" onClick={backupNow} disabled={bkBusy}>Back up now</button>
              )}
            </div>
            {(s3list.data ?? []).length > 0 && (
              <div className="table-wrap" style={{ marginTop: 12 }}>
                <table>
                  <thead><tr><th>Backup</th><th>Size</th><th /></tr></thead>
                  <tbody>
                    {(s3list.data ?? []).slice(0, 8).map((o) => (
                      <tr key={o.key}>
                        <td className="mono" style={{ fontSize: 12 }}>{o.key}</td>
                        <td className="mono faint">{(o.size / 1024).toFixed(1)} kB</td>
                        <td><button className="btn btn-sm" onClick={() => restoreS3(o.key)}>Restore</button></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div className="card">
            <h2>Alerts</h2>
            <p className="faint" style={{ marginTop: -6 }}>
              Banners always show on the Dashboard; add a webhook to get notified when the tab is closed.
              Alerts fire only after a condition holds for its duration, and send a recovery notice.
            </p>
            <div className="row-2">
              <Field label="GPU temp threshold (°C)">
                <input type="number" value={alertCfg.gpu_temp_c ?? 85} onChange={(e) => setAlert("gpu_temp_c", Number(e.target.value))} />
              </Field>
              <Field label="KV cache threshold (%)" hint="sustained = model overloaded">
                <input type="number" value={alertCfg.kv_cache_pct ?? 95} onChange={(e) => setAlert("kv_cache_pct", Number(e.target.value))} />
              </Field>
            </div>
            <div className="row-2">
              <Field label="Disk free threshold (%)">
                <input type="number" value={alertCfg.disk_free_pct ?? 10} onChange={(e) => setAlert("disk_free_pct", Number(e.target.value))} />
              </Field>
              <Field label="Node offline after (s)">
                <input type="number" value={alertCfg.node_offline_seconds ?? 60} onChange={(e) => setAlert("node_offline_seconds", Number(e.target.value))} />
              </Field>
            </div>
            <div className="row-2">
              <Field label="Webhook type">
                <select value={alertCfg.webhook_kind ?? "generic"} onChange={(e) => setAlert("webhook_kind", e.target.value)}>
                  <option value="generic">Generic JSON POST</option>
                  <option value="ntfy">ntfy</option>
                  <option value="discord">Discord</option>
                  <option value="slack">Slack</option>
                </select>
              </Field>
              <Field label="Webhook URL" hint={settings.data?.has_alert_webhook ? "A webhook is stored. Enter a new URL to replace it." : "e.g. https://ntfy.sh/your-topic — stored encrypted"}>
                <input type="password" placeholder={settings.data?.has_alert_webhook ? "•••••• (stored)" : "https://…"} value={webhookUrl} onChange={(e) => setWebhookUrl(e.target.value)} />
              </Field>
            </div>
            <div className="btn-row">
              <button className="btn btn-primary" onClick={saveAlerts} disabled={alertBusy || (Object.keys(alertDraft).length === 0 && !webhookUrl)}>
                {alertBusy ? <Spinner /> : "Save alerts"}
              </button>
              {settings.data?.has_alert_webhook && (
                <>
                  <button className="btn" onClick={testWebhook}>Send test</button>
                  <button className="btn btn-danger" onClick={clearWebhook}>Remove webhook</button>
                </>
              )}
            </div>
          </div>

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

          {settings.data?.mcp_enabled !== undefined && (
            <div className="card">
              <h2>MCP server</h2>
              <p className="faint" style={{ fontSize: 12, marginTop: -6 }}>
                Model Context Protocol endpoint for driving this control plane from an MCP client (e.g. a Claude skill).
              </p>
              <div className="kv">
                <dt>Status</dt>
                <dd><Badge kind={settings.data.mcp_enabled ? "green" : "gray"}>{settings.data.mcp_enabled ? "enabled" : "disabled"}</Badge></dd>
                {settings.data.mcp_path && (<><dt>Endpoint</dt><dd className="mono">{settings.data.mcp_path}</dd></>)}
                <dt>Bearer token</dt>
                <dd className="mono">
                  {settings.data.mcp_token ? (
                    <div className="flex gap-sm">
                      <span>{settings.data.mcp_token}</span>
                      <button className="btn btn-sm" onClick={() => { navigator.clipboard?.writeText(settings.data!.mcp_token!); toast("MCP token copied", "success"); }}>Copy</button>
                    </div>
                  ) : settings.data.has_mcp_token ? (
                    "•••••• (set)"
                  ) : (
                    <span className="faint">not set</span>
                  )}
                </dd>
              </div>
            </div>
          )}

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
