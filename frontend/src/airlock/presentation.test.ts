import { describe, expect, it } from "vitest";
import { controlRoomPresentation } from "./presentation";

const connectedStation = { connected: true, hasStation: true, armed: true };

describe("control room presentation", () => {
  it("does not present stale subscribed state as verified while disconnected", () => {
    expect(controlRoomPresentation({
      ...connectedStation,
      connected: false,
      lock: { status: "confirmed", simulated: false },
      incident: { ruleId: "owner_panic" },
    })).toEqual({ title: "Console disconnected", status: "Disconnected" });
  });

  it.each([
    { status: "confirmed", simulated: false, label: "Screen lock confirmed" },
    { status: "confirmed", simulated: true, label: "Synthetic lock confirmation" },
    { status: "requested", simulated: false, label: "Screen lock requested" },
    { status: "requested", simulated: true, label: "Synthetic lock request committed" },
  ])("keeps $label distinct and ahead of panic or armed state", ({ status, simulated, label }) => {
    expect(controlRoomPresentation({
      ...connectedStation,
      lock: { status, simulated },
      incident: { ruleId: "owner_panic" },
    })).toEqual({ title: label, status: label });
  });

  it("identifies owner panic before generic incident or armed state", () => {
    expect(controlRoomPresentation({ ...connectedStation, incident: { ruleId: "owner_panic" } }))
      .toEqual({ title: "Owner security alert", status: "Panic requested" });
  });

  it("asks for evidence review without accusing someone, ahead of armed state", () => {
    expect(controlRoomPresentation({ ...connectedStation, incident: { ruleId: "unknown_usb" } }))
      .toEqual({ title: "Activity needs your review", status: "Review required" });
  });

  it.each([
    { armed: true, title: "Your workstation is armed", status: "Armed" },
    { armed: false, title: "Your workstation is disarmed", status: "Disarmed" },
  ])("shows $status when there is no current incident or lock", ({ armed, title, status }) => {
    expect(controlRoomPresentation({ ...connectedStation, armed })).toEqual({ title, status });
  });

  it("preserves the onboarding title when there is no workstation", () => {
    expect(controlRoomPresentation({ ...connectedStation, hasStation: false, armed: false }))
      .toEqual({ title: "Your desk. Your boundary.", status: "Disarmed" });
    expect(controlRoomPresentation({ ...connectedStation, hasStation: false, connected: false }))
      .toEqual({ title: "Console disconnected", status: "Disconnected" });
  });
});
