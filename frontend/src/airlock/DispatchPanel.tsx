import { useEffect, useState } from "react";
import { Send } from "lucide-react";
import { api } from "../services/api";
import { type Row, clock, shortId, words } from "./format";
import { deliveryLabel, routeFor } from "./dispatch";
import { Badge, Panel } from "./ui";

/** Who was notified about the selected incident, from the M4 backend's GET /api/security/notifications (read-only). Owner console only:
 *  the security desk never calls the M4. If the backend has no dispatch yet, it says so instead of guessing. */
export function DispatchPanel({ incidentId }: { incidentId?: string }) {
  const [data, setData] = useState<Row | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let stop = false;
    const load = async () => {
      try { const d = await api.securityNotifications(); if (!stop) { setData(d); setError(null); } }
      catch (e) { if (!stop) setError(e instanceof Error ? e.message : String(e)); }
    };
    void load();
    const id = window.setInterval(load, 3000);
    return () => { stop = true; window.clearInterval(id); };
  }, []);
  const entry = (data?.incidents as Row[] | undefined)?.find(i => i.incident_id === incidentId);
  const phone = data?.phone as Row | undefined;
  return <Panel title="Notifications" icon={<Send size={16} />} aside={phone && <Badge tone={phone.configured ? "cyan" : "quiet"}>{phone.configured ? `Phone: ${phone.provider}` : "Phone: off"}</Badge>}>
    {error ? <p className="al-small al-muted">Dispatch status unavailable from this Mac's backend ({error}). Local warnings do not depend on it.</p>
      : !data ? <p className="al-small al-muted">Loading dispatch status…</p>
      : !entry ? <p className="al-small al-muted">{incidentId ? "No notification recorded for this incident." : "Select an incident."}</p>
      : <div className="al-dispatch">
          <p className="al-small">Rule {words(entry.rule_id)} → {routeFor(data.policy as Row, String(entry.rule_id))} · notified at {words(entry.notified_for_severity)} severity · <code>{shortId(entry.incident_id)}</code></p>
          {entry.skipped ? <p className="al-small al-muted">Nothing sent: {String(entry.skipped)}</p> : null}
          {((entry.deliveries as Row[]) ?? []).map((d, n) => { const l = deliveryLabel(d); return <div key={n} className="al-inline"><Badge tone={l.tone} dot>{l.text}</Badge>{d.accepted_at_ms ? <span className="al-small">{clock(d.accepted_at_ms)}</span> : null}</div>; })}
          {entry.acknowledged_by ? <p className="al-small">Acknowledged by {shortId(entry.acknowledged_by)} at {clock(entry.acknowledged_at_ms)}</p> : null}
          {phone?.last_error ? <p className="al-small al-text-amber">Phone channel: {String(phone.last_error)}</p> : null}
        </div>}
  </Panel>;
}
