# AIRLOCK camera

The camera reports what happens at the workstation as timestamped observations: someone in view, someone at the desk, a hand in the USB zone, a covered lens. It never says who someone is beyond "matches the enrolled owner", and it never judges intent. The Mac's USB watcher remains the authority on whether a device was actually connected. Sid's SpacetimeDB module decides incidents.

```
camera (CameraSource: index, MJPEG URL or file)
  -> inference adapter (mac_mediapipe today, opencv_dnn for the Jetson)
  -> trackers: camera health, presence (faces and zones next)
  -> rate limit -> SecurityEventV1 from the camera's own device id -> security publisher
```

Code lives in `backend/app/vision/airlock/`. Geometry, trackers and events use only the standard library and numpy, so the Jetson runs the same code.

## Run it

```bash
cd backend
.venv/bin/python scripts/download_models.py --airlock-vision    # once, about 42 MB, checksummed in models.sha256
.venv/bin/python scripts/airlock_vision.py --seconds 30         # the Mac's webcam; prints each event and a summary
.venv/bin/python -m pytest -q tests/test_airlock_vision.py
```

The script uses the camera's own identity, `data/airlock_vision/identity.json` (`cam-...`), and its own sequence. Delivery is local only, so nothing goes to SpacetimeDB. Frames stay in memory and nothing is saved. The camera light is on while it runs.

## Models

| Model | Use | Size | Source |
|---|---|---|---|
| `efficientdet_lite0.tflite` (int8) | people (COCO "person") on the Mac | 4.6 MB | Google MediaPipe models |
| `face_detection_yunet_2023mar.onnx` | faces with 5 landmarks, OpenCV DNN | 0.2 MB | OpenCV Zoo |
| `face_recognition_sface_2021dec.onnx` | owner verification embeddings, OpenCV DNN | 38.7 MB | OpenCV Zoo |
| `hand_landmarker.task` (already present) | hands for zone interaction | 7.8 MB | Google MediaPipe models |

## Presence

The tracker has four states: `unknown`, `empty`, `in_view` and `at_desk`.

- **At the desk.** A person is at the desk when at least 30% of their box is inside the `desk` zone and the box is at least `desk_min_height` of the frame tall. That second rule keeps someone walking past behind the chair `in_view`.
- **Entering.** It takes 3 of the last 5 frames, spanning at least 0.25 s.
- **Leaving.** It takes 1.5 s without a sighting.
- **Clear.** It takes 3 s with nobody in view.
- **Approaching.** This is reported once when a visible person's box grows by 25%, or gains 15% overlap with the desk zone, within 1 s.

Events:

| Event | When |
|---|---|
| `person_in_view` | soft; at most one every 10 s |
| `person_approaching` | soft; at most one every 10 s |
| `person_in_zone` | someone has arrived at the desk |
| `person_left_zone` | includes `dwell_ms` |
| `zone_clear` | at most one every 5 s |

A token bucket (burst 6, then 2 per second) caps everything.

## Camera health

The camera is `obstructed` after 1.5 s of frames that are dark (grey mean below 14 of 255) or flat (spread below 5). It is `offline` when frames stop after the camera has worked, and `unavailable` when it never opens. While the camera cannot see, the trackers conclude nothing, so a covered lens is not reported as an empty room. Mac USB and app monitoring carry on regardless.

## Owner face: voluntary, local, contextual only

```bash
.venv/bin/python scripts/airlock_face.py enroll --i-consent      # the owner starts it; about 30 s
.venv/bin/python scripts/airlock_face.py status
.venv/bin/python scripts/airlock_face.py verify --seconds 20     # live verdicts and scores, printed only
.venv/bin/python scripts/airlock_face.py delete --yes            # overwrite and remove the template
.venv/bin/python scripts/airlock_vision.py --faces               # face states as events
```

**Enrollment.** It accepts 15 samples: 7 looking at the camera, and 4 turned slightly to each side. The owner must be alone in view. A sample counts only if the YuNet score is at least 0.85, the face is at least 80 px wide, nothing is cut off by the frame, the face is not turned too far, and the light and sharpness pass. Consecutive samples must differ. `--add` adds samples in other lighting, up to 60, and clears any earlier calibration.

