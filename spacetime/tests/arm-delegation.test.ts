import { expect, it } from 'vitest';
import { ConnectionId, Timestamp } from 'spacetimedb';
import { createModuleTestHarness, TestAuth } from 'spacetimedb/server/test-utils';
import spacetimedb, * as moduleExports from '../src/index';

const auth = (name: string, connection: bigint) => TestAuth.fromJwtPayload(JSON.stringify({ iss: 'https://arm.test', sub: name }), new ConnectionId(connection));
const owner = auth('owner', 1n), ownerOtherConnection = auth('owner', 2n);
const mac = auth('mac', 3n), otherMac = auth('other-mac', 4n), outsider = auth('outsider', 5n);
const camera = auth('camera', 6n), service = auth('service', 7n), reviewer = auth('reviewer', 8n);
const START = 1_000_000n;
type Harness = ReturnType<typeof createHarness>;

function createHarness() {
  const test = createModuleTestHarness(spacetimedb, moduleExports);
  test.clock.set(new Timestamp(START * 1000n));
  test.withReducerTx(owner, ctx => moduleExports.register_workstation(ctx, { workstationId: 'desk', name: 'Desk' }));
  return test;
}
function enroll(test: Harness, who = mac, deviceId = 'mac', role = 'mac', workstationId = 'desk') {
  test.withReducerTx(owner, ctx => moduleExports.enroll_device(ctx, { workstationId, deviceId, identityHex: test.viewContext(who).sender.toHexString(), role, label: deviceId }));
}
function capture(test: Harness, overrides = {}, who = mac) {
  test.withReducerTx(who, ctx => moduleExports.record_baseline(ctx, { workstationId: 'desk', deviceId: 'mac', observedAtMs: test.clock.now().microsSinceUnixEpoch / 1000n, baselineJson: '["approved-usb"]', activeAppId: 'com.apple.finder', ...overrides }));
}
function setup() {
  const test = createHarness(); enroll(test); capture(test); return test;
}
function args(test: Harness, overrides = {}) {
  return { workstationId: 'desk', authorizationId: 'grant', deviceId: 'mac', sessionId: 'delegated-session', baselineJson: '["approved-usb"]', activeAppId: 'com.apple.finder', expectedOwnerPolicyHash: test.db.workstations.id.find('desk')!.ownerPolicyHash, approvedAppsJson: '["com.apple.finder"]', durationMs: 60_000n, ttlMs: 60_000n, ...overrides };
}
function authorize(test: Harness, overrides = {}, who = owner) {
  const input = args(test, overrides);
  test.withReducerTx(who, ctx => moduleExports.authorize_arm(ctx, input));
}
function consume(test: Harness, who = mac, workstationId = 'desk', authorizationId = 'grant') {
  test.withReducerTx(who, ctx => moduleExports.arm_session_delegated(ctx, { workstationId, authorizationId }));
}
function setTime(test: Harness, offset: bigint) { test.clock.set(new Timestamp((START + offset) * 1000n)); }
function noSession(test: Harness) {
  expect(test.db.sessions.count()).toBe(0n); expect(test.db.lockRequests.count()).toBe(0n);
}
function directArm(test: Harness, who = owner) {
  test.withReducerTx(who, ctx => moduleExports.arm_session(ctx, { workstationId: 'desk', sessionId: 'owner-session', baselineJson: '["approved-usb"]', approvedAppsJson: '[]', durationMs: 60_000n, simulated: false }));
}

