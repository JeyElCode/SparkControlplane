import { NavLink, Route, Routes } from "react-router-dom";
import { api } from "./lib/api";
import { usePoll } from "./lib/hooks";
import { Badge } from "./components/ui";
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
        <div className="sidebar-foot">v{meta.data?.version ?? "1.0.0"}</div>
      </aside>
      <div className="main">
        <header className="topbar">
          <div className="muted">2-node DGX Spark cluster</div>
          <HealthPill />
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
