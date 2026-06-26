import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { usePoll } from "../lib/hooks";
import { Badge, Field, Spinner } from "../components/ui";
import { statusKind } from "../lib/format";

export default function Playground() {
  const instances = usePoll(() => api.listInstances(), 0);
  const [instanceId, setInstanceId] = useState<number>(0);
  const [system, setSystem] = useState("");
  const [prompt, setPrompt] = useState("Write one short sentence confirming the cluster is working.");
  const [maxTokens, setMaxTokens] = useState(128);
  const [temperature, setTemperature] = useState(0.7);
  const [busy, setBusy] = useState(false);
  const [out, setOut] = useState<{ ok: boolean; content?: string; error?: string } | null>(null);

  useEffect(() => {
    if (!instanceId && instances.data && instances.data.length > 0) {
      const running = instances.data.find((i) => i.status === "running");
      setInstanceId((running ?? instances.data[0]).id);
    }
  }, [instances.data, instanceId]);

  const send = async () => {
    if (!instanceId) return;
    setBusy(true);
    setOut(null);
    try {
      const r = await api.playground({ instance_id: instanceId, prompt, system: system || undefined, max_tokens: maxTokens, temperature });
      setOut(r);
    } catch (e: any) {
      setOut({ ok: false, error: e.message });
    } finally {
      setBusy(false);
    }
  };

  const list = instances.data ?? [];

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Playground</h1>
          <p>Send a prompt to a running instance to smoke-test it.</p>
        </div>
      </div>

      {list.length === 0 ? (
        <div className="card faint">No instances yet. Create one on <Link to="/instances">Instances</Link>.</div>
      ) : (
        <div className="grid grid-2">
          <div className="card">
            <Field label="Instance">
              <select value={instanceId} onChange={(e) => setInstanceId(Number(e.target.value))}>
                {list.map((i) => <option key={i.id} value={i.id}>{i.name} — {i.status}</option>)}
              </select>
            </Field>
            <Field label="System prompt (optional)"><textarea value={system} onChange={(e) => setSystem(e.target.value)} style={{ minHeight: 50 }} /></Field>
            <Field label="Prompt"><textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} style={{ minHeight: 90 }} /></Field>
            <div className="row-2">
              <Field label="Max tokens"><input type="number" value={maxTokens} onChange={(e) => setMaxTokens(Number(e.target.value))} /></Field>
              <Field label="Temperature"><input type="number" step="0.1" value={temperature} onChange={(e) => setTemperature(Number(e.target.value))} /></Field>
            </div>
            <button className="btn btn-primary" onClick={send} disabled={busy || !instanceId}>{busy ? <Spinner /> : "Send"}</button>
          </div>

          <div className="card">
            <div className="card-head">
              <h2 style={{ margin: 0 }}>Response</h2>
              {out && <Badge kind={out.ok ? "green" : "red"}>{out.ok ? "ok" : "error"}</Badge>}
            </div>
            {busy && <div className="center" style={{ padding: 30 }}><Spinner /></div>}
            {out?.ok && <div className="logs" style={{ maxHeight: 420 }}>{out.content || "(empty response)"}</div>}
            {out && !out.ok && <div className="banner banner-warn">⚠ {out.error}</div>}
            {!out && !busy && <div className="faint center" style={{ padding: 30 }}>The model reply will appear here.</div>}
          </div>
        </div>
      )}
    </div>
  );
}
