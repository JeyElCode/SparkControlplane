import { useEffect, useRef, useState } from "react";
import { api, wsUrl } from "../lib/api";
import { statusKind } from "../lib/format";
import { Badge, Meter } from "./ui";

interface LogLine {
  seq: number;
  stream: string;
  text: string;
}

const TERMINAL = ["success", "error", "cancelled"];

export function JobLogPanel({
  jobId,
  title,
  onDone,
}: {
  jobId: number;
  title?: string;
  onDone?: (status: string) => void;
}) {
  const [lines, setLines] = useState<LogLine[]>([]);
  const [status, setStatus] = useState("running");
  const [progress, setProgress] = useState<number | null>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const doneRef = useRef(false);

  useEffect(() => {
    setLines([]);
    setStatus("running");
    setProgress(null);
    doneRef.current = false;
    let pollTimer: ReturnType<typeof setInterval> | null = null;
    let disposed = false;

    const finish = (s: string) => {
      if (!doneRef.current && TERMINAL.includes(s)) {
        doneRef.current = true;
        onDone?.(s);
      }
    };

    // Reconcile authoritative state from the server (used when the live socket
    // drops). A dropped WebSocket is NOT a job failure.
    const reconcile = async () => {
      try {
        const j = await api.getJob(jobId);
        setStatus(j.status);
        setProgress(typeof j.progress === "number" ? j.progress : null);
        if (Array.isArray(j.logs)) {
          setLines(j.logs.map((l: any) => ({ seq: l.seq, stream: l.stream, text: l.text })));
        }
        if (TERMINAL.includes(j.status)) {
          finish(j.status);
          if (pollTimer) {
            clearInterval(pollTimer);
            pollTimer = null;
          }
        }
      } catch {
        /* transient — will retry on the next tick */
      }
    };

    const ws = new WebSocket(wsUrl(`/api/jobs/${jobId}/logs`));
    ws.onmessage = (ev) => {
      const e = JSON.parse(ev.data);
      if (e.type === "log") {
        setLines((xs) => [...xs, { seq: e.seq, stream: e.stream, text: e.text }]);
      } else if (e.type === "progress") {
        setProgress(typeof e.progress === "number" ? e.progress : null);
      } else if (e.type === "status") {
        setStatus(e.status);
        finish(e.status);
      } else if (e.type === "end") {
        ws.close();
      }
    };
    ws.onclose = () => {
      if (disposed || doneRef.current) return;
      // Socket dropped before the job finished — fall back to polling the real
      // job status from the API so the badge reflects reality, not the transport.
      reconcile();
      if (!pollTimer) pollTimer = setInterval(reconcile, 3000);
    };

    return () => {
      disposed = true;
      if (pollTimer) clearInterval(pollTimer);
      ws.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId]);

  useEffect(() => {
    if (boxRef.current) boxRef.current.scrollTop = boxRef.current.scrollHeight;
  }, [lines]);

  return (
    <div>
      <div className="spread mb">
        <strong>{title ?? `Job #${jobId}`}</strong>
        <Badge kind={statusKind(status)}>{status}</Badge>
      </div>
      {progress != null && status === "running" && (
        <div className="progress-row">
          <Meter value={progress} max={1} />
          <span className="pct">{Math.round(progress * 100)}%</span>
        </div>
      )}
      <div className="logs" ref={boxRef}>
        {lines.length === 0 ? (
          <span className="logs-empty">Waiting for output…</span>
        ) : (
          lines.map((l, i) => (
            <div key={i} className={`l-${l.stream}`}>
              {l.text}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
