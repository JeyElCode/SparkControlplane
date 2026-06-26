export function fmtBytes(n?: number | null): string {
  if (n == null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let f = n;
  let i = 0;
  while (f >= 1024 && i < units.length - 1) {
    f /= 1024;
    i++;
  }
  return `${f.toFixed(f < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
}

export function fmtGib(n?: number | null): string {
  if (n == null) return "—";
  return `${n.toFixed(0)} GiB`;
}

export function timeAgo(iso?: string | null): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  const s = Math.floor((Date.now() - t) / 1000);
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export type BadgeKind = "green" | "amber" | "red" | "blue" | "gray";

const STATUS_KIND: Record<string, BadgeKind> = {
  running: "green",
  success: "green",
  present: "green",
  ok: "green",
  active: "green",
  starting: "amber",
  stopping: "amber",
  pending: "amber",
  downloading: "amber",
  syncing: "amber",
  verifying: "amber",
  warn: "amber",
  error: "red",
  stopped: "gray",
  absent: "gray",
  cancelled: "gray",
};

export function statusKind(status?: string | null): BadgeKind {
  if (!status) return "gray";
  return STATUS_KIND[status.toLowerCase()] ?? "gray";
}

export function boolKind(v?: boolean | null): BadgeKind {
  if (v == null) return "gray";
  return v ? "green" : "red";
}
