import { describe, expect, it } from "vitest";
import { deliveryLabel, routeFor } from "./dispatch";

describe("dispatch status wording", () => {
  it("never calls a provider receipt delivered", () => {
    const l = deliveryLabel({ recipient: "owner", channel: "phone", status: "accepted", attempts: 2 });
    expect(l.text).toBe("owner · phone: accepted by provider (attempt 2)");
    expect(l.text).not.toMatch(/delivered|seen|read/);
  });
  it("says why a delivery failed and how often it tried", () => {
    expect(deliveryLabel({ recipient: "owner", channel: "phone", status: "failed", error: "timeout", attempts: 3 }).text).toBe("owner · phone: failed (timeout), 3 attempts");
  });
  it("reads the routing rule from the backend's policy", () => {
    const policy = { version: 1, rules: { correlated_usb_physical: "owner+desk", soft_observation: "none" } };
    expect(routeFor(policy, "correlated_usb_physical")).toBe("owner+desk");
    expect(routeFor(policy, "weird_rule")).toBe("not in policy");
  });
});
