#!/usr/bin/env node
/** Read-only investigator authorization verification. Never calls a reducer. */
import { constants } from 'node:fs';
import { open, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs } from 'node:util';
import { AirlockHttp } from './airlock_http.mjs';

export const DEFAULT_INVESTIGATOR = 'c200b5b1765c4d0dbc9951485cfe94e3b47977725051b23037df5b6043e88883';
const INVESTIGATOR_VIEWS = ['my_investigator_incidents', 'my_investigator_events', 'my_investigator_baselines'];
const REQUIRED_PRIVATE = ['incident_evidence_grants', 'incidents', 'device_events', 'baseline_snapshots', 'memberships', 'devices', 'lock_requests', 'protected_actions', 'approvals'];
const MAX_RESPONSE_BYTES = 8 * 1024 * 1024;
const id = value => typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,128}$/.test(value);
const sha256 = value => createHash('sha256').update(value).digest('hex');

export class VerificationError extends Error {
  constructor(code, status) { super(code); this.code = code; this.status = status; }
}
function requireCheck(value, code) { if (!value) throw new VerificationError(code); }

export async function readInvestigatorToken(path) {
  const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const info = await file.stat();
    requireCheck(info.isFile() && info.uid === process.getuid() && !(info.mode & 0o077) && info.size <= 65536, 'TOKEN_FILE_MUST_BE_PRIVATE_OWNED_REGULAR_FILE');
    const saved = JSON.parse(await file.readFile('utf8'));
    requireCheck(typeof saved.token === 'string' && saved.token.length >= 16 && !/[\r\n]/.test(saved.token), 'TOKEN_FILE_MISSING_TOKEN');
    return saved.token;
  } finally { await file.close(); }
}

export function verificationOrigin(server) {
  const url = new URL(server);
  requireCheck(!url.username && !url.password && !url.search && !url.hash && ['/', ''].includes(url.pathname), 'SERVER_MUST_BE_AN_ORIGIN');
  requireCheck(url.protocol === 'https:' || (url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)), 'HTTPS_REQUIRED_EXCEPT_LOOPBACK');
  return url.origin;
}

async function boundedBody(response) {
  requireCheck(Number(response.headers.get('content-length') || 0) <= MAX_RESPONSE_BYTES, 'RESPONSE_TOO_LARGE');
  if (!response.body) return '';
  const reader = response.body.getReader(); const chunks = []; let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read(); if (done) break;
      size += value.byteLength; requireCheck(size <= MAX_RESPONSE_BYTES, 'RESPONSE_TOO_LARGE'); chunks.push(value);
    }
    return Buffer.concat(chunks).toString('utf8');
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
}

/** Restricts even accidental use of inherited methods to schema GET and SELECT. */
export class InvestigatorReadClient extends AirlockHttp {
  constructor({ server, database, token, fetchImpl = fetch }) {
    requireCheck(id(database), 'INVALID_DATABASE');
    super({ uri: verificationOrigin(server), database, token });
    this.fetchImpl = fetchImpl;
    this.prefix = `/v1/database/${encodeURIComponent(database)}`;
  }
  async response(path, body, contentType = 'application/json') {
    const schemaRead = path === `${this.prefix}/schema?version=9` && body === undefined;
    const outsideRead = typeof body === 'string' && /^SELECT \* FROM (my_investigator_incidents|my_investigator_events|my_investigator_baselines) WHERE workstation_id = '[A-Za-z0-9_.:-]{1,128}'$/.test(body);
    const sqlRead = path === `${this.prefix}/sql` && typeof body === 'string' && (/^SELECT (?:\*|id) FROM [a-z_]+$/.test(body) || outsideRead);
    requireCheck(schemaRead || sqlRead, 'NON_READ_OPERATION_FORBIDDEN');
    const response = await this.fetchImpl(`${this.uri}${path}`, {
      method: body === undefined ? 'GET' : 'POST', redirect: 'error',
      headers: { Authorization: `Bearer ${this.token}`, ...(body === undefined ? {} : { 'Content-Type': contentType }) },
      body, signal: AbortSignal.timeout(15000),
    });
    const raw = await boundedBody(response);
    if (!response.ok) {
      const denial = [400, 403, 404].includes(response.status) && /permission|unauthorized|not authorized|private|not found|no such table|unknown table|not accessible/i.test(raw);
      throw new VerificationError(denial ? 'SQL_ACCESS_DENIED' : 'HTTP_REQUEST_FAILED', response.status);
    }
    return { headers: response.headers, data: raw ? JSON.parse(raw) : undefined };
  }
  async request(path, body, contentType) { return (await this.response(path, body, contentType)).data; }
  async authenticatedSchema() { return this.response(`${this.prefix}/schema?version=9`); }
}

