import { test } from 'node:test';
import assert from 'node:assert/strict';
import { reconcileInvestigatorReport } from './airlock_investigator_report.mjs';

function fixture() {
  return {
    incidentId: 'incident:usb-1',
    incidents: [{
      id: 'incident:usb-1', workstationId: 'desk', sessionId: 'session', severity: 'high',
      state: 'open', createdAtMs: 1794084000001n, updatedAtMs: 1794084002000n,
      simulated: false, approvedEventIdsJson: '["usb-1","camera-1"]',
      evidenceApprovedAtMs: 1794084003000n, evidenceComplete: true,
    }],
    events: [
      { id: 'usb-1', workstationId: 'desk', sessionId: 'session', deviceId: 'mac',
        source: 'mac', kind: 'usb_added', observedAtMs: 1794084000000n, receivedAtMs: 1794084000001n,
        seq: 1n, confidence: 1, metadataJson: '{"deviceKey":"sha256:redacted"}',
        simulated: false, schemaVersion: 1, classification: 'unknown_usb' },
      { id: 'camera-1', workstationId: 'desk', sessionId: 'session', deviceId: 'camera',
        source: 'jetson', kind: 'person_in_zone', observedAtMs: 1794083999000n, receivedAtMs: 1794084001000n,
        seq: 12n, confidence: 0.7, metadataJson: '{"captureMode":"actual webcam"}',
        simulated: false, schemaVersion: 1, classification: 'soft_observation' },
    ],
    modelAssessment: { severity: 'medium', rationale: 'The returned evidence does not establish intent.', evidenceIds: ['usb-1'] },
  };
}

test('server high remains authoritative when model assesses medium', () => {
  const result = reconcileInvestigatorReport(fixture());
  assert.equal(result.severity, 'high');
  assert.equal(result.incident_severity, 'high');
  assert.equal(result.model_assessment.severity, 'medium');
  assert.equal(result.evidenceIncomplete, false);
});

test('server medium remains authoritative when model assesses high', () => {
  const input = fixture(); input.incidents[0].severity = 'medium'; input.modelAssessment.severity = 'high';
  const result = reconcileInvestigatorReport(input);
  assert.equal(result.severity, 'medium');
  assert.equal(result.incident_severity, 'medium');
  assert.equal(result.model_assessment.severity, 'high');
});

test('timestamps, metadata, zero confidence, and provenance retain exact server values', () => {
  const input = fixture(); input.events[0].confidence = 0;
  const result = reconcileInvestigatorReport(input);
  assert.equal(result.authoritative_incident_updated_at, input.incidents[0].updatedAtMs);
  assert.deepEqual(result.evidence, input.events);
  assert.equal(result.simulated, false);
});

test('invented model evidence ID is denied', () => {
  const input = fixture(); input.modelAssessment.evidenceIds.push('invented');
  assert.throws(() => reconcileInvestigatorReport(input), /not both approved and returned/);
});

test('approved but absent evidence cannot be cited', () => {
  const input = fixture(); input.events = input.events.filter(event => event.id !== 'usb-1');
  assert.throws(() => reconcileInvestigatorReport(input), /not both approved and returned/);
});

for (const [field, replacement, expected] of [
  ['workstationId', 'unrelated-desk', /workstation mismatch/],
  ['sessionId', 'unrelated-session', /session mismatch/],
  ['simulated', true, /provenance mismatch/],
] as const) test(`returned event with wrong ${field} is denied even if approved ID matches`, () => {
  const input = fixture(); (input.events[0] as any)[field] = replacement;
  assert.throws(() => reconcileInvestigatorReport(input), expected);
});

test('returned but unapproved evidence is denied, not leaked into report', () => {
  const input = fixture(); input.incidents[0].approvedEventIdsJson = '["usb-1"]';
  assert.throws(() => reconcileInvestigatorReport(input), /not approved/);
});

test('revoked or missing case in current authorized view yields no report', () => {
  const input = fixture(); input.incidents = [];
  assert.throws(() => reconcileInvestigatorReport(input), /missing\/revoked/);
});

