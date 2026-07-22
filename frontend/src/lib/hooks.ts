import { useCallback, useEffect, useRef, useState } from "react";
import { api, StatusSnapshot } from "./api";

export interface AsyncState<T> {
  data: T | undefined;
  error: string | undefined;
  loading: boolean;
  reload: () => void;
}

/** Load `fn` on mount and optionally poll every `intervalMs`. */
export function usePoll<T>(fn: () => Promise<T>, intervalMs = 0): AsyncState<T> {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<string>();
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;

  const run = useCallback(async () => {
    try {
      const d = await fnRef.current();
      setData(d);
      setError(undefined);
    } catch (e: any) {
      setError(e?.message ?? String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    run();
    if (intervalMs > 0) {
      const id = setInterval(run, intervalMs);
      return () => clearInterval(id);
    }
  }, [run, intervalMs]);

  return { data, error, loading, reload: run };
}

/** Live StatusSnapshot over the status WebSocket, with automatic reconnect and
 * a plain-polling fallback while the socket is down. */
export function useStatusStream(intervalSeconds = 3): {
  data: StatusSnapshot | undefined;
  error: string | undefined;
  connected: boolean;
} {
  const [data, setData] = useState<StatusSnapshot>();
  const [error, setError] = useState<string>();
  const [connected, setConnected] = useState(false);
  const connectedRef = useRef(false);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    let retryMs = 2000;
    let retryTimer: number | undefined;

    const connect = () => {
      if (closed) return;
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${window.location.host}/api/status/ws?interval=${intervalSeconds}`);
      ws.onopen = () => {
        retryMs = 2000;
        connectedRef.current = true;
        setConnected(true);
        setError(undefined);
      };
      ws.onmessage = (ev) => {
        try {
          setData(JSON.parse(ev.data));
          setError(undefined);
        } catch {
          /* malformed frame — keep the last snapshot */
        }
      };
      ws.onclose = () => {
        connectedRef.current = false;
        setConnected(false);
        if (!closed) {
          retryTimer = window.setTimeout(connect, retryMs);
          retryMs = Math.min(retryMs * 2, 15000);
        }
      };
      ws.onerror = () => ws?.close();
    };
    connect();

    // Polling fallback: only fires while the socket is down.
    const poll = window.setInterval(async () => {
      if (connectedRef.current) return;
      try {
        setData(await api.getStatus());
        setError(undefined);
      } catch (e: any) {
        setError(e?.message ?? String(e));
      }
    }, Math.max(3000, intervalSeconds * 1000));

    return () => {
      closed = true;
      if (retryTimer) window.clearTimeout(retryTimer);
      window.clearInterval(poll);
      ws?.close();
    };
  }, [intervalSeconds]);

  return { data, error, connected };
}
