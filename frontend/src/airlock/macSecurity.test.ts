import { describe, expect, it } from "vitest";
import { type MacLink, latestInvestigationId, remoteFreshness, reportedStage } from "./macSecurity";

const live = (data: any): MacLink => ({ status: "live", data, error: null, at: 1 });

describe("reportedStage", () => {
  it("shows exactly the stage the backend reports", () => {
    expect(reportedStage(live({ visual_state: { current_security_mode: "WATCHFUL" } }))).toBe("WATCHFUL");
    expect(reportedStage(live({ visual_state: { current_security_mode: "OWNER_RETURNED" } }))).toBe("OWNER_RETURNED");
  });
  it("lights no stage when the backend is offline, even with an old report kept", () => {
    expect(reportedStage({ status: "offline", data: { visual_state: { current_security_mode: "CRITICAL" } }, error: "x", at: 1 })).toBeNull();
    expect(reportedStage({ status: "connecting", data: null, error: null, at: null })).toBeNull();
  });
  it("never guesses an unknown or missing value", () => {
    expect(reportedStage(live({ visual_state: { current_security_mode: "PANIC" } }))).toBeNull();
    expect(reportedStage(live({}))).toBeNull();
  });
});

describe("remoteFreshness", () => {
  it("judges Jetson freshness by the server's receipt time", () => {
    expect(remoteFreshness({ server_received_at_ms: 100_000, observed_at_ms: 1 }, 110_000)).toBe("fresh");
    expect(remoteFreshness({ server_received_at_ms: 100_000, observed_at_ms: 109_999 }, 130_000)).toBe("stale");
    expect(remoteFreshness(null, 1)).toBe("none");
    expect(remoteFreshness({ observed_at_ms: 1 }, 1)).toBe("none");
  });
});

describe("latestInvestigationId", () => {
  it("takes the newest turn with a well-formed investigation id", () => {
    const turns = [{ file: "a" }, { nemotron: { investigation_id: "../etc" } }, { nemotron: { investigation_id: "inv_abc123" } }, { nemotron: { investigation_id: "inv_old" } }];
    expect(latestInvestigationId(turns)?.id).toBe("inv_abc123");
    expect(latestInvestigationId([{ nemotron: { status: "unavailable" } }])).toBeNull();
    expect(latestInvestigationId(undefined)).toBeNull();
  });
});
