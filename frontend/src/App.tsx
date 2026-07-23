import { useEffect, useState } from "react";
import { NavLink, Route, Routes } from "react-router-dom";
import { api } from "./lib/api";
import { usePoll } from "./lib/hooks";
import { Badge } from "./components/ui";
import { Login } from "./components/Login";
import Dashboard from "./pages/Dashboard";
import Setup from "./pages/Setup";
import Nodes from "./pages/Nodes";
import Models from "./pages/Models";
import Instances from "./pages/Instances";
import Evals from "./pages/Evals";
import Playground from "./pages/Playground";
import Teardown from "./pages/Teardown";
import SettingsPage from "./pages/Settings";

const NAV = [
  { to: "/", label: "Dashboard", icon: "▦", end: true },
  { to: "/setup", label: "Setup", icon: "⚙" },
  { to: "/nodes", label: "Nodes", icon: "▤" },
  { to: "/models", label: "Models", icon: "◈" },
  { to: "/instances", label: "Instances", icon: "▶" },
  { to: "/evals", label: "Evals", icon: "≈" },
  { to: "/playground", label: "Playground", icon: "✦" },
  { to: "/teardown", label: "Teardown", icon: "⌫" },
  { to: "/settings", label: "Settings", icon: "⚙" },
];

const THEMES = [
  { id: "dark", label: "◐ Dark" },
  { id: "light", label: "○ Light" },
  { id: "oled", label: "● OLED" },
];

function ThemeSelect() {
  const [theme, setTheme] = useState(
    () => localStorage.getItem("spark-theme") ?? "dark"
  );
  useEffect(() => {
    if (theme === "dark") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme;
    localStorage.setItem("spark-theme", theme);
  }, [theme]);
  return (
    <select
      value={theme}
      onChange={(e) => setTheme(e.target.value)}
      title="Theme"
      style={{ width: "auto", padding: "5px 8px", fontSize: 12 }}
    >
      {THEMES.map((t) => (
        <option key={t.id} value={t.id}>{t.label}</option>
      ))}
    </select>
  );
}

function HealthPill() {
  const { data } = usePoll(() => api.getSettings(), 20000);
  if (!data) return null;
  return (
    <Badge kind={data.setup_complete ? "green" : "amber"}>
      {data.setup_complete ? "Cluster ready" : "Setup incomplete"}
    </Badge>
  );
}

export default function App() {
  const meta = usePoll(() => api.health(), 0);
  const auth = usePoll(() => api.authMe(), 0);

  useEffect(() => {
    const onUnauthorized = () => auth.reload();
    window.addEventListener("spark:unauthorized", onUnauthorized);
    return () => window.removeEventListener("spark:unauthorized", onUnauthorized);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const logout = async () => {
    await api.logout();
    auth.reload();
  };

  if (!auth.data && !auth.error) {
    return <div style={{ minHeight: "100vh", display: "grid", placeItems: "center", background: "var(--bg)" }} />;
  }
  if (auth.data?.auth_required && !auth.data.authenticated) {
    return <Login mode={auth.data.auth_mode} onSuccess={() => auth.reload()} />;
  }

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-logo">S</div>
          <div>
            <div className="brand-name">Spark Control</div>
            <div className="brand-sub">DGX Spark vLLM</div>
          </div>
        </div>
        {NAV.map((n) => (
          <NavLink key={n.to} to={n.to} end={n.end} className={({ isActive }) => `nav-link ${isActive ? "active" : ""}`}>
            <span className="ico">{n.icon}</span>
            {n.label}
          </NavLink>
        ))}
        <div className="sidebar-foot">
          {auth.data?.auth_required && (
            <div style={{ marginBottom: 6 }}>
              <span className="mono">{auth.data.user}</span>
              {" · "}
              <a onClick={logout} style={{ cursor: "pointer" }}>sign out</a>
            </div>
          )}
          v{meta.data?.version ?? "1.0.0"}
        </div>
      </aside>
      <div className="main">
        <header className="topbar">
          <div className="muted">DGX Spark cluster</div>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <ThemeSelect />
            <HealthPill />
          </div>
        </header>
        <div className="content">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/setup" element={<Setup />} />
            <Route path="/nodes" element={<Nodes />} />
            <Route path="/models" element={<Models />} />
            <Route path="/instances" element={<Instances />} />
            <Route path="/evals" element={<Evals />} />
            <Route path="/playground" element={<Playground />} />
            <Route path="/teardown" element={<Teardown />} />
            <Route path="/settings" element={<SettingsPage />} />
          </Routes>
        </div>
      </div>
    </div>
  );
}
