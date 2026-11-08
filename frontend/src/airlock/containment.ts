import { type Row, milliseconds, words } from "./format";
import { type MacLink, reportedStage } from "./macSecurity";
import { playbackLabel, warningPlayed } from "./protection";

/** An incident older than this never opens the full-screen view by itself: a reload, a reconnect or a replayed subscription shows it in
 *  the dashboard, never as a fresh takeover. */
export const TAKEOVER_FRESH_MS = 10 * 60_000;
/** The Mac's own unknown-USB CRITICAL can precede SpacetimeDB's incident row; it opens the view only while it is this recent. */
export const MAC_CRITICAL_FRESH_MS = 120_000;

export type ContainmentReason = "incident" | "mac_critical";
export interface ContainmentTrigger { key: string; reason: ContainmentReason; incident?: Row }

const toMs = (v: unknown): number => {
  const n = Number(v);
  if (!Number.isFinite(n) || n <= 0) return 0;
  return n < 1e11 ? n * 1000 : milliseconds(n);
};

/** When the CRITICAL containment view opens. Only a real (non-simulated) open incident of THIS workstation's live armed session, created
 *  recently, or this Mac's own CRITICAL stage in that same session. Never a person alone, an owner panic, a synthetic or old record, another
 *  session or workstation, or anything the owner already dismissed. Nothing here decides severity or authorizes any action. */
export function containmentTrigger(p: { incident?: Row; liveSessionId?: string; stationId?: string; mac: MacLink; now: number; dismissed: ReadonlySet<string> }): ContainmentTrigger | null {
  const { incident, liveSessionId, stationId, mac, now, dismissed } = p;
  if (!liveSessionId || !stationId) return null;
  if (incident && incident.simulated === false && incident.sessionId === liveSessionId && incident.workstationId === stationId
    && incident.state !== "resolved" && incident.ruleId !== "owner_panic") {
    const age = now - milliseconds(incident.createdAtMs);
    const key = String(incident.id);
    if (age >= -30_000 && age <= TAKEOVER_FRESH_MS && !dismissed.has(key)) return { key, reason: "incident", incident };
    if (dismissed.has(key)) return null;  // the owner already reviewed this incident: the Mac's matching CRITICAL doesn't reopen it
  }
  if (mac.status === "live" && reportedStage(mac) === "CRITICAL" && mac.data?.session?.session_id === liveSessionId) {
    const since = toMs(mac.data?.visual_state?.mode_since);
    const key = `critical:${liveSessionId}:${since}`;
    if (since && now - since >= -30_000 && now - since <= MAC_CRITICAL_FRESH_MS && !dismissed.has(key)) return { key, reason: "mac_critical" };
  }
  return null;
}

/** One factual sentence, worded only from the evidence actually linked: USB alone is only USB; a person alone is never an intrusion. */
export function containmentStatement(events: Row[], incident: Row | undefined, mac: MacLink): string {
  const kinds = new Set(events.map(e => String(e.kind)));
  const usb = kinds.has("usb_added");
  const person = kinds.has("person_in_zone");
  if (usb && person) return "Unknown USB device connected while a person was detected at the workstation.";
  if (usb) return "Unknown USB device connected to this workstation while protection was on.";
  if (kinds.has("app_launched")) return "A watched application was opened while protection was on.";
  if (incident?.summary) return String(incident.summary);
  const reason = String(mac.data?.visual_state?.mode_reason ?? "");
  if (reason.includes("usb")) return "This Mac's USB watcher reported an unknown device while protection was on.";
  return "LOCKI's local security stage is CRITICAL.";
}

export type Tone = "ok" | "warn" | "alert" | "muted";
export interface StatusLine { label: string; tone: Tone; detail: string }

/** macOS screen lock, exactly as the Mac's lock adapter reports it for this session: dry run, requested, executed and OS-confirmed are
 *  different things and are never merged. The browser view itself is never a lock. */
