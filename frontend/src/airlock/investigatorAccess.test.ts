import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { Perform } from "./forms";
import { InvestigatorAccess } from "./InvestigatorAccess";
import {
  changeInvestigatorAccess, frozenInvestigatorBaseline, investigatorAccessReview, linkedInvestigatorEvents,
  mayChangeInvestigatorAccess, realInvestigatorIncidents, type InvestigatorAccessContext,
} from "./investigatorAccessPolicy";

const OWNER = "a".repeat(64), INVESTIGATOR = "b".repeat(64);

function context(): InvestigatorAccessContext {
  const event = {
    id: "usb-a", workstationId: "desk", sessionId: "session-a", deviceId: "mac-a", source: "mac",
    kind: "usb_added", observedAtMs: 100000n, receivedAtMs: 100001n, seq: 1n, confidence: 1,
    metadataJson: '{"deviceKey":"reviewed-key"}', simulated: false, schemaVersion: 1, classification: "unknown_usb",
  };
  return {
    station: { id: "desk", name: "Existing desk", owner: OWNER },
    incidents: [{ id: "incident:usb-a", workstationId: "desk", sessionId: "session-a", severity: "high",
      state: "open", createdAtMs: 100001n, updatedAtMs: 100002n, simulated: false, eventIdsJson: '["usb-a","camera-a"]', summary: "Owner-visible summary" }],
    events: [event, { ...event, id: "camera-a", kind: "person_in_zone", deviceId: "jetson-a", source: "jetson", seq: 2n },
      { ...event, id: "unlinked", seq: 3n }],
    snapshots: [{ id: "session:session-a", workstationId: "desk", sessionId: "session-a", deviceId: "mac-a",
      publisher: OWNER, observedAtMs: 99000n, receivedAtMs: 99001n, baselineJson: '["approved-key"]', activeAppId: "com.apple.Terminal" },
    { id: "latest:mac-a", workstationId: "desk", sessionId: "", deviceId: "mac-a", baselineJson: '["later-key"]' }],
    owner: true, identity: OWNER, connectionId: "owner-socket", connected: true, incidentId: "incident:usb-a",
    investigatorIdentity: INVESTIGATOR, selectedEventIds: ["usb-a"], includeBaseline: true, action: "grant",
  };
}

