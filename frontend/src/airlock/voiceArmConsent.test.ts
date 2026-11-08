import { describe, expect, it } from "vitest";
import { mayAuthorizeVoiceArm, voiceArmGrantState, voiceArmReview, type VoiceArmContext } from "./voiceArmConsent";

function context(): VoiceArmContext {
  return {
    station: { id: "desk", owner: "owner", ownerPolicyHash: "policy", policyVersion: 1,
      activeSessionId: "", autoLockEnabled: false, lockCooldownMs: 30000n },
    device: { id: "mac", workstationId: "desk", identity: "mac-identity", enabled: true, role: "mac" },
    capture: { deviceId: "mac", workstationId: "desk", publisher: "mac-identity", sessionId: "",
      observedAtMs: 100000n, baselineJson: '["usb-b","usb-a"]', activeAppId: "com.apple.Terminal" },
    health: { deviceId: "mac", workstationId: "desk", component: "usb", status: "online", lastSeenMs: 100000n },
    apps: ["app-b", "app-a"], durationMinutes: "15", now: 101000,
    connected: true, available: true, identity: "owner", connectionId: "owner-connection",
  };
}

describe("owner voice ARM review", () => {
  it("builds exact canonical reviewed fields and a bounded duration", () => {
    const reviewed = voiceArmReview(context())!;
    expect(reviewed.baselineJson).toBe('["usb-a","usb-b"]');
    expect(reviewed.approvedAppsJson).toBe('["app-a","app-b"]');
    expect(reviewed.activeAppId).toBe("com.apple.Terminal");
    expect(reviewed.durationMs).toBe(900000n);
    expect(reviewed.deviceId).toBe("mac");
  });

  it.each(["connected", "available"] as const)("blocks unavailable %s", key => {
    const current = context(); current[key] = false;
    expect(voiceArmReview(current)).toBeNull();
  });

  it("requires the actual owner identity and a current persistent connection", () => {
    expect(voiceArmReview({ ...context(), identity: "not-owner" })).toBeNull();
    expect(voiceArmReview({ ...context(), connectionId: "" })).toBeNull();
  });

  it("refuses active sessions, auto-lock policies and wrong target role/enrollment", () => {
    for (const station of [{ activeSessionId: "armed-session" }, { autoLockEnabled: true }, { autoLockEnabled: undefined }]) {
      const current = context(); Object.assign(current.station, station);
      expect(voiceArmReview(current)).toBeNull();
    }
    for (const device of [{ enabled: false }, { role: "jetson" }, { identity: "different" }, { workstationId: "other" }]) {
      const current = context(); Object.assign(current.device!, device);
      expect(voiceArmReview(current)).toBeNull();
    }
  });

  it("requires matching target capture and USB health with fresh timestamps", () => {
    for (const capture of [{ deviceId: "other-mac" }, { publisher: "other" }, { sessionId: "frozen-session" },
      { workstationId: "other" }, { observedAtMs: 70000n }, { activeAppId: "" }]) {
      const current = context(); Object.assign(current.capture!, capture);
      expect(voiceArmReview(current)).toBeNull();
    }
    for (const health of [{ status: "offline" }, { status: "degraded" }, { lastSeenMs: 85000n },
      { deviceId: "other" }, { workstationId: "other" }, { component: "apps" }]) {
      const current = context(); Object.assign(current.health!, health);
      expect(voiceArmReview(current)).toBeNull();
    }
  });

  it("invalidates prior consent when active app, inventory, policy, duration or socket changes", () => {
    const old = voiceArmReview(context())!.fingerprint;
    const changes: Array<(c: VoiceArmContext) => void> = [
      c => { c.capture!.activeAppId = "different-app"; },
      c => { c.capture!.baselineJson = '["new-usb"]'; },
      c => { c.station.ownerPolicyHash = "changed-policy"; },
      c => { c.durationMinutes = "60"; },
      c => { c.connectionId = "reconnected-owner"; },
      c => { c.apps = ["different-app"]; },
      c => { c.device!.identity = "new-mac"; c.capture!.publisher = "new-mac"; },
    ];
    for (const change of changes) {
      const current = context(); change(current);
      expect(mayAuthorizeVoiceArm(old, current)).toBe(false);
    }
  });

  it("keeps review valid for timestamp-only capture refresh but rechecks freshness at submission", () => {
    const old = voiceArmReview(context())!.fingerprint;
    const refreshed = context(); refreshed.capture!.observedAtMs = 101000n; refreshed.health!.lastSeenMs = 101000n;
    expect(mayAuthorizeVoiceArm(old, refreshed)).toBe(true);
    expect(mayAuthorizeVoiceArm(old, { ...refreshed, now: 117000 })).toBe(false);
    expect(mayAuthorizeVoiceArm(null, context())).toBe(false);
  });

  it.each(["0", "61", "1.5", "NaN", "", "-1"])("rejects invalid session minutes %s", durationMinutes => {
    expect(voiceArmReview({ ...context(), durationMinutes })).toBeNull();
  });

  it("allows a genuinely empty inventory but rejects malformed baseline and app list", () => {
    const current = context(); current.capture!.baselineJson = "[]";
    expect(voiceArmReview(current)?.baselineJson).toBe("[]");
    current.capture!.baselineJson = "invalid";
    expect(voiceArmReview(current)).toBeNull();
    expect(voiceArmReview({ ...context(), apps: [""] })).toBeNull();
  });
});

describe("voice ARM permission status", () => {
  it("expires at the deadline even when maintenance has not updated the row", () => {
    const grant = { status: "authorized", expiresAtMs: 130000n };
    expect(voiceArmGrantState(grant, true, 129999)).toBe("authorized");
    expect(voiceArmGrantState(grant, true, 130000)).toBe("expired");
  });
  it("never treats cached grants as usable while disconnected", () => {
    expect(voiceArmGrantState({ status: "authorized", expiresAtMs: 130000n }, false, 100000)).toBe("unverified");
  });
  it("shows consumed/revoked/missing state without claiming an armed session", () => {
    expect(voiceArmGrantState({ status: "consumed" }, true, 100000)).toBe("consumed");
    expect(voiceArmGrantState({ status: "revoked" }, true, 100000)).toBe("revoked");
    expect(voiceArmGrantState(undefined, true, 100000)).toBe("none");
    expect(voiceArmGrantState({ status: "unexpected" }, true, 100000)).toBe("unverified");
  });
});
