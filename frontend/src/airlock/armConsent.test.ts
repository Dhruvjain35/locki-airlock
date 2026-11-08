import { describe, expect, it } from "vitest";
import { armConsent, maySubmitLiveArm, voiceArmGrant } from "./armConsent";

const station = { id: "desk", owner: "owner-a", ownerPolicyHash: "policy-a", policyVersion: 1, autoLockEnabled: true, lockCooldownMs: 30000n };
const capture = { deviceId: "mac-a", publisher: { toHexString: () => "publisher-a" }, baselineJson: '["usb-b","usb-a"]', observedAtMs: 100000n };
const reviewed = () => armConsent(station, capture, ["app-a"], "60")!;

describe("live arm consent", () => {
  it("submits the exact canonical reviewed USB set, including an explicitly empty inventory", () => {
    expect(reviewed().baselineJson).toBe('["usb-a","usb-b"]');
    expect(armConsent(station, { ...capture, baselineJson: "[]" }, [], "60")?.baselineJson).toBe("[]");
  });

  it("blocks changed inventory synchronously before any UI reset effect", () => {
    const old = reviewed();
    const changed = armConsent(station, { ...capture, baselineJson: '["new-usb"]' }, ["app-a"], "60");
    expect(maySubmitLiveArm(old.fingerprint, changed, capture.observedAtMs, 101000)).toBe(false);
    expect(maySubmitLiveArm(changed!.fingerprint, changed, capture.observedAtMs, 101000)).toBe(true);
  });

  it.each([
    { id: "other-desk" }, { owner: "owner-b" }, { ownerPolicyHash: "policy-b" },
    { policyVersion: 2 }, { autoLockEnabled: false }, { lockCooldownMs: 60000n },
  ])("invalidates consent when workstation authority or policy changes (%#)", change => {
    const current = armConsent({ ...station, ...change }, capture, ["app-a"], "60");
    expect(maySubmitLiveArm(reviewed().fingerprint, current, capture.observedAtMs, 101000)).toBe(false);
  });

  it("invalidates changes to publisher, device, app allowlist and duration", () => {
    const variants = [
      armConsent(station, { ...capture, publisher: "publisher-b" }, ["app-a"], "60"),
      armConsent(station, { ...capture, deviceId: "mac-b" }, ["app-a"], "60"),
      armConsent(station, capture, ["app-b"], "60"),
      armConsent(station, capture, ["app-a"], "15"),
    ];
    for (const current of variants) expect(maySubmitLiveArm(reviewed().fingerprint, current, capture.observedAtMs, 101000)).toBe(false);
  });

  it("preserves consent across timestamp-only refresh and equivalent canonical inventories", () => {
    const refreshed = armConsent(station, { ...capture, observedAtMs: 101000n, baselineJson: '["usb-a","usb-b","usb-a"]' }, ["app-a"], "60");
    expect(refreshed?.fingerprint).toBe(reviewed().fingerprint);
    expect(maySubmitLiveArm(reviewed().fingerprint, refreshed, 101000n, 102000)).toBe(true);
  });

  it("checks consent and capture freshness again at submission time", () => {
    expect(maySubmitLiveArm(null, reviewed(), 100000n, 100000)).toBe(false);
    for (const now of [94999, 130001, NaN]) expect(maySubmitLiveArm(reviewed().fingerprint, reviewed(), 100000n, now)).toBe(false);
    for (const now of [95000, 130000]) expect(maySubmitLiveArm(reviewed().fingerprint, reviewed(), 100000n, now)).toBe(true);
  });

  it("fails closed on missing publisher or malformed baseline instead of consenting to an invented empty inventory", () => {
    for (const baselineJson of ["not-json", "null", "{}", '[42]', '[""]', JSON.stringify(["x".repeat(257)])]) {
      expect(armConsent(station, { ...capture, baselineJson }, [], "60")).toBeNull();
    }
    expect(armConsent(station, { ...capture, publisher: "" }, [], "60")).toBeNull();
    expect(armConsent(station, undefined, [], "60")).toBeNull();
  });
});

describe("voice arm grant", () => {
  const disarmedStation = { ...station, autoLockEnabled: false };
  const liveCapture = { ...capture, activeAppId: "com.apple.Terminal" };
  const ids = () => { let n = 0; return () => `id-${++n}`; };

  it("carries only reviewed server values, fresh ids, and Sid's u64 limits", () => {
    const g = voiceArmGrant(disarmedStation, liveCapture, armConsent(disarmedStation, liveCapture, ["b", "a", "a"], "15"), ["b", "a", "a"], "15", ids())!;
    expect(g).toEqual({
      workstationId: "desk", authorizationId: "voice-arm-id-1", deviceId: "mac-a", sessionId: "session-id-2", baselineJson: '["usb-a","usb-b"]',
      activeAppId: "com.apple.Terminal", approvedAppsJson: '["a","b"]', expectedOwnerPolicyHash: "policy-a", durationMs: 900000n, ttlMs: 60000n,
    });
  });

  it("refuses what authorize_arm would refuse", () => {
    const reviewedLive = armConsent(disarmedStation, liveCapture, [], "60");
    expect(voiceArmGrant(disarmedStation, liveCapture, reviewedLive, [], "240")).toBeNull();  // over an hour
    expect(voiceArmGrant(station, liveCapture, armConsent(station, liveCapture, [], "60"), [], "60")).toBeNull();  // auto-lock on
    expect(voiceArmGrant(disarmedStation, liveCapture, null, [], "60")).toBeNull();  // no reviewed capture
    expect(voiceArmGrant({ ...disarmedStation, ownerPolicyHash: "" }, liveCapture, reviewedLive, [], "60")).toBeNull();
  });

  it("never reuses an id", () => {
    const a = voiceArmGrant(disarmedStation, liveCapture, armConsent(disarmedStation, liveCapture, [], "60"), [], "60")!;
    const b = voiceArmGrant(disarmedStation, liveCapture, armConsent(disarmedStation, liveCapture, [], "60"), [], "60")!;
    expect(a.authorizationId).not.toBe(b.authorizationId);
    expect(a.sessionId).not.toBe(b.sessionId);
  });
});
