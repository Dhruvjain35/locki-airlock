import { useState } from "react";
import { Check, Copy, Headset, ShieldAlert } from "lucide-react";
import { type Row, ago, clock, shortId, words } from "./format";
import type { Perform } from "./forms";
import { acknowledged } from "./alerts";
import { Investigation, ReviewPanel } from "./Investigation";
import { IncidentSummary, Timeline } from "./Timeline";
import { Badge, Empty, Panel } from "./ui";

/** The Demo Security Desk (?client=desk&view=desk): a separate console with its OWN persisted identity. The owner enrolls it as a
 *  reviewer that cannot approve actions (Enroll identity -> Grant human reviewer access, approval unchecked). It sees only what the
 *  module's views give that identity for this workstation: incidents, their evidence, the investigation, acknowledgements and audit.
 *  Its one action is acknowledge; it never holds owner credentials. */
export function DeskView({ station, role, identity, connected, pending, perform, now, incidents, events, audit, runs, steps, acknowledgements, locks }: {
  station?: Row; role: string; identity: string; connected: boolean; pending: boolean; perform: Perform; now: number;
  incidents: Row[]; events: Row[]; audit: Row[]; runs: Row[]; steps: Row[]; acknowledgements: Row[]; locks: Row[];
}) {
  const [selected, setSelected] = useState("");
  const [copied, setCopied] = useState(false);
  const reviewer = role === "reviewer" || role === "owner";
  const sorted = [...incidents].sort((a, b) => (a.state === "resolved" ? 1 : 0) - (b.state === "resolved" ? 1 : 0) || Number(b.updatedAtMs) - Number(a.updatedAtMs));
  const incident = sorted.find(i => i.id === selected) || sorted[0];
  if (!connected) return <main className="al-main"><Empty icon={<Headset size={24} />} title="Security desk disconnected">Reconnecting with this desk's own identity.</Empty></main>;
  if (!station || !reviewer) return <main className="al-main"><Panel title="Security desk: waiting for the owner" icon={<Headset size={16} />}>
    <p>This desk has its own identity. The workstation owner enrolls it as a reviewer without approval rights: <strong>Enroll identity → Grant human reviewer access</strong>, approval unchecked.</p>
    <code className="al-identity-full">{identity || "identity available once connected"}</code>
    {identity && <button className="al-text-button" onClick={async () => { await navigator.clipboard.writeText(identity); setCopied(true); }}>{copied ? <Check size={13} /> : <Copy size={13} />}{copied ? "Copied" : "Copy this desk's public identity"}</button>}
    <p className="al-small al-muted">Only the public identity is shared. The desk never receives owner credentials.</p>
  </Panel></main>;
  const acks = acknowledgements.filter(a => a.incidentId === incident?.id).sort((a, b) => Number(b.createdAtMs) - Number(a.createdAtMs));
  return <main className="al-main">
    <section className="al-status-section"><div className="al-status-copy"><div className="al-inline"><Badge tone="cyan" dot>Security desk</Badge><Badge tone="quiet">{words(role)} · {shortId(identity)}</Badge></div>
      <h1>{sorted.filter(i => i.state !== "resolved").length ? "Active incidents" : "No active incidents"}</h1><p>{station.name} · {station.id}. Observations are evidence, not proof of intent.</p></div></section>
    <div className="al-control-grid">
      <aside className="al-left-column"><Panel title="Incidents" icon={<ShieldAlert size={16} />}>
        {sorted.length ? sorted.map(i => <button key={String(i.id)} className={`al-desk-incident ${incident?.id === i.id ? "selected" : ""}`} onClick={() => setSelected(String(i.id))}>
          <div className="al-inline al-spread"><Badge tone={i.state === "resolved" ? "quiet" : i.severity === "high" || i.severity === "critical" ? "magenta" : "amber"}>{words(i.severity)}</Badge><span className="al-small">{clock(i.createdAtMs)} · {ago(i.updatedAtMs, now)}</span></div>
          <strong>{words(i.ruleId)}</strong><span className="al-small">{words(i.state)} · {acknowledged(i, acknowledgements) ? "acknowledged" : "not acknowledged"}{i.simulated ? " · synthetic" : ""}</span>
        </button>) : <Empty icon={<ShieldAlert size={22} />} title="Nothing to review">Incidents for this workstation appear here as the module opens them.</Empty>}
      </Panel></aside>
      <div className="al-center-column">
        {incident ? <><IncidentSummary incident={incident} events={events} /><Investigation runs={runs} steps={steps} incidentId={String(incident.id)} />
          <Panel title="Acknowledgements" icon={<Check size={16} />}>{acks.length ? acks.map(a => <p key={String(a.id)} className="al-small"><strong>{words(a.kind)}</strong> by {shortId(a.actor)} · {clock(a.createdAtMs)}{a.note ? ` · ${a.note}` : ""}</p>) : <p className="al-small al-muted">Not acknowledged yet.</p>}</Panel>
          <Timeline events={events} audit={audit} incidents={incidents} selectedIncidentId={String(incident.id)} onSelect={setSelected} /></> : null}
      </div>
      <aside className="al-right-column"><ReviewPanel key={String(incident?.id || "none")} incident={incident} locks={locks} owner={false} reviewer={reviewer} connected={connected} pending={pending} perform={perform} /></aside>
    </div>
  </main>;
}
