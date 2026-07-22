// Small, dependency-free charts themed to the app's CSS variables.

export const PALETTE = ["#4f9cff", "#a78bfa", "#34d399", "#fbbf24", "#f87171", "#38bdf8", "#fb923c"];

export interface BarDatum {
  label: string;
  value: number;
  valueLabel?: string;
  color?: string;
}

/** Horizontal labeled bars (values normalized to `max`, default = data max). */
export function BarList({ data, max, unit }: { data: BarDatum[]; max?: number; unit?: string }) {
  const top = max ?? Math.max(1, ...data.map((d) => d.value));
  return (
    <div className="flex-col" style={{ gap: 8 }}>
      {data.map((d, i) => (
        <div key={i} className="bar-row">
          <span className="bar-label" title={d.label}>{d.label}</span>
          <div className="bar-track">
            <span
              className="bar-fill"
              style={{ width: `${Math.max(0, Math.min(100, (d.value / top) * 100))}%`, background: d.color ?? PALETTE[i % PALETTE.length] }}
            />
          </div>
          <span className="bar-value mono">{d.valueLabel ?? (Math.round(d.value * 10) / 10).toString()}{unit ? ` ${unit}` : ""}</span>
        </div>
      ))}
      {data.length === 0 && <span className="faint">No data.</span>}
    </div>
  );
}

export interface Group {
  label: string;
  items: { key: string; value: number; color?: string }[];
}

