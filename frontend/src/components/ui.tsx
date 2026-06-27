import { ReactNode } from "react";
import { BadgeKind } from "../lib/format";

export function Badge({ kind = "gray", children, dot = true }: { kind?: BadgeKind; children: ReactNode; dot?: boolean }) {
  return (
    <span className={`badge badge-${kind}`}>
      {dot && <span className="dot" />}
      {children}
    </span>
  );
}

export function Spinner() {
  return <span className="spin" />;
}

export function Modal({
  title,
  onClose,
  children,
  footer,
  wide = false,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}) {
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className={`modal ${wide ? "modal-lg" : ""}`} onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <h2 style={{ margin: 0 }}>{title}</h2>
          <button className="btn btn-ghost btn-sm" onClick={onClose}>✕</button>
        </div>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}

export function HelpTip({ text }: { text: string }) {
  return (
    <span className="help" tabIndex={0} aria-label={text}>
      ?<span className="help-tip">{text}</span>
    </span>
  );
}

export function Field({
  label,
  hint,
  help,
  children,
}: {
  label: string;
  hint?: string;
  help?: string;
  children: ReactNode;
}) {
  return (
    <div className="field">
      <label>
        {label}
        {help && <HelpTip text={help} />}
      </label>
      {children}
      {hint && <div className="hint">{hint}</div>}
    </div>
  );
}

export function EmptyState({ icon, title, children }: { icon: string; title: string; children?: ReactNode }) {
  return (
    <div className="empty-state">
      <div className="big">{icon}</div>
      <div style={{ fontWeight: 600, color: "var(--text-dim)" }}>{title}</div>
      {children && <div style={{ marginTop: 8 }}>{children}</div>}
    </div>
  );
}

export function Meter({ value, max }: { value: number; max: number }) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  const cls = pct > 92 ? "crit" : pct > 75 ? "warn" : "";
  return (
    <div className={`meter ${cls}`}>
      <span style={{ width: `${pct}%` }} />
    </div>
  );
}
