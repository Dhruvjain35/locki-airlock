import { type Row, milliseconds } from "./format";

// Live session vs history. The live view shows only what belongs to the session SpacetimeDB says is active right now; everything
// else is history: kept, labelled with its own session and time, and never shown as a fresh detection. Nothing here edits a record.

/** The workstation's active session, only while SpacetimeDB says it is armed and it has not expired. */
export function liveSession(sessions: Row[], activeSessionId: unknown, now: number): Row | undefined {
  const s = sessions.find(x => x.id === activeSessionId);
  return s && s.state === "armed" && milliseconds(s.expiresAtMs) > now ? s : undefined;
}

/** Rows that carry this session's id, and the rest. No session: nothing is current. */
export function bySession<T extends Row>(rows: T[], sessionId: unknown): { current: T[]; history: T[] } {
  if (typeof sessionId !== "string" || !sessionId) return { current: [], history: rows };
  const current: T[] = [], history: T[] = [];
  for (const r of rows) (r.sessionId === sessionId ? current : history).push(r);
  return { current, history };
}

export type PastSession = { session: Row; events: number; incidents: number; open: number; ended: number | null };

/** Every session other than the live one, newest first, with how much evidence each holds. */
export function pastSessions(sessions: Row[], events: Row[], incidents: Row[], liveId: unknown): PastSession[] {
  return sessions.filter(s => s.id !== liveId).sort((a, b) => milliseconds(b.armedAtMs) - milliseconds(a.armedAtMs)).map(s => {
    const own = incidents.filter(i => i.sessionId === s.id);
    const disarmed = milliseconds(s.disarmedAtMs);
    return { session: s, events: events.filter(e => e.sessionId === s.id).length, incidents: own.length, open: own.filter(i => i.state !== "resolved").length,
      ended: disarmed > 0 ? disarmed : milliseconds(s.expiresAtMs) || null };
  });
}

/** The incident to show: the explicitly chosen one if it is in view, else the newest open one in view, else the newest in view. */
export function pickIncident(inView: Row[], chosenId: string): Row | undefined {
  const sorted = [...inView].sort((a, b) => milliseconds(b.createdAtMs) - milliseconds(a.createdAtMs));
  return sorted.find(i => i.id === chosenId) || sorted.find(i => i.state !== "resolved") || sorted[0];
}

/** The Mac backend's own armed session: id and arm time, or null when the Mac isn't armed. */
export function macSession(mac: any): { id: string; armedAtMs: number } | null {
  const s = mac?.session;
  const at = Number(s?.armed_at_ms);
  return mac?.armed === true && typeof s?.session_id === "string" && Number.isFinite(at) ? { id: s.session_id, armedAtMs: at } : null;
}

/** Mac receipts that happened in its current session (times in ms, or seconds when `seconds`). None when not armed. */
export function sinceArmed<T>(items: T[] | undefined, armedAtMs: number | null | undefined, at: (x: T) => number): T[] {
  if (!armedAtMs || !Array.isArray(items)) return [];
  return items.filter(x => { const t = at(x); return Number.isFinite(t) && t >= armedAtMs; });
}

/** The Jetson observation, only if the server received it during the Mac's current session. */
export function remoteInSession(remote: any, armedAtMs: number | null | undefined): boolean {
  const t = Number(remote?.server_received_at_ms);
  return Boolean(remote && armedAtMs && Number.isFinite(t) && t >= armedAtMs);
}

/** The newest voice turn whose Nemotron run was about one of these incidents. */
export function turnForIncidents(turns: any[] | undefined, incidentIds: Set<string>): any | null {
  for (const t of turns ?? []) {
    const id = t?.nemotron?.incident_id;
    if (typeof id === "string" && incidentIds.has(id)) return t;
  }
  return null;
}