export function sqlRows(result) {
  requireCheck(Array.isArray(result) && result.length === 1, 'INVALID_SQL_RESPONSE');
  const statement = result[0], names = statement.schema?.elements?.map(field => field.name?.some);
  requireCheck(Array.isArray(names) && names.every(name => typeof name === 'string') && new Set(names).size === names.length && Array.isArray(statement.rows), 'INVALID_SQL_SCHEMA');
  return statement.rows.map(row => {
    requireCheck(Array.isArray(row) && row.length === names.length, 'INVALID_SQL_ROW');
    return Object.fromEntries(names.map((name, index) => [name, row[index]]));
  });
}
function safeInteger(value, name) {
  requireCheck(Number.isSafeInteger(value) && value >= 0, `UNSAFE_${name}`); return value;
}
function approvedIds(incident) {
  const ids = JSON.parse(incident.approved_event_ids_json);
  requireCheck(Array.isArray(ids) && ids.length <= 256 && ids.every(id) && new Set(ids).size === ids.length, 'INVALID_APPROVED_EVENT_IDS');
  return ids;
}
function grantScope(incident) {
  return JSON.stringify([incident.id, incident.workstation_id, incident.session_id, incident.created_at_ms,
    incident.evidence_approved_at_ms, approvedIds(incident).sort()]);
}

