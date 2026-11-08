"""Hands in calibrated zones: dwell, motion continuity and episodes, with scripted landmarks (no camera)."""

from __future__ import annotations

import math

import pytest

from app.security.publisher import FixtureTransport, SecurityEventPublisher
from app.vision.airlock.geometry import ZoneMap
from app.vision.airlock.interaction import InteractionTracker, grow
from app.vision.airlock.observations import HandDet

ZONES = ZoneMap(zones={
    "desk": [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)],
    "keyboard_zone": [(0.3, 0.6), (0.7, 0.6), (0.7, 0.8), (0.3, 0.8)],
    "laptop_zone": [(0.3, 0.3), (0.7, 0.3), (0.7, 0.58), (0.3, 0.58)],
    "usb_port_zone": [(0.85, 0.5), (0.95, 0.5), (0.95, 0.6), (0.85, 0.6)],
    "protected_device_zone": [(0.05, 0.6), (0.2, 0.6), (0.2, 0.8), (0.05, 0.8)],
}, calibrated=True)
FPS = 15


def hand(tx, ty, score=0.9):
    """21 MediaPipe-style landmarks with the five fingertips in a row around (tx, ty), the palm below."""
    pts = [(tx, ty + 0.12)]  # wrist
    for f in range(5):
        x = tx + (f - 2) * 0.012
        pts += [(x, ty + 0.08), (x, ty + 0.05), (x, ty + 0.025), (x, ty)]
    return HandDet(tuple(pts), score)


def run(tracker, seconds, at):
    out = []
    for i in range(int(seconds * FPS)):
        t = i / FPS
        out += [(round(t, 2), o) for o in tracker.update(t, at(i))]
    return out


def kinds(ev):
    return [o.kind for _, o in ev]


def typing(i):
    return [hand(0.5 + 0.01 * math.sin(i * 1.7), 0.7 + 0.008 * math.cos(i * 2.3))]


def test_typing_is_near_then_interaction_with_dwell_and_zone():
    ev = run(InteractionTracker(ZONES), 2.0, typing)
    assert kinds(ev) == ["hand_near_keyboard", "keyboard_interaction"]
    near, typed = ev[0][1], ev[1][1]
    assert near.zone == typed.zone == "keyboard_zone" and typed.dwell_ms >= 1000 and "moving" in typed.detail
    assert 0.39 <= ev[0][0] <= 0.5 and ev[1][0] >= 1.0


def test_a_hand_resting_still_on_the_keys_is_near_but_not_typing():
    assert kinds(run(InteractionTracker(ZONES), 3.0, lambda i: [hand(0.5, 0.7)])) == ["hand_near_keyboard"]


def test_reaching_into_the_usb_ports():
    ev = run(InteractionTracker(ZONES), 1.2, lambda i: [hand(0.9, 0.55)])
    assert kinds(ev) == ["hand_near_usb_zone", "usb_zone_interaction"]
    assert ev[1][1].dwell_ms >= 600


def test_a_hand_brushing_past_the_ports_is_nothing():
    assert kinds(run(InteractionTracker(ZONES), 1.5, lambda i: [hand(0.9, 0.55)] if i < 4 else [])) == []


def test_a_hand_seen_in_only_half_the_frames_is_not_a_sustained_interaction():
    ev = run(InteractionTracker(ZONES), 3.0, lambda i: [hand(0.9, 0.55)] if i % 2 == 0 else [hand(0.6, 0.2)])
    assert "usb_zone_interaction" not in kinds(ev)


def test_fingertips_jumping_across_the_zone_every_frame_never_settle_into_typing():
    def jumpy(i):  # two hands' worth of detections alternating 0.36 apart: not one continuous hand
        return [hand(0.33 if i % 2 else 0.67, 0.7 + 0.01 * math.sin(i))]

    assert kinds(run(InteractionTracker(ZONES), 3.0, jumpy)) == ["hand_near_keyboard"]


def test_laptop_and_protected_device_zones_have_interactions_only():
    ev = run(InteractionTracker(ZONES), 1.5, lambda i: [hand(0.5, 0.45), hand(0.12, 0.7)])
    assert sorted(kinds(ev)) == ["laptop_interaction", "protected_device_interaction"]


def test_one_episode_one_event_and_a_new_episode_after_the_hand_is_gone():
    def twice(i):
        return [hand(0.9, 0.55)] if i < 15 or 40 <= i < 55 else []

    assert kinds(run(InteractionTracker(ZONES), 4.0, twice)) == ["hand_near_usb_zone", "usb_zone_interaction"] * 2


def test_uncalibrated_zones_watch_nothing_and_low_scores_are_ignored():
    assert run(InteractionTracker(ZoneMap()), 2.0, typing) == []
    assert run(InteractionTracker(ZONES), 2.0, lambda i: [hand(0.9, 0.55, score=0.2)]) == []


def test_near_region_grows_the_zone():
    big = grow(ZONES.get("usb_port_zone"), 0.04)
    assert min(p[0] for p in big) < 0.85 and max(p[0] for p in big) > 0.95


@pytest.mark.parametrize("kind", ["usb_zone_interaction", "keyboard_interaction", "hand_near_usb_zone"])
def test_interaction_events_are_valid_camera_events(tmp_path, kind):
    from app.security.contracts import SecurityKind, SecuritySource

    pub = SecurityEventPublisher("cam-test", FixtureTransport(), directory=tmp_path)
    ev = pub.build(SecuritySource.MAC_VISION, SecurityKind(kind), {"camera": "mac_webcam", "inference": "mac_mediapipe", "zone": "usb_port_zone",
                                                                    "dwell_ms": 700, "armed": True}, confidence=0.9)
    assert ev.metadata["zone"] == "usb_port_zone"
    pub.close()
