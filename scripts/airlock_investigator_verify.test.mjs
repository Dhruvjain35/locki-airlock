import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtemp, writeFile, chmod, symlink, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DEFAULT_INVESTIGATOR, InvestigatorReadClient, readInvestigatorToken, verifyInvestigator, verificationOrigin } from './airlock_investigator_verify.mjs';

const privateTables = ['incident_evidence_grants', 'incidents', 'device_events', 'baseline_snapshots', 'memberships', 'devices', 'lock_requests', 'protected_actions', 'approvals'];
const oldViews = ['my_memberships', 'my_workstations', 'my_devices', 'my_incidents', 'my_device_events', 'my_lock_requests', 'my_protected_actions', 'my_approvals'];
const targetViews = ['my_investigator_incidents', 'my_investigator_events', 'my_investigator_baselines'];
const caseRow = { id: 'incident:test', workstation_id: 'dhruv-m4-air', session_id: 'session', severity: 'high', state: 'open',
  created_at_ms: 1000, updated_at_ms: 1001, evidence_approved_at_ms: 1001, simulated: false, evidence_complete: true, approved_event_ids_json: '["event"]' };
const eventRow = { id: 'event', workstation_id: 'dhruv-m4-air', session_id: 'session', device_id: 'camera', kind: 'person_in_zone', source: 'jetson',
  observed_at_ms: 1000, received_at_ms: 1001, seq: 1, schema_version: 1, simulated: false, metadata_json: '{"detail":"private-approved-detail"}' };
const baselineRow = { id: 'session:session', workstation_id: 'dhruv-m4-air', session_id: 'session', device_id: 'mac', observed_at_ms: 900, received_at_ms: 901, baseline_json: '[]', active_app_id: 'app' };
function result(rows) {
  const names = rows.length ? Object.keys(rows[0]) : ['id'];
  return [{ schema: { elements: names.map(name => ({ name: { some: name } })) }, rows: rows.map(row => names.map(name => row[name])) }];
}
function fixture(overrides = {}) {
  const calls = []; let incidentReads = 0;
  const schema = { tables: privateTables.map(name => ({ name, table_access: { Private: [] } })), misc_exports: [...oldViews, ...targetViews].map(name => ({ View: { name } })) };
  const fetchImpl = async (url, options) => {
    calls.push({ url, method: options.method, body: options.body, redirect: options.redirect });
    assert.equal(options.redirect, 'error');
    if (url.endsWith('/schema?version=9')) return Response.json(schema, { headers: { 'spacetime-identity': overrides.identity || DEFAULT_INVESTIGATOR } });
    assert.ok(url.endsWith('/sql')); assert.equal(options.method, 'POST'); assert.match(options.body, /^SELECT /);
    const name = options.body.split(' ').at(-1);
    if (privateTables.includes(name)) {
      if (overrides.rawAccess) return Response.json(result([{ id: 'private' }]));
      if (overrides.rawFailure) return new Response('upstream unavailable', { status: 503 });
      if (overrides.expiredToken) return new Response('unauthorized', { status: 401 });
      return new Response(`No such table: ${name}`, { status: 400 });
    }
    if (oldViews.includes(name)) return Response.json(result(overrides.oldAccess ? [{ id: 'forbidden' }] : []));
    if (name === 'my_investigator_incidents') {
      incidentReads++;
      return Response.json(result(overrides.revokedDuringRead && incidentReads > 1 ? [] : [{ ...caseRow, ...overrides.incident }]));
    }
    if (name === 'my_investigator_events') return Response.json(result([{ ...eventRow, ...overrides.event }]));
    if (name === 'my_investigator_baselines') return Response.json(result([{ ...baselineRow, ...overrides.baseline }]));
    throw new Error('unexpected query');
  };
  return { fetchImpl, calls };
}
const verify = options => verifyInvestigator({ token: 'TOKEN_MUST_NEVER_APPEAR_IN_REPORT', incidentId: 'incident:test', ...options });

