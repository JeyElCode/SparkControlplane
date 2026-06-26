import { useState } from "react";
import { api, ConnectionTest, Node, NodeInput, Role } from "../lib/api";
import { usePoll } from "../lib/hooks";
import { boolKind } from "../lib/format";
import { Badge, Field, Modal, Spinner } from "../components/ui";
import { JobLogPanel } from "../components/JobLogPanel";
import { useToast } from "../components/Toast";

const EMPTY: NodeInput = {
  role: "head",
  name: "",
  lan_ip: "",
  qsfp_ip: "",
  qsfp_iface: "enp1s0f1np1",
  ssh_user: "",
  ssh_port: 22,
  auth_method: "password",
  ssh_password: "",
  ssh_private_key: "",
  ssh_key_passphrase: "",
  sudo_mode: "password",
  sudo_password: "",
};

function NodeForm({
  initial,
  editing,
  onSave,
  onClose,
}: {
  initial: NodeInput;
  editing: Node | null;
  onSave: (n: NodeInput) => Promise<void>;
  onClose: () => void;
}) {
  const [n, setN] = useState<NodeInput>(initial);
  const [busy, setBusy] = useState(false);
  const set = (k: keyof NodeInput, v: any) => setN((p) => ({ ...p, [k]: v }));

  const submit = async () => {
    setBusy(true);
    try {
      await onSave(n);
    } finally {
      setBusy(false);
    }
  };

  const secretHint = editing ? "Leave blank to keep the stored value." : undefined;

  return (
    <Modal
      title={editing ? `Edit ${editing.name}` : "Add node"}
      onClose={onClose}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary" onClick={submit} disabled={busy}>
            {busy ? <Spinner /> : "Save"}
          </button>
        </>
      }
    >
      <div className="row-2">
        <Field label="Role">
          <select value={n.role} disabled={!!editing} onChange={(e) => set("role", e.target.value as Role)}>
            <option value="head">head (spark-01)</option>
            <option value="worker">worker (spark-02)</option>
          </select>
        </Field>
        <Field label="Hostname"><input value={n.name} placeholder="spark-01" onChange={(e) => set("name", e.target.value)} /></Field>
      </div>
      <div className="row-2">
        <Field label="LAN IP" hint="Management IP used for SSH"><input value={n.lan_ip} placeholder="192.168.1.160" onChange={(e) => set("lan_ip", e.target.value)} /></Field>
        <Field label="QSFP IP" hint="Private high-speed link IP"><input value={n.qsfp_ip} placeholder="10.10.10.1" onChange={(e) => set("qsfp_ip", e.target.value)} /></Field>
      </div>
      <div className="row-2">
        <Field label="QSFP interface"><input value={n.qsfp_iface} onChange={(e) => set("qsfp_iface", e.target.value)} /></Field>
        <Field label="SSH user"><input value={n.ssh_user} placeholder="jlindalen" onChange={(e) => set("ssh_user", e.target.value)} /></Field>
      </div>
      <div className="row-2">
        <Field label="SSH port"><input type="number" value={n.ssh_port} onChange={(e) => set("ssh_port", Number(e.target.value))} /></Field>
        <Field label="Auth method">
          <select value={n.auth_method} onChange={(e) => set("auth_method", e.target.value)}>
            <option value="password">password</option>
            <option value="key">private key</option>
          </select>
        </Field>
      </div>
      {n.auth_method === "password" ? (
        <Field label="SSH password" hint={secretHint}><input type="password" value={n.ssh_password ?? ""} onChange={(e) => set("ssh_password", e.target.value)} /></Field>
      ) : (
        <>
          <Field label="Private key (PEM/OpenSSH)" hint={secretHint}><textarea value={n.ssh_private_key ?? ""} onChange={(e) => set("ssh_private_key", e.target.value)} placeholder="-----BEGIN OPENSSH PRIVATE KEY-----" /></Field>
          <Field label="Key passphrase (optional)" hint={secretHint}><input type="password" value={n.ssh_key_passphrase ?? ""} onChange={(e) => set("ssh_key_passphrase", e.target.value)} /></Field>
        </>
      )}
      <div className="row-2">
        <Field label="Sudo mode">
          <select value={n.sudo_mode} onChange={(e) => set("sudo_mode", e.target.value)}>
            <option value="nopasswd">passwordless (NOPASSWD)</option>
            <option value="password">sudo password</option>
          </select>
        </Field>
        {n.sudo_mode === "password" && (
          <Field label="Sudo password" hint={secretHint}><input type="password" value={n.sudo_password ?? ""} onChange={(e) => set("sudo_password", e.target.value)} /></Field>
        )}
      </div>
    </Modal>
  );
}

