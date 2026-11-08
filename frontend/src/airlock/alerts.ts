import { type Row, milliseconds } from "./format";

/** Severity order, for "alert again only when it gets worse". */
export const SEVERITY_RANK: Record<string, number> = { info: 0, low: 1, medium: 2, high: 3, critical: 4 };
export const ALERT_MAX_AGE_MS = 10 * 60_000;

/** The incidents to alert on now: open, recent (created or escalated within ALERT_MAX_AGE_MS, so loading the console never replays
 *  history), and either never alerted or more severe than when they were. One alert per incident otherwise. */
export function incidentsToAlert(incidents: Row[], alerted: Record<string, number>, now: number, maxAgeMs = ALERT_MAX_AGE_MS): Row[] {
  return incidents.filter(i => {
    if (i.state === "resolved") return false;
    const recent = now - Math.max(milliseconds(i.createdAtMs), milliseconds(i.updatedAtMs)) <= maxAgeMs;
    const rank = SEVERITY_RANK[String(i.severity)] ?? 0;
    return recent && (alerted[String(i.id)] === undefined || rank > alerted[String(i.id)]);
  }).sort((a, b) => milliseconds(b.updatedAtMs) - milliseconds(a.updatedAtMs));
}

export function acknowledged(incident: Row, acknowledgements: Row[]): boolean {
  return Boolean(incident.acknowledgedBy && String(incident.acknowledgedBy).length) || acknowledgements.some(a => a.incidentId === incident.id && a.kind === "acknowledged");
}

/** What the alert says: severity, time, workstation, incident id and the module's own summary. No guesses about intent. */
export function alertText(incident: Row, workstation: string): { title: string; body: string } {
  const sev = String(incident.severity || "unknown").toUpperCase();
  const at = new Date(milliseconds(incident.updatedAtMs) || milliseconds(incident.createdAtMs)).toLocaleTimeString();
  const synthetic = incident.simulated ? " (synthetic rehearsal)" : "";
  return { title: `${sev} security incident · ${workstation}${synthetic}`, body: `${at} · ${incident.summary || incident.ruleId} · ${incident.id}` };
}
