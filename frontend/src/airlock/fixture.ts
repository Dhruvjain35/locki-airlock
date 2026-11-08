// Design-review fixture for the command center. DEV ONLY, and only with ?fixture=… in the URL.
// It replaces the SpacetimeDB subscription and answers the console's /api/* reads from memory, so each state of the
// redesigned console can be reviewed without the live backend. A permanent banner says so, every reducer call is refused,
// and a production build never includes it (FIXTURE_ACTIVE folds to false outside `vite dev`).
//   ?fixture=1 or ?fixture=incident  armed session with an open high-severity incident (investigator access approved)
//   ?fixture=armed                    armed session, no incident
//   ?fixture=off                      protection off, one past session in History
import { useState } from "react";
import type { AirlockData, ConnectionConfig } from "./client";
import { INVESTIGATOR_IDENTITY } from "./protection";

export const FIXTURE_ACTIVE: boolean = import.meta.env.DEV && new URLSearchParams(location.search).has("fixture");
const MODE = FIXTURE_ACTIVE ? new URLSearchParams(location.search).get("fixture") : null;
const SCENARIO: "incident" | "armed" | "off" = MODE === "armed" ? "armed" : MODE === "off" ? "off" : "incident";

const WS = "fixture-desk";
const SESSION = "session-5acd7186-4a89-45b2-b671-87b45f405a20";
const PAST = "session-31c0b2de-9f0a-4b7e-a5d2-6c1e2f4b8a90";
const OWNER = "c2001edc895aea869c7842e331a64646cef95f65911985e4e55389bfc5190c62";
const INCIDENT = "incident:4f7a1c2e9b3d48a6a0e15c7d2f9b6e31";
const T0 = Date.now();
const at = (secondsAgo: number) => T0 - secondsAgo * 1000;
const armed = SCENARIO !== "off";
const withIncident = SCENARIO === "incident";