export default function Nodes() {
  const { data: nodes, reload } = usePoll(() => api.listNodes(), 0);
  const { toast } = useToast();
  const [form, setForm] = useState<{ initial: NodeInput; editing: Node | null } | null>(null);
  const [tests, setTests] = useState<Record<number, ConnectionTest | "loading">>({});
  const [hardenJob, setHardenJob] = useState<number | null>(null);

  const haveRoles = new Set((nodes ?? []).map((n) => n.role));

  const save = async (n: NodeInput) => {
    try {
      if (form?.editing) {
        await api.updateNode(form.editing.id, n);
        toast("Node updated", "success");
      } else {
        await api.createNode(n);
        toast("Node added", "success");
      }
      setForm(null);
      reload();
    } catch (e: any) {
      toast(e.message, "error");
    }
  };

  const runTest = async (id: number) => {
    setTests((t) => ({ ...t, [id]: "loading" }));
    try {
      const r = await api.testNode(id);
      setTests((t) => ({ ...t, [id]: r }));
    } catch (e: any) {
      setTests((t) => ({ ...t, [id]: { ok: false, message: e.message } }));
    }
  };

  const harden = async (id: number) => {
    try {
      const r = await api.hardenNode(id);
      setHardenJob(r.job_id);
    } catch (e: any) {
      toast(e.message, "error");
    }
  };

  const del = async (n: Node) => {
    if (!confirm(`Remove ${n.name} from the portal? (does not touch the node itself)`)) return;
    await api.deleteNode(n.id);
    reload();
  };

  const addRole = (role: Role) => setForm({ initial: { ...EMPTY, role, name: role === "head" ? "spark-01" : "spark-02" }, editing: null });

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Nodes</h1>
          <p>SSH access to the two DGX Spark boxes. Credentials are encrypted at rest.</p>
        </div>
        <div className="btn-row">
          {!haveRoles.has("head") && <button className="btn btn-primary" onClick={() => addRole("head")}>+ Head node</button>}
          {!haveRoles.has("worker") && <button className="btn btn-primary" onClick={() => addRole("worker")}>+ Worker node</button>}
        </div>
      </div>

      <div className="grid grid-2">
        {(nodes ?? []).map((n) => {
          const t = tests[n.id];
          return (
            <div key={n.id} className="card">
              <div className="card-head">
                <div className="flex">
                  <strong>{n.name}</strong>
                  <Badge kind="blue" dot={false}>{n.role}</Badge>
                  {n.hardened && <Badge kind="green">key auth</Badge>}
                </div>
              </div>
              <dl className="kv">
                <dt>LAN IP</dt><dd className="mono">{n.lan_ip}:{n.ssh_port}</dd>
                <dt>QSFP IP</dt><dd className="mono">{n.qsfp_ip} <span className="faint">({n.qsfp_iface})</span></dd>
                <dt>SSH</dt><dd>{n.ssh_user} · {n.auth_method}{n.has_ssh_password ? " (pw set)" : ""}{n.has_ssh_key ? " (key set)" : ""}</dd>
                <dt>Sudo</dt><dd>{n.sudo_mode}{n.has_sudo_password ? " (pw set)" : ""}</dd>
              </dl>
              {t && t !== "loading" && (
                <div className={`banner ${t.ok ? "banner-info" : "banner-warn"}`} style={{ marginTop: 12 }}>
                  <div className="flex-col">
                    <div>{t.ok ? "✓" : "✗"} {t.message}</div>
                    {t.ok && (
                      <div className="flex wrap gap-sm">
                        <Badge kind={boolKind(t.sudo_ok)}>sudo</Badge>
                        <Badge kind={boolKind(t.docker_ok)}>docker</Badge>
                        <Badge kind={boolKind(t.gpu_ok)}>gpu</Badge>
                      </div>
                    )}
                  </div>
                </div>
              )}
              <div className="btn-row mt">
                <button className="btn btn-sm" onClick={() => runTest(n.id)} disabled={t === "loading"}>
                  {t === "loading" ? <Spinner /> : "Test connection"}
                </button>
                <button className="btn btn-sm" onClick={() => setForm({ initial: { ...EMPTY, ...n, ssh_password: "", ssh_private_key: "", sudo_password: "", ssh_key_passphrase: "" }, editing: n })}>Edit</button>
                {!n.hardened && <button className="btn btn-sm" onClick={() => harden(n.id)}>Harden → key</button>}
                <button className="btn btn-sm btn-danger" onClick={() => del(n)}>Remove</button>
              </div>
            </div>
          );
        })}
        {(nodes ?? []).length === 0 && (
          <div className="card faint">No nodes configured. Add the head and worker nodes to begin.</div>
        )}
      </div>

      {form && <NodeForm initial={form.initial} editing={form.editing} onSave={save} onClose={() => setForm(null)} />}
      {hardenJob && (
        <Modal title="Hardening node" onClose={() => { setHardenJob(null); reload(); }}>
          <JobLogPanel jobId={hardenJob} title="Install key & switch to key auth" onDone={() => reload()} />
        </Modal>
      )}
    </div>
  );
}
