// The native takeover, as this Mac's backend decides it (GET /api/security -> "takeover"). The dashboard only shows that decision and
// answers the native alert window's hand-off requests; it never decides on its own that something is critical.
import type { MacLink } from "./macSecurity";

export type ResponseState = { state: string; detail: string };
export type TakeoverEvidence = { kind: string; label: string; detail: string; at_ms: number | null; source: string; event_id?: string; severity?: string };
export type TakeoverRecord = {
  id: string; live: boolean; rehearsal: boolean; trigger: string; detected_at_ms: number; fresh: boolean;
  local_mode: string; local_mode_verified: boolean; headline: string; sentence: string; sentence_source: string;
  evidence: TakeoverEvidence[]; response: Record<"audio" | "tools" | "lock" | "monitoring" | "nemotron" | "evidence", ResponseState>;
  incident_id: string | null; acknowledged_at_ms: number | null; shown_at_ms: number | null; shown_latency_ms: number | null;
};
export type TakeoverHandoff = { nonce: string; view: "explain" | "evidence" | "stop"; takeover_id: string; incident_id: string | null; at_ms: number };
export type NativeClient = { connected: boolean; last_poll_ms: number | null; pid?: number | null; version?: string | null };
export type Takeover = { supported: boolean; armed: boolean; sessionId: string | null; record: TakeoverRecord | null; native: NativeClient | null; handoff: TakeoverHandoff | null };

/** null while this Mac's backend can't be read: then nothing is shown as current. supported=false means a backend without the native alert. */
export function readTakeover(mac: MacLink): Takeover | null {
  if (mac.status !== "live" || !mac.data) return null;
  const t = mac.data.takeover;
  if (!t || typeof t !== "object") return { supported: false, armed: Boolean(mac.data.armed), sessionId: null, record: null, native: null, handoff: null };
  const record = t.takeover && typeof t.takeover.id === "string" ? t.takeover as TakeoverRecord : null;
  return { supported: true, armed: Boolean(t.armed), sessionId: t.session_id ?? null, record, native: t.native ?? null, handoff: t.handoff ?? null };
}

/** Plain words and a tone for each response line. Tones only restate the backend's own state; nothing here upgrades an outcome. */
export const RESPONSE_ROWS: { key: keyof TakeoverRecord["response"]; label: string }[] = [
  { key: "audio", label: "Warning audio" }, { key: "lock", label: "Screen lock" }, { key: "tools", label: "LOCKI tools" },
  { key: "monitoring", label: "Monitoring" }, { key: "nemotron", label: "NVIDIA Nemotron" }, { key: "evidence", label: "Incident evidence" },
];
const WORD: Record<string, string> = {
  played: "Played", failed: "Failed", muted: "Muted", skipped: "Not repeated", pending: "Pending", unknown: "Unknown",
  frozen: "Frozen", not_frozen: "Not frozen", not_configured: "Not configured",
  not_locked: "Not locked", dry_run: "Dry run", requested: "Requested", executed: "Executed, unconfirmed", confirmed: "Locked (confirmed)", refused: "Refused",
  running: "Running", degraded: "Degraded", stopped: "Stopped",
  investigating: "Investigating", answered: "Answered", available: "Available", unavailable: "Unavailable",
  recorded: "Recorded", local_only: "Local record only",
};
const TONE: Record<string, "ok" | "warn" | "alert" | "muted"> = {
  played: "ok", failed: "alert", muted: "warn", skipped: "muted", pending: "muted", unknown: "muted",
  frozen: "ok", not_frozen: "muted", not_configured: "muted",
  not_locked: "muted", dry_run: "warn", requested: "warn", executed: "warn", confirmed: "ok", refused: "alert",
  running: "ok", degraded: "warn", stopped: "alert",
  investigating: "warn", answered: "ok", available: "ok", unavailable: "alert",
  recorded: "ok", local_only: "warn",
};
export const responseWord = (s: string) => WORD[s] ?? s.replace(/_/g, " ");
export const responseTone = (s: string) => TONE[s] ?? "muted";

/** The native alert window's state, for the sidebar and the takeover card. */
export function nativeStatus(t: Takeover | null, now: number): { tone: "ok" | "warn" | "muted"; label: string; detail: string } {
  if (!t) return { tone: "muted", label: "Unknown", detail: "This Mac's backend isn't answering, so the alert window's state is unknown." };
  if (!t.supported) return { tone: "warn", label: "Not supported", detail: "This backend predates the native alert window. Restart it from the updated checkout." };
  const last = Number(t.native?.last_poll_ms);
  if (t.native?.connected && Number.isFinite(last)) return { tone: "ok", label: "Running", detail: `LOCKI Alert.app is polling this Mac's backend (last ${Math.max(0, Math.round((now - last) / 1000))} s ago). A critical event covers the main display even when this tab is hidden.` };
  return { tone: "warn", label: "Not running", detail: "LOCKI Alert.app isn't polling this Mac's backend. A critical event will only show in this tab (degraded)." };
}

/** One hand-off per request, and never one replayed after this page reloads or a minute has passed. */
const HANDOFF_KEY = "airlock.takeover.handoff.v1";
export function readHandledHandoff(): string | null { try { return sessionStorage.getItem(HANDOFF_KEY); } catch { return null; } }
export function markHandoffHandled(nonce: string) { try { sessionStorage.setItem(HANDOFF_KEY, nonce); } catch { /* the in-memory ref still holds it */ } }
export const HANDOFF_MAX_AGE_MS = 60_000;