test('missing authorized snapshot and duplicate case are denied', () => {
  const input = fixture(); input.incidents.push(input.incidents[0]);
  assert.throws(() => reconcileInvestigatorReport(input), /ambiguous/);
  assert.throws(() => reconcileInvestigatorReport({ ...fixture(), incidents: undefined }), /view is missing/);
});

for (const severity of ['critical', 'LOW', '', null]) test(`unknown server severity ${severity} is denied`, () => {
  const input = fixture(); (input.incidents[0] as any).severity = severity;
  assert.throws(() => reconcileInvestigatorReport(input), /unknown server incident severity/);
});

test('unknown model severity is denied while advisory low remains separate', () => {
  const input = fixture(); input.modelAssessment.severity = 'critical';
  assert.throws(() => reconcileInvestigatorReport(input), /unknown model assessment severity/);
  input.modelAssessment.severity = 'low';
  assert.equal(reconcileInvestigatorReport(input).severity, 'high');
});

test('incomplete server evidence cannot be promoted to complete by available rows', () => {
  const input = fixture(); input.incidents[0].evidenceComplete = false;
  const result = reconcileInvestigatorReport(input);
  assert.equal(result.server_evidence_complete, false);
  assert.equal(result.evidenceIncomplete, true);
});

test('missing uncited approved row remains an explicit incomplete report', () => {
  const input = fixture(); input.events = input.events.filter(event => event.id === 'usb-1');
  const result = reconcileInvestigatorReport(input);
  assert.equal(result.server_evidence_complete, true);
  assert.equal(result.evidenceIncomplete, true);
  assert.deepEqual(result.missing_approved_event_ids, ['camera-1']);
});

test('model override fields are rejected and extra incident fields are never copied', () => {
  const input = fixture();
  (input.modelAssessment as any).incident_severity = 'low';
  assert.throws(() => reconcileInvestigatorReport(input), /unsupported fields/);
  delete (input.modelAssessment as any).incident_severity;
  (input.incidents[0] as any).summary = 'Unapproved private text';
  (input.incidents[0] as any).incident_severity = 'low';
  const result = reconcileInvestigatorReport(input);
  assert.equal(result.incident_severity, 'high');
  assert.equal('summary' in result, false);
});

test('duplicate approved IDs, returned IDs, or model citations are denied', () => {
  const approved = fixture(); approved.incidents[0].approvedEventIdsJson = '["usb-1","usb-1"]';
  assert.throws(() => reconcileInvestigatorReport(approved), /duplicate IDs/);
  const returned = fixture(); returned.events.push(returned.events[0]);
  assert.throws(() => reconcileInvestigatorReport(returned), /duplicate returned/);
  const cited = fixture(); cited.modelAssessment.evidenceIds.push('usb-1');
  assert.throws(() => reconcileInvestigatorReport(cited), /duplicate IDs/);
});

test('unsafe integer timestamp is denied instead of rounded', () => {
  const input = fixture(); (input.events[0] as any).observedAtMs = Number.MAX_SAFE_INTEGER + 1;
  assert.throws(() => reconcileInvestigatorReport(input), /exact unsigned integer/);
});

test('decimal-string u64 timestamp retains its original representation', () => {
  const input = fixture(); (input.events[0] as any).observedAtMs = '1794084000000';
  assert.equal(reconcileInvestigatorReport(input).evidence[0].observedAtMs, '1794084000000');
});

test('raw-image metadata and a real-labeled fixture are rejected', () => {
  const input = fixture(); input.events[0].metadataJson = '{"raw_face_image":"unapproved"}';
  assert.throws(() => reconcileInvestigatorReport(input), /sanitized contract/);
  input.events[0].metadataJson = '{}'; input.events[0].source = 'fixture';
  assert.throws(() => reconcileInvestigatorReport(input), /fixture cannot claim real/);
});

test('output uses copies and does not mutate caller snapshots', () => {
  const input = fixture(); const original = structuredClone(input);
  const result = reconcileInvestigatorReport(input);
  assert.deepEqual(input, original);
  result.evidence[0].metadataJson = '{}'; result.model_assessment.evidenceIds.push('changed');
  assert.deepEqual(input, original);
});
