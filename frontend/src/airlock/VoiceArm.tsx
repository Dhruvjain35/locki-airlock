import { useEffect, useState } from "react";
import { KeyRound, Laptop, LockKeyhole } from "lucide-react";
import { type Row, clock, parseJson, textValue, words } from "./format";
import { type Perform } from "./forms";
import { Badge, Button, FormField, Modal, Panel } from "./ui";
import { mayAuthorizeVoiceArm, voiceArmGrantState, voiceArmReview } from "./voiceArmConsent";

interface Props {
  station: Row; devices: Row[]; snapshots: Row[]; health: Row[]; grants: Row[];
  previous?: Row; now: number; connected: boolean; available: boolean;
  identity: string; connectionId: string; pending: boolean; perform: Perform;
}

function VoiceArmForm({ onClose, ...props }: Props & { onClose: () => void }) {
  const macs = props.devices.filter(d => d.workstationId === props.station.id && d.enabled && d.role === "mac");
  const [deviceId, setDeviceId] = useState(macs[0]?.id || "");
  const [apps, setApps] = useState((parseJson(props.previous?.approvedAppsJson, []) as string[]).join("\n"));
  const [duration, setDuration] = useState("15");
  const [consented, setConsented] = useState<string | null>(null);
  const device = macs.find(d => d.id === deviceId);
  const capture = props.snapshots.filter(b => b.deviceId === deviceId && b.sessionId === "")
    .sort((a, b) => Number(b.observedAtMs) - Number(a.observedAtMs))[0];
  const usb = props.health.find(h => h.deviceId === deviceId && h.component === "usb");
  const context = { station: props.station, device, capture, health: usb,
    apps: apps.split(/[\n,]/).map(app => app.trim()).filter(Boolean), durationMinutes: duration,
    now: props.now, connected: props.connected, available: props.available,
    identity: props.identity, connectionId: props.connectionId };
  const review = voiceArmReview(context);
  const fingerprint = review?.fingerprint ?? null;
  const checked = fingerprint !== null && consented === fingerprint;
  useEffect(() => { setConsented(null); }, [fingerprint]);

  return <Modal title="Authorize the next voice ARM" subtitle="Review one exact session for an enrolled Mac. This does not arm it yet." onClose={onClose}>
    <form className="al-form" onSubmit={async event => {
      event.preventDefault();
      const current = { ...context, now: Date.now() };
      if (!mayAuthorizeVoiceArm(consented, current)) return;
      const reviewed = voiceArmReview(current)!;
      const ok = await props.perform("authorizeArm", {
        workstationId: props.station.id, authorizationId: `voice-arm-${crypto.randomUUID()}`,
        deviceId: reviewed.deviceId, sessionId: `session-${crypto.randomUUID()}`,
        baselineJson: reviewed.baselineJson, activeAppId: reviewed.activeAppId,
        approvedAppsJson: reviewed.approvedAppsJson,
        expectedOwnerPolicyHash: props.station.ownerPolicyHash,
        durationMs: reviewed.durationMs, ttlMs: 30000n,
      }, "Authorization request committed. Check permission status below and keep this owner console connected.");
      if (ok) onClose();
    }}>
      <FormField label="Enrolled Mac" hint="Only this Mac's enrolled identity can use the authorization.">
        <select required value={deviceId} onChange={event => setDeviceId(event.target.value)}>
          {!macs.length && <option value="">No enabled Mac enrolled</option>}
          {macs.map(mac => <option key={mac.id} value={mac.id}>{mac.label} · {mac.id}</option>)}
        </select>
      </FormField>
      <div className="al-form-note"><Laptop size={17} /><div><strong>Target Mac capture</strong>
        <p>Active app: {capture?.activeAppId || "unavailable"}. Captured: {clock(capture?.observedAtMs)}.</p>
        <code className="al-identity-full">{device ? textValue(device.identity) : "No target identity"}</code>
      </div></div>
      <FormField label="Reviewed USB baseline" hint="The Mac must still have this exact inventory and active app when voice ARM is requested.">
        <textarea rows={3} readOnly value={review ? (JSON.parse(review.baselineJson) as string[]).join("\n") : ""}
          placeholder={review ? "No USB peripherals in the authenticated capture" : "Waiting for a valid fresh Mac capture and online USB health"} />
      </FormField>
      <FormField label="Approved app identifiers" hint="One per line. These are frozen for the authorized session.">
        <textarea rows={2} value={apps} onChange={event => setApps(event.target.value)} />
      </FormField>
      <FormField label="Session duration"><select value={duration} onChange={event => setDuration(event.target.value)}>
        <option value="1">1 minute</option><option value="15">15 minutes</option><option value="60">1 hour</option>
      </select></FormField>
      <div className="al-form-note"><LockKeyhole size={17} /><div><strong>Automatic screen lock must be disabled</strong>
        <p>Policy: {props.station.ownerPolicyHash}. Authorization lasts 30 seconds and can be used once.
          Voice cannot disarm. Keep this owner tab open and connected.</p>
      </div></div>
      {!review && <p className="al-text-amber" role="status">Authorization unavailable. Connect as the workstation owner,
        keep the workstation disarmed with auto-lock off, and obtain a fresh target Mac baseline with online USB health.</p>}
      <label className="al-checkbox"><input type="checkbox" checked={checked} disabled={!review}
        onChange={event => setConsented(event.target.checked ? fingerprint : null)} />
        <span>I reviewed this Mac, USB inventory, active app, approved apps and duration, and authorize one voice ARM with auto-lock off.</span>
      </label>
      <div className="al-modal-actions"><Button type="button" onClick={onClose}>Cancel</Button>
        <Button type="submit" kind="primary" disabled={props.pending || !checked || !review}>Authorize once · 30 seconds</Button>
      </div>
    </form>
  </Modal>;
}