export async function verifyInvestigator({ token, incidentId, workstation = 'dhruv-m4-air', expectedIdentity = DEFAULT_INVESTIGATOR,
  server = 'https://maincloud.spacetimedb.com', database = 'locki-w91bw', outsideWorkstation, fetchImpl }) {
  requireCheck(id(incidentId) && id(workstation) && /^[a-f0-9]{64}$/i.test(expectedIdentity), 'INVALID_EXPECTED_SCOPE');
  requireCheck(outsideWorkstation === undefined || (id(outsideWorkstation) && outsideWorkstation !== workstation), 'INVALID_OUTSIDE_WORKSTATION');
  requireCheck(typeof token === 'string' && token.length >= 16 && !/[\r\n]/.test(token), 'INVALID_TOKEN');
  const client = new InvestigatorReadClient({ server, database, token, fetchImpl });
  const { headers, data: schema } = await client.authenticatedSchema();
  requireCheck(headers.get('spacetime-identity')?.replace(/^0x/, '').toLowerCase() === expectedIdentity.toLowerCase(), 'AUTHENTICATED_IDENTITY_MISMATCH');
  const viewNames = schema.misc_exports?.flatMap(entry => entry.View ? [entry.View.name] : []) || [];
  requireCheck(INVESTIGATOR_VIEWS.every(view => viewNames.includes(view)), 'INVESTIGATOR_CONTRACT_NOT_DEPLOYED');
  const tables = new Map((schema.tables || []).map(table => [table.name, table]));
  requireCheck(REQUIRED_PRIVATE.every(name => tables.get(name)?.table_access?.Private !== undefined), 'REQUIRED_TABLE_NOT_PRIVATE');
  const read = async name => sqlRows(await client.sql(`SELECT * FROM ${name}`));
  const cases = await read('my_investigator_incidents');
  requireCheck(cases.every(row => row.workstation_id === workstation && id(row.id) && id(row.session_id)), 'INCIDENT_SCOPE_LEAK');
  requireCheck(new Set(cases.map(row => row.id)).size === cases.length, 'DUPLICATE_INCIDENT_ROWS');
  const target = cases.find(row => row.id === incidentId); requireCheck(target, 'REQUESTED_INCIDENT_NOT_GRANTED');
  for (const row of cases) {
    approvedIds(row); safeInteger(row.created_at_ms, 'INCIDENT_CAPTURE_TIME'); safeInteger(row.evidence_approved_at_ms, 'APPROVAL_TIME');
    requireCheck(typeof row.simulated === 'boolean' && typeof row.evidence_complete === 'boolean', 'INVALID_INCIDENT_PROVENANCE');
  }
  const events = await read('my_investigator_events');
  requireCheck(new Set(events.map(row => row.id)).size === events.length, 'DUPLICATE_EVENT_ROWS');
  for (const event of events) {
    requireCheck(event.workstation_id === workstation && cases.some(row => row.session_id === event.session_id && row.simulated === event.simulated && approvedIds(row).includes(event.id)), 'EVENT_SCOPE_LEAK');
    safeInteger(event.observed_at_ms, 'EVENT_CAPTURE_TIME'); safeInteger(event.received_at_ms, 'EVENT_RECEIPT_TIME');
    safeInteger(event.seq, 'EVENT_SEQUENCE');
    requireCheck(event.seq >= 1 && event.schema_version === 1 && typeof event.metadata_json === 'string' && typeof event.simulated === 'boolean', 'INVALID_EVENT_ENVELOPE');
    requireCheck(event.observed_at_ms <= event.received_at_ms + 5000 && event.observed_at_ms + 120000 >= event.received_at_ms, 'INVALID_EVENT_CAPTURE_RECEIPT_TIMING');
  }
  const baselines = await read('my_investigator_baselines');
  for (const baseline of baselines) {
    requireCheck(baseline.workstation_id === workstation && baseline.id === `session:${baseline.session_id}` && cases.some(row => row.session_id === baseline.session_id), 'BASELINE_SCOPE_LEAK');
    safeInteger(baseline.observed_at_ms, 'BASELINE_CAPTURE_TIME'); safeInteger(baseline.received_at_ms, 'BASELINE_RECEIPT_TIME');
  }
  // These are successful scoped reads returning no rows, not transport-error denials.
  // The caller supplies an independently known existing workstation; these views
  // intentionally cannot establish whether an inaccessible workstation exists.
  const outsideCheck = { requested: outsideWorkstation !== undefined, performed: false };
  if (outsideWorkstation !== undefined) {
    const checks = [];
    for (const view of INVESTIGATOR_VIEWS) {
      const rows = sqlRows(await client.sql(`SELECT * FROM ${view} WHERE workstation_id = '${outsideWorkstation}'`));
      requireCheck(rows.length === 0, `OUTSIDE_WORKSTATION_ACCESS_${view}`);
      checks.push({ view, row_count: 0 });
    }
    Object.assign(outsideCheck, { performed: true, workstation_id: outsideWorkstation, existence_verified: false, views: checks });
  }
  const deniedViews = viewNames.filter(name => name.startsWith('my_') && !INVESTIGATOR_VIEWS.includes(name));
  requireCheck(['my_memberships', 'my_workstations', 'my_devices', 'my_incidents', 'my_device_events', 'my_lock_requests', 'my_protected_actions', 'my_approvals'].every(name => deniedViews.includes(name)), 'EXPECTED_NEGATIVE_VIEWS_MISSING');
  for (const view of deniedViews) requireCheck((await read(view)).length === 0, `UNEXPECTED_ACCESS_${view}`);
  const privateChecks = [];
  for (const name of REQUIRED_PRIVATE) {
    try { await client.sql(`SELECT id FROM ${name}`); }
    catch (error) {
      if (error instanceof VerificationError && error.code === 'SQL_ACCESS_DENIED') { privateChecks.push({ table: name, denied: true, http_status: error.status }); continue; }
      throw error;
    }
    throw new VerificationError(`RAW_PRIVATE_TABLE_ACCESS_${name}`);
  }
  const finalCases = await read('my_investigator_incidents');
  requireCheck(finalCases.every(row => row.workstation_id === workstation), 'INCIDENT_SCOPE_LEAK');
  const finalTarget = finalCases.find(row => row.id === incidentId);
  requireCheck(finalTarget && grantScope(finalTarget) === grantScope(target), 'GRANT_CHANGED_DURING_VERIFICATION');
  const approved = approvedIds(target), selected = events.filter(event => approved.includes(event.id));
  requireCheck(approved.every(eventId => selected.some(event => event.id === eventId)), 'APPROVED_EVIDENCE_MISSING_OR_CHANGED');
  return {
    passed: true, checked_at: new Date().toISOString(), server: client.uri, database, identity: expectedIdentity.toLowerCase(),
    workstation_id: workstation, incident_id: incidentId, session_id: target.session_id,
    severity: finalTarget.severity, state: finalTarget.state, simulated: target.simulated, evidence_complete: finalTarget.evidence_complete,
    evidence_approved_at_ms: target.evidence_approved_at_ms, approved_event_ids: approved,
    events: selected.map(event => ({ event_id: event.id, device_id: event.device_id, kind: event.kind, source: event.source,
      observed_at_ms: event.observed_at_ms, received_at_ms: event.received_at_ms, seq: event.seq, simulated: event.simulated,
      metadata_sha256: sha256(event.metadata_json) })),
    baselines: baselines.filter(row => row.session_id === target.session_id).map(row => ({ id: row.id, device_id: row.device_id,
      observed_at_ms: row.observed_at_ms, received_at_ms: row.received_at_ms, baseline_sha256: sha256(row.baseline_json) })),
    empty_existing_views: deniedViews, private_table_denials: privateChecks, outside_workstation_check: outsideCheck, reducer_calls: 0,
    proof_boundary: 'Authenticated read-only API scope and stored timestamps only. No proof of camera capture, model inference, or tool execution. Revocation cannot recall previously read data.',
  };
}

