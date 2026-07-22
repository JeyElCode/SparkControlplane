import { useState } from "react";
import { api, ConnectionTest, InterfaceInfo, Node, NodeInput, Role } from "../lib/api";
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
  const [ifaces, setIfaces] = useState<InterfaceInfo[] | null>(null);
  const [detecting, setDetecting] = useState(false);
  const [detectErr, setDetectErr] = useState<string | null>(null);
  const set = (k: keyof NodeInput, v: any) => setN((p) => ({ ...p, [k]: v }));

  const detect = async () => {
    if (!editing) return;
    setDetecting(true);
    setDetectErr(null);
    try {
      setIfaces(await api.listInterfaces(editing.id));
    } catch (e: any) {
      setDetectErr(e.message);
    } finally {
      setDetecting(false);
    }
  };

  const ifaceLabel = (i: InterfaceInfo) => {
    const speed = i.speed_mbps ? `${i.speed_mbps >= 1000 ? `${i.speed_mbps / 1000}G` : `${i.speed_mbps}M`}` : "?";
    return `${i.name} — ${i.carrier ? `link UP · ${speed}` : "no link"}${i.qsfp_candidate ? " · QSFP" : ""}`;
  };

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
        <Field label="QSFP IP" hint="Private high-speed link IP. The head node's is the master-addr for distributed instances."><input value={n.qsfp_ip} placeholder="10.0.0.1" onChange={(e) => set("qsfp_ip", e.target.value)} /></Field>
      </div>
      <div className="row-2">
        <Field
          label="QSFP interface"
          hint={editing ? "Detect lists the node's ports with link state — pick the one with the cable." : "Any back-panel QSFP port works. Save the node, then Edit → Detect ports to pick from a list."}
        >
          {ifaces ? (
            <select value={n.qsfp_iface} onChange={(e) => set("qsfp_iface", e.target.value)}>
              {!ifaces.some((i) => i.name === n.qsfp_iface) && n.qsfp_iface && (
                <option value={n.qsfp_iface}>{n.qsfp_iface} (current)</option>
              )}
              {ifaces.map((i) => (
                <option key={i.name} value={i.name}>{ifaceLabel(i)}</option>
              ))}
            </select>
          ) : (
            <input value={n.qsfp_iface} onChange={(e) => set("qsfp_iface", e.target.value)} />
          )}
          {editing && (
            <button type="button" className="btn btn-sm" style={{ marginTop: 6 }} onClick={detect} disabled={detecting}>
              {detecting ? <Spinner /> : ifaces ? "Re-detect" : "Detect ports"}
            </button>
          )}
          {detectErr && <div className="faint" style={{ color: "var(--red, #e66)", fontSize: 12 }}>{detectErr}</div>}
        </Field>
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
  const [powerJob, setPowerJob] = useState<{ id: number; title: string } | null>(null);

  const MAX_NODES = 4;
  const all = nodes ?? [];
  const hasHead = all.some((n) => n.role === "head");
  const canAddWorker = all.length < MAX_NODES;

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

  const power = async (n: Node, action: "shutdown" | "reboot" | "wake") => {
    if (action === "wake") {
      if (!confirm(`Send a Wake-on-LAN magic packet to ${n.name} (${n.mac_address})?`)) return;
    } else {
      let msg = `${action === "reboot" ? "Reboot" : "Shut down"} ${n.name}?`;
      try {
        const affected = await api.powerAffected(n.id);
        if (affected.length > 0) {
          msg += `\n\nThis will take down running instance(s): ${affected.join(", ")}`;
        }
      } catch { /* best-effort preview */ }
      if (action === "shutdown" && !n.mac_address) {
        msg += "\n\nNote: no MAC captured yet — Wake-on-LAN will NOT be available afterwards.";
      }
      if (!confirm(msg)) return;
    }
    try {
      const r = await api.nodePower(n.id, action);
      setPowerJob({ id: r.job_id, title: `${action} ${n.name}` });
    } catch (e: any) {
      toast(e.message, "error");
    }
  };

  const batchPower = async (action: "shutdown" | "wake") => {
    const msg =
      action === "shutdown"
        ? "Shut down ALL nodes (workers first, then the head)? Running instances will go down."
        : "Send Wake-on-LAN to all nodes with a known MAC?";
    if (!confirm(msg)) return;
    try {
      const r = await api.batchPower(action);
      setPowerJob({ id: r.job_id, title: `Batch ${action}` });
    } catch (e: any) {
      toast(e.message, "error");
    }
  };

  const del = async (n: Node) => {
    if (!confirm(`Remove ${n.name} from the portal? (does not touch the node itself)`)) return;
    await api.deleteNode(n.id);
    reload();
  };

  const addRole = (role: Role) => {
    const name = role === "head" ? "spark-01" : `spark-0${all.length + 1}`;
    setForm({ initial: { ...EMPTY, role, name }, editing: null });
  };

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Nodes</h1>
          <p>SSH access to your DGX Spark boxes — 1 head + up to 3 workers. Credentials are encrypted at rest.</p>
        </div>
        <div className="btn-row">
          {!hasHead && <button className="btn btn-primary" onClick={() => addRole("head")}>+ Head node</button>}
          {hasHead && canAddWorker && <button className="btn btn-primary" onClick={() => addRole("worker")}>+ Worker node</button>}
          {all.length > 1 && (
            <>
              <button className="btn" onClick={() => batchPower("wake")}>⏻ Wake all</button>
              <button className="btn btn-danger" onClick={() => batchPower("shutdown")}>⏼ Shut down all</button>
            </>
          )}
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
                <dt>MAC</dt><dd className="mono">{n.mac_address ?? <span className="faint">unknown — Test connection captures it (needed for Wake)</span>}</dd>
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
                <button className="btn btn-sm" onClick={() => power(n, "reboot")}>Reboot</button>
                <button className="btn btn-sm" onClick={() => power(n, "shutdown")}>Shut down</button>
                <button className="btn btn-sm" disabled={!n.mac_address} title={n.mac_address ? "Wake-on-LAN" : "MAC unknown — run Test connection while the node is up"} onClick={() => power(n, "wake")}>⏻ Wake</button>
                <button className="btn btn-sm btn-danger" onClick={() => del(n)}>Remove</button>
              </div>
            </div>
          );
        })}
        {all.length === 0 && (
          <div className="card faint">No nodes configured. Add the head node, then your worker node(s), to begin.</div>
        )}
      </div>

      {form && <NodeForm initial={form.initial} editing={form.editing} onSave={save} onClose={() => setForm(null)} />}
      {hardenJob && (
        <Modal title="Hardening node" onClose={() => { setHardenJob(null); reload(); }}>
          <JobLogPanel jobId={hardenJob} title="Install key & switch to key auth" onDone={() => reload()} />
        </Modal>
      )}
      {powerJob && (
        <Modal title="Power control" onClose={() => { setPowerJob(null); reload(); }}>
          <JobLogPanel jobId={powerJob.id} title={powerJob.title} onDone={() => reload()} />
        </Modal>
      )}
    </div>
  );
}
