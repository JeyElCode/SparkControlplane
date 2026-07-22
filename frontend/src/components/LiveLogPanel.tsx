import { useEffect, useRef, useState } from "react";
import { api, LogUnit } from "../lib/api";
import { Badge, Spinner } from "./ui";

const MAX_LINES = 2000;

/** Live journalctl tail over the logs WebSocket, with a unit picker.
 * `filter` preselects the first unit whose label contains it. */
export function LiveLogPanel({ filter }: { filter?: string }) {
  const [units, setUnits] = useState<LogUnit[] | null>(null);
  const [sel, setSel] = useState<string>("");
  const [lines, setLines] = useState<string[]>([]);
  const [connected, setConnected] = useState(false);
  const [follow, setFollow] = useState(true);
  const preRef = useRef<HTMLPreElement>(null);

  useEffect(() => {
    api.listLogUnits().then((u) => {
      setUnits(u);
      const first =
        (filter && u.find((x) => x.label.toLowerCase().includes(filter.toLowerCase()))) || u[0];
      if (first) setSel(`${first.node_id}|${first.unit}`);
    }).catch(() => setUnits([]));
  }, [filter]);

  useEffect(() => {
    if (!sel) return;
    const [nodeId, unit] = sel.split("|");
    setLines([]);
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(
      `${proto}://${window.location.host}/api/logs/ws?node_id=${nodeId}&unit=${encodeURIComponent(unit)}`
    );
    ws.onopen = () => setConnected(true);
    ws.onclose = () => setConnected(false);
    ws.onmessage = (ev) =>
      setLines((prev) => {
        const next = prev.length >= MAX_LINES ? prev.slice(-MAX_LINES + 1) : prev.slice();
        next.push(ev.data);
        return next;
      });
    return () => ws.close();
  }, [sel]);

  useEffect(() => {
    if (follow && preRef.current) preRef.current.scrollTop = preRef.current.scrollHeight;
  }, [lines, follow]);

  if (units === null) return <div className="center" style={{ padding: 30 }}><Spinner /></div>;
  if (units.length === 0) return <div className="faint">No tailable units (configure nodes first).</div>;

  return (
    <div className="flex-col" style={{ gap: 10 }}>
      <div className="flex wrap gap-sm" style={{ alignItems: "center" }}>
        <select value={sel} onChange={(e) => setSel(e.target.value)} style={{ flex: 1, minWidth: 200 }}>
          {units.map((u) => (
            <option key={`${u.node_id}|${u.unit}`} value={`${u.node_id}|${u.unit}`}>{u.label}</option>
          ))}
        </select>
        <Badge kind={connected ? "green" : "amber"}>{connected ? "streaming" : "disconnected"}</Badge>
        <label className="flex gap-sm faint" style={{ alignItems: "center", fontSize: 12 }}>
          <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} /> follow
        </label>
      </div>
      <pre
        ref={preRef}
        className="mono"
        style={{
          margin: 0, padding: 12, background: "var(--bg)", border: "1px solid var(--border)",
          borderRadius: "var(--radius-sm)", height: 420, overflow: "auto", fontSize: 11.5,
          whiteSpace: "pre-wrap", wordBreak: "break-all",
        }}
      >
        {lines.length === 0 ? "waiting for output…" : lines.join("\n")}
      </pre>
    </div>
  );
}
