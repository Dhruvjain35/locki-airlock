import { useEffect, useRef, useState } from "react";
import { KeyRound, ShieldCheck } from "lucide-react";
import { type Row, clock, shortId, textValue, words } from "./format";
import type { Perform } from "./forms";
import { Badge, Button, Empty, FormField, JsonDetails, Panel } from "./ui";
import {
  changeInvestigatorAccess, frozenInvestigatorBaseline, investigatorAccessReview, investigatorOwnerMatches,
  linkedInvestigatorEventIds, linkedInvestigatorEvents, realInvestigatorIncidents,
  type InvestigatorAccessAction, type InvestigatorAccessContext, type InvestigatorAccessOutcome,
} from "./investigatorAccessPolicy";

export interface InvestigatorAccessProps {
  station: Row; incidents: Row[]; events: Row[]; snapshots: Row[];
  owner: boolean; identity: string; connectionId: string; connected: boolean;
  pending: boolean; perform: Perform; selectedIncidentId?: string;
}

export function InvestigatorAccess(props: InvestigatorAccessProps) {
  const parentSelection = props.selectedIncidentId ?? "";
  const [selection, setSelection] = useState({ parent: parentSelection, id: parentSelection });
  const incidentId = selection.parent === parentSelection ? selection.id : parentSelection;
  const [investigatorIdentity, setInvestigatorIdentity] = useState("");
  const [action, setAction] = useState<InvestigatorAccessAction>("grant");
  const [scope, setScope] = useState({ incidentId, ids: [] as string[], includeBaseline: false });
  const selectedEventIds = scope.incidentId === incidentId ? scope.ids : [];
  const includeBaseline = scope.incidentId === incidentId && scope.includeBaseline;
  const [consented, setConsented] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<InvestigatorAccessOutcome | null>(null);
  const inFlight = useRef(false);
  const context: InvestigatorAccessContext = { ...props, incidentId, investigatorIdentity, action, selectedEventIds, includeBaseline };
  const latest = useRef(context); latest.current = context;
  const review = investigatorAccessReview(context);
  const reviewFingerprint = review?.fingerprint ?? null;
  const verified = Boolean(review && consented === reviewFingerprint);
  useEffect(() => { setConsented(null); }, [reviewFingerprint]);
  const disabled = props.pending || busy;
  const incidents = realInvestigatorIncidents(props.station, props.incidents);
  const incident = incidents.find(row => row.id === incidentId);
  const linked = incident ? linkedInvestigatorEvents(incident, props.events) : [];
  const baseline = incident ? frozenInvestigatorBaseline(incident, props.snapshots) : undefined;
  const update = (next: Partial<typeof scope>) => {
    setConsented(null);
    setScope({ incidentId, ids: selectedEventIds, includeBaseline, ...next });
  };

  if (!props.owner || textValue(props.station.owner, "").toLowerCase() !== props.identity.toLowerCase()) return null;
  if (!investigatorOwnerMatches(context)) return <Panel title="Investigator evidence access" icon={<KeyRound size={16} />}><Empty title="Owner connection required">Reconnect with the authenticated owner before reviewing or changing evidence access.</Empty></Panel>;

  return <Panel id="airlock-investigator-access" title="Investigator evidence access" icon={<KeyRound size={16} />} aside={<Badge tone="quiet">Owner consent</Badge>}>
    <form className="al-form" onSubmit={async event => {
      event.preventDefault();
      if (inFlight.current || props.pending || !verified) return;
      inFlight.current = true; setBusy(true); setOutcome(null); setConsented(null);
      try { setOutcome(await changeInvestigatorAccess(latest.current, consented, props.perform)); }
      finally { inFlight.current = false; setBusy(false); }
    }}>
      <p className="al-small al-muted">Share one real incident with an existing investigator identity. Only selected event records and an optional frozen baseline are included.</p>
      <FormField label="Investigator public identity" hint="Enter the existing investigator's 64-character public identity. Keep private tokens out of this console.">
        <input required pattern="[a-fA-F0-9]{64}" maxLength={64} autoComplete="off" spellCheck={false} value={investigatorIdentity} disabled={disabled} onChange={event => { setConsented(null); setInvestigatorIdentity(event.target.value.trim()); }} placeholder="64 hexadecimal characters" />
      </FormField>
      <FormField label="Real incident" hint="The list contains current server records from this workstation. Synthetic incidents are excluded.">
        <select required value={incident?.id ?? ""} disabled={disabled} onChange={event => { setConsented(null); setSelection({ parent: parentSelection, id: event.target.value }); setScope({ incidentId: event.target.value, ids: [], includeBaseline: false }); }}>
          <option value="">Select an incident</option>
          {incidents.map(row => <option key={row.id} value={row.id}>{words(row.severity)} · {shortId(row.id)} · {words(row.state)}</option>)}
        </select>
      </FormField>
      {incident && <div className="al-form-note"><ShieldCheck size={17} /><div>
        <strong>Authoritative incident severity: {words(incident.severity)}</strong>
        <p>State: {words(incident.state)}. Workstation: {props.station.id}.</p>
        <code className="al-identity-full">{incident.id}</code><p>Session</p><code className="al-identity-full">{incident.sessionId}</code>
        <p>The incident's severity, state and scope identifiers are shared even when no event records are selected.</p>
      </div></div>}
      <FormField label="Permission change"><select value={action} disabled={disabled} onChange={event => { setConsented(null); setAction(event.target.value as InvestigatorAccessAction); }}>
        <option value="grant">Grant or replace this incident's evidence scope</option><option value="revoke">Revoke this incident's evidence access</option>
      </select></FormField>
      {incident && action === "grant" && <>
        <fieldset style={{ border: 0, padding: 0, margin: 0, display: "grid", gap: 12 }} disabled={disabled}>
          <legend className="al-small">Linked event records: {selectedEventIds.length} selected</legend>
          {linked.map(event => <div key={event.id}>
            <label className="al-checkbox"><input type="checkbox" checked={selectedEventIds.includes(event.id)} onChange={change => update({ ids: change.target.checked ? [...selectedEventIds, event.id] : selectedEventIds.filter(id => id !== event.id) })} /><span>{words(event.kind)} · {clock(event.observedAtMs)}<code className="al-identity-full">{event.id}</code></span></label>
            <JsonDetails label={`Review event ${shortId(event.id)}`} value={event} />
          </div>)}
          {!linked.length && <p className="al-small al-muted">No matching linked event records are available. You can approve incident status alone.</p>}
        </fieldset>
        {linkedInvestigatorEventIds(incident) === null && <p className="al-small al-text-amber" role="alert">The linked event list is invalid. A new grant cannot be reviewed; revocation remains available.</p>}
        {selectedEventIds.some(id => !linked.some(row => row.id === id)) && <p className="al-small al-text-amber" role="alert">Selected evidence changed or became unavailable. <button type="button" className="al-text-button" disabled={disabled} onClick={() => update({ ids: [] })}>Clear event selection</button></p>}
        <label className="al-checkbox"><input type="checkbox" checked={includeBaseline} disabled={disabled || (!baseline && !includeBaseline)} onChange={event => update({ includeBaseline: event.target.checked })} /><span>Include the frozen baseline from this incident's session.</span></label>
        {baseline ? <JsonDetails label={`Review frozen baseline ${baseline.id}`} value={{ id: baseline.id, workstationId: baseline.workstationId, deviceId: baseline.deviceId, sessionId: baseline.sessionId, observedAtMs: baseline.observedAtMs, receivedAtMs: baseline.receivedAtMs, baselineJson: baseline.baselineJson, activeAppId: baseline.activeAppId }} /> : <p className="al-small al-muted">The frozen incident-session baseline is unavailable. A current or unrelated baseline cannot be substituted.</p>}
        <p className="al-small al-muted">Granting again replaces the previous scope for this investigator and incident. Later linked events require a new review and grant.</p>
      </>}
      {incident && action === "revoke" && <p className="al-small al-text-amber">Revoke this investigator's evidence access to the displayed incident. Other incident grants require separate revocation.</p>}
      <label className="al-checkbox"><input type="checkbox" required checked={verified} disabled={disabled || !review} onChange={event => setConsented(event.target.checked ? reviewFingerprint : null)} /><span>I reviewed the workstation, investigator identity, incident and session, selected event records and baseline choice, and authorize this permission change.</span></label>
      <Button kind={action === "revoke" ? "warning" : "primary"} type="submit" disabled={disabled || !verified}>{busy ? "Waiting for server receipt…" : action === "grant" ? "Grant reviewed evidence access" : "Revoke incident evidence access"}</Button>
      {outcome && <div role="status" className="al-small">
        {outcome.status === "blocked" ? <p>Review the current scope before changing permission.</p> : <>
          <p>{outcome.status === "committed" ? `${outcome.review.action === "grant" ? "Grant change" : "Revocation"} committed by the server.` : "No commit receipt was confirmed. Permission state is unverified."}</p>
          <code className="al-identity-full">{outcome.review.incident.id}</code><p>Investigator</p><code className="al-identity-full">{outcome.review.investigatorIdentity}</code>
          {outcome.review.action === "grant" && <p>Requested scope: {outcome.review.events.length} event records; frozen baseline {outcome.review.baseline ? "included" : "excluded"}.</p>}
          {reviewFingerprint !== outcome.review.fingerprint && <p>The review changed during or after this request. Review the current evidence again before another change.</p>}
        </>}
      </div>}
      <p className="al-small al-muted">The server enforces owner authority and evidence scope. This panel cannot read existing grants or verify the investigator's principal. Confirm effective access with the M5 verifier after any change.</p>
    </form>
  </Panel>;
}