it('consumes exactly the owner-reviewed intent, preserves actor attribution and never enables locks', () => {
  const test = setup(); authorize(test); consume(test);
  const session = test.db.sessions.id.find('delegated-session')!;
  expect(session.armedBy.isEqual(test.viewContext(mac).sender)).toBe(true);
  expect(session.baselineJson).toBe('["approved-usb"]'); expect(session.approvedAppsJson).toBe('["com.apple.finder"]');
  expect(session.expiresAtMs - session.armedAtMs).toBe(60_000n);
  expect(session.simulated).toBe(false); expect(session.autoLockEnabled).toBe(false);
  expect(test.db.baselineSnapshots.id.find('session:delegated-session')?.deviceId).toBe('mac');
  expect(test.db.armAuthorizations.id.find('grant')?.status).toBe('consumed');
  expect(test.db.lockRequests.count()).toBe(0n);
  const audit = [...test.db.auditEvents.iter()];
  expect(audit.find(a => a.kind === 'arm_authorized')?.actor.isEqual(test.viewContext(owner).sender)).toBe(true);
  expect(audit.find(a => a.kind === 'delegated_arm_consumed')?.actor.isEqual(test.viewContext(mac).sender)).toBe(true);
});

for (const who of [mac, outsider, camera, service, reviewer]) it(`only owner may authorize: connection ${who.connectionId}`, () => {
  const test = setup(); expect(() => authorize(test, {}, who)).toThrow(/owner/); noSession(test);
  expect(test.db.armAuthorizations.count()).toBe(0n);
});

it('rejects null owner connection rather than accepting a caller-provided session label', () => {
  const test = setup(), input = args(test);
  expect(() => test.withReducerTx(owner, ctx => moduleExports.authorize_arm({ ...ctx, connectionId: null }, input))).toThrow(/connection/);
  expect(test.db.armAuthorizations.count()).toBe(0n);
});

for (const [field, value] of [['ttlMs', 0n], ['ttlMs', 60_001n], ['durationMs', 59_999n], ['durationMs', 3_600_001n]] as const)
  it(`rejects out-of-bound ${field} ${value}`, () => {
    const test = setup(); expect(() => authorize(test, { [field]: value })).toThrow(); noSession(test);
  });

for (const [field, value] of [['baselineJson', '["unreviewed-usb"]'], ['activeAppId', 'unreviewed-app'], ['expectedOwnerPolicyHash', 'wrong-policy']] as const)
  it(`rejects stale reviewed ${field} at issuance`, () => {
    const test = setup(); expect(() => authorize(test, { [field]: value })).toThrow();
    expect(test.db.armAuthorizations.count()).toBe(0n); noSession(test);
  });

it('canonicalizes the reviewed inventory and application sets without broadening them', () => {
  const test = setup(); authorize(test, { baselineJson: '["approved-usb","approved-usb"]', approvedAppsJson: '["z-app","a-app","z-app"]' }); consume(test);
  expect(test.db.sessions.id.find('delegated-session')?.approvedAppsJson).toBe('["a-app","z-app"]');
});

for (const [who, role] of [[camera, 'jetson'], [service, 'service']] as const) it(`cannot delegate ARM to ${role}`, () => {
  const test = setup(); enroll(test, who, role, role);
  expect(() => authorize(test, { deviceId: role })).toThrow(/Mac/); noSession(test);
});

it('requires a real fresh baseline at issuance and a fresh baseline again at consume', () => {
  const test = setup(); setTime(test, 16_000n);
  expect(() => authorize(test)).toThrow(/Fresh/);
  capture(test); authorize(test); setTime(test, 32_000n);
  expect(() => consume(test)).toThrow(/Fresh/);
  expect(test.db.armAuthorizations.id.find('grant')?.status).toBe('authorized'); noSession(test);
  capture(test); consume(test); expect(test.db.sessions.count()).toBe(1n);
});

for (const change of [{ baselineJson: '["new-usb"]' }, { activeAppId: 'com.other.app' }])
  it(`rejects changed capture at consume: ${JSON.stringify(change)}`, () => {
    const test = setup(); authorize(test); setTime(test, 1n); capture(test, change);
    expect(() => consume(test)).toThrow(/changed/); noSession(test);
    expect(test.db.armAuthorizations.id.find('grant')?.status).toBe('authorized');
  });

