import { expect, it } from 'vitest';
import { ConnectionId, Timestamp } from 'spacetimedb';
import { createModuleTestHarness, TestAuth } from 'spacetimedb/server/test-utils';
import spacetimedb, * as moduleExports from '../src/index';

const auth = (name: string, id: bigint) => TestAuth.fromJwtPayload(JSON.stringify({ iss: 'https://evidence.test', sub: name }), new ConnectionId(id));
const owner = auth('owner', 1n), investigator = auth('investigator', 2n), outsider = auth('outsider', 3n);
const mac = auth('mac', 4n), camera = auth('camera', 5n), service = auth('service', 6n), reviewer = auth('reviewer', 7n);
const BASE = 1_000_000n;
function harness() {
  const test = createModuleTestHarness(spacetimedb, moduleExports);
  test.clock.set(new Timestamp(BASE * 1000n)); return test;
}
type Harness = ReturnType<typeof harness>;
function hex(test: Harness, who = investigator) { return test.viewContext(who).sender.toHexString(); }
function enroll(test: Harness, who: typeof mac, deviceId: string, role: string, ws = 'desk') {
  const identityHex = hex(test, who);
  test.withReducerTx(owner, ctx => moduleExports.enroll_device(ctx, { workstationId: ws, deviceId, identityHex, role, label: deviceId }));
}
function seedStation(test: Harness, ws = 'desk', autoLockEnabled = false) {
  test.withReducerTx(owner, ctx => moduleExports.register_workstation(ctx, { workstationId: ws, name: ws }));
  enroll(test, mac, `${ws}-mac`, 'mac', ws); enroll(test, camera, `${ws}-camera`, 'jetson', ws);
  test.withReducerTx(owner, ctx => moduleExports.set_policy(ctx, { workstationId: ws, autoLockEnabled, lockCooldownMs: 30_000n }));
  test.withReducerTx(mac, ctx => moduleExports.record_baseline(ctx, { workstationId: ws, deviceId: `${ws}-mac`, observedAtMs: BASE, baselineJson: '["fixture-approved"]', activeAppId: 'fixture.app' }));
  test.withReducerTx(owner, ctx => moduleExports.arm_session(ctx, { workstationId: ws, sessionId: `${ws}-session`, baselineJson: '["fixture-approved"]', approvedAppsJson: '[]', durationMs: 600_000n, simulated: false }));
  emit(test, `${ws}-usb`, 1n, 'usb_added', ws);
  emit(test, `${ws}-app`, 2n, 'app_launched', ws);
}
function setup(autoLockEnabled = false) { const test = harness(); seedStation(test, 'desk', autoLockEnabled); return test; }
function emit(test: Harness, eventId: string, seq: bigint, kind: string, ws = 'desk') {
  const physical = kind === 'person_in_zone';
  const metadataJson = JSON.stringify(kind === 'usb_added' ? { deviceKey: 'fixture-unknown', detail: 'UNIT TEST ONLY' } : kind === 'app_launched' ? { appId: 'fixture.unapproved', detail: 'UNIT TEST ONLY' } : { detail: 'UNIT TEST ONLY; no camera opened' });
  test.withReducerTx(physical ? camera : mac, ctx => moduleExports.ingest_device_event(ctx, { workstationId: ws, sessionId: `${ws}-session`, deviceId: `${ws}-${physical ? 'camera' : 'mac'}`, eventId, kind, observedAtMs: test.clock.now().microsSinceUnixEpoch / 1000n, seq, confidence: 1, metadataJson, simulated: false, schemaVersion: 1 }));
}
function grant(test: Harness, overrides = {}, who = owner) {
  const input = { workstationId: 'desk', identityHex: hex(test), incidentId: 'incident:desk-usb', eventIdsJson: '["desk-usb"]', includeBaseline: true, ...overrides };
  test.withReducerTx(who, ctx => moduleExports.grant_incident_evidence(ctx, input));
}
function revoke(test: Harness, overrides = {}, who = owner) {
  const input = { workstationId: 'desk', identityHex: hex(test), incidentId: 'incident:desk-usb', ...overrides };
  test.withReducerTx(who, ctx => moduleExports.revoke_incident_evidence(ctx, input));
}
const views = [moduleExports.my_investigator_incidents, moduleExports.my_investigator_events, moduleExports.my_investigator_baselines];
function expectEmpty(test: Harness, who = investigator) { for (const view of views) expect(view(test.viewContext(who))).toEqual([]); }

