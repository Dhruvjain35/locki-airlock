/** Missing/legacy capabilities never enable biometric product controls. */
export function faceIdentificationEnabled(snapshot: { face_identification_enabled?: unknown } | null): boolean {
  return snapshot?.face_identification_enabled === true;
}

const faceSteps = new Set(["enroll", "recognized", "owner_return"]);
const faceTimings = new Set([
  "enrollment_ms", "owner_recognition_after_enrollment_ms", "owner_identity_ms",
  "unrecognized_identity_ms", "face_pass_ms", "face_compare_ms",
]);

export const visibleDemoStep = (key: string, enabled: boolean) => enabled || !faceSteps.has(key);
export const visibleDemoTiming = (key: string, enabled: boolean) => enabled || !faceTimings.has(key);
