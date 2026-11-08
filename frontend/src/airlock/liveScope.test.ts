import { describe, expect, it } from "vitest";
import { bySession, liveSession, macSession, pastSessions, pickIncident, remoteInSession, sinceArmed, turnForIncidents } from "./liveScope";

const NOW = 1_794_140_000_000;
const live = { id: "session-new", state: "armed", armedAtMs: BigInt(NOW - 60_000), expiresAtMs: BigInt(NOW + 600_000), simulated: false };
const old = { id: "dhruv-m4-air-1794093957", state: "disarmed", armedAtMs: BigInt(NOW - 50_000_000), expiresAtMs: BigInt(NOW - 49_000_000), disarmedAtMs: BigInt(NOW - 49_500_000), simulated: false };
const oldIncident = { id: "incident:23000b774fdc4ddaab79820a70af101b", sessionId: old.id, state: "open", severity: "high", createdAtMs: BigInt(NOW - 49_800_000) };
const oldEvent = { id: "e-old", sessionId: old.id, kind: "usb_added" };
const oldJetson = { id: "j-old", sessionId: old.id, kind: "person_in_zone" };

describe("live session scope", () => {
  it("starts clean: an armed live session with only historical records has nothing current", () => {
    const s = liveSession([live, old], "session-new", NOW);
    expect(s?.id).toBe("session-new");
    const ev = bySession([oldEvent, oldJetson], s?.id);
    const inc = bySession([oldIncident], s?.id);
    expect(ev.current).toEqual([]);
    expect(inc.current).toEqual([]);
    expect(pickIncident(inc.current, "")).toBeUndefined();
    expect(ev.history).toHaveLength(2);
    expect(inc.history).toEqual([oldIncident]);
  });
  it("a new Jetson event in this session is the only one in the live feed", () => {
    const fresh = { id: "j-new", sessionId: "session-new", kind: "person_in_zone" };
    expect(bySession([oldEvent, oldJetson, fresh], "session-new").current).toEqual([fresh]);
  });
  it("a new USB incident becomes the live incident, never the old open one", () => {
    const usb = { id: "incident:new", sessionId: "session-new", state: "open", severity: "critical", createdAtMs: BigInt(NOW) };
    const { current } = bySession([oldIncident, usb], "session-new");
    expect(pickIncident(current, oldIncident.id)?.id).toBe("incident:new");
  });
  it("after disarm or expiry there is no live session and everything is history", () => {
    expect(liveSession([{ ...live, state: "disarmed" }], "session-new", NOW)).toBeUndefined();
    expect(liveSession([live], "session-new", NOW + 700_000)).toBeUndefined();
    expect(bySession([oldEvent], undefined)).toEqual({ current: [], history: [oldEvent] });
  });
  it("keeps past sessions with their own ids, times and evidence counts, unchanged", () => {
    const before = JSON.stringify([old, oldIncident, oldEvent], (_k, v) => typeof v === "bigint" ? String(v) : v);
    const past = pastSessions([live, old], [oldEvent, oldJetson], [oldIncident], "session-new");
    expect(past).toHaveLength(1);
    expect(past[0]).toMatchObject({ events: 2, incidents: 1, open: 1, ended: NOW - 49_500_000 });
    expect(past[0].session.id).toBe(old.id);
    expect(JSON.stringify([old, oldIncident, oldEvent], (_k, v) => typeof v === "bigint" ? String(v) : v)).toBe(before);
  });
});

describe("Mac session scope", () => {
  const mac = { armed: true, session: { session_id: "session-new", armed_at_ms: NOW - 60_000 } };
  it("reads the Mac's own armed session", () => {
    expect(macSession(mac)).toEqual({ id: "session-new", armedAtMs: NOW - 60_000 });
    expect(macSession({ armed: false, session: null })).toBeNull();
  });
  it("shows only receipts since arming, and nothing when not armed", () => {
    const items = [{ at: NOW - 120_000 }, { at: NOW - 1_000 }];
    expect(sinceArmed(items, NOW - 60_000, x => x.at)).toEqual([{ at: NOW - 1_000 }]);
    expect(sinceArmed(items, null, x => x.at)).toEqual([]);
  });
  it("treats a Jetson receipt from before arming as history", () => {
    expect(remoteInSession({ server_received_at_ms: NOW - 3_800_000 }, NOW - 60_000)).toBe(false);
    expect(remoteInSession({ server_received_at_ms: NOW - 10_000 }, NOW - 60_000)).toBe(true);
    expect(remoteInSession({ server_received_at_ms: NOW }, null)).toBe(false);
  });
  it("picks a Nemotron answer only for an incident in view", () => {
    const turns = [{ nemotron: { incident_id: oldIncident.id, investigation_id: "inv_old" } }];
    expect(turnForIncidents(turns, new Set(["incident:new"]))).toBeNull();
    expect(turnForIncidents(turns, new Set([oldIncident.id]))?.nemotron.investigation_id).toBe("inv_old");
  });
});