it('uses only the target Mac capture even when another Mac has a newer baseline', () => {
  const test = setup(); enroll(test, otherMac, 'other-mac'); authorize(test); setTime(test, 1n);
  capture(test, { deviceId: 'other-mac', baselineJson: '["other-usb"]' }, otherMac);
  consume(test); expect(test.db.baselineSnapshots.id.find('session:delegated-session')?.deviceId).toBe('mac');
});

it('rejects exact expiry boundary even with fresh capture', () => {
  const test = setup(); authorize(test, { ttlMs: 1000n }); setTime(test, 1000n); capture(test);
  expect(() => consume(test)).toThrow(/expired/); noSession(test);
});

it('accepts immediately before expiry', () => {
  const test = setup(); authorize(test, { ttlMs: 1000n }); setTime(test, 999n); consume(test);
  expect(test.db.sessions.count()).toBe(1n);
});

it('refuses replay and grant ID reuse after successful consumption', () => {
  const test = setup(); authorize(test); consume(test);
  expect(() => consume(test)).toThrow(/consumed/); expect(test.db.sessions.count()).toBe(1n);
  test.withReducerTx(owner, ctx => moduleExports.disarm_session(ctx, { workstationId: 'desk' }));
  expect(() => authorize(test, { sessionId: 'new-session' })).toThrow(/Authorization id already used/);
});

it('rejects other devices, owner consumption and cross-workstation calls', () => {
  const test = setup(); enroll(test, otherMac, 'other-mac'); authorize(test);
  for (const who of [otherMac, owner, outsider]) expect(() => consume(test, who)).toThrow(/delegate/);
  expect(() => consume(test, mac, 'other')).toThrow(/delegate/); noSession(test);
});

it('revokes only for the issuing owner connection and not another owner tab', () => {
  const test = setup(); authorize(test);
  test.withReducerTx(ownerOtherConnection, ctx => moduleExports.arm_owner_disconnected(ctx, {}));
  expect(test.db.armAuthorizations.id.find('grant')?.status).toBe('authorized');
  test.withReducerTx(owner, ctx => moduleExports.arm_owner_disconnected(ctx, {}));
  expect(test.db.armAuthorizations.id.find('grant')?.status).toBe('revoked');
  expect(() => consume(test)).toThrow(/revoked/); noSession(test);
});

it('does not revoke grants when a different identity disconnects using the same test connection ID', () => {
  const test = setup(); authorize(test);
  test.withReducerTx(auth('outsider', 1n), ctx => moduleExports.arm_owner_disconnected(ctx, {}));
  expect(test.db.armAuthorizations.id.find('grant')?.status).toBe('authorized');
});

for (const mutation of ['disarm', 'policy', 're-enroll', 'revoke-member', 'explicit-revoke'] as const)
  it(`revokes on ${mutation}`, () => {
    const test = setup(); authorize(test);
    if (mutation === 'disarm') test.withReducerTx(owner, ctx => moduleExports.disarm_session(ctx, { workstationId: 'desk' }));
    if (mutation === 'policy') test.withReducerTx(owner, ctx => moduleExports.set_policy(ctx, { workstationId: 'desk', autoLockEnabled: false, lockCooldownMs: 30_000n }));
    if (mutation === 're-enroll') enroll(test);
    if (mutation === 'revoke-member') test.withReducerTx(owner, ctx => moduleExports.revoke_member(ctx, { workstationId: 'desk', identityHex: test.viewContext(mac).sender.toHexString() }));
    if (mutation === 'explicit-revoke') test.withReducerTx(owner, ctx => moduleExports.revoke_arm_authorization(ctx, { workstationId: 'desk', authorizationId: 'grant' }));
    expect(test.db.armAuthorizations.id.find('grant')?.status).toBe('revoked');
    expect(() => consume(test)).toThrow(); noSession(test);
  });

