import { describe, expect, it } from "vitest";
import { INVESTIGATOR_IDENTITY, describeEvent, evidenceGrantState, explainBlocker, explainScope, grantedIncidentIds, openGrantIncidentIds, playbackLabel, protectionState, startReview } from "./protection";

const NOW = 1_794_150_000_000;
const OWNER = "c200bf80129f645f49c762279a175c3c3e84cf56af437c06191a237ac3eac422";
const station = { id: "dhruv-m4-air", owner: OWNER, ownerPolicyHash: "abc", policyVersion: 3, autoLockEnabled: false, lockCooldownMs: 0 };
const capture = (ageMs: number, extra = {}) => ({ id: "cap", workstationId: "dhruv-m4-air", sessionId: "", deviceId: "mac-707bf6209e27", publisher: "c200b00c", baselineJson: JSON.stringify(["k2", "k1"]), observedAtMs: BigInt(NOW - ageMs), ...extra });

describe("protectionState", () => {
  it("is unverified unless the owner console is connected", () => {
    expect(protectionState({ connected: false, owner: true, armed: true, incidentOpen: true, investigating: false })).toBe("UNVERIFIED");
    expect(protectionState({ connected: true, owner: false, armed: true, incidentOpen: false, investigating: false })).toBe("UNVERIFIED");
  });
  it("follows the session and its incidents", () => {
    const base = { connected: true, owner: true };
    expect(protectionState({ ...base, armed: false, incidentOpen: true, investigating: true })).toBe("OFF");
    expect(protectionState({ ...base, armed: true, incidentOpen: false, investigating: false })).toBe("PROTECTED");
    expect(protectionState({ ...base, armed: true, incidentOpen: true, investigating: false })).toBe("ATTENTION");
    expect(protectionState({ ...base, armed: true, incidentOpen: true, investigating: true })).toBe("INVESTIGATING");
  });
});

describe("startReview", () => {
  it("uses the newest pre-session capture and the newest live session's apps, never a synthetic session's", () => {
    const sessions = [
      { id: "s-syn", simulated: true, armedAtMs: BigInt(NOW - 1000), approvedAppsJson: JSON.stringify(["synthetic.app"]) },
      { id: "s-live", simulated: false, armedAtMs: BigInt(NOW - 9000), approvedAppsJson: JSON.stringify(["com.apple.Terminal"]) },
    ];
    const r = startReview(station, [capture(40_000, { id: "old" }), capture(3000), capture(1000, { sessionId: "session:x" })], sessions, "60", NOW);
    expect(r.capture?.id).toBe("cap");
    expect(r.fresh).toBe(true);
    expect(r.apps).toEqual(["com.apple.Terminal"]);
    expect(r.keys).toEqual(["k1", "k2"]);
    expect(r.blockers).toEqual([]);
    expect(r.reviewed?.fingerprint).toContain("dhruv-m4-air");
  });
  it("blocks a stale, future or missing capture and enabled auto-lock", () => {
    expect(startReview(station, [capture(31_000)], [], "60", NOW).blockers).toEqual(["stale_capture"]);
    expect(startReview(station, [capture(-6000)], [], "60", NOW).blockers).toEqual(["stale_capture"]);
    expect(startReview(station, [], [], "60", NOW).blockers).toEqual(["no_capture"]);
    expect(startReview({ ...station, autoLockEnabled: true }, [capture(1000)], [], "60", NOW).blockers).toEqual(["autolock"]);
    expect(startReview(station, [capture(1000, { baselineJson: "not json" })], [], "60", NOW).blockers).toEqual(["invalid_capture"]);
  });
  it("changes the consent fingerprint when the baseline or duration changes", () => {
    const a = startReview(station, [capture(1000)], [], "60", NOW).reviewed!.fingerprint;
    expect(startReview(station, [capture(1000, { baselineJson: JSON.stringify(["k1", "k3"]) })], [], "60", NOW).reviewed!.fingerprint).not.toBe(a);
    expect(startReview(station, [capture(1000)], [], "15", NOW).reviewed!.fingerprint).not.toBe(a);
  });
});

