import { useState } from "react";
import { api, ModelUsage } from "../lib/api";
import { usePoll } from "../lib/hooks";
import { timeAgo } from "../lib/format";
import { EmptyState, Spinner } from "../components/ui";
import { LineChart, PALETTE } from "../components/charts";

function fmtTokens(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`;
  return String(n);
}

const RANGES = [
  { days: 1, bucket: "hour" as const, label: "24h" },
  { days: 7, bucket: "day" as const, label: "7d" },
  { days: 30, bucket: "day" as const, label: "30d" },
  { days: 90, bucket: "day" as const, label: "90d" },
];

export default function Usage() {
  const [range, setRange] = useState(RANGES[2]);
  const { data, error, loading } = usePoll(
    () => api.getUsage(range.days, range.bucket), 60000
  );

  const models: ModelUsage[] = data ?? [];
  const top = models.slice(0, 5);
  const xFmt = (ts: number) => {
    const d = new Date(ts * 1000);
    return range.bucket === "hour"
      ? `${String(d.getHours()).padStart(2, "0")}:00`
      : `${d.getMonth() + 1}/${d.getDate()}`;
  };

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Usage</h1>
          <p>Tokens served per model over time (5-minute rollups, kept for months).</p>
        </div>
        <div className="btn-row">
          {RANGES.map((r) => (
            <button key={r.label} className={`btn btn-sm ${r.label === range.label ? "btn-primary" : ""}`}
                    onClick={() => setRange(r)}>{r.label}</button>
          ))}
        </div>
      </div>

      <GatewayTrafficCard />

      {error && <div className="banner banner-warn">⚠ {error}</div>}
      {loading && !data && <div className="card center" style={{ padding: 40 }}><Spinner /></div>}

      {data && models.length === 0 && (
        <EmptyState icon="≋" title="No usage recorded yet">
          Serve some requests — rollups land every ~5 minutes while an instance is active.
        </EmptyState>
      )}

      {models.length > 0 && (
        <>
          <div className="card mb">
            <h2>Totals — last {range.label}</h2>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr><th>Model</th><th>Generated tokens</th><th>Prompt tokens</th><th>Requests</th></tr>
                </thead>
                <tbody>
                  {models.map((m) => (
                    <tr key={m.model_name}>
                      <td className="mono">{m.model_name}</td>
                      <td className="mono">{fmtTokens(m.total_gen_tokens)}</td>
                      <td className="mono">{fmtTokens(m.total_prompt_tokens)}</td>
                      <td className="mono">{m.total_requests.toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div className="grid grid-2">
            <div className="card">
              <h2>Generated tokens per {range.bucket}</h2>
              <LineChart
                series={top.map((m, i) => ({
                  label: m.model_name, color: PALETTE[i % PALETTE.length],
                  points: m.points.map((p) => [p.ts, p.gen_tokens] as [number, number]),
                }))}
                fmtX={xFmt}
                fmtY={(n) => fmtTokens(n)}
                yLabel="tokens"
              />
            </div>
            <div className="card">
              <h2>Mean TTFT per {range.bucket}</h2>
              <LineChart
                series={top.map((m, i) => ({
                  label: m.model_name, color: PALETTE[i % PALETTE.length],
                  points: m.points
                    .filter((p) => p.ttft_ms_avg != null)
                    .map((p) => [p.ts, p.ttft_ms_avg as number] as [number, number]),
                }))}
                fmtX={xFmt}
                fmtY={(n) => `${Math.round(n)}`}
                yLabel="ms"
              />
            </div>
          </div>
        </>
      )}
    </div>
  );
}


/** Live gateway attribution: who is calling what, and how it's going.
 *
 * Distinct from the token rollups above, which come from vLLM's own counters
 * and therefore cannot see anything the gateway rejected — a client with a
 * stale token or a wrong model name was previously invisible everywhere. */
function GatewayTrafficCard() {
  const traffic = usePoll(() => api.gatewayTraffic(), 5000);
  const rows = traffic.data?.since_start ?? [];
  const recent = traffic.data?.recent ?? [];
  const [showRecent, setShowRecent] = useState(false);
  if (!rows.length && !recent.length) return null;

  const failing = recent.filter((r) => r.status >= 400).slice(0, 8);

  return (
    <div className="card">
      <div className="card-head">
        <div>
          <h2>Gateway traffic</h2>
          <p className="faint" style={{ marginTop: -6 }}>
            Per-client, since the portal started. Includes requests the gateway
            rejected — which vLLM's own counters never see.
          </p>
        </div>
        <button className="btn btn-sm" onClick={() => setShowRecent((v) => !v)}>
          {showRecent ? "Hide" : "Recent requests"}
        </button>
      </div>
      <div className="table-wrap">
        <table>
          <thead><tr>
            <th>Client</th><th>Model</th><th>Requests</th><th>Errors</th>
            <th>Avg</th><th>Avg TTFB</th><th>In flight</th>
          </tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.client}/${r.model}`}>
                <td>{r.client}</td>
                <td className="mono">{r.model}</td>
                <td>{r.requests}</td>
                <td className={r.errors ? "text-red" : undefined}>
                  {r.errors || "—"}{r.rejected ? ` (${r.rejected} rejected)` : ""}
                </td>
                <td>{r.avg_ms != null ? `${Math.round(r.avg_ms)} ms` : "—"}</td>
                <td>{r.avg_ttfb_ms != null ? `${Math.round(r.avg_ttfb_ms)} ms` : "—"}</td>
                <td>{traffic.data?.in_flight?.[r.client] ?? 0}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {failing.length > 0 && !showRecent && (
        <p className="faint mt">
          Recent failures: {failing.map((f) => `${f.status} ${f.model}`).join(", ")}
        </p>
      )}
      {showRecent && (
        <div className="table-wrap mt">
          <table>
            <thead><tr>
              <th>When</th><th>Client</th><th>Model</th><th>Status</th>
              <th>Duration</th><th>Detail</th>
            </tr></thead>
            <tbody>
              {recent.slice(0, 50).map((r, i) => (
                <tr key={i}>
                  <td className="faint">{timeAgo(new Date(r.ts * 1000).toISOString())}</td>
                  <td>{r.client}</td>
                  <td className="mono">{r.model}</td>
                  <td className={r.status >= 400 ? "text-red" : undefined}>{r.status}</td>
                  <td>{r.duration_ms} ms{r.streamed ? " (stream)" : ""}</td>
                  <td className="faint">{r.error ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