it('grants only the approved case, event and minimized frozen baseline without membership', () => {
  const test = setup(); seedStation(test, 'other'); grant(test);
  expect([...test.db.memberships.identity.filter(test.viewContext(investigator).sender)]).toHaveLength(0);
  expect([...test.db.devices.identity.filter(test.viewContext(investigator).sender)]).toHaveLength(0);
  const cases = moduleExports.my_investigator_incidents(test.viewContext(investigator));
  expect(cases.map(i => i.id)).toEqual(['incident:desk-usb']); expect(cases[0].evidenceComplete).toBe(true);
  expect(cases[0]).not.toHaveProperty('summary'); expect(cases[0]).not.toHaveProperty('resolutionNote');
  expect(cases[0]).not.toHaveProperty('physicalEventId'); expect(cases[0]).not.toHaveProperty('acknowledgedBy');
  const events = moduleExports.my_investigator_events(test.viewContext(investigator));
  expect(events.map(e => e.id)).toEqual(['desk-usb']); expect(events[0].observedAtMs).toBe(BASE); expect(events[0].simulated).toBe(false);
  const baseline = moduleExports.my_investigator_baselines(test.viewContext(investigator));
  expect(baseline.map(b => b.id)).toEqual(['session:desk-session']);
  expect(baseline[0]).not.toHaveProperty('publisher'); expect(baseline[0]).not.toHaveProperty('healthJson');
  expect(baseline[0].baselineJson).toBe('["fixture-approved"]');
  expectEmpty(test, outsider); expectEmpty(test, owner); expectEmpty(test, mac);
});

it('never substitutes latest baseline for the approved frozen snapshot', () => {
  const test = setup(); grant(test);
  test.clock.set(new Timestamp((BASE + 1n) * 1000n));
  test.withReducerTx(mac, ctx => moduleExports.record_baseline(ctx, { workstationId: 'desk', deviceId: 'desk-mac', observedAtMs: BASE + 1n, baselineJson: '["new-current-usb"]', activeAppId: 'new.current.app' }));
  expect(moduleExports.my_investigator_baselines(test.viewContext(investigator))[0].baselineJson).toBe('["fixture-approved"]');
  test.withReducerTx(owner, ctx => ctx.db.baselineSnapshots.id.delete('session:desk-session'));
  expect(moduleExports.my_investigator_baselines(test.viewContext(investigator))).toEqual([]);
});

it('requires explicit baseline approval and permits incident status without event content', () => {
  const test = setup(); grant(test, { eventIdsJson: '[]', includeBaseline: false });
  expect(moduleExports.my_investigator_incidents(test.viewContext(investigator))).toHaveLength(1);
  expect(moduleExports.my_investigator_incidents(test.viewContext(investigator))[0].evidenceComplete).toBe(false);
  expect(moduleExports.my_investigator_events(test.viewContext(investigator))).toEqual([]);
  expect(moduleExports.my_investigator_baselines(test.viewContext(investigator))).toEqual([]);
});

for (const who of [investigator, outsider, mac, camera, reviewer, service]) it(`denies nonowner grant/revoke connection ${who.connectionId?.toHexString()}`, () => {
  const test = setup(); expect(() => grant(test, {}, who)).toThrow(/owner/); grant(test);
  expect(() => revoke(test, {}, who)).toThrow(/owner/); expect(moduleExports.my_investigator_events(test.viewContext(investigator))).toHaveLength(1);
});

for (const eventIdsJson of ['["desk-app"]', '["missing"]', '["other-usb"]']) it(`rejects unlinked or cross-workstation event scope ${eventIdsJson}`, () => {
  const test = setup(); seedStation(test, 'other'); expect(() => grant(test, { eventIdsJson })).toThrow(/Every approved event/); expectEmpty(test);
});

it('rejects a case from another workstation', () => {
  const test = setup(); seedStation(test, 'other'); expect(() => grant(test, { incidentId: 'incident:other-usb' })).toThrow(/outside/); expectEmpty(test);
});

