import { DbConnection } from "../airlock-bindings";
import type { ConnectionConfig } from "./client";
import { textValue, type Row } from "./format";

export const MAX_CREDENTIAL_BYTES = 65536;
export const credentialKey = (c: ConnectionConfig) => `airlock.token.v1:${c.uri}:${c.database}:${c.profile}`;
const validToken = (value: unknown): string => {
  if (typeof value !== "string" || !/^[A-Za-z0-9._~+\/-]+={0,2}$/.test(value.trim()) || value.length > MAX_CREDENTIAL_BYTES)
    throw new Error("No single valid credential token was found.");
  return value.trim();
};

export function parseOwnerCredential(contents: string): string {
  if (new TextEncoder().encode(contents).length > MAX_CREDENTIAL_BYTES) throw new Error("Credential file exceeds 64 KiB.");
  const value = contents.trim();
  if (value.startsWith("{")) {
    let parsed: unknown;
    try { parsed = JSON.parse(value); } catch { throw new Error("Invalid credential JSON."); }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("Invalid credential JSON.");
    const obj = parsed as Record<string, unknown>;
    const candidates = [obj.token, obj.spacetimedb_token].filter(v => v !== undefined);
    if (candidates.length !== 1) throw new Error("Credential JSON must contain exactly one token field.");
    return validToken(candidates[0]);
  }
  if (/^[A-Za-z0-9._~+\/-]+={0,2}$/.test(value)) return validToken(value);
  // CLI TOML: only a single quoted top-level spacetimedb_token is accepted.
  let inTable = false;
  const tokens: string[] = [];
  for (const line of value.split(/\r?\n/)) {
    const stripped = line.trim();
    if (stripped.startsWith("[")) inTable = true;
    if (inTable || !/^spacetimedb_token\s*=/.test(stripped)) continue;
    const match = stripped.match(/^spacetimedb_token\s*=\s*("[^"\\]*"|'[^']*')\s*(?:#.*)?$/);
    if (!match) throw new Error("Unsupported CLI credential syntax.");
    tokens.push(validToken(match[1].slice(1, -1)));
  }
  if (tokens.length !== 1) throw new Error("Expected one top-level CLI spacetimedb_token.");
  return tokens[0];
}

export function recoveryDestination(config: ConnectionConfig): string {
  let url: URL;
  try { url = new URL(config.uri); } catch { throw new Error("Invalid database destination."); }
  const cloud = ["https:", "wss:"].includes(url.protocol) && url.hostname === "maincloud.spacetimedb.com" && !url.port;
  const local = ["http:", "https:", "ws:", "wss:"].includes(url.protocol) && ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname);
  if ((!cloud && !local) || url.username || url.password || url.pathname !== "/" || url.search || url.hash
      || /[\s\u0000-\u001f\u007f]/.test(config.uri) || !/^[A-Za-z0-9_-]+$/.test(config.database))
    throw new Error("Recovery supports only maincloud.spacetimedb.com or an exact loopback database origin.");
  return `${url.origin} / ${config.database}`;
}

export function savedOwnerProfiles(storage: Storage, config: ConnectionConfig): string[] {
  const prefix = credentialKey({ ...config, profile: "" });
  const profiles: string[] = [];
  for (let i = 0; i < storage.length; i++) {
    const key = storage.key(i);
    if (key?.startsWith(prefix) && key.length > prefix.length) profiles.push(key.slice(prefix.length));
  }
  return profiles.sort();
}

export function provesOwner(identity: string, workstationId: string, stations: Row[], memberships: Row[]): boolean {
  return /^[0-9a-f]{64}$/i.test(identity) && stations.some(s => s.id === workstationId && textValue(s.owner) === identity)
    && memberships.some(m => m.workstationId === workstationId && textValue(m.identity) === identity && m.role === "owner" && m.enabled === true);
}

export async function verifyOwnerCredential(config: ConnectionConfig, workstationId: string, token: string, signal: AbortSignal): Promise<string> {
  recoveryDestination(config);
  token = validToken(token);
  if (!workstationId.trim() || workstationId.length > 256) throw new Error("Enter the exact existing workstation ID.");
  if (signal.aborted) throw new Error("Recovery cancelled.");
  return new Promise((resolve, reject) => {
    let conn: DbConnection | undefined;
    let settled = false;
    let authenticated = "";
    const finish = (identity?: string) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal.removeEventListener("abort", cancel);
      conn?.disconnect();
      if (identity && !signal.aborted) resolve(identity);
      else reject(new Error("Owner verification failed or was cancelled. Check the credential, workstation and destination."));
    };
    const cancel = () => finish();
    const timer = setTimeout(cancel, 15000);
    signal.addEventListener("abort", cancel, { once: true });
    try {
      conn = DbConnection.builder().withUri(config.uri).withDatabaseName(config.database).withToken(token)
        .onConnect((ctx, id) => {
          if (settled || signal.aborted) { ctx.disconnect(); return; }
          try {
            authenticated = id.toHexString();
            ctx.subscriptionBuilder().onApplied(() => {
              if (settled) return;
              try {
                finish(provesOwner(authenticated, workstationId, [...ctx.db.myWorkstations.iter()], [...ctx.db.myMemberships.iter()]) ? authenticated : undefined);
              } catch { finish(); }
            }).onError(() => finish()).subscribe(["SELECT * FROM my_workstations", "SELECT * FROM my_memberships"]);
          } catch { finish(); }
        }).onConnectError(() => finish()).onDisconnect(() => finish()).build();
      if (settled) conn.disconnect();
    } catch { finish(); }
  });
}

export async function recoverOwnerProfile(config: ConnectionConfig, workstationId: string, token: string, signal: AbortSignal,
  storage: Storage, href: string): Promise<string> {
  await verifyOwnerCredential(config, workstationId, token, signal);
  if (signal.aborted) throw new Error("Recovery cancelled.");
  const profile = `owner-recovered-${crypto.randomUUID()}`;
  const key = credentialKey({ ...config, profile });
  if (storage.getItem(key) !== null) throw new Error("Owner profile already exists; retry recovery.");
  const url = new URL(href);
  url.searchParams.set("client", profile);
  url.searchParams.set("airlockUri", config.uri);
  url.searchParams.set("airlockDatabase", config.database);
  url.searchParams.set("workstation", workstationId);
  url.searchParams.set("product", "airlock");
  url.searchParams.delete("view");
  storage.setItem(key, validToken(token));
  return url.toString();
}

/** An explicit target must never silently resolve to a different owned desk. */
export function workstationSelection(stations: Row[], requestedId: string) {
  const station = requestedId ? stations.find(row => row.id === requestedId) : stations[0];
  return { station, targetUnavailable: Boolean(requestedId && !station), mayCreate: !requestedId && !station };
}
