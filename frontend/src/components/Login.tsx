import { FormEvent, useState } from "react";
import { api } from "../lib/api";
import { Spinner } from "./ui";

/** Full-screen login gate, shown when auth is on and there is no session. */
export function Login({ mode, onSuccess }: { mode: string; onSuccess: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(username, password);
      onSuccess();
    } catch (err: any) {
      setError(err.message ?? String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ minHeight: "100vh", display: "grid", placeItems: "center", background: "var(--bg)" }}>
      <form className="card" style={{ width: 340 }} onSubmit={submit}>
        <div className="brand" style={{ padding: "0 0 14px" }}>
          <div className="brand-logo">S</div>
          <div>
            <div className="brand-name">Spark Control</div>
            <div className="brand-sub">{mode === "ldap" ? "Sign in with your directory account" : "Sign in"}</div>
          </div>
        </div>
        <label className="faint" style={{ fontSize: 12 }}>Username</label>
        <input autoFocus autoComplete="username" value={username}
               onChange={(e) => setUsername(e.target.value)} style={{ marginBottom: 10 }} />
        <label className="faint" style={{ fontSize: 12 }}>Password</label>
        <input type="password" autoComplete="current-password" value={password}
               onChange={(e) => setPassword(e.target.value)} style={{ marginBottom: 14 }} />
        {error && <div className="banner banner-warn" style={{ marginBottom: 12 }}>⚠ {error}</div>}
        <button className="btn btn-primary" style={{ width: "100%" }} disabled={busy || !username || !password}>
          {busy ? <Spinner /> : "Sign in"}
        </button>
      </form>
    </div>
  );
}
