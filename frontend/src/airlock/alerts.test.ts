import { describe, expect, it } from "vitest";
import { acknowledged, alertText, incidentsToAlert } from "./alerts";

const now = 1_800_000_000_000;
const inc = (id: string, severity: string, ageMs = 1000, state = "open") => ({ id, severity, state, createdAtMs: BigInt(now - ageMs), updatedAtMs: BigInt(now - ageMs), summary: "Unknown USB device", ruleId: "unknown_usb" });

describe("incident alerts", () => {
  it("alerts once per incident, again only when its severity rises", () => {
    expect(incidentsToAlert([inc("a", "medium")], {}, now).map(i => i.id)).toEqual(["a"]);
    expect(incidentsToAlert([inc("a", "medium")], { a: 2 }, now)).toEqual([]);
    expect(incidentsToAlert([inc("a", "high")], { a: 2 }, now).map(i => i.id)).toEqual(["a"]);
  });
  it("never replays old or resolved incidents when the console opens", () => {
    expect(incidentsToAlert([inc("old", "high", 11 * 60_000), inc("done", "high", 1000, "resolved")], {}, now)).toEqual([]);
  });
  it("says severity, workstation, summary and id, and labels rehearsals", () => {
    const t = alertText({ ...inc("incident:9", "high"), simulated: true }, "dhruv-m4-air");
    expect(t.title).toBe("HIGH security incident · dhruv-m4-air (synthetic rehearsal)");
    expect(t.body).toContain("Unknown USB device · incident:9");
  });
  it("knows when someone acknowledged it", () => {
    expect(acknowledged(inc("a", "high"), [{ incidentId: "a", kind: "acknowledged" }])).toBe(true);
    expect(acknowledged(inc("a", "high"), [])).toBe(false);
  });
});
