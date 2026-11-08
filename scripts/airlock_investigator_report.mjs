/**
 * Proposed integration helper only; no investigator host or model is wired here.
 *
 * Reconcile one current authorized incident projection with its returned approved
 * event rows and an advisory model assessment. No network, model, auth, or mutation.
 * The host MUST obtain a current coherent authorized-view snapshot immediately
 * before generating/displaying a report and remove it on revocation/disconnect.
 * A pure function cannot distinguish a stale cached snapshot from a fresh one.
 *
 * Inputs use generated-SDK camelCase fields. `incidents` is the current
 * my_investigator_incidents view; `events` is that incident's subset of returned
 * my_investigator_events, selected using approvedEventIdsJson, never invented IDs.
 * U64 timestamp values retain their original bigint, safe-number, or decimal-string
 * representation. Serialize bigint explicitly at the host boundary without Number().
 * Rationale is untrusted advisory text, not a verified statement or an instruction.
 */

const SERVER_SEVERITIES = new Set(['medium', 'high']);
const MODEL_SEVERITIES = new Set(['low', 'medium', 'high']);
const EVENT_SOURCES = new Set(['mac', 'jetson', 'service', 'fixture']);
const EVENT_KINDS = new Set([
  'usb_added', 'usb_removed', 'app_launched', 'person_in_zone', 'person_cleared',
  'camera_offline', 'camera_online', 'heartbeat',
]);
const U64_MAX = (1n << 64n) - 1n;

function requireValue(condition, detail) {
  if (!condition) throw new TypeError(`Investigator report denied: ${detail}`);
}

function record(value, label) {
  requireValue(value !== null && typeof value === 'object' && !Array.isArray(value), `${label} must be an object`);
  return value;
}

function text(value, label, limit = 256) {
  requireValue(typeof value === 'string' && value.length > 0 && value.length <= limit, `${label} must be bounded nonempty text`);
  return value;
}

function boolean(value, label) {
  requireValue(typeof value === 'boolean', `${label} must be a boolean`);
  return value;
}

function u64(value, label, positive = false) {
  const valid = typeof value === 'bigint'
    || (typeof value === 'number' && Number.isSafeInteger(value))
    || (typeof value === 'string' && /^(0|[1-9][0-9]{0,19})$/.test(value));
  requireValue(valid, `${label} must be an exact unsigned integer`);
  const integer = BigInt(value);
  requireValue(integer >= (positive ? 1n : 0n) && integer <= U64_MAX, `${label} is outside u64 range`);
  return value;
}

function ids(value, label) {
  requireValue(Array.isArray(value) && value.length <= 512, `${label} must be a bounded array`);
  const result = value.map(id => text(id, label));
  requireValue(new Set(result).size === result.length, `${label} contains duplicate IDs`);
  return result;
}

function approvedIds(incident) {
  requireValue(typeof incident.approvedEventIdsJson === 'string'
    && incident.approvedEventIdsJson.length <= 131072, 'approvedEventIdsJson is invalid');
  let parsed;
  try { parsed = JSON.parse(incident.approvedEventIdsJson); }
  catch { throw new TypeError('Investigator report denied: approvedEventIdsJson is not JSON'); }
  return ids(parsed, 'approved event IDs');
}