it('rejects a linked event whose stored workstation, session or provenance does not match the case', () => {
  for (const change of [{ workstationId: 'other' }, { sessionId: 'other-session' }, { simulated: true }]) {
    const test = setup(); test.withReducerTx(owner, ctx => ctx.db.deviceEvents.id.update({ ...ctx.db.deviceEvents.id.find('desk-usb')!, ...change }));
    expect(() => grant(test)).toThrow(/Every approved event/); expectEmpty(test);
  }
});

it('rejects missing or mismatched frozen baseline when requested', () => {
  const test = setup(); test.withReducerTx(owner, ctx => ctx.db.baselineSnapshots.id.update({ ...ctx.db.baselineSnapshots.id.find('session:desk-session')!, workstationId: 'other' }));
  expect(() => grant(test)).toThrow(/Frozen/);
  test.withReducerTx(owner, ctx => ctx.db.baselineSnapshots.id.delete('session:desk-session'));
  expect(() => grant(test)).toThrow(/Frozen/); expectEmpty(test);
});

it('bounds and canonicalizes explicitly approved event IDs', () => {
  const test = setup(); expect(() => grant(test, { eventIdsJson: JSON.stringify(Array.from({ length: 257 }, (_, i) => `e-${i}`)) })).toThrow(/256/);
  grant(test, { eventIdsJson: '["desk-usb","desk-usb"]' });
  expect(moduleExports.my_investigator_incidents(test.viewContext(investigator))[0].approvedEventIdsJson).toBe('["desk-usb"]');
});

it('does not expose later correlated physical evidence even when live severity rises', () => {
  const test = setup(); grant(test);
  expect(moduleExports.my_investigator_incidents(test.viewContext(investigator))[0].severity).toBe('medium');
  emit(test, 'new-physical', 1n, 'person_in_zone');
  const row = moduleExports.my_investigator_incidents(test.viewContext(investigator))[0];
  expect(row.severity).toBe('high'); expect(row.evidenceComplete).toBe(false); expect(row.approvedEventIdsJson).toBe('["desk-usb"]');
  expect(row).not.toHaveProperty('ruleId'); expect(row).not.toHaveProperty('summary');
  expect(moduleExports.my_investigator_events(test.viewContext(investigator)).map(e => e.id)).toEqual(['desk-usb']);
});

it('regrant replaces scope instead of retaining prior event or baseline permissions', () => {
  const test = setup(); emit(test, 'physical', 1n, 'person_in_zone'); grant(test, { eventIdsJson: '["desk-usb","physical"]' });
  grant(test, { eventIdsJson: '["physical"]', includeBaseline: false });
  expect(test.db.incidentEvidenceGrants.count()).toBe(1n);
  expect(moduleExports.my_investigator_events(test.viewContext(investigator)).map(e => e.id)).toEqual(['physical']);
  expect(moduleExports.my_investigator_baselines(test.viewContext(investigator))).toEqual([]);
  expect([...test.db.auditEvents.iter()].filter(a => a.kind === 'incident_evidence_granted')).toHaveLength(2);
});

it('revocation removes all dedicated views and only owner reapproval restores them', () => {
  const test = setup(); grant(test); revoke(test); expectEmpty(test);
  expect(() => grant(test, {}, investigator)).toThrow(/owner/); expectEmpty(test);
  grant(test); expect(moduleExports.my_investigator_events(test.viewContext(investigator))).toHaveLength(1);
});

it('cannot assign an enabled investigator to another workstation until all current grants are revoked', () => {
  const test = setup(); seedStation(test, 'other'); grant(test);
  grant(test, { incidentId: 'incident:desk-app', eventIdsJson: '["desk-app"]' });
  const other = { workstationId: 'other', incidentId: 'incident:other-usb', eventIdsJson: '["other-usb"]' };
  expect(() => grant(test, other)).toThrow(/another workstation/); revoke(test);
  expect(() => grant(test, other)).toThrow(/another workstation/); revoke(test, { incidentId: 'incident:desk-app' });
  grant(test, other); expect(moduleExports.my_investigator_incidents(test.viewContext(investigator))[0].workstationId).toBe('other');
});

for (const role of ['mac', 'jetson', 'service', 'reviewer', 'owner']) it(`rejects investigator grant to enabled ${role} membership`, () => {
  const test = setup();
  if (role === 'owner') test.withReducerTx(investigator, ctx => moduleExports.register_workstation(ctx, { workstationId: 'own-desk', name: 'own desk' }));
  else if (role === 'reviewer') test.withReducerTx(owner, ctx => moduleExports.grant_reviewer(ctx, { workstationId: 'desk', identityHex: hex(test), canApprove: false }));
  else enroll(test, investigator, 'investigator-device', role);
  expect(() => grant(test)).toThrow(/no enabled memberships/); expectEmpty(test);
});

