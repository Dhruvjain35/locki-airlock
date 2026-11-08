import { beforeEach, describe, expect, it, vi } from "vitest";
const sdk = vi.hoisted(() => ({ connect: undefined as any, connectError: undefined as any, disconnect: undefined as any,
  applied: undefined as any, error: undefined as any, context: undefined as any, token: undefined as any, builder: vi.fn(), subscriptions: [] as string[] }));
vi.mock("../airlock-bindings", () => ({ DbConnection: { builder: sdk.builder } }));
import { credentialKey, MAX_CREDENTIAL_BYTES, parseOwnerCredential, provesOwner, recoverOwnerProfile, recoveryDestination, savedOwnerProfiles, verifyOwnerCredential, workstationSelection } from "./ownerRecoveryPolicy";
const identity = "a".repeat(64), other = "b".repeat(64);
const config = { uri: "https://maincloud.spacetimedb.com", database: "locki-w91bw", profile: "operator" };
const station = { id: "desk", owner: { toHexString: () => identity } };
const membership = { workstationId: "desk", identity: station.owner, role: "owner", enabled: true };
function storage(): Storage {
  const values = new Map<string, string>();
  return { get length() { return values.size; }, clear: () => values.clear(), key: i => [...values.keys()][i] ?? null,
    getItem: k => values.get(k) ?? null, removeItem: k => { values.delete(k); }, setItem: (k, v) => { values.set(k, v); } };
}
function connect(stations = [station], memberships = [membership]) {
  sdk.context.db = { myWorkstations: { iter: () => stations }, myMemberships: { iter: () => memberships } };
  sdk.connect(sdk.context, { toHexString: () => identity }, "server-refresh-token");
}
beforeEach(() => {
  sdk.subscriptions = []; sdk.token = undefined;
  sdk.context = { disconnect: vi.fn(), db: {}, reducers: new Proxy({}, { get: () => { throw new Error("No reducers allowed"); } }), subscriptionBuilder: () => {
    const sub = { onApplied: (fn: any) => { sdk.applied = fn; return sub; }, onError: (fn: any) => { sdk.error = fn; return sub; }, subscribe: (sql: string[]) => { sdk.subscriptions = sql; } }; return sub;
  } };
  sdk.builder.mockReset().mockImplementation(() => {
    const builder = { withUri: () => builder, withDatabaseName: () => builder, withToken: (token: any) => { sdk.token = token; return builder; },
      onConnect: (fn: any) => { sdk.connect = fn; return builder; }, onConnectError: (fn: any) => { sdk.connectError = fn; return builder; },
      onDisconnect: (fn: any) => { sdk.disconnect = fn; return builder; }, build: () => sdk.context }; return builder;
  });
});
describe("local credential parsing", () => {
  it.each(["abc.def.token", '{"token":"abc.def.token"}', '{"spacetimedb_token":"abc.def.token","identity":"ignored"}', 'spacetimedb_token = "abc.def.token"\n[server]\nname="ignored"', "spacetimedb_token = 'abc.def.token' # CLI token"])("accepts supported format", raw => expect(parseOwnerCredential(raw)).toBe("abc.def.token"));
  it.each(["", "  ", "x".repeat(MAX_CREDENTIAL_BYTES + 1), '{"token":3}', '{"token":"a","spacetimedb_token":"b"}', '[server]\nspacetimedb_token="a"', 'spacetimedb_token="a"\nspacetimedb_token="b"', 'spacetimedb_token="a\\nb"', '{bad', "token with spaces"])("rejects unsupported/ambiguous credentials", raw => expect(() => parseOwnerCredential(raw)).toThrow());
});
it.each(["http://localhost:3000", "http://127.0.0.1:3000", "ws://[::1]:3000", "wss://maincloud.spacetimedb.com"])("accepts approved origin %s", uri => expect(recoveryDestination({ ...config, uri })).toContain(config.database));
it.each(["https://maincloud.spacetimedb.com.evil.test", "http://maincloud.spacetimedb.com", "https://maincloud.spacetimedb.com:444", "http://localhost.evil.test", "http://user@localhost:3000", "http://localhost:3000/path", "http://localhost:3000?key=x", "http://localhost:3000#x", "http://localhost:99999", "http://localhost:3000\n"])("blocks other destinations before SDK receives token %s", async uri => {
  await expect(verifyOwnerCredential({ ...config, uri }, "desk", "candidate", new AbortController().signal)).rejects.toThrow();
  expect(sdk.builder).not.toHaveBeenCalled();
});
it("lists only existing profiles for the exact server and database", () => {
  const saved = storage(); saved.setItem(credentialKey(config), "operator-token"); saved.setItem(credentialKey({ ...config, profile: "old-owner" }), "owner-token");
  saved.setItem(credentialKey({ ...config, database: "other", profile: "unrelated" }), "unrelated-token");
  expect(savedOwnerProfiles(saved, config)).toEqual(["old-owner", "operator"]);
});
it("requires exact requested station AND enabled owner membership", () => {
  expect(provesOwner(identity, "desk", [station], [membership])).toBe(true);
  expect(provesOwner(identity, "other-desk", [station], [membership])).toBe(false);
  expect(provesOwner(other, "desk", [station], [membership])).toBe(false);
  for (const changed of [{ role: "mac" }, { enabled: false }, { workstationId: "elsewhere" }]) expect(provesOwner(identity, "desk", [station], [{ ...membership, ...changed }])).toBe(false);
});
it("saves only after applied scoped proof into new owner profile; keeps operator and origin", async () => {
  const saved = storage(); saved.setItem(credentialKey(config), "old-token");
  const result = recoverOwnerProfile(config, "desk", "candidate", new AbortController().signal, saved, "http://127.0.0.1:5174/?client=operator&view=desk");
  expect(saved.length).toBe(1); expect(sdk.token).toBe("candidate");
  connect(); expect(saved.length).toBe(1);
  expect(sdk.subscriptions).toEqual(["SELECT * FROM my_workstations", "SELECT * FROM my_memberships"]);
  sdk.applied(); const url = new URL(await result);
  expect(url.origin).toBe("http://127.0.0.1:5174"); expect(url.searchParams.get("airlockUri")).toBe(config.uri);
  expect(url.searchParams.get("airlockDatabase")).toBe(config.database); expect(url.searchParams.get("workstation")).toBe("desk"); expect(url.searchParams.has("view")).toBe(false);
  expect(url.searchParams.get("client")).toMatch(/^owner-recovered-/); expect(url.toString()).not.toContain("candidate");
  expect(saved.getItem(credentialKey(config))).toBe("old-token"); expect(saved.length).toBe(2);
  expect(sdk.context.disconnect).toHaveBeenCalled();
});
it.each(["cancel", "error", "disconnect", "denied", "other-workstation", "cancel-after-proof"])("does not persist on %s or late callback", async reason => {
  const saved = storage(), control = new AbortController();
  const result = recoverOwnerProfile(config, "desk", "candidate", control.signal, saved, "http://localhost:5174/");
  const rejection = expect(result).rejects.toThrow();
  connect(reason === "other-workstation" ? [{ ...station, id: "unrelated-owner-desk" }] : [station], reason === "denied" ? [] : [membership]);
  if (reason === "error") sdk.error(); else if (reason === "disconnect") sdk.disconnect(); else if (reason === "cancel") control.abort();
  else { sdk.applied(); if (reason === "cancel-after-proof") control.abort(); }
  sdk.applied(); await rejection; expect(saved.length).toBe(0); expect(sdk.context.disconnect).toHaveBeenCalled();
});
it("times out and disconnects without saving", async () => {
  vi.useFakeTimers();
  try {
    const saved = storage(); const result = recoverOwnerProfile(config, "desk", "candidate", new AbortController().signal, saved, "http://localhost/");
    const rejection = expect(result).rejects.toThrow(); await vi.advanceTimersByTimeAsync(15000); await rejection;
    expect(saved.length).toBe(0); expect(sdk.context.disconnect).toHaveBeenCalled();
  } finally { vi.useRealTimers(); }
});
it("never creates a new SDK identity for an empty credential", async () => {
  await expect(verifyOwnerCredential(config, "desk", "", new AbortController().signal)).rejects.toThrow(); expect(sdk.builder).not.toHaveBeenCalled();
});
it("owner URL restores the verified destination rather than stale shared connection settings", async () => {
  const saved = storage(); saved.setItem("airlock.connection.v1", JSON.stringify({ uri: "http://localhost:3000", database: "old-local" }));
  vi.stubGlobal("location", { search: `?client=owner-recovered-test&airlockUri=${encodeURIComponent(config.uri)}&airlockDatabase=${config.database}` });
  vi.stubGlobal("localStorage", saved);
  try {
    const { readConfig, storedConnectionToken } = await import("./client");
    const recovered = { ...config, profile: "owner-recovered-test" };
    expect(readConfig()).toEqual(recovered);
    // Another browser may have an operator/other-owner token, but must never
    // fall back to either or create an anonymous identity for this owner URL.
    saved.setItem(credentialKey(config), "operator-token");
    saved.setItem(credentialKey({ ...recovered, profile: "owner-recovered-other" }), "other-token");
    expect(() => storedConnectionToken(recovered, saved)).toThrow("Recover existing owner access in this browser");
    expect(sdk.builder).not.toHaveBeenCalled();
    saved.setItem(credentialKey(recovered), " ");
    expect(() => storedConnectionToken(recovered, saved)).toThrow("credential is missing");
    saved.setItem(credentialKey(recovered), "verified-owner-token");
    expect(storedConnectionToken(recovered, saved)).toBe("verified-owner-token");
    expect(storedConnectionToken(config, saved)).toBe("operator-token");
  } finally { vi.unstubAllGlobals(); }
});
it("cleans up synchronous subscription setup failures without leaking SDK errors", async () => {
  const result = verifyOwnerCredential(config, "desk", "candidate", new AbortController().signal);
  sdk.context.subscriptionBuilder = () => { throw new Error("sensitive SDK details"); };
  connect();
  await expect(result).rejects.toThrow("Owner verification failed");
  expect(sdk.context.disconnect).toHaveBeenCalled();
});

it("an unavailable explicit target never selects a different desk owned by this identity", () => {
  expect(workstationSelection([station], "requested-other-desk")).toEqual({ station: undefined, targetUnavailable: true, mayCreate: false });
});
it("two visible desks cannot substitute for an unavailable explicit target", () => {
  expect(workstationSelection([station, { ...station, id: "second" }], "missing")).toEqual({ station: undefined, targetUnavailable: true, mayCreate: false });
});
it("a valid explicit target selects exactly that desk regardless of list order", () => {
  const requested = { ...station, id: "second" };
  expect(workstationSelection([station, requested], "second")).toEqual({ station: requested, targetUnavailable: false, mayCreate: false });
});
it("only untargeted fresh users get creation; untargeted existing users retain first desk default", () => {
  expect(workstationSelection([], "")).toEqual({ station: undefined, targetUnavailable: false, mayCreate: true });
  expect(workstationSelection([station], "")).toEqual({ station, targetUnavailable: false, mayCreate: false });
  expect(workstationSelection([], "existing-desk").mayCreate).toBe(false);
});