function eventProjection(event, incident, approved) {
  record(event, 'event');
  const id = text(event.id, 'event ID');
  requireValue(approved.has(id), 'returned event is not approved for this incident');
  requireValue(event.workstationId === incident.workstationId, 'event workstation mismatch');
  requireValue(event.sessionId === incident.sessionId, 'event session mismatch');
  requireValue(event.simulated === incident.simulated, 'event simulation provenance mismatch');
  requireValue(EVENT_SOURCES.has(event.source), 'unknown event source');
  requireValue(EVENT_KINDS.has(event.kind), 'unknown event kind');
  requireValue(event.source !== 'fixture' || event.simulated === true, 'fixture cannot claim real provenance');
  requireValue(event.schemaVersion === 1, 'unsupported event schema version');
  requireValue(typeof event.confidence === 'number' && Number.isFinite(event.confidence)
    && event.confidence >= 0 && event.confidence <= 1, 'event confidence is invalid');
  requireValue(typeof event.metadataJson === 'string'
    && Buffer.byteLength(event.metadataJson, 'utf8') <= 2048, 'event metadata exceeds contract');
  let metadata;
  try { metadata = JSON.parse(event.metadataJson); }
  catch { throw new TypeError('Investigator report denied: event metadata is not JSON'); }
  record(metadata, 'event metadata');
  const allowedMetadata = new Set(['deviceKey', 'appId', 'label', 'detail', 'captureMode', 'bootId']);
  requireValue(Object.keys(metadata).every(key => allowedMetadata.has(key))
    && Object.values(metadata).every(value => typeof value === 'string' && value.length <= 256),
  'event metadata is outside sanitized contract');
  // Explicit fields prevent caller/model extras from replacing authoritative data.
  return {
    id,
    workstationId: incident.workstationId,
    sessionId: incident.sessionId,
    deviceId: text(event.deviceId, 'event device ID'),
    source: event.source,
    kind: event.kind,
    observedAtMs: u64(event.observedAtMs, 'observation timestamp'),
    receivedAtMs: u64(event.receivedAtMs, 'receipt timestamp'),
    seq: u64(event.seq, 'event sequence', true),
    confidence: event.confidence,
    metadataJson: event.metadataJson,
    simulated: incident.simulated,
    schemaVersion: 1,
    classification: text(event.classification, 'event classification'),
  };
}

/**
 * @param {{incidentId:string, incidents:Array<object>, events:Array<object>,
 * modelAssessment:{severity:'low'|'medium'|'high',rationale:string,evidenceIds:Array<string>}}} input
 * @returns {object} Server-authoritative report or throws; never emits a partial report on denial.
 */
export function reconcileInvestigatorReport(input) {
  record(input, 'input');
  const incidentId = text(input.incidentId, 'incident ID');
  requireValue(Array.isArray(input.incidents), 'current incident view is missing');
  const matches = input.incidents.filter(incident => incident?.id === incidentId);
  requireValue(matches.length === 1, 'incident missing/revoked or ambiguous in current authorized view');
  const incident = record(matches[0], 'incident');
  requireValue(SERVER_SEVERITIES.has(incident.severity), 'unknown server incident severity');
  text(incident.workstationId, 'workstation ID');
  text(incident.sessionId, 'session ID');
  boolean(incident.simulated, 'incident simulation provenance');
  boolean(incident.evidenceComplete, 'server evidence completeness');
  const approved = approvedIds(incident);
  const approvedSet = new Set(approved);
  requireValue(Array.isArray(input.events) && input.events.length <= 512, 'returned events must be a bounded array');
  const evidence = input.events.map(event => eventProjection(event, incident, approvedSet));
  const returned = new Set(evidence.map(event => event.id));
  requireValue(returned.size === evidence.length, 'duplicate returned event IDs');

  const assessment = record(input.modelAssessment, 'model assessment');
  requireValue(Object.keys(assessment).every(key => ['severity', 'rationale', 'evidenceIds'].includes(key)),
    'model assessment contains unsupported fields');
  requireValue(MODEL_SEVERITIES.has(assessment.severity), 'unknown model assessment severity');
  const modelEvidenceIds = ids(assessment.evidenceIds, 'model evidence IDs');
  requireValue(modelEvidenceIds.every(id => approvedSet.has(id) && returned.has(id)),
    'model cites evidence not both approved and returned');
  const missingApprovedIds = approved.filter(id => !returned.has(id));

  return {
    report_version: 1,
    incident_id: incident.id,
    workstation_id: incident.workstationId,
    session_id: incident.sessionId,
    severity: incident.severity,
    incident_severity: incident.severity,
    incident_state: text(incident.state, 'incident state'),
    simulated: incident.simulated,
    incident_created_at_ms: u64(incident.createdAtMs, 'incident creation timestamp'),
    authoritative_incident_updated_at: u64(incident.updatedAtMs, 'incident update timestamp'),
    evidence_approved_at_ms: u64(incident.evidenceApprovedAtMs, 'evidence approval timestamp'),
    server_evidence_complete: incident.evidenceComplete,
    evidenceIncomplete: !incident.evidenceComplete || missingApprovedIds.length > 0,
    approved_event_ids: approved,
    missing_approved_event_ids: missingApprovedIds,
    evidence,
    model_assessment: {
      severity: assessment.severity,
      rationale: text(assessment.rationale, 'model rationale', 4096),
      evidenceIds: modelEvidenceIds,
    },
  };
}
