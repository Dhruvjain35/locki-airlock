import { armConsent } from "./armConsent";
import { type Row, textValue } from "./format";

export interface VoiceArmContext {
  station: Row; device?: Row; capture?: Row; health?: Row;
  apps: string[]; durationMinutes: string; now: number;
  connected: boolean; available: boolean; identity: string; connectionId: string;
}
export interface VoiceArmReview {
  fingerprint: string; baselineJson: string; activeAppId: string;
  approvedAppsJson: string; durationMs: bigint; deviceId: string;
}

const fresh = (value: unknown, maxAge: number, now: number) => {
  const age = now - Number(value);
  return value !== undefined && value !== null && Number.isFinite(age) && age >= -5000 && age <= maxAge;
};

/** Review only exact owner/target state, and bind consent to the issuing socket. */
export function voiceArmReview(context: VoiceArmContext): VoiceArmReview | null {
  const { station, device, capture, health, apps, durationMinutes, now, identity, connectionId } = context;
  if (!context.connected || !context.available || !identity || !connectionId
    || textValue(station.owner, "") !== identity || !station.id || !station.ownerPolicyHash
    || station.activeSessionId || station.autoLockEnabled !== false) return null;
  if (!device || device.enabled !== true || device.role !== "mac" || device.workstationId !== station.id
    || !textValue(device.identity, "") || !capture || capture.workstationId !== station.id
    || capture.deviceId !== device.id || capture.sessionId !== ""
    || textValue(capture.publisher, "") !== textValue(device.identity, "")) return null;
  if (!fresh(capture.observedAtMs, 30000, now) || !health || health.workstationId !== station.id
    || health.deviceId !== device.id || health.component !== "usb" || health.status !== "online"
    || !fresh(health.lastSeenMs, 15000, now)) return null;
  if (typeof capture.activeAppId !== "string" || !capture.activeAppId || capture.activeAppId.length > 256
    || !Array.isArray(apps) || apps.length > 256
    || apps.some(app => typeof app !== "string" || !app || app.length > 256)
    || !/^[0-9]+$/.test(durationMinutes)) return null;
  const minutes = Number(durationMinutes);
  if (!Number.isSafeInteger(minutes) || minutes < 1 || minutes > 60) return null;
  const reviewed = armConsent(station, capture, apps, durationMinutes);
  if (!reviewed) return null;
  return {
    fingerprint: JSON.stringify([reviewed.fingerprint, connectionId, identity, capture.activeAppId, textValue(device.identity)]),
    baselineJson: reviewed.baselineJson,
    activeAppId: capture.activeAppId,
    approvedAppsJson: JSON.stringify([...new Set(apps)].sort()),
    durationMs: BigInt(minutes) * 60000n,
    deviceId: device.id,
  };
}

export function mayAuthorizeVoiceArm(consented: string | null, context: VoiceArmContext): boolean {
  const reviewed = voiceArmReview(context);
  return Boolean(reviewed && consented === reviewed.fingerprint);
}

/** A stale authorized status must not survive disconnect or local expiry. */
export function voiceArmGrantState(grant: Row | undefined, connected: boolean, now: number): string {
  if (!connected) return "unverified";
  if (!grant) return "none";
  if (!["authorized", "consumed", "revoked", "expired"].includes(grant.status)) return "unverified";
  if (grant.status === "authorized" && (!Number.isFinite(Number(grant.expiresAtMs))
    || Number(grant.expiresAtMs) <= now)) return "expired";
  return grant.status;
}
