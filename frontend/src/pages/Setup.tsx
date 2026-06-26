import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { usePoll } from "../lib/hooks";
import { Field, Spinner } from "../components/ui";
import { JobLogPanel } from "../components/JobLogPanel";
import { useToast } from "../components/Toast";

export default function Setup() {
  const phases = usePoll(() => api.listPhases(), 0);
  const nodes = usePoll(() => api.listNodes(), 0);
  const settings = usePoll(() => api.getSettings(), 0);
  const config = usePoll(() => api.getConfig(), 0);
  const { toast } = useToast();
  const [job, setJob] = useState<{ id: number; label: string } | null>(null);
  const [hfToken, setHfToken] = useState("");
  const [image, setImage] = useState<string | null>(null);

  const ready = (nodes.data ?? []).some((n) => n.role === "head") && (nodes.data ?? []).some((n) => n.role === "worker");

  const run = async (selected?: string[], label = "Full setup") => {
    try {
      const r = await api.runSetup(selected);
      setJob({ id: r.job_id, label });
    } catch (e: any) {
      toast(e.message, "error");
    }
  };

  const saveToken = async () => {
    await api.updateSettings({ hf_token: hfToken });
    setHfToken("");
    settings.reload();
    toast("HuggingFace token saved", "success");
  };

  const saveImage = async () => {
    if (image == null) return;
    await api.updateConfig({ vllm_image: image });
    config.reload();
    toast("vLLM image updated", "success");
  };

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Setup</h1>
          <p>Provision the cluster from bare metal. Each phase is idempotent and safe to re-run.</p>
        </div>
        <button className="btn btn-primary" disabled={!ready} onClick={() => run(undefined, "Full setup")}>▶ Run full setup</button>
      </div>

      {!ready && (
        <div className="banner banner-warn">⚠ Configure both the head and worker nodes first on <Link to="/nodes">Nodes</Link>.</div>
      )}

      <div className="grid grid-2">
        <div className="card">
          <h2>Phases</h2>
          <p className="faint" style={{ marginTop: -6 }}>Run the whole pipeline or a single phase.</p>
          <div className="table-wrap">
            <table>
              <tbody>
                {(phases.data ?? []).map((p, i) => (
                  <tr key={p.phase}>
                    <td style={{ width: 28 }} className="faint mono">{i + 1}</td>
                    <td><strong>{p.phase}</strong><div className="faint" style={{ fontSize: 12 }}>{p.title}</div></td>
                    <td style={{ width: 70, textAlign: "right" }}>
                      <button className="btn btn-sm" disabled={!ready} onClick={() => run([p.phase], `Phase: ${p.phase}`)}>Run</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="flex-col" style={{ gap: 16 }}>
          <div className="card">
            <h2>Essentials</h2>
            <Field label="vLLM container image">
              <div className="flex gap-sm">
                <input value={image ?? config.data?.vllm_image ?? ""} onChange={(e) => setImage(e.target.value)} />
                <button className="btn btn-sm" onClick={saveImage} disabled={image == null}>Save</button>
              </div>
            </Field>
            <Field label="HuggingFace token" hint={settings.data?.has_hf_token ? "A token is stored. Enter a new one to replace it." : "Needed for gated/private model downloads."}>
              <div className="flex gap-sm">
                <input type="password" placeholder={settings.data?.has_hf_token ? "•••••• (stored)" : "hf_..."} value={hfToken} onChange={(e) => setHfToken(e.target.value)} />
                <button className="btn btn-sm" onClick={saveToken} disabled={!hfToken}>Save</button>
              </div>
            </Field>
            <div className="faint" style={{ fontSize: 12 }}>
              More options on <Link to="/settings">Settings</Link>.
            </div>
          </div>

          {job ? (
            <div className="card">
              <JobLogPanel jobId={job.id} title={job.label} onDone={(s) => { if (s === "success") { settings.reload(); toast("Setup phase finished", "success"); } }} />
            </div>
          ) : (
            <div className="card faint center" style={{ padding: 30 }}>
              {ready ? "Run a phase to see live logs here." : "Configure nodes to enable setup."}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