it('blocks mixing any membership with an enabled evidence grant, including another station', () => {
  const test = setup(); seedStation(test, 'other'); grant(test);
  expect(() => test.withReducerTx(investigator, ctx => moduleExports.register_workstation(ctx, { workstationId: 'own-desk', name: 'own desk' }))).toThrow(/Evidence-only/);
  for (const ws of ['desk', 'other']) {
    expect(() => enroll(test, investigator, 'investigator-device', 'service', ws)).toThrow(/Revoke evidence/);
    expect(() => test.withReducerTx(owner, ctx => moduleExports.grant_reviewer(ctx, { workstationId: ws, identityHex: hex(test), canApprove: true }))).toThrow(/Revoke evidence/);
  }
  revoke(test); enroll(test, investigator, 'investigator-device', 'service');
  expectEmpty(test); expect(() => grant(test)).toThrow(/no enabled memberships/);
});

it('withholds investigator views if a conflicting enabled membership appears through migration', () => {
  const test = setup(); grant(test); const who = test.viewContext(investigator).sender;
  test.withReducerTx(owner, ctx => ctx.db.memberships.insert({ id: `elsewhere:${who.toHexString()}`, workstationId: 'elsewhere', identity: who, role: 'service', enabled: true, canApprove: false, createdAtMs: BASE }));
  expectEmpty(test);
});

for (const anchor of ['incident', 'session', 'owner', 'owner-membership'] as const) it(`withdraws every read after ${anchor} authorization anchor changes`, () => {
  const test = setup(); grant(test);
  test.withReducerTx(owner, ctx => {
    if (anchor === 'incident') ctx.db.incidents.id.update({ ...ctx.db.incidents.id.find('incident:desk-usb')!, createdAtMs: BASE + 1n });
    if (anchor === 'session') ctx.db.sessions.id.update({ ...ctx.db.sessions.id.find('desk-session')!, armedAtMs: BASE + 1n });
    if (anchor === 'owner') ctx.db.workstations.id.update({ ...ctx.db.workstations.id.find('desk')!, owner: test.viewContext(outsider).sender });
    if (anchor === 'owner-membership') { const id = `desk:${hex(test, owner)}`; ctx.db.memberships.id.update({ ...ctx.db.memberships.id.find(id)!, enabled: false }); }
  }); expectEmpty(test);
});

it('does not revive a grant for a deleted and recreated incident or session generation', () => {
  for (const table of ['incidents', 'sessions'] as const) {
    const test = setup(); grant(test);
    test.withReducerTx(owner, ctx => {
      if (table === 'incidents') { const prior = ctx.db.incidents.id.find('incident:desk-usb')!; ctx.db.incidents.id.delete(prior.id); ctx.db.incidents.insert({ ...prior, createdAtMs: BASE + 86_400_000n }); }
      else { const prior = ctx.db.sessions.id.find('desk-session')!; ctx.db.sessions.id.delete(prior.id); ctx.db.sessions.insert({ ...prior, armedAtMs: BASE + 86_400_000n }); }
    }); expectEmpty(test);
  }
});

it('does not expose modified or reused event content under the same event ID', () => {
  const test = setup(); grant(test);
  test.withReducerTx(owner, ctx => { const prior = ctx.db.deviceEvents.id.find('desk-usb')!; ctx.db.deviceEvents.id.delete(prior.id); ctx.db.deviceEvents.insert({ ...prior, metadataJson: '{"detail":"new unapproved content"}', seq: 20n }); });
  expect(moduleExports.my_investigator_events(test.viewContext(investigator))).toEqual([]);
  expect(moduleExports.my_investigator_incidents(test.viewContext(investigator))[0].evidenceComplete).toBe(false);
});

it('does not expose modified or reused frozen baseline content', () => {
  const test = setup(); grant(test);
  test.withReducerTx(owner, ctx => { const prior = ctx.db.baselineSnapshots.id.find('session:desk-session')!; ctx.db.baselineSnapshots.id.delete(prior.id); ctx.db.baselineSnapshots.insert({ ...prior, baselineJson: '["replacement-secret"]' }); });
  expect(moduleExports.my_investigator_baselines(test.viewContext(investigator))).toEqual([]);
});

