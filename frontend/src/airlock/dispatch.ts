import { type Row } from "./format";

/** How a delivery is described: a phone provider's receipt is "accepted by provider" and a console row is "published", never "delivered"
 *  or "seen"; only an acknowledgement says a person acted on it. */
export function deliveryLabel(d: Row): { text: string; tone: "cyan" | "amber" | "magenta" | "quiet" } {
  const who = `${d.recipient ?? "?"} · ${d.channel ?? "?"}`;
  switch (d.status) {
    case "acknowledged": return { text: `${who}: acknowledged`, tone: "cyan" };
    case "published": return { text: `${who}: published to its console (not proof it was displayed)`, tone: "amber" };
    case "accepted": return { text: `${who}: accepted by provider${d.attempts > 1 ? ` (attempt ${d.attempts})` : ""}`, tone: "cyan" };
    case "failed": return { text: `${who}: failed${d.error ? ` (${d.error})` : ""}${d.attempts ? `, ${d.attempts} attempt${d.attempts > 1 ? "s" : ""}` : ""}`, tone: "magenta" };
    case "sending": return { text: `${who}: sending`, tone: "amber" };
    case "queued": return { text: `${who}: queued`, tone: "amber" };
    default: return { text: `${who}: ${d.status ?? "unknown"}`, tone: "quiet" };
  }
}

/** The routing rule for an incident, as the backend's policy states it ("none" means no one is notified beyond the console). */
export function routeFor(policy: Row | undefined, ruleId: string): string {
  const rules = (policy?.rules ?? {}) as Record<string, string>;
  return rules[ruleId] ?? "not in policy";
}