describe("owner incident evidence review", () => {
  it("builds only the existing incident-scoped grant arguments", () => {
    const review = investigatorAccessReview(context())!;
    expect(review.args).toEqual({ workstationId: "desk", identityHex: INVESTIGATOR, incidentId: "incident:usb-a", eventIdsJson: '["usb-a"]', includeBaseline: true });
    expect(review.events.map(event => event.id)).toEqual(["usb-a"]);
    expect(review.baseline?.id).toBe("session:session-a");
  });

  it.each([
    { connected: false }, { owner: false }, { identity: "c".repeat(64) }, { identity: "" }, { connectionId: "" },
  ])("requires the authenticated owner on a current socket: %j", change => {
    expect(investigatorAccessReview({ ...context(), ...change })).toBeNull();
  });

  it("matches SDK public identities without accepting membership alone", () => {
    const current = context(); current.station.owner = { toHexString: () => OWNER };
    expect(investigatorAccessReview(current)).not.toBeNull();
    current.station.owner = "c".repeat(64);
    expect(investigatorAccessReview(current)).toBeNull();
  });

  it.each(["", "not-an-identity", "b".repeat(63), "g".repeat(64), OWNER])("rejects invalid or owner investigator identity %s", investigatorIdentity => {
    expect(investigatorAccessReview({ ...context(), investigatorIdentity })).toBeNull();
  });

  it("selects only exact current real incidents in this workstation", () => {
    const current = context();
    current.incidents.push({ ...current.incidents[0], id: "incident:synthetic", simulated: true }, { ...current.incidents[0], id: "incident:foreign", workstationId: "other" });
    expect(realInvestigatorIncidents(current.station, current.incidents).map(row => row.id)).toEqual(["incident:usb-a"]);
    for (const incidentId of ["incident:synthetic", "incident:foreign", "incident:missing"]) {
      expect(investigatorAccessReview({ ...current, incidentId })).toBeNull();
    }
  });

  it("denies absent, synthetic and ambiguous incident incarnations", () => {
    for (const change of [{ simulated: true }, { simulated: undefined }, { sessionId: "" }, { severity: "" }]) {
      const current = context(); Object.assign(current.incidents[0], change);
      expect(investigatorAccessReview(current)).toBeNull();
    }
    const current = context(); current.incidents.push({ ...current.incidents[0] });
    expect(investigatorAccessReview(current)).toBeNull();
  });

  it("requires an explicit subset, rejecting unrelated and missing event IDs", () => {
    for (const selectedEventIds of [["unlinked"], ["missing"], [""], Array.from({ length: 257 }, (_, i) => `event-${i}`)]) {
      expect(investigatorAccessReview({ ...context(), selectedEventIds })).toBeNull();
    }
  });

  it("rejects selected events from another workstation, session or simulation provenance", () => {
    for (const change of [{ workstationId: "other" }, { sessionId: "other-session" }, { simulated: true }, { simulated: undefined }]) {
      const current = context(); Object.assign(current.events[0], change);
      expect(investigatorAccessReview(current)).toBeNull();
      expect(linkedInvestigatorEvents(current.incidents[0], current.events).map(row => row.id)).toEqual(["camera-a"]);
    }
    const current = context(); current.events.push({ ...current.events[0] });
    expect(investigatorAccessReview(current)).toBeNull();
  });

  it("permits minimal incident status with no events or baseline", () => {
    const current = context(); current.selectedEventIds = []; current.includeBaseline = false; current.events = []; current.snapshots = [];
    const review = investigatorAccessReview(current)!;
    expect(review.args.eventIdsJson).toBe("[]"); expect(review.args.includeBaseline).toBe(false);
    expect(review.events).toEqual([]); expect(review.baseline).toBeUndefined();
  });

  it("canonicalizes only the explicit selected IDs and never adds later linked events", () => {
    const current = context(); current.selectedEventIds = ["usb-a", "camera-a", "usb-a"];
    expect(investigatorAccessReview(current)?.args.eventIdsJson).toBe('["camera-a","usb-a"]');
    current.selectedEventIds = ["usb-a"];
    const previous = investigatorAccessReview(current)!;
    current.incidents[0].eventIdsJson = '["usb-a","camera-a","later"]';
    current.events.push({ ...current.events[0], id: "later", seq: 4n });
    expect(mayChangeInvestigatorAccess(previous.fingerprint, current)).toBe(false);
    expect(investigatorAccessReview(current)?.args.eventIdsJson).toBe('["usb-a"]');
  });

  it.each(["invalid", "null", "{}", '[42]', '[""]'])("fails closed for malformed linked lists %s", eventIdsJson => {
    const current = context(); current.incidents[0].eventIdsJson = eventIdsJson;
    expect(investigatorAccessReview(current)).toBeNull();
  });

  it("accepts only the exact frozen incident-session baseline", () => {
    for (const change of [{ id: "latest:mac-a" }, { workstationId: "other" }, { sessionId: "other-session" }, { baselineJson: "not-json" }]) {
      const current = context(); Object.assign(current.snapshots[0], change);
      expect(frozenInvestigatorBaseline(current.incidents[0], current.snapshots)).toBeUndefined();
      expect(investigatorAccessReview(current)).toBeNull();
      expect(investigatorAccessReview({ ...current, includeBaseline: false })).not.toBeNull();
    }
    const current = context(); current.snapshots.shift();
    expect(investigatorAccessReview(current)).toBeNull();
  });

  it("allows an empty frozen inventory and rejects duplicate frozen records", () => {
    const current = context(); current.snapshots[0].baselineJson = "[]";
    expect(investigatorAccessReview(current)?.baseline?.baselineJson).toBe("[]");
    current.snapshots.push({ ...current.snapshots[0] });
    expect(investigatorAccessReview(current)).toBeNull();
  });

  it("invalidates consent for changed authority, incident, selected content or baseline", () => {
    const consent = investigatorAccessReview(context())!.fingerprint;
    const changes: Array<(c: InvestigatorAccessContext) => void> = [
      c => { c.connectionId = "reconnected-owner"; },
      c => { c.station.id = "other"; },
      c => { c.station.owner = "c".repeat(64); },
      c => { c.investigatorIdentity = "d".repeat(64); },
      c => { c.incidents[0].severity = "medium"; },
      c => { c.incidents[0].state = "resolved"; },
      c => { c.incidents[0].sessionId = "replacement-session"; },
      c => { c.incidents[0].createdAtMs = 200000n; },
      c => { c.incidents[0].updatedAtMs = 200001n; },
      c => { c.incidents[0].eventIdsJson = '["usb-a"]'; },
      c => { c.events[0].metadataJson = '{"deviceKey":"replaced-key"}'; },
      c => { c.events[0].observedAtMs = 100003n; },
      c => { c.events[0].receivedAtMs = 100004n; },
      c => { c.events[0].seq = 2n; },
      c => { c.events[0].deviceId = "replacement-mac"; },
      c => { c.selectedEventIds = ["camera-a"]; },
      c => { c.includeBaseline = false; },
      c => { c.snapshots[0].baselineJson = '["new-key"]'; },
      c => { c.snapshots[0].activeAppId = "different.app"; },
      c => { c.snapshots[0].receivedAtMs = 99002n; },
      c => { c.action = "revoke"; },
    ];
    for (const change of changes) {
      const current = context(); change(current);
      expect(mayChangeInvestigatorAccess(consent, current)).toBe(false);
    }
    expect(mayChangeInvestigatorAccess(null, context())).toBe(false);
    expect(mayChangeInvestigatorAccess(consent, context())).toBe(true);
  });

  it("does not require evidence or a baseline to revoke the exact incident grant", () => {
    const current = context(); current.action = "revoke"; current.incidents[0].eventIdsJson = "unavailable"; current.events = []; current.snapshots = [];
    const review = investigatorAccessReview(current)!;
    expect(review.args).toEqual({ workstationId: "desk", identityHex: INVESTIGATOR, incidentId: "incident:usb-a" });
    expect(review.events).toEqual([]); expect(review.baseline).toBeUndefined();
  });
});