it('does not reveal mutable owner notes or freeform incident summary', () => {
  const test = setup(); grant(test);
  test.withReducerTx(owner, ctx => moduleExports.resolve_incident(ctx, { incidentId: 'incident:desk-usb', note: 'OWNER PRIVATE NOTE' }));
  test.withReducerTx(owner, ctx => ctx.db.incidents.id.update({ ...ctx.db.incidents.id.find('incident:desk-usb')!, summary: 'OWNER PRIVATE SUMMARY' }));
  const row = moduleExports.my_investigator_incidents(test.viewContext(investigator))[0];
  expect(row.state).toBe('resolved');
  expect(JSON.stringify(row, (_, v) => typeof v === 'bigint' ? v.toString() : v)).not.toMatch(/OWNER PRIVATE/);
});

it('keeps every pre-existing my_* view inaccessible to the investigator', () => {
  const test = setup(true); grant(test);
  const oldViews = Object.entries(moduleExports).filter(([name]) => name.startsWith('my_') && !name.startsWith('my_investigator_'));
  expect(oldViews.length).toBeGreaterThanOrEqual(15);
  for (const [name, fn] of oldViews) expect((fn as (ctx: ReturnType<typeof test.viewContext>) => unknown)(test.viewContext(investigator)), name).toEqual([]);
});

it('keeps all backing tables private at the actual schema level', () => {
  for (const table of Object.values(spacetimedb.schemaType.tables)) expect(table.tableDef.tableAccess.tag, table.sourceName).toBe('Private');
});

