import { describe, expect, it } from "vitest";
import { faceIdentificationEnabled, visibleDemoStep, visibleDemoTiming } from "./demoScope";

describe("person and desk monitoring scope", () => {
  it("requires an explicit true capability before showing identity controls", () => {
    for (const snapshot of [null, {}, { face_identification_enabled: false }, { face_identification_enabled: "true" }]) {
      expect(faceIdentificationEnabled(snapshot)).toBe(false);
    }
    expect(faceIdentificationEnabled({ face_identification_enabled: true })).toBe(true);
  });
  it("keeps person, interaction, arming and recovery steps without enrollment", () => {
    for (const key of ["start", "zones", "arm", "monitor", "incident", "disarm"]) expect(visibleDemoStep(key, false)).toBe(true);
    for (const key of ["enroll", "recognized", "owner_return"]) {
      expect(visibleDemoStep(key, false)).toBe(false);
      expect(visibleDemoStep(key, true)).toBe(true);
    }
  });
  it("shows activity latency without presenting identity measurements", () => {
    for (const key of ["mode_switch_ms", "event_to_speech_ms", "investigation_ms", "person_detect_ms", "capture_to_event_ms"]) expect(visibleDemoTiming(key, false)).toBe(true);
    for (const key of ["enrollment_ms", "owner_recognition_after_enrollment_ms", "owner_identity_ms", "unrecognized_identity_ms", "face_pass_ms", "face_compare_ms"]) {
      expect(visibleDemoTiming(key, false)).toBe(false);
      expect(visibleDemoTiming(key, true)).toBe(true);
    }
  });
});
