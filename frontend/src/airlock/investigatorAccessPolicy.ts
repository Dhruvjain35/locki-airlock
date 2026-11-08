import { type Row, textValue } from "./format";
import type { Perform } from "./forms";

export type InvestigatorAccessAction = "grant" | "revoke";

export interface InvestigatorAccessContext {
  station: Row; incidents: Row[]; events: Row[]; snapshots: Row[];
  owner: boolean; identity: string; connectionId: string; connected: boolean;
  incidentId: string; investigatorIdentity: string; selectedEventIds: string[];
  includeBaseline: boolean; action: InvestigatorAccessAction;
}

export interface InvestigatorAccessReview {
  fingerprint: string; authorityFingerprint: string;
  action: InvestigatorAccessAction; args: Row;
  incident: Row; investigatorIdentity: string; events: Row[]; baseline?: Row;
}

export type InvestigatorAccessOutcome =
  | { status: "blocked" }
  | { status: "committed" | "unconfirmed"; review: InvestigatorAccessReview };

const validId = (value: unknown): value is string => typeof value === "string" && /^[a-zA-Z0-9_.:-]{1,128}$/.test(value);
const publicIdentity = (value: string) => /^[a-fA-F0-9]{64}$/.test(value) ? value.toLowerCase() : "";
const uniqueRow = (rows: Row[], id: string): Row | undefined => {
  const matches = rows.filter(row => row.id === id);
  return matches.length === 1 ? matches[0] : undefined;
};

/** A fingerprint stays local. Include row contents, not only reusable record IDs. */
function fingerprint(value: unknown): string {
  const normalized = (item: any): any => {
    if (typeof item === "bigint") return { u64: item.toString() };
    if (item && typeof item.toHexString === "function") return item.toHexString();
    if (Array.isArray(item)) return item.map(normalized);
    if (item && typeof item === "object") return Object.fromEntries(Object.keys(item).sort().map(key => [key, normalized(item[key])]));
    return item;
  };
  return JSON.stringify(normalized(value));
}

export function investigatorOwnerMatches(context: Pick<InvestigatorAccessContext, "station" | "owner" | "identity" | "connectionId" | "connected">): boolean {
  return context.owner === true && context.connected === true && Boolean(context.connectionId)
    && validId(context.station.id) && Boolean(publicIdentity(context.identity))
    && textValue(context.station.owner, "").toLowerCase() === context.identity.toLowerCase();
}

export function realInvestigatorIncidents(station: Row, incidents: Row[]): Row[] {
  return incidents.filter(incident => incident.workstationId === station.id && incident.simulated === false
    && validId(incident.id) && validId(incident.sessionId)
    && typeof incident.severity === "string" && incident.severity.length > 0
    && typeof incident.state === "string" && incident.state.length > 0);
}

export function linkedInvestigatorEventIds(incident: Row): string[] | null {
  try {
    const ids: unknown = JSON.parse(incident.eventIdsJson);
    if (!Array.isArray(ids) || ids.length > 256 || ids.some(id => !validId(id))) return null;
    return [...new Set(ids)].sort();
  } catch { return null; }
}

export function linkedInvestigatorEvents(incident: Row, events: Row[]): Row[] {
  return (linkedInvestigatorEventIds(incident) ?? []).flatMap(id => {
    const event = uniqueRow(events, id);
    return event && event.workstationId === incident.workstationId && event.sessionId === incident.sessionId
      && event.simulated === false ? [event] : [];
  });
}

export function frozenInvestigatorBaseline(incident: Row, snapshots: Row[]): Row | undefined {
  const baseline = uniqueRow(snapshots, `session:${incident.sessionId}`);
  if (!baseline || baseline.workstationId !== incident.workstationId || baseline.sessionId !== incident.sessionId) return undefined;
  try {
    const keys: unknown = JSON.parse(baseline.baselineJson);
    if (!Array.isArray(keys) || keys.length > 256 || keys.some(key => typeof key !== "string" || !key || key.length > 256)) return undefined;
  } catch { return undefined; }
  return baseline;
}

/** Build only the existing evidence reducers' arguments; this never creates a role. */
export function investigatorAccessReview(context: InvestigatorAccessContext): InvestigatorAccessReview | null {
  if (!investigatorOwnerMatches(context) || !["grant", "revoke"].includes(context.action)) return null;
  const investigatorIdentity = publicIdentity(context.investigatorIdentity);
  if (!investigatorIdentity || investigatorIdentity === context.identity.toLowerCase()) return null;
  const incident = uniqueRow(realInvestigatorIncidents(context.station, context.incidents), context.incidentId);
  if (!incident) return null;
  const authorityFingerprint = fingerprint([context.identity, context.connectionId, context.station.id, textValue(context.station.owner, "")]);
  const args: Row = { workstationId: context.station.id, identityHex: investigatorIdentity, incidentId: incident.id };
  let selectedEvents: Row[] = [];
  let baseline: Row | undefined;
  if (context.action === "grant") {
    const linkedIds = linkedInvestigatorEventIds(incident);
    if (!linkedIds || !Array.isArray(context.selectedEventIds) || context.selectedEventIds.length > 256
      || context.selectedEventIds.some(id => !validId(id)) || typeof context.includeBaseline !== "boolean") return null;
    const ids = [...new Set(context.selectedEventIds)].sort();
    const available = linkedInvestigatorEvents(incident, context.events);
    for (const id of ids) {
      const event = available.find(row => row.id === id);
      if (!event || !linkedIds.includes(id)) return null;
      selectedEvents.push({ ...event });
    }
    if (context.includeBaseline) {
      baseline = frozenInvestigatorBaseline(incident, context.snapshots);
      if (!baseline) return null;
      baseline = { ...baseline };
    }
    args.eventIdsJson = JSON.stringify(ids);
    args.includeBaseline = context.includeBaseline;
  }
  return {
    fingerprint: fingerprint([authorityFingerprint, context.action, args, incident, selectedEvents, baseline ?? null]),
    authorityFingerprint, action: context.action, args, incident: { ...incident }, investigatorIdentity,
    events: selectedEvents, baseline,
  };
}

export function mayChangeInvestigatorAccess(consented: string | null, context: InvestigatorAccessContext): boolean {
  const review = investigatorAccessReview(context);
  return Boolean(review && consented === review.fingerprint);
}

/** A sent request or network failure is not a commit receipt. */
export async function changeInvestigatorAccess(context: InvestigatorAccessContext, consented: string | null, perform: Perform): Promise<InvestigatorAccessOutcome> {
  const review = investigatorAccessReview(context);
  if (!review || consented !== review.fingerprint) return { status: "blocked" };
  try {
    const confirmed = await perform(review.action === "grant" ? "grantIncidentEvidence" : "revokeIncidentEvidence", { ...review.args });
    return { status: confirmed === true ? "committed" : "unconfirmed", review };
  } catch { return { status: "unconfirmed", review }; }
}
