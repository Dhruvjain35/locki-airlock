import { type Row, textValue } from "./format";

export interface ArmConsent { baselineJson: string; fingerprint: string }

/** Bind consent to reviewed contents/authority, not periodic capture timestamps. */
export function armConsent(station: Row, capture: Row | undefined, apps: string[], duration: string): ArmConsent | null {
  if (!capture?.deviceId || !capture.publisher) return null;
  let keys: unknown;
  try { keys = JSON.parse(capture.baselineJson); } catch { return null; }
  if (!Array.isArray(keys) || keys.length > 256 || keys.some(key => typeof key !== "string" || !key.length || key.length > 256)) return null;
  const baselineJson = JSON.stringify([...new Set(keys)].sort());
  const fingerprint = JSON.stringify([
    station.id, textValue(station.owner, ""), capture.deviceId, textValue(capture.publisher, ""), baselineJson,
    station.ownerPolicyHash, String(station.policyVersion), Boolean(station.autoLockEnabled), String(station.lockCooldownMs),
    [...new Set(apps)].sort(), duration,
  ]);
  return { baselineJson, fingerprint };
}

export function maySubmitLiveArm(consented: string | null, current: ArmConsent | null, observedAtMs: unknown, now: number): boolean {
  const age = now - Number(observedAtMs);
  return Boolean(current && consented === current.fingerprint && Number.isFinite(age) && age >= -5000 && age <= 30000);
}

/** Sid's limits for a delegated (voice) ARM, enforced again by authorize_arm. */
export const VOICE_GRANT_TTL_MS = 60_000n;
export const VOICE_MAX_DURATION_MIN = 60;

/** The owner's one-use voice ARM grant, built only from reviewed, server-observed values: the enrolled Mac's fresh capture (its device,
 *  canonical baseline and active app), the workstation's current policy hash, the owner's app list and a duration within 1–60 minutes.
 *  Fresh unique authorization and session ids each time; nothing comes from speech. Returns null when anything is missing or out of range. */
export function voiceArmGrant(station: Row, capture: Row | undefined, reviewed: ArmConsent | null, apps: string[], durationMin: string, newId: () => string = () => crypto.randomUUID()) {
  const minutes = Number(durationMin);
  if (!reviewed || !capture?.deviceId || typeof station.ownerPolicyHash !== "string" || !station.ownerPolicyHash) return null;
  if (!Number.isInteger(minutes) || minutes < 1 || minutes > VOICE_MAX_DURATION_MIN) return null;
  if (station.autoLockEnabled) return null;  // authorize_arm refuses delegated ARM with automatic locking on
  return {
    workstationId: String(station.id), authorizationId: `voice-arm-${newId()}`, deviceId: String(capture.deviceId), sessionId: `session-${newId()}`,
    baselineJson: reviewed.baselineJson, activeAppId: typeof capture.activeAppId === "string" ? capture.activeAppId : "",
    approvedAppsJson: JSON.stringify([...new Set(apps)].sort()), expectedOwnerPolicyHash: station.ownerPolicyHash,
    durationMs: BigInt(minutes * 60_000), ttlMs: VOICE_GRANT_TTL_MS,
  };
}
