import { useEffect, useRef, useState } from "react";
import { wsUrl } from "../lib/api";
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
    const ws = new WebSocket(wsUrl(`/api/jobs/${jobId}/logs`));
    ws.onmessage = (ev) => {
      const e = JSON.parse(ev.data);
      if (e.type === "log") {
        setLines((xs) => [...xs, { seq: e.seq, stream: e.stream, text: e.text }]);
      } else if (e.type === "progress") {
        setProgress(typeof e.progress === "number" ? e.progress : null);
      } else if (e.type === "status") {
        setStatus(e.status);
        if (TERMINAL.includes(e.status) && !doneRef.current) {
          doneRef.current = true;
          onDone?.(e.status);
        }
      } else if (e.type === "end") {
        ws.close();
      }
    };
    ws.onerror = () => setStatus("error");
    return () => ws.close();
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