describe("evidence grants from the audit trail", () => {
  const inv = INVESTIGATOR_IDENTITY;
  const granted = (id: number, at: number, incident = "incident:a", who = inv) => ({ id: BigInt(id), kind: "incident_evidence_granted", entityId: incident, detail: JSON.stringify({ investigator: who, scope: "x" }), createdAtMs: BigInt(at) });
  const revoked = (id: number, at: number, incident = "incident:a", who = inv) => ({ id: BigInt(id), kind: "incident_evidence_revoked", entityId: incident, detail: who, createdAtMs: BigInt(at) });
  it("newest record wins, per incident and investigator", () => {
    expect(evidenceGrantState([], "incident:a")).toBe("none");
    expect(evidenceGrantState([granted(1, 10)], "incident:a")).toBe("granted");
    expect(evidenceGrantState([granted(1, 10), revoked(2, 20)], "incident:a")).toBe("revoked");
    expect(evidenceGrantState([granted(1, 10), revoked(2, 20), granted(3, 30)], "incident:a")).toBe("granted");
    expect(evidenceGrantState([granted(1, 10), revoked(2, 10)], "incident:a")).toBe("revoked");
    expect(evidenceGrantState([granted(1, 10, "incident:b")], "incident:a")).toBe("none");
    expect(evidenceGrantState([granted(1, 10, "incident:a", "f".repeat(64))], "incident:a")).toBe("none");
  });
  it("Stop revokes only this session's granted incidents", () => {
    const incidents = [{ id: "incident:a", sessionId: "s1" }, { id: "incident:b", sessionId: "s1" }, { id: "incident:c", sessionId: "s0" }];
    const audit = [granted(1, 10, "incident:a"), granted(2, 11, "incident:b"), revoked(3, 12, "incident:b"), granted(4, 13, "incident:c")];
    expect(grantedIncidentIds(audit, incidents, "s1")).toEqual(["incident:a"]);
    expect(grantedIncidentIds(audit, incidents, undefined)).toEqual([]);
  });
  it("finds every still-open grant across sessions, so none outlives its session unnoticed", () => {
    const incidents = [{ id: "incident:a", sessionId: "s1" }, { id: "incident:b", sessionId: "s1" }, { id: "incident:c", sessionId: "s0" }];
    const audit = [granted(1, 10, "incident:a"), granted(2, 11, "incident:b"), revoked(3, 12, "incident:b"), granted(4, 13, "incident:c")];
    expect(openGrantIncidentIds(audit, incidents)).toEqual(["incident:a", "incident:c"]);
  });
});

describe("explain", () => {
  const incident = { id: "incident:abc", workstationId: "dhruv-m4-air", sessionId: "s1", simulated: false, eventIdsJson: JSON.stringify(["e2", "e1"]) };
  const mac = (incidentId = "incident:abc", session = "s1") => ({ status: "live", data: { incident: { incident_id: incidentId }, session: { session_id: session } } });
  it("scope is only the incident's own linked, real, same-session events plus the frozen baseline", () => {
    const events = [
      { id: "e1", workstationId: "dhruv-m4-air", sessionId: "s1", simulated: false, observedAtMs: 2n },
      { id: "e2", workstationId: "dhruv-m4-air", sessionId: "s1", simulated: false, observedAtMs: 1n },
      { id: "e3", workstationId: "dhruv-m4-air", sessionId: "s1", simulated: false, observedAtMs: 3n },
      { id: "e4", workstationId: "dhruv-m4-air", sessionId: "s0", simulated: false, observedAtMs: 4n },
    ];
    const snapshots = [{ id: "session:s1", workstationId: "dhruv-m4-air", sessionId: "s1", baselineJson: JSON.stringify(["k1"]) }];
    const scope = explainScope(incident, events, snapshots);
    expect(scope.eventIds).toEqual(["e1", "e2"]);
    expect(scope.events.map(e => e.id)).toEqual(["e2", "e1"]);
    expect(scope.baseline?.id).toBe("session:s1");
    expect(explainScope({ ...incident, eventIdsJson: JSON.stringify(["e4"]) }, events, []).eventIds).toEqual([]);
  });
  it("runs only when the console, the session and this Mac agree on the incident", () => {
    expect(explainBlocker(incident, "s1", mac())).toBeNull();
    expect(explainBlocker(undefined, "s1", mac())).toMatch(/no incident/);
    expect(explainBlocker({ ...incident, simulated: true }, "s1", mac())).toMatch(/Synthetic/);
    expect(explainBlocker(incident, "s2", mac())).toMatch(/current protected session/);
    expect(explainBlocker(incident, "s1", { status: "offline", data: null })).toMatch(/isn't responding/);
    expect(explainBlocker(incident, "s1", mac("incident:other"))).toMatch(/hasn't reported/);
    expect(explainBlocker(incident, "s1", mac("incident:abc", "s0"))).toMatch(/isn't on the current session/);
  });
});

describe("presentation helpers", () => {
  it("describes only what an event carries", () => {
    const person = describeEvent({ kind: "person_in_zone", deviceId: "jetson-01", confidence: 0.885, metadataJson: JSON.stringify({ zone: "desk" }) });
    expect(person).toMatchObject({ title: "Person detected at the desk", source: "Jetson camera · jetson-01", detail: "desk zone · 89% confidence" });
    expect(describeEvent({ kind: "usb_added", deviceId: "mac-707bf6209e27", classification: "unknown_device", metadataJson: "{}" }).detail).toBe("unknown device");
    expect(describeEvent({ kind: "usb_added", deviceId: "mac-1", metadataJson: "{}" }).detail).toBe("");
  });
  it("reports playback from the receipt", () => {
    expect(playbackLabel({ spoken: true, audio: { outcome: "finished", source_label: "Local Kokoro voice (pre-rendered)" } })).toEqual({ text: "Played · Local Kokoro voice (pre-rendered)", ok: true });
    expect(playbackLabel({ spoken: true }).ok).toBe(false);
    expect(playbackLabel({ spoken: false, skipped: true }).text).toBe("Not repeated");
  });
});