describe("permission change receipts", () => {
  it("does not call any reducer without exact fresh consent", async () => {
    const current = context(), perform = vi.fn<Perform>();
    expect(await changeInvestigatorAccess(current, null, perform)).toEqual({ status: "blocked" });
    const old = investigatorAccessReview(current)!.fingerprint;
    current.events[0].metadataJson = "{}";
    expect(await changeInvestigatorAccess(current, old, perform)).toEqual({ status: "blocked" });
    expect(perform).not.toHaveBeenCalled();
  });

  it("sends the exact reviewed replacement scope and reports only the awaited receipt", async () => {
    const current = context(), consent = investigatorAccessReview(current)!.fingerprint;
    let resolve!: (confirmed: boolean) => void;
    const perform = vi.fn<Perform>(() => new Promise<boolean>(done => { resolve = done; }));
    let settled = false;
    const result = changeInvestigatorAccess(current, consent, perform).then(outcome => { settled = true; return outcome; });
    expect(perform).toHaveBeenCalledExactlyOnceWith("grantIncidentEvidence", { workstationId: "desk", identityHex: INVESTIGATOR, incidentId: "incident:usb-a", eventIdsJson: '["usb-a"]', includeBaseline: true });
    await Promise.resolve(); expect(settled).toBe(false);
    current.selectedEventIds.push("camera-a"); current.incidents[0].severity = "medium";
    resolve(true);
    const outcome = await result;
    expect(outcome.status).toBe("committed");
    if (outcome.status !== "blocked") {
      expect(outcome.review.args.eventIdsJson).toBe('["usb-a"]');
      expect(outcome.review.incident.severity).toBe("high");
    }
  });

  it("uses only the deployed revoke reducer for the exact target", async () => {
    const current = context(); current.action = "revoke";
    const perform = vi.fn<Perform>().mockResolvedValue(true);
    expect((await changeInvestigatorAccess(current, investigatorAccessReview(current)!.fingerprint, perform)).status).toBe("committed");
    expect(perform).toHaveBeenCalledExactlyOnceWith("revokeIncidentEvidence", { workstationId: "desk", identityHex: INVESTIGATOR, incidentId: "incident:usb-a" });
  });

  it.each(["false", "throw"])("does not claim a commit when receipt is %s", async mode => {
    const current = context();
    const perform = vi.fn<Perform>(async () => { if (mode === "throw") throw new Error("transport disconnected"); return false; });
    expect((await changeInvestigatorAccess(current, investigatorAccessReview(current)!.fingerprint, perform)).status).toBe("unconfirmed");
  });
});

describe("owner access panel", () => {
  const markup = (change: Partial<InvestigatorAccessContext> = {}) => {
    const current = { ...context(), ...change };
    return renderToStaticMarkup(createElement(InvestigatorAccess, { ...current, pending: false, perform: vi.fn<Perform>(), selectedIncidentId: current.incidentId }));
  };

  it("renders no grant control for a reviewer or a different authenticated identity", () => {
    expect(markup({ owner: false })).toBe("");
    expect(markup({ identity: "c".repeat(64) })).toBe("");
  });

  it("offers no mutation while the owner connection is unavailable", () => {
    const html = markup({ connected: false });
    expect(html).toContain("Owner connection required");
    expect(html).not.toContain("Grant reviewed evidence access");
  });

  it("starts with an empty identity and no preselected evidence or baseline", () => {
    const html = markup();
    expect(html).toContain("Linked event records: 0 selected");
    expect(html).not.toContain(INVESTIGATOR);
    expect(html).not.toContain("checked=");
    expect(html).not.toContain("committed by the server");
    expect(html).toContain("Confirm effective access with the M5 verifier");
  });
});