function data(): AirlockData {
  const armedAt = at(14 * 60);
  const person = { id: "67ab9d4e-64b6-41c3-935d-deddef917955", workstationId: WS, sessionId: SESSION, deviceId: "jetson-01", kind: "person_in_zone", source: "jetson", classification: "soft_observation", observedAtMs: at(372), receivedAtMs: at(371.8), confidence: 0.91, metadataJson: "{}", simulated: false, seq: 14n };
  const usb = { id: "c41e7b20-58aa-4d0e-9a61-2b7f0c9de114", workstationId: WS, sessionId: SESSION, deviceId: "mac-fixture", kind: "usb_added", source: "mac", classification: "unknown_usb", observedAtMs: at(368), receivedAtMs: at(367.9), confidence: 1, metadataJson: JSON.stringify({ deviceKey: "usb:0781:5583", label: "Unknown USB mass storage" }), simulated: false, seq: 41n };
  const sessionEvents = [
    { ...person, id: "0b9f8c11-4a2e-4f5c-9d3b-71aa0e2c6d45", observedAtMs: at(790), receivedAtMs: at(789.8), seq: 12n },
    { ...person, id: "5e2d1c77-3b9a-4c81-b0f6-8a24d61e9c03", kind: "person_cleared", observedAtMs: at(742), receivedAtMs: at(741.8), seq: 13n },
    ...(withIncident ? [person, usb] : []),
    { ...usb, id: "e8d4f2a6-1c3b-47e9-8f50-93b6a0d7c218", kind: "app_launched", classification: "unapproved_app", observedAtMs: at(205), receivedAtMs: at(204.9), metadataJson: JSON.stringify({ appId: "com.example.remote-admin", label: "Remote-admin app" }), seq: 42n },
    { ...person, id: "9a3c5e71-2d4f-4b86-a1e0-c7f3b8d2e649", observedAtMs: at(8), receivedAtMs: at(7.8), seq: 15n },
  ];
  const past = { ...person, id: "1f4e7a90-6b2c-4d38-8e15-a0c9d3b72f64", sessionId: PAST, observedAtMs: at(4.8 * 3600), receivedAtMs: at(4.8 * 3600), seq: 9n };
  return {
    workstations: [{ id: WS, name: "Fixture desk", owner: OWNER, activeSessionId: armed ? SESSION : "", autoLockEnabled: false, ownerPolicyHash: "9c1f0e7a2b6d4c83a5e1f7b20d9c4e68" }],
    memberships: [{ workstationId: WS, identity: OWNER, enabled: true, role: "owner", canApprove: true }],
    devices: [
      { workstationId: WS, id: "jetson-01", role: "jetson", enabled: true, lastSeq: 15n },
      { workstationId: WS, id: "mac-fixture", role: "mac", enabled: true, lastSeq: 42n },
      { workstationId: WS, id: "nemotron-m5", role: "service", enabled: true, lastSeq: 3n },
    ],
    sessions: [
      ...(armed ? [{ workstationId: WS, id: SESSION, state: "armed", armedAtMs: armedAt, expiresAtMs: at(-46 * 60), simulated: false, autoLockEnabled: false, baselineJson: JSON.stringify(["usb:05ac:8104"]), approvedAppsJson: JSON.stringify(["com.apple.Safari"]), ownerPolicyHash: "9c1f0e7a2b6d4c83a5e1f7b20d9c4e68" }] : []),
      { workstationId: WS, id: PAST, state: "disarmed", armedAtMs: at(5 * 3600), expiresAtMs: at(4 * 3600), disarmedAtMs: at(4.6 * 3600), simulated: false, autoLockEnabled: false, baselineJson: "[]", approvedAppsJson: "[]" },
    ],
    events: [...(armed ? sessionEvents : []), past],
    incidents: [
      ...(withIncident ? [{ id: INCIDENT, workstationId: WS, sessionId: SESSION, ruleId: "correlated_usb_physical", severity: "high", state: "open", createdAtMs: at(367.6), physicalEventId: person.id, digitalEventId: usb.id, summary: "Unknown USB storage connected 4.0 s after a person entered the desk zone.", eventIdsJson: JSON.stringify([person.id, usb.id]) }] : []),
      { id: "incident:2a9c4e7f0b3d41e8a6c5d9f2b1e7a043", workstationId: WS, sessionId: PAST, ruleId: "unknown_usb", severity: "medium", state: "open", createdAtMs: at(4.7 * 3600), summary: "Unknown USB while armed.", eventIdsJson: "[]" },
    ],
    health: [
      { workstationId: WS, deviceId: "jetson-01", component: "camera", status: "online", detail: "Brio 101 · 15 fps", lastSeenMs: at(2) },
      { workstationId: WS, deviceId: "mac-fixture", component: "usb", status: "online", detail: "3 devices", lastSeenMs: at(1) },
      { workstationId: WS, deviceId: "mac-fixture", component: "apps", status: "online", detail: "visible apps", lastSeenMs: at(1) },
    ],
    locks: [], actions: [], approvals: [], runs: [], steps: [], acknowledgements: [],
    audit: [
      { id: 1n, workstationId: WS, kind: "session_armed", detail: "Owner armed a live session", entityId: SESSION, actor: OWNER, createdAtMs: armedAt },
      ...(withIncident ? [{ id: 2n, workstationId: WS, kind: "incident_evidence_granted", detail: JSON.stringify({ investigator: INVESTIGATOR_IDENTITY }), entityId: INCIDENT, actor: OWNER, createdAtMs: at(300) }] : []),
      // ?fixture=off: a grant from the past session that was never revoked, so the disarmed views must show "Revoke access".
      ...(SCENARIO === "off" ? [{ id: 3n, workstationId: WS, kind: "incident_evidence_granted", detail: JSON.stringify({ investigator: INVESTIGATOR_IDENTITY }), entityId: "incident:2a9c4e7f0b3d41e8a6c5d9f2b1e7a043", actor: OWNER, createdAtMs: at(4.65 * 3600) }] : []),
    ],
    baselines: [], armAuthorizations: [],
  } as unknown as AirlockData;
}

export function useFixtureAirlock(_config: ConnectionConfig) {
  const [snapshot] = useState(data);
  const call = async () => { throw new Error("Fixture data: actions are disabled. Remove ?fixture from the URL to use the live console."); };
  return { data: snapshot, status: "connected" as const, identity: OWNER, connectionId: "fixture", armAuthorizationAvailable: true, error: "", lastSyncedAt: Date.now(), pending: "", call, reconnect: () => {} };
}