it('new owner intent supersedes the previous grant', () => {
  const test = setup(); authorize(test); authorize(test, { authorizationId: 'grant-2', sessionId: 'session-2' });
  expect(test.db.armAuthorizations.id.find('grant')?.status).toBe('revoked');
  expect(() => consume(test)).toThrow(/revoked/); consume(test, mac, 'desk', 'grant-2');
  expect(test.db.sessions.id.find('session-2')?.state).toBe('armed');
});

it('direct owner arming cancels an outstanding grant and no rearm remains after disarm', () => {
  const test = setup(); authorize(test); directArm(test);
  expect(test.db.armAuthorizations.id.find('grant')?.status).toBe('revoked');
  test.withReducerTx(owner, ctx => moduleExports.disarm_session(ctx, { workstationId: 'desk' }));
  expect(() => consume(test)).toThrow(/revoked/);
  expect(test.db.sessions.count()).toBe(1n); expect(test.db.workstations.id.find('desk')?.activeSessionId).toBe('');
});

it('active sessions prevent new authorization and automatic-lock policy prevents delegation', () => {
  const test = setup(); directArm(test); expect(() => authorize(test)).toThrow(/Disarm/);
  test.withReducerTx(owner, ctx => moduleExports.disarm_session(ctx, { workstationId: 'desk' }));
  test.withReducerTx(owner, ctx => moduleExports.set_policy(ctx, { workstationId: 'desk', autoLockEnabled: true, lockCooldownMs: 30_000n }));
  expect(() => authorize(test)).toThrow(/automatic locking disabled/); expect(test.db.armAuthorizations.count()).toBe(0n);
});

it('delegation never grants original arm, disarm, enrollment, policy, or grant revocation permissions', () => {
  const test = setup(); authorize(test);
  expect(() => directArm(test, mac)).toThrow(/owner/);
  expect(() => test.withReducerTx(mac, ctx => moduleExports.revoke_arm_authorization(ctx, { workstationId: 'desk', authorizationId: 'grant' }))).toThrow(/owner/);
  expect(() => test.withReducerTx(mac, ctx => moduleExports.set_policy(ctx, { workstationId: 'desk', autoLockEnabled: true, lockCooldownMs: 30_000n }))).toThrow(/owner/);
  expect(() => test.withReducerTx(mac, ctx => moduleExports.enroll_device(ctx, { workstationId: 'desk', deviceId: 'attacker', identityHex: test.viewContext(outsider).sender.toHexString(), role: 'service', label: 'attacker' }))).toThrow(/owner/);
  consume(test);
  expect(() => test.withReducerTx(mac, ctx => moduleExports.disarm_session(ctx, { workstationId: 'desk' }))).toThrow(/owner/);
  expect(test.db.sessions.id.find('delegated-session')?.state).toBe('armed'); expect(test.db.lockRequests.count()).toBe(0n);
});

it('exposes grants only to the issuing owner and matching enabled Mac', () => {
  const test = setup(); enroll(test, otherMac, 'other-mac'); enroll(test, camera, 'camera', 'jetson'); enroll(test, service, 'service', 'service');
  test.withReducerTx(owner, ctx => moduleExports.grant_reviewer(ctx, { workstationId: 'desk', identityHex: test.viewContext(reviewer).sender.toHexString(), canApprove: true }));
  authorize(test);
  expect(moduleExports.my_arm_authorizations(test.viewContext(owner))).toHaveLength(1);
  expect(moduleExports.my_arm_authorizations(test.viewContext(mac))).toHaveLength(1);
  for (const who of [otherMac, camera, service, reviewer, outsider]) expect(moduleExports.my_arm_authorizations(test.viewContext(who))).toHaveLength(0);
  test.withReducerTx(owner, ctx => moduleExports.revoke_member(ctx, { workstationId: 'desk', identityHex: test.viewContext(mac).sender.toHexString() }));
  expect(moduleExports.my_arm_authorizations(test.viewContext(mac))).toHaveLength(0);
});
