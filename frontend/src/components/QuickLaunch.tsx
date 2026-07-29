/**
 * One-decision launch: pick a model, read what the portal worked out, start it.
 *
 * The whole point of a control plane over a shell is that it knows things the
 * operator would otherwise have to look up — how much memory is free, whether
 * the weights fit one box, what context the KV cache leaves room for. This
 * dialog is that knowledge, applied.
 *
 * Two things it deliberately does NOT do:
 *
 * It does not hide the reasoning. Every derived value is shown with the
 * sentence explaining where the number came from, because a recommendation an
 * operator cannot audit is one they cannot trust or learn from — and the first
 * time it is wrong for their hardware, the explanation is what lets them fix it.
 *
 * It does not remove the form. "Customize" hands the whole plan to the normal
 * create dialog with every field editable and the advanced surface untouched.
 * Nothing here is a ceiling on what the software can do; it is a floor under
 * what the operator has to know.
 */
import { useState } from "react";
import { api, Model, Plan, InstanceInput } from "../lib/api";
import { usePoll } from "../lib/hooks";
import { Modal, Spinner } from "./ui";
import { useToast } from "./Toast";

function valueText(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") return v.toLocaleString();
  return String(v);
}

export function PlanDetails({ plan }: { plan: Plan }) {
  return (
    <>
      <div className="plan-summary">{plan.summary}</div>

      <div className="plan-reasons">
        {plan.reasons.map((r) => (
          <div className="plan-reason" key={r.field}>
            <div className="plan-reason-head">
              <span className="plan-reason-label">{r.label}</span>
              <span className="plan-reason-value">{valueText(r.value)}</span>
            </div>
            <div className="plan-reason-why">{r.why}</div>
          </div>
        ))}
      </div>

      {plan.warnings.map((w, i) => (
        <div className="banner banner-warn" key={i} style={{ marginTop: 10 }}>
          ⚠ {w}
        </div>
      ))}
    </>
  );
}

export function QuickLaunch({
  model,
  onClose,
  onLaunched,
  onCustomize,
}: {
  model: Model;
  onClose: () => void;
  onLaunched: () => void;
  /** Hand the derived settings to the full create form, nothing locked. */
  onCustomize: (plan: Plan) => void;
}) {
  const { toast } = useToast();
  const [busy, setBusy] = useState(false);
  // Planning may need a round-trip to HuggingFace the first time a model is
  // used, so this is a real load, not an instant computation.
  const { data: plan, error, loading } = usePoll(() => api.planInstance({ model_id: model.id }), 0);

  const launch = async () => {
    if (!plan) return;
    setBusy(true);
    try {
      const inst = await api.createInstance({
        ...(plan.settings as InstanceInput),
        name: plan.name,
        model_id: model.id,
      });
      await api.startInstance(inst.id);
      toast(`Starting ${plan.name} — watch the job for progress`, "success");
      onLaunched();
      onClose();
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title={`Run ${model.name}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn"
            onClick={() => plan && onCustomize(plan)}
            disabled={!plan}
            title="Open the full form with these settings filled in — everything stays editable"
          >
            Customize
          </button>
          <button
            className="btn btn-primary"
            onClick={launch}
            disabled={busy || !plan || !plan.feasible}
            title={
              plan && !plan.feasible
                ? "This model does not fit the cluster as configured — see the warnings"
                : undefined
            }
          >
            {busy ? <Spinner /> : "Run it"}
          </button>
        </>
      }
    >
      {loading && (
        <div className="plan-loading">
          <Spinner /> Working out how to run this on your cluster…
        </div>
      )}

      {error && (
        <div className="banner banner-warn">
          ⚠ Could not plan this model: {error}
          <div className="badge-note" style={{ marginTop: 4 }}>
            You can still create the instance by hand from the Instances page.
          </div>
        </div>
      )}

      {plan && <PlanDetails plan={plan} />}

      {plan && (
        <div className="badge-note" style={{ marginTop: 14 }}>
          These become an instance named <strong>{plan.name}</strong>, on an
          automatically assigned port, reachable through the <code>/v1</code>{" "}
          gateway. Every value above stays editable afterwards — use{" "}
          <strong>Customize</strong> to change them before it starts.
        </div>
      )}
    </Modal>
  );
}
