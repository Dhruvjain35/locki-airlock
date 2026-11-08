import { type Row, milliseconds, parseJson, words } from "./format";
import { type ArmConsent, armConsent } from "./armConsent";
import { frozenInvestigatorBaseline, linkedInvestigatorEvents } from "./investigatorAccessPolicy";

/** Mustafa's read-only investigator. A public SpacetimeDB identity, never a secret. Overridable at build time. */
export const INVESTIGATOR_IDENTITY: string = (() => {
  const configured = String((import.meta as any).env?.VITE_LOCKI_INVESTIGATOR_IDENTITY ?? "").trim().toLowerCase();
  return /^[a-f0-9]{64}$/.test(configured) ? configured : "c200b5b1765c4d0dbc9951485cfe94e3b47977725051b23037df5b6043e88883";
})();

/** The question the Explain button asks. It goes through the Mac's typed-command route, exactly like a spoken question. */
export const EXPLAIN_QUESTION = "LOCKI, what happened while I was gone?";

export type ProtectionState = "UNVERIFIED" | "OFF" | "PROTECTED" | "INVESTIGATING" | "ATTENTION";

/** The hero word, from authoritative state only: the owner console's SpacetimeDB subscription and the current session's incidents. */
export function protectionState(p: { connected: boolean; owner: boolean; armed: boolean; incidentOpen: boolean; investigating: boolean }): ProtectionState {
  if (!p.connected || !p.owner) return "UNVERIFIED";
  if (!p.armed) return "OFF";
  if (p.investigating) return "INVESTIGATING";
  return p.incidentOpen ? "ATTENTION" : "PROTECTED";
}

export type StartBlocker = "autolock" | "no_capture" | "stale_capture" | "invalid_capture";
export interface StartReview { capture?: Row; ageMs: number; fresh: boolean; apps: string[]; keys: string[]; reviewed: ArmConsent | null; blockers: StartBlocker[] }

const asList = (value: unknown): string[] => {
  const parsed = parseJson(value, []);
  return Array.isArray(parsed) ? [...new Set(parsed.filter((v: unknown): v is string => typeof v === "string" && v.length > 0))] : [];
};

/** Start Protection review: the enrolled Mac's newest pre-session capture (the same one the live arm path freezes), approved apps carried
 *  from the newest LIVE session (never a synthetic one), and every reason the server would refuse a live arm, so the button never lies. */
export function startReview(station: Row, snapshots: Row[], sessions: Row[], duration: string, now: number): StartReview {
  const capture = [...snapshots].filter(s => !s.sessionId && s.workstationId === station.id)
    .sort((a, b) => milliseconds(b.observedAtMs) - milliseconds(a.observedAtMs))[0];
  const ageMs = capture ? now - milliseconds(capture.observedAtMs) : Number.POSITIVE_INFINITY;
  const fresh = Boolean(capture) && ageMs >= -5000 && ageMs <= 30_000;
  const previousLive = [...sessions].filter(s => s.simulated === false).sort((a, b) => milliseconds(b.armedAtMs) - milliseconds(a.armedAtMs))[0];
  const apps = asList(previousLive?.approvedAppsJson).sort();
  const reviewed = armConsent(station, capture, apps, duration);
  const keys = reviewed ? (JSON.parse(reviewed.baselineJson) as string[]) : [];
  const blockers: StartBlocker[] = [];
  if (station.autoLockEnabled) blockers.push("autolock");
  if (!capture) blockers.push("no_capture");
  else if (!fresh) blockers.push("stale_capture");
  else if (!reviewed) blockers.push("invalid_capture");
  return { capture, ageMs, fresh, apps, keys, reviewed, blockers };
}

const EVIDENCE_KINDS = new Set(["incident_evidence_granted", "incident_evidence_revoked"]);

function auditInvestigator(row: Row): string {
  if (row.kind === "incident_evidence_revoked") return String(row.detail ?? "").trim().toLowerCase();
  const detail = parseJson(row.detail, {});
  return typeof detail?.investigator === "string" ? detail.investigator.toLowerCase() : "";
}

/** Whether the owner's investigator grant for one incident is in force, from the owner-visible audit trail (newest record wins). The owner
 *  cannot read the investigator's own grant table, so this is the only evidence the console has; it never assumes a grant. */
export function evidenceGrantState(audit: Row[], incidentId: string, investigator = INVESTIGATOR_IDENTITY): "granted" | "revoked" | "none" {
  const rows = audit.filter(a => EVIDENCE_KINDS.has(a.kind) && a.entityId === incidentId && auditInvestigator(a) === investigator.toLowerCase())
    .sort((a, b) => milliseconds(b.createdAtMs) - milliseconds(a.createdAtMs) || Number(BigInt(b.id ?? 0) - BigInt(a.id ?? 0)));
  if (!rows.length) return "none";
  return rows[0].kind === "incident_evidence_granted" ? "granted" : "revoked";
}