**Storage.** The template is `data/airlock/owner_face.json`: mode 0600, in a 0700 folder that is git-ignored. It holds SFace embeddings (128 numbers per sample) and never a picture. It goes nowhere else: not SpacetimeDB, the dashboard, the logs or the investigator. Delete overwrites it, then removes it; on an SSD the overwrite is best effort. It is not yet encrypted at rest beyond FileVault.

**Matching.** A live face is detected every third frame (about 10 times a second). Each face is aligned, embedded, compared and then dropped. Its score is the mean of its 3 best cosine similarities to the enrolled samples. Each frame gives a verdict:

| Verdict | When |
|---|---|
| match | score ≥ 0.50 |
| non-match | score ≤ 0.30 |
| uncertain | anything between, or a sample that fails quality (small, cut off, turned, tilted, dark, bright, blurry) |

These thresholds are a conservative default, stricter than SFace's published 0.363. They hold until `calibrate` and `report --apply` set them from this owner and consenting visitors.

**Speed.** Faces are found every second frame, about 15 times a second, on a half-size copy, which YuNet handles in a few ms. Each face is tracked by overlap. A new face is compared on every pass until its state settles. After that it is compared again only every 2 s, or as soon as its box grows or shrinks by 40%. If a revalidation disagrees, for example someone else sits down in the same place, the face is compared on every pass again.

**States.** A state settles when 4 of the face's last 6 verdicts agree within 1.5 s, after at least 0.4 s of watching. An owner match also needs 1 s of the photo check, so the owner is identified in about 1 s and someone else in about 0.4-0.6 s. Confidence is the share of recent verdicts that agree, not a probability of identity.

| State | When |
|---|---|
| `owner_face_match` | the vote settles on match |
| `unrecognized_face` | the vote settles on non-match. Never reported when nobody is enrolled |
| `face_uncertain` | a face is seen for 1.5 s without the vote settling, or the face matches but never moved |
| `no_face_visible` | someone is in view but no face is seen for 2.5 s |

An obscured or turned face is uncertain, never an intruder.

**Photos.** An owner match waits until the face has been watched for about 1 s. If the face did not move at all in that time (centre spread below 0.15% of the frame, almost no change in head turn), the result is uncertain, not a match. This catches a still photo or a frozen screen. It does not catch a hand-held photo or a video replay, and it is not biometric liveness.

**A match is context only.** It never unlocks, disarms or authenticates. Disarming still needs the owner's authenticated confirmation on the dashboard, and an unknown USB device is reported whoever is in view.

**Calibration.** `calibrate --label owner` takes held-out owner samples, which are not added to the template. `calibrate --label visitor --volunteer v1` takes samples from a consenting visitor. Both keep only scores and labels, never an embedding or a picture. `report --holdout v3` sets thresholds from the remaining volunteers and reports false owner matches on the held-out ones. The sample is small, so its rates describe these people in this room, not a population.

## Security modes and what LOCKI does about them

The camera's events and the Mac's USB facts drive one state machine, `backend/app/security/modes.py`. The modes are NORMAL, WATCHFUL, DEFENSIVE, CRITICAL and OWNER_RETURNED (see `CONTRACTS.md`). Every change is published at once, and the voice reacts on the same call, with no model in the way:

| Mode | Voice | Tone |
|---|---|---|
| WATCHFUL | "Hey, someone's at your workstation while it's armed." Said once a face shows it is not the owner, or 1.5 s after arrival with no owner match. If the owner's face matches within that time, nothing is said | plain |
| DEFENSIVE | P0, interrupting any reply: "Security alert. Someone is interacting with your computer while it's armed." A local incident opens | short and serious |
| CRITICAL | the USB warning names what the camera saw, for example "while an unrecognized person was at the desk". The module's correlated incident gets its own line | short and serious |
| OWNER_RETURNED | "Welcome back. I'm still armed until you disarm on the dashboard." | normal again |