export function lockStatus(mac: MacLink): StatusLine {
  if (mac.status !== "live") return { label: "Unverified", tone: "muted", detail: "This Mac's backend isn't responding." };
  const mode = mac.data?.lock_mode;
  const sid = mac.data?.session?.session_id;
  const lock = sid ? (mac.data?.locks ?? []).filter((l: any) => l?.session_id === sid).slice(-1)[0] : undefined;
  if (!lock) return { label: "Not locked", tone: "muted", detail: mode === "dry_run" ? "Real screen locking is disabled for this configuration (dry run)." : "No screen lock was requested." };
  const status = String(lock.status ?? "");
  if (lock.dry_run === true || status === "LOCK_DRY_RUN") return { label: "Dry run", tone: "warn", detail: "A lock request was recorded as a dry run. The screen was not locked." };
  if (status === "LOCK_CONFIRMED") return { label: "Locked · OS confirmed", tone: "ok", detail: "macOS confirmed the screen is locked." };
  if (status === "LOCK_EXECUTED") return { label: "Lock executed", tone: "warn", detail: "Sent to macOS; waiting for confirmation." };
  if (status === "LOCK_REQUESTED") return { label: "Requested", tone: "warn", detail: "A screen lock was requested; not yet executed." };
  if (status === "LOCK_REFUSED" || status === "LOCK_FAILED") return { label: status === "LOCK_REFUSED" ? "Refused" : "Failed", tone: "alert", detail: "The screen was not locked." };
  return { label: words(status) || "Unknown", tone: "muted", detail: "Status reported by the Mac's lock adapter." };
}

/** LOCKI's protected-tool gate, exactly as the backend reports it. It only governs tools called through LOCKI's own gateway. */
export function gateStatus(mac: MacLink): StatusLine & { tools: string[] } {
  const g = mac.data?.gate;
  if (mac.status !== "live" || !g) return { label: "Unverified", tone: "muted", detail: "The protected-tool gate can't be read right now.", tools: [] };
  const tools: string[] = Array.isArray(g.tools) ? g.tools.map(String) : [];
  if (typeof g.frozen === "string" && g.frozen) return { label: "Frozen", tone: "ok", detail: `LOCKI-controlled tools are blocked: ${g.frozen}`, tools };
  const policy = mac.data?.session?.policy?.freeze_protected_tools === true;
  return { label: "Not enabled", tone: "muted", detail: policy ? "Policy allows a freeze, but the gate reports none." : "This session's policy doesn't freeze protected tools.", tools };
}

/** The newest spoken security warning of this session and what actually happened to its audio. */
export function warningAudio(mac: MacLink): StatusLine {
  if (mac.status !== "live") return { label: "Unverified", tone: "muted", detail: "This Mac's backend isn't responding." };
  const armedAt = Number(mac.data?.session?.armed_at_ms) || 0;
  const w = (mac.data?.warnings ?? []).filter((x: any) => Number(x?.at) * 1000 >= armedAt).slice(-1)[0];
  if (!w) return { label: "None yet", tone: "muted", detail: "No security warning has played this session." };
  if (warningPlayed(w)) return { label: "Played", tone: "ok", detail: w.audio?.source_label ? String(w.audio.source_label) : "Reached the speaker." };
  if (w.audio?.outcome) return { label: "Not played", tone: "alert", detail: playbackLabel(w).text.replace(/^Not played · /, "") };
  return { label: w.spoken ? "Queued" : "Not played", tone: w.spoken ? "warn" : "muted", detail: "No playback receipt yet." };
}

export type FlowStep = "protect" | "detect" | "contain" | "explain" | "recover";
export const FLOW: { id: FlowStep; label: string }[] = [
  { id: "protect", label: "Protect" }, { id: "detect", label: "Detect" }, { id: "contain", label: "Contain" }, { id: "explain", label: "Explain" }, { id: "recover", label: "Recover" },
];

/** Where the real run is in Protect → Detect → Contain → Explain → Recover, from authoritative state only. */
export function flowStep(p: { armed: boolean; stage: string | null; incident: boolean; explained: boolean; stoppedAfterIncident: boolean; sessionEvents: number }): FlowStep | null {
  if (!p.armed) return p.stoppedAfterIncident ? "recover" : null;
  if (p.incident) return p.explained ? "explain" : "contain";
  if (p.sessionEvents > 0 || (p.stage && p.stage !== "NORMAL")) return "detect";
  return "protect";
}