export function grantApprovedAt(audit: Row[], incidentId: string, investigator = INVESTIGATOR_IDENTITY): number | null {
  const row = audit.filter(a => a.kind === "incident_evidence_granted" && a.entityId === incidentId && auditInvestigator(a) === investigator.toLowerCase())
    .sort((a, b) => milliseconds(b.createdAtMs) - milliseconds(a.createdAtMs))[0];
  return row ? milliseconds(row.createdAtMs) : null;
}

/** The incidents of one session whose investigator access is still in force: exactly what Stop Protection revokes. */
export function grantedIncidentIds(audit: Row[], incidents: Row[], sessionId: string | undefined, investigator = INVESTIGATOR_IDENTITY): string[] {
  if (!sessionId) return [];
  return incidents.filter(i => i.sessionId === sessionId).map(i => String(i.id)).filter(id => evidenceGrantState(audit, id, investigator) === "granted");
}

/** What the investigator would be allowed to read for this incident: its own linked, real, same-session events and the session's frozen
 *  USB baseline. Nothing else is offered. */
export function explainScope(incident: Row, events: Row[], snapshots: Row[]): { eventIds: string[]; events: Row[]; baseline?: Row } {
  const linked = linkedInvestigatorEvents(incident, events).sort((a, b) => milliseconds(a.observedAtMs) - milliseconds(b.observedAtMs));
  return { eventIds: linked.map(e => String(e.id)).sort(), events: linked, baseline: frozenInvestigatorBaseline(incident, snapshots) };
}

/** Why Explain can't run right now, or null when it can. The Mac answers about its own current incident in its own armed session, so all
 *  three must agree with the owner console. */
export function explainBlocker(incident: Row | undefined, liveSessionId: string | undefined, mac: { status: string; data: any }): string | null {
  if (!incident) return "There is no incident in this session.";
  if (incident.simulated) return "Synthetic incidents are never sent to the investigator.";
  if (!liveSessionId || incident.sessionId !== liveSessionId) return "This incident isn't from the current protected session.";
  if (mac.status !== "live") return "This Mac's security backend isn't responding.";
  const macIncident = mac.data?.incident;
  if (macIncident?.incident_id !== incident.id) return "This Mac hasn't reported this incident yet.";
  if (mac.data?.session?.session_id !== liveSessionId) return "This Mac isn't on the current session yet.";
  return null;
}

export interface EventView { title: string; source: string; detail: string; tone: "person" | "usb" | "app" | "camera" | "other" }

/** A plain description of one real device event. Only fields the event actually carries are shown. */
export function describeEvent(e: Row): EventView {
  const kind = String(e.kind ?? "");
  const meta = parseJson(e.metadataJson, {});
  const device = String(e.deviceId ?? "");
  const source = device.startsWith("jetson") ? `Jetson camera · ${device}` : device.startsWith("mac") ? "This Mac" : device || "Unknown source";
  const confidence = typeof e.confidence === "number" && e.confidence > 0 && e.confidence <= 1 ? `${Math.round(e.confidence * 100)}% confidence` : "";
  const zone = typeof meta?.zone === "string" && meta.zone ? `${words(meta.zone)} zone` : "";
  const classification = typeof e.classification === "string" && e.classification ? words(e.classification) : "";
  const join = (...parts: string[]) => parts.filter(Boolean).join(" · ");
  switch (kind) {
    case "person_in_zone": return { title: "Person detected at the desk", source, detail: join(zone, confidence), tone: "person" };
    case "person_cleared": return { title: "Desk clear", source, detail: zone, tone: "person" };
    case "usb_added": return { title: "USB device connected", source, detail: join(classification, typeof meta?.label === "string" ? meta.label : ""), tone: "usb" };
    case "usb_removed": return { title: "USB device removed", source, detail: classification, tone: "usb" };
    case "app_launched": return { title: "App launched", source, detail: join(typeof meta?.appId === "string" ? meta.appId : "", classification), tone: "app" };
    case "camera_offline": return { title: "Camera went offline", source, detail: "", tone: "camera" };
    case "camera_online": return { title: "Camera back online", source, detail: "", tone: "camera" };
    default: return { title: words(kind) || "Observation", source, detail: classification, tone: "other" };
  }
}

/** What actually happened to a spoken warning, from the Mac's own playback receipt. */
export function playbackLabel(w: any): { text: string; ok: boolean } {
  const audio = w?.audio;
  if (audio?.outcome === "finished") return { text: `Played${audio.source_label ? ` · ${audio.source_label}` : ""}`, ok: true };
  if (audio?.outcome) return { text: words(audio.outcome), ok: false };
  if (w?.skipped) return { text: "Not repeated", ok: false };
  return { text: w?.spoken ? "Queued for playback" : "Not played", ok: false };
}

export function countdown(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
}