it('denies all applicable write and action reducers, including ARM, with no new privileges or state changes', () => {
  const test = setup(true); enroll(test, service, 'desk-service', 'service');
  test.withReducerTx(service, ctx => moduleExports.propose_protected_action(ctx, { workstationId: 'desk', actionId: 'action', missionId: 'mission', incidentId: 'incident:desk-usb', toolName: 'safe_count', argsJson: '{}', scope: 'sandbox:read', ttlMs: 60_000n }));
  test.withReducerTx(owner, ctx => moduleExports.decide_action(ctx, { actionId: 'action', approve: true, ttlMs: 30_000n, note: 'UNIT TEST' }));
  grant(test); const identityHex = hex(test), approval = test.db.protectedActions.id.find('action')!;
  const calls: Array<[string, (ctx: Parameters<typeof moduleExports.disarm_session>[0]) => unknown]> = [
    ['register_workstation', ctx => moduleExports.register_workstation(ctx, { workstationId: 'attacker', name: 'attacker' })],
    ['enroll_device', ctx => moduleExports.enroll_device(ctx, { workstationId: 'desk', deviceId: 'attacker', identityHex, role: 'service', label: 'attacker' })],
    ['grant_reviewer', ctx => moduleExports.grant_reviewer(ctx, { workstationId: 'desk', identityHex, canApprove: true })],
    ['revoke_member', ctx => moduleExports.revoke_member(ctx, { workstationId: 'desk', identityHex: hex(test, mac) })],
    ['set_policy', ctx => moduleExports.set_policy(ctx, { workstationId: 'desk', autoLockEnabled: false, lockCooldownMs: 30_000n })],
    ['record_baseline', ctx => moduleExports.record_baseline(ctx, { workstationId: 'desk', deviceId: 'desk-mac', observedAtMs: BASE, baselineJson: '[]', activeAppId: 'attacker' })],
    ['arm_session', ctx => moduleExports.arm_session(ctx, { workstationId: 'desk', sessionId: 'attacker', baselineJson: '[]', approvedAppsJson: '[]', durationMs: 60_000n, simulated: true })],
    ['authorize_arm', ctx => moduleExports.authorize_arm(ctx, { workstationId: 'desk', authorizationId: 'attacker', deviceId: 'desk-mac', sessionId: 'attacker', baselineJson: '[]', activeAppId: 'attacker', approvedAppsJson: '[]', expectedOwnerPolicyHash: '', durationMs: 60_000n, ttlMs: 1000n })],
    ['arm_session_delegated', ctx => moduleExports.arm_session_delegated(ctx, { workstationId: 'desk', authorizationId: 'attacker' })],
    ['revoke_arm_authorization', ctx => moduleExports.revoke_arm_authorization(ctx, { workstationId: 'desk', authorizationId: 'attacker' })],
    ['disarm_session', ctx => moduleExports.disarm_session(ctx, { workstationId: 'desk' })],
    ['ingest_device_event', ctx => moduleExports.ingest_device_event(ctx, { workstationId: 'desk', sessionId: 'desk-session', deviceId: 'desk-mac', eventId: 'attacker', kind: 'heartbeat', observedAtMs: BASE, seq: 3n, confidence: 1, metadataJson: '{}', simulated: false, schemaVersion: 1 })],
    ['report_health', ctx => moduleExports.report_health(ctx, { workstationId: 'desk', deviceId: 'desk-mac', component: 'usb', status: 'online', detail: 'attacker' })],
    ['acknowledge_incident', ctx => moduleExports.acknowledge_incident(ctx, { incidentId: 'incident:desk-usb', note: 'attacker' })],
    ['request_incident_info', ctx => moduleExports.request_incident_info(ctx, { incidentId: 'incident:desk-usb', note: 'attacker' })],
    ['resolve_incident', ctx => moduleExports.resolve_incident(ctx, { incidentId: 'incident:desk-usb', note: 'attacker' })],
    ['request_lock', ctx => moduleExports.request_lock(ctx, { incidentId: 'incident:desk-usb' })],
    ['panic_session', ctx => moduleExports.panic_session(ctx, { workstationId: 'desk', reason: 'attacker' })],
    ['claim_lock', ctx => moduleExports.claim_lock(ctx, { actionId: 'lock:incident:desk-usb', executionId: 'attacker' })],
    ['report_lock_result', ctx => moduleExports.report_lock_result(ctx, { actionId: 'lock:incident:desk-usb', status: 'executed', detail: 'attacker' })],
    ['propose_protected_action', ctx => moduleExports.propose_protected_action(ctx, { workstationId: 'desk', actionId: 'attacker', missionId: 'mission', incidentId: 'incident:desk-usb', toolName: 'safe_count', argsJson: '{}', scope: 'sandbox:read', ttlMs: 1000n })],
    ['decide_action', ctx => moduleExports.decide_action(ctx, { actionId: 'action', approve: false, ttlMs: 1000n, note: 'attacker' })],
    ['consume_approval', ctx => moduleExports.consume_approval(ctx, { actionId: 'action', approvalId: approval.approvalId, actionDigest: approval.actionDigest, executionId: 'attacker' })],
    ['report_action_result', ctx => moduleExports.report_action_result(ctx, { actionId: 'action', executionId: 'attacker', status: 'succeeded', detail: 'attacker' })],
    ['record_agent_step', ctx => moduleExports.record_agent_step(ctx, { workstationId: 'desk', runId: 'attacker', incidentId: 'incident:desk-usb', stepId: 'attacker', step: 1, toolName: 'read', status: 'complete', evidenceId: 'desk-usb', durationMs: 1n, inputType: 'event', argsJson: '{}', resultSummary: 'attacker', traceId: 'attacker', model: 'attacker' })],
    ['refresh_workstation', ctx => moduleExports.refresh_workstation(ctx, { workstationId: 'desk' })],
    ['grant_incident_evidence', ctx => moduleExports.grant_incident_evidence(ctx, { workstationId: 'desk', identityHex, incidentId: 'incident:desk-app', eventIdsJson: '["desk-app"]', includeBaseline: true })],
    ['revoke_incident_evidence', ctx => moduleExports.revoke_incident_evidence(ctx, { workstationId: 'desk', identityHex, incidentId: 'incident:desk-usb' })],
  ];
  const beforeAudit = test.db.auditEvents.count();
  for (const [name, call] of calls) expect(() => test.withReducerTx(investigator, call), name).toThrow();
  expect(test.db.auditEvents.count()).toBe(beforeAudit); expect(test.db.workstations.id.find('desk')?.activeSessionId).toBe('desk-session');
  expect(test.db.protectedActions.id.find('action')?.status).toBe('approved');
  expect(test.db.incidents.id.find('incident:desk-usb')?.state).toBe('open');
  expect(test.db.lockRequests.id.find('lock:incident:desk-usb')?.status).toBe('requested');
  expect(moduleExports.my_investigator_events(test.viewContext(investigator)).map(e => e.id)).toEqual(['desk-usb']);
});