async function main() {
  const { values } = parseArgs({ options: {
    'token-file': { type: 'string' }, incident: { type: 'string' }, workstation: { type: 'string' },
    'expected-identity': { type: 'string' }, 'outside-workstation': { type: 'string' }, server: { type: 'string' }, database: { type: 'string' },
    output: { type: 'string' }, help: { type: 'boolean' },
  } });
  if (values.help) {
    console.log('Read-only investigator verification; never calls reducers or creates credentials.\nnode scripts/airlock_investigator_verify.mjs --token-file PRIVATE.json --incident INCIDENT_ID [--output NEW_REPORT.json] [--outside-workstation KNOWN_EXISTING_ID]\nDefaults: maincloud locki-w91bw, dhruv-m4-air, expected Mustafa identity.\n--outside-workstation explicitly checks all three investigator views return no rows for another workstation; supply an independently verified existing ID.\nExplicit test overrides: --server ORIGIN --database NAME --workstation ID --expected-identity HEX.'); return;
  }
  requireCheck(values['token-file'] && values.incident, 'TOKEN_FILE_AND_INCIDENT_REQUIRED');
  const report = await verifyInvestigator({ token: await readInvestigatorToken(values['token-file']), incidentId: values.incident,
    workstation: values.workstation, expectedIdentity: values['expected-identity'], server: values.server, database: values.database,
    outsideWorkstation: values['outside-workstation'] });
  const json = JSON.stringify(report, null, 2) + '\n';
  if (values.output) await writeFile(values.output, json, { mode: 0o600, flag: 'wx' });
  console.log(json.trimEnd());
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => { console.error(JSON.stringify({ passed: false, code: error instanceof VerificationError ? error.code : 'VERIFICATION_FAILED', ...(error instanceof VerificationError && error.status ? { http_status: error.status } : {}), reducer_calls: 0 })); process.exitCode = 1; });
}