**Questions.** "Is someone at my desk?", "Who was near my workstation?" and "Did somebody touch my laptop?" are answered from tracked camera events, and say when the camera could not see. "What happened while I was gone?" goes to the investigator, with the desk visits in the record. Every model turn also gets a short `SECURITY (observed ...)` block, current at that turn: the mode, who is present, what is being touched, and the recent camera events. That block also tells the model never to guess who someone is. In DEFENSIVE or CRITICAL it asks for one short, firm sentence.

## Zones and hands

```bash
.venv/bin/python scripts/airlock_zones.py                 # a window on the live picture: draw the zones, watch the detectors
.venv/bin/python scripts/airlock_vision.py --hands --faces
```

The zone tool draws person boxes, showing whether each one is tall enough to count as at the desk. It also draws hand landmarks, so the camera position and occlusions can be checked before relying on a zone. Only the zone corners are saved, to `data/airlock/zones.json`.

Zones are `keyboard_zone`, `laptop_zone`, `usb_port_zone` and `protected_device_zone`, plus `desk` for presence. Hands come from LOCKED IN's MediaPipe hand landmarker, every second frame. Detection is geometry and time only:

| Event | When |
|---|---|
| `hand_near_keyboard`, `hand_near_usb_zone` | any landmark inside the zone grown by 4% of the frame, for 0.4 s |
| `keyboard_interaction` | 2+ fingertips inside for 1.0 s, and the fingertips move (a resting hand is not typing) |
| `laptop_interaction` | 2+ fingertips inside for 1.0 s |
| `usb_zone_interaction` | 2+ fingertips inside for 0.6 s |
| `protected_device_interaction` | 2+ fingertips inside for 0.8 s |

An interaction also needs the hand present in at least 70% of frames during the dwell, with no jump across the image. The dwell restarts after 0.5 s out of the zone. Each episode produces one event, and an episode ends 1 s after the hand is gone.

A hand at the USB ports does not mean a device was connected. The Mac's USB watcher is the authority on that, and the database correlates the two. A hand the camera cannot see (behind the body, or off the edge of the picture) produces nothing.

## Measured on the M4 MacBook Air (2026-11-07, built-in webcam, 640x480)

| | |
|---|---|
| Real webcam to a contract event | `person_in_zone`, confidence 0.85, settled over 328 ms, from `cam-e2d5d60c513c` |
| Capture to event | 11.9 ms, after the frame that settled the observation |
| Throughput | 30.0 fps captured, 30.3 fps processed, 3 frames dropped of 521 |
| Person detection | p50 12.0 ms, p95 13.3 ms |
| Process memory | 308 MB RSS (the script alone: Python, MediaPipe, OpenCV) |
| Face detection (YuNet), live frames | full size p50 6.8 ms, p95 8.1 ms; half size (now the default) p50 2.2 ms, p95 2.7 ms |
| Pipeline with faces (`airlock_identity_bench.py`, 20 s) | 31 fps; person detection p50 11.3 ms, p95 12.2 ms; 344 MB RSS |
| Presence | first frame with someone at the desk to `person_in_zone`: 281 ms (n = 1, includes the 3-of-5 frame debounce) |
| Event to publisher queue, event to mode change | 1.0 ms; under 0.1 ms (NORMAL to WATCHFUL when someone not enrolled arrived) |
| Identity | not yet measured for owner or unrecognized: nobody was enrolled, so the face could only be uncertain (after 1.54 s) |
| Face alignment and embedding (SFace) | 4.2 ms per face (synthetic input; time does not depend on the face) |

Timing starts when OpenCV hands over the frame. The webcam's own exposure and transfer delay before that is not measured.

## Not done yet

The work is ordered: SpacetimeDB and voice, then the Jetson adapter. Zone interaction has not yet run on a calibrated real desk.

The Jetson is not verified. Nothing here has run on the Jetson, and there is no TensorRT path. The deployment assumptions are written up with the edge entrypoint.