export function VoiceArmAuthorization(props: Props) {
  const [open, setOpen] = useState(false);
  const grant = [...props.grants].sort((a, b) => Number(b.issuedAtMs) - Number(a.issuedAtMs))[0];
  const state = voiceArmGrantState(grant, props.connected && props.available, props.now);
  const seconds = grant ? Math.max(0, Math.ceil((Number(grant.expiresAtMs) - props.now) / 1000)) : 0;
  const thisConnection = grant && textValue(grant.ownerConnectionId) === props.connectionId;
  const authenticatedOwner = Boolean(props.identity) && textValue(props.station.owner, "") === props.identity;
  return <Panel title="Voice ARM permission" icon={<KeyRound size={16} />} aside={<Badge tone={state === "authorized" ? "amber" : "quiet"}>{words(state)}</Badge>}>
    {!props.connected ? <p role="status">Owner console disconnected. Authorization cannot be verified; reconnect and issue a new permission.</p>
      : !props.available ? <p role="status">Voice ARM authorization is unavailable on this database connection. The server contract must be deployed before use.</p>
      : state === "authorized" ? <p role="status">{grant.deviceId} can arm the reviewed session once within {seconds}s.
        {thisConnection ? " Keep this owner console open and connected." : " Keep the issuing owner console open and connected."}</p>
      : state === "consumed" ? <p>Permission used for session <code>{grant.sessionId}</code>. Confirm the committed armed state above.</p>
      : <p>{state === "expired" || state === "revoked" ? "The previous permission is no longer usable. " : ""}Authorize one reviewed session before asking the enrolled Mac to arm by voice. Voice disarming stays unavailable.</p>}
    <div className="al-inline">
      <Button disabled={!authenticatedOwner || !props.connected || !props.available || props.pending || Boolean(props.station.activeSessionId)
        || props.station.autoLockEnabled !== false} onClick={() => setOpen(true)}>Authorize next voice ARM</Button>
      {state === "authorized" && <Button disabled={props.pending} onClick={() => props.perform("revokeArmAuthorization",
        { workstationId: props.station.id, authorizationId: grant.id }, "Voice ARM permission revoked.")}>Revoke permission</Button>}
    </div>
    {props.station.autoLockEnabled && <p className="al-small al-text-amber">Disable automatic screen lock in Response policy before authorizing voice ARM.</p>}
    {open && <VoiceArmForm {...props} onClose={() => setOpen(false)} />}
  </Panel>;
}