/** Grouped horizontal bars — one group per category, a bar per series (run). */
export function GroupedBarList({ groups, max, unit }: { groups: Group[]; max?: number; unit?: string }) {
  const top = max ?? Math.max(1, ...groups.flatMap((g) => g.items.map((i) => i.value)));
  const keys = Array.from(new Set(groups.flatMap((g) => g.items.map((i) => i.key))));
  return (
    <div>
      <div className="flex wrap gap-sm mb" style={{ fontSize: 11 }}>
        {keys.map((k, i) => (
          <span key={k} className="flex gap-sm" style={{ alignItems: "center" }}>
            <span style={{ width: 10, height: 10, borderRadius: 2, background: PALETTE[i % PALETTE.length], display: "inline-block" }} />
            <span className="faint">{k}</span>
          </span>
        ))}
      </div>
      <div className="flex-col" style={{ gap: 12 }}>
        {groups.map((g) => (
          <div key={g.label}>
            <div className="faint" style={{ fontSize: 12, marginBottom: 4 }}>{g.label}</div>
            <div className="flex-col" style={{ gap: 3 }}>
              {g.items.map((it) => (
                <div key={it.key} className="bar-row">
                  <div className="bar-track">
                    <span className="bar-fill" style={{ width: `${Math.min(100, (it.value / top) * 100)}%`, background: it.color ?? PALETTE[keys.indexOf(it.key) % PALETTE.length] }} />
                  </div>
                  <span className="bar-value mono">{Math.round(it.value * 10) / 10}{unit ? ` ${unit}` : ""}</span>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export interface SparkSeries {
  color?: string;
  points: [number, number][]; // [ts, value]
}

/** Compact axis-less area/line chart for in-card trends. The first series gets
 * a soft area fill; extra series render as plain lines. */
export function Sparkline({
  series,
  height = 34,
  max,
}: {
  series: SparkSeries[];
  height?: number;
  max?: number;
}) {
  const W = 200;
  const H = 40; // internal viewBox height; rendered height set via style
  const all = series.flatMap((s) => s.points);
  if (all.length < 2) {
    return <div style={{ height }} className="faint center" />;
  }
  const xMin = Math.min(...all.map((p) => p[0]));
  const xMax = Math.max(...all.map((p) => p[0]));
  const yTop = Math.max(max ?? 0, ...all.map((p) => p[1]), 1e-9) * (max ? 1 : 1.1);
  const sx = (x: number) => ((x - xMin) / Math.max(1e-9, xMax - xMin)) * W;
  const sy = (y: number) => H - 2 - (Math.min(y, yTop) / yTop) * (H - 4);
  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="none"
      style={{ width: "100%", height, display: "block" }}
      role="img"
    >
      {series.map((s, i) => {
        const color = s.color ?? PALETTE[i % PALETTE.length];
        const pts = [...s.points].sort((a, b) => a[0] - b[0]);
        const line = pts
          .map((p, j) => `${j === 0 ? "M" : "L"} ${sx(p[0]).toFixed(1)} ${sy(p[1]).toFixed(1)}`)
          .join(" ");
        const area =
          `${line} L ${sx(pts[pts.length - 1][0]).toFixed(1)} ${H} L ${sx(pts[0][0]).toFixed(1)} ${H} Z`;
        return (
          <g key={i}>
            {i === 0 && <path d={area} fill={color} fillOpacity={0.12} stroke="none" />}
            <path
              d={line}
              fill="none"
              stroke={color}
              strokeWidth={1.5}
              vectorEffect="non-scaling-stroke"
            />
          </g>
        );
      })}
    </svg>
  );
}

export interface LineSeries {
  label: string;
  color?: string;
  points: [number, number][]; // [x, y]
}

/** SVG line chart with numeric x/y, gridlines, and a legend. */
export function LineChart({
  series,
  height = 220,
  xLabel,
  yLabel,
  fmtY = (n: number) => String(Math.round(n)),
  fmtX = (n: number) => String(n),
}: {
  series: LineSeries[];
  height?: number;
  xLabel?: string;
  yLabel?: string;
  fmtY?: (n: number) => string;
  fmtX?: (n: number) => string;
}) {
  const W = 640;
  const H = height;
  const padL = 52;
  const padR = 16;
  const padB = 34;
  const padT = 12;
  const xs = series.flatMap((s) => s.points.map((p) => p[0]));
  const ys = series.flatMap((s) => s.points.map((p) => p[1]));
  if (xs.length === 0) return <div className="faint">No data.</div>;
  const xMin = Math.min(...xs);
  const xMax = Math.max(...xs);
  const yMax = Math.max(1, ...ys) * 1.1;
  const yMin = 0;
  const sx = (x: number) => padL + ((x - xMin) / Math.max(1e-9, xMax - xMin)) * (W - padL - padR);
  const sy = (y: number) => H - padB - ((y - yMin) / Math.max(1e-9, yMax - yMin)) * (H - padB - padT);
  const xTicks = Array.from(new Set(xs)).sort((a, b) => a - b);
  const yTicks = [0, 0.25, 0.5, 0.75, 1].map((f) => yMin + f * (yMax - yMin));

  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "auto" }} role="img">
        {yTicks.map((t, i) => (
          <g key={i}>
            <line x1={padL} y1={sy(t)} x2={W - padR} y2={sy(t)} stroke="var(--border)" strokeWidth={1} />
            <text x={padL - 6} y={sy(t) + 3} textAnchor="end" fontSize={10} fill="var(--text-faint)">{fmtY(t)}</text>
          </g>
        ))}
        {xTicks.map((t, i) => (
          <text key={i} x={sx(t)} y={H - padB + 14} textAnchor="middle" fontSize={10} fill="var(--text-faint)">{fmtX(t)}</text>
        ))}
        <line x1={padL} y1={H - padB} x2={W - padR} y2={H - padB} stroke="var(--border-light)" strokeWidth={1} />
        {series.map((s, i) => {
          const color = s.color ?? PALETTE[i % PALETTE.length];
          const pts = [...s.points].sort((a, b) => a[0] - b[0]);
          const d = pts.map((p, j) => `${j === 0 ? "M" : "L"} ${sx(p[0]).toFixed(1)} ${sy(p[1]).toFixed(1)}`).join(" ");
          return (
            <g key={i}>
              <path d={d} fill="none" stroke={color} strokeWidth={2} />
              {pts.map((p, j) => <circle key={j} cx={sx(p[0])} cy={sy(p[1])} r={3} fill={color} />)}
            </g>
          );
        })}
        {yLabel && <text x={12} y={padT + 4} fontSize={10} fill="var(--text-faint)">{yLabel}</text>}
        {xLabel && <text x={W - padR} y={H - 4} textAnchor="end" fontSize={10} fill="var(--text-faint)">{xLabel}</text>}
      </svg>
      <div className="flex wrap gap-sm" style={{ fontSize: 11, marginTop: 4 }}>
        {series.map((s, i) => (
          <span key={i} className="flex gap-sm" style={{ alignItems: "center" }}>
            <span style={{ width: 12, height: 3, background: s.color ?? PALETTE[i % PALETTE.length], display: "inline-block" }} />
            <span className="faint">{s.label}</span>
          </span>
        ))}
      </div>
    </div>
  );
}