test('returns exact approved IDs/timestamps, verifies denied reads, and performs no mutations', async () => {
  const f = fixture(); const report = await verify(f);
  assert.equal(report.passed, true); assert.equal(report.reducer_calls, 0);
  assert.equal(report.events[0].event_id, 'event'); assert.equal(report.events[0].observed_at_ms, 1000);
  assert.equal(report.events[0].received_at_ms, 1001); assert.equal(report.events[0].simulated, false);
  assert.equal(report.private_table_denials.length, privateTables.length);
  assert.deepEqual(report.empty_existing_views, oldViews);
  assert.ok(f.calls.every(call => call.url.includes('/schema?') || call.url.endsWith('/sql')));
  assert.doesNotMatch(JSON.stringify(report), /TOKEN_MUST|private-approved-detail/);
});

test('wrong authenticated identity stops before any SQL request', async () => {
  const f = fixture({ identity: 'c'.repeat(64) });
  await assert.rejects(verify(f), { code: 'AUTHENTICATED_IDENTITY_MISMATCH' }); assert.equal(f.calls.length, 1);
});

for (const [label, overrides, code] of [
  ['other workstation case', { incident: { workstation_id: 'other' } }, 'INCIDENT_SCOPE_LEAK'],
  ['unapproved event', { event: { id: 'unapproved' } }, 'EVENT_SCOPE_LEAK'],
  ['other session event', { event: { session_id: 'other' } }, 'EVENT_SCOPE_LEAK'],
  ['simulation mismatch', { event: { simulated: true } }, 'EVENT_SCOPE_LEAK'],
  ['latest baseline substitution', { baseline: { id: 'latest:mac' } }, 'BASELINE_SCOPE_LEAK'],
  ['unsafe timestamp precision', { event: { observed_at_ms: Number.MAX_SAFE_INTEGER + 1 } }, 'UNSAFE_EVENT_CAPTURE_TIME'],
  ['existing member view accessible', { oldAccess: true }, 'UNEXPECTED_ACCESS_my_memberships'],
  ['raw private table accessible', { rawAccess: true }, 'RAW_PRIVATE_TABLE_ACCESS_incident_evidence_grants'],
  ['server outage is not denial', { rawFailure: true }, 'HTTP_REQUEST_FAILED'],
  ['expired token is not authorization proof', { expiredToken: true }, 'HTTP_REQUEST_FAILED'],
  ['revocation during read', { revokedDuringRead: true }, 'GRANT_CHANGED_DURING_VERIFICATION'],
]) test(`fails closed: ${label}`, async () => {
  await assert.rejects(verify(fixture(overrides)), { code });
});

test('transport rejects all reducer and identity-creation calls before network', async () => {
  let calls = 0;
  const client = new InvestigatorReadClient({ server: 'https://maincloud.spacetimedb.com', database: 'locki-w91bw', token: 'private', fetchImpl: async () => { calls++; throw new Error('must not run'); } });
  await assert.rejects(client.call('arm_session', []), { code: 'NON_READ_OPERATION_FORBIDDEN' });
  await assert.rejects(client.newIdentity(), { code: 'NON_READ_OPERATION_FORBIDDEN' });
  await assert.rejects(client.sql('DELETE FROM incidents'), { code: 'NON_READ_OPERATION_FORBIDDEN' });
  assert.equal(calls, 0);
});

test('rejects nonlocal cleartext, embedded credentials, paths and query-bearing origins', () => {
  for (const server of ['http://example.com', 'https://user:secret@example.com', 'https://example.com/path', 'https://example.com?token=secret']) assert.throws(() => verificationOrigin(server));
  assert.equal(verificationOrigin('http://127.0.0.1:3000'), 'http://127.0.0.1:3000');
});

test('token reader rejects world-readable files and symlinks', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'airlock-investigator-'));
  try {
    const path = join(dir, 'token.json'), link = join(dir, 'token-link.json');
    await writeFile(path, JSON.stringify({ token: 'private-token-long-enough' }), { mode: 0o600 });
    assert.equal(await readInvestigatorToken(path), 'private-token-long-enough');
    await chmod(path, 0o644); await assert.rejects(readInvestigatorToken(path), { code: 'TOKEN_FILE_MUST_BE_PRIVATE_OWNED_REGULAR_FILE' });
    await chmod(path, 0o600); await symlink(path, link); await assert.rejects(readInvestigatorToken(link));
  } finally { await rm(dir, { recursive: true, force: true }); }
});