function security(now: number) {
  const armedAt = at(14 * 60);
  const mode = withIncident ? "CRITICAL" : armed ? "WATCHFUL" : "NORMAL";
  return {
    armed, session: armed ? { session_id: SESSION, armed_at_ms: armedAt } : null, lock_mode: "dry_run",
    incident: withIncident ? { incident_id: INCIDENT } : null,
    visual_state: { current_security_mode: mode, mode_reason: withIncident ? "correlated_usb_physical" : armed ? "person_in_zone" : "desk_clear", mode_since: (withIncident ? at(367.4) : at(8)) / 1000 },
    demo: { camera_running: false },
    remote_camera: armed ? { kind: "person_in_zone", device_id: "jetson-01", event_id: "9a3c5e71-2d4f-4b86-a1e0-c7f3b8d2e649", server_received_at_ms: now - 4_200, acted_on: true } : null,
    camera_events: [{ event_id: "9a3c5e71-2d4f-4b86-a1e0-c7f3b8d2e649", kind: "person_in_zone", zone: "desk_01", confidence: 0.91 }],
    warnings: armed ? [
      { event_id: "0b9f8c11", reason: "person_at_desk", spoken: true, at: at(789) / 1000, audio: { outcome: "finished", source_label: "Kokoro" } },
      ...(withIncident ? [{ event_id: "c41e7b20", reason: "correlated", spoken: true, at: at(367.2) / 1000, audio: { outcome: "finished", source_label: "Kokoro" } }] : []),
      { event_id: "9a3c5e71", reason: "person_at_desk", skipped: true, at: at(7) / 1000 },
    ] : [],
    locks: withIncident ? [{ session_id: SESSION, status: "dry_run_recorded", dry_run: true }] : [],
    monitor: { running: true, usb: { connected: 3, error: null }, apps: { error: null } },
    spacetimedb: { connected: true, authorized: true },
  };
}

const COMMAND = {
  ok: true, message: "", reply: "Fixture answer: a person sat at the desk and an unknown USB drive was connected four seconds later. The screen lock stayed in dry run.",
  data: { nemotron: { investigation_id: "inv_fixture01", incident_id: INCIDENT, evidence_badge: "LIVE", status: "completed", total_ms: 11840 } },
};
const INVESTIGATION = {
  investigation_id: "inv_fixture01", official_severity: "high", headline: "Fixture record: unknown USB storage inserted while a person was at the desk", needs_review: false, total_latency_ms: 11840,
  observed_facts: [{ fact: "The Jetson saw a person enter desk_01 at 0.91 confidence.", evidence_ids: ["67ab9d4e-64b6-41c3-935d-deddef917955"] }, { fact: "USB 0781:5583 is not in the session's frozen baseline.", evidence_ids: ["c41e7b20-58aa-4d0e-9a61-2b7f0c9de114"] }],
  tools: [{ tool: "get_incident_events", status: "ok", live_data: true }, { tool: "compare_usb_baseline", status: "ok", live_data: true }, { tool: "get_face_identity", status: "unavailable", live_data: false }],
};

/** Answer the console's /api reads from memory. Nothing in fixture mode reaches a backend. */
export function installFixtureFetch() {
  const json = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
  const real = window.fetch.bind(window);
  window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const path = url.replace(location.origin, "").split("?")[0];
    if (!path.startsWith("/api/")) return real(input, init);
    const method = (init?.method ?? "GET").toUpperCase();
    if (path === "/api/command" && method === "POST") return new Promise(r => window.setTimeout(() => r(new Response(JSON.stringify(COMMAND), { status: 200, headers: { "Content-Type": "application/json" } })), 1800));
    if (method !== "GET") return json({ detail: "Fixture data: actions are disabled." }, 409);
    if (path === "/api/security") return json(security(Date.now()));
    if (path === "/api/voice/devices") return json({ available: true, inputs: [], outputs: [{ name: "Fixture speaker", default: true }] });
    if (path === "/api/state") return json({ voice: { output: { warning: null } } });
    if (path.startsWith("/api/security/investigation/")) return json(INVESTIGATION);
    return json({ detail: "Not part of the fixture." }, 404);
  };
}
