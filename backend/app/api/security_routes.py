"""The AIRLOCK routes for this Mac's own dashboard. Arming and disarming need the owner's code. Whether a lock is real is set by how the
backend was started (LOCKEDIN_SECURITY_LOCK_MODE), and no request can change it."""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..security.contracts import LockRequestV1
from ..security.service import SecurityError
from ..security.session import ResponsePolicy
from .routes import _own_dashboard, rt_of

router = APIRouter(prefix="/api/security")


class ArmBody(BaseModel):
    owner_code: str = Field(..., min_length=1, max_length=128)
    policy: ResponsePolicy | None = None


class CodeBody(BaseModel):
    owner_code: str = Field(..., min_length=1, max_length=128)


def _refused(exc: SecurityError) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc))


@router.get("")
async def state(request: Request):
    return rt_of(request).security.snapshot()


@router.get("/latency")
async def latency(request: Request):
    """Everything needed to measure this session's camera-to-voice chain: warnings with their audio timing, camera events, mode changes."""
    security = rt_of(request).security
    return {"warnings": list(security.warnings), "camera_events": [e.model_dump(mode="json") for _t, e in security.physical],
            "transitions": list(security.modes.transitions), "armed": security.session is not None}


@router.get("/baseline")
async def baseline(request: Request):
    """What is connected now, as the arm_session baseline: salted fingerprints only."""
    from ..security.spacetime_adapter import BaselineReport

    security = rt_of(request).security
    if security.monitor is None:
        raise HTTPException(status_code=409, detail="monitoring is not running")
    report = BaselineReport(observed_at=0, usb=security.monitor.fingerprints())
    return {"baseline_json": report.baseline_json(), "devices": sum(report.usb.values())}


@router.get("/lock/preflight")
async def lock_preflight(request: Request):
    return rt_of(request).security.lock.preflight()


@router.get("/notifications")
async def notifications(request: Request):
    """Who was told about which incident (ids are SpacetimeDB's my_incidents.id), over which channel, and exactly what each receipt says."""
    dispatch = rt_of(request).security.dispatch
    if dispatch is None:
        return {"policy": None, "phone": None, "incidents": [], "detail": "not connected to SpacetimeDB, so no incidents to dispatch"}
    return dispatch.snapshot()


@router.post("/monitor/start")
async def monitor_start(request: Request):
    _own_dashboard(request)
    return await rt_of(request).security.start_monitoring()


@router.post("/monitor/stop")
async def monitor_stop(request: Request):
    _own_dashboard(request)
    try:
        await rt_of(request).security.stop_monitoring()
    except SecurityError as exc:
        raise _refused(exc) from None
    return {"ok": True}


@router.post("/arm")
async def arm(body: ArmBody, request: Request):
    _own_dashboard(request)
    security = rt_of(request).security
    try:
        await security.arm(body.owner_code, body.policy)
    except SecurityError as exc:
        raise _refused(exc) from None
    return security.snapshot()


@router.post("/disarm")
async def disarm(body: CodeBody, request: Request):
    _own_dashboard(request)
    security = rt_of(request).security
    try:
        await security.disarm(body.owner_code)
    except SecurityError as exc:
        raise _refused(exc) from None
    return security.snapshot()


@router.post("/lock")
async def owner_lock(body: CodeBody, request: Request):
    _own_dashboard(request)
    try:
        result = await rt_of(request).security.owner_lock(body.owner_code)
    except SecurityError as exc:
        raise _refused(exc) from None
    return result.to_dict()


@router.post("/lock-request")
async def lock_request(body: LockRequestV1, request: Request):
    """A LockRequestV1, for local development. Every check still applies, and a fixture never locks a real screen."""
    _own_dashboard(request)
    return (await rt_of(request).security.handle_lock_request(body)).to_dict()


# ------------------------------------------------------------------ demo mode (security/demo.py)
class DemoStart(BaseModel):
    mode: Literal["live", "rehearsal"] = "live"


def _demo(request: Request):
    security = rt_of(request).security
    if security.demo is None:
        from ..security.demo import DemoService

        security.demo = DemoService(security)
    return security.demo


@router.get("/demo")
async def demo_state(request: Request):
    return _demo(request).snapshot()


@router.post("/demo/start")
async def demo_start(body: DemoStart, request: Request):
    _own_dashboard(request)
    try:
        return await _demo(request).start(body.mode)
    except SecurityError as exc:
        raise _refused(exc) from exc


@router.post("/demo/enroll")
async def demo_enroll(request: Request):
    """The owner pressed Enroll: their face is enrolled for this demo only, as embeddings on this Mac."""
    _own_dashboard(request)
    try:
        return await _demo(request).enroll()
    except SecurityError as exc:
        raise _refused(exc) from exc


@router.post("/demo/reset")
async def demo_reset(request: Request):
    _own_dashboard(request)
    try:
        return await _demo(request).reset()
    except SecurityError as exc:
        raise _refused(exc) from exc


@router.post("/demo/end")
async def demo_end(request: Request):
    _own_dashboard(request)
    return await _demo(request).end()


class TrialBody(BaseModel):
    label: Literal["genuine", "impostor"]
    participant: str = Field("p", max_length=16)


@router.post("/demo/trial")
async def demo_trial(body: TrialBody, request: Request):
    """A consenting person is about to walk into view: record what the camera decides about them, and how fast."""
    _own_dashboard(request)
    try:
        return _demo(request).trial_start(body.label, body.participant)
    except SecurityError as exc:
        raise _refused(exc) from exc


@router.post("/demo/rehearsal/usb")
async def demo_fixture_usb(request: Request):
    _own_dashboard(request)
    try:
        return _demo(request).rehearsal_usb()
    except SecurityError as exc:
        raise _refused(exc) from exc


@router.get("/demo/stream.mjpg")
async def demo_stream(request: Request):
    """The live camera with what the detectors found on each frame. Local only (the backend listens on 127.0.0.1); never stored."""
    security = rt_of(request).security
    vision = getattr(security.camera, "vision", None) if security.camera is not None else None
    if vision is None or not security.camera.running:
        raise HTTPException(503, "CAMERA OFF")
    boundary = "airlockframe"

    async def frames():
        last = -1
        vision.stream_clients += 1
        try:
            while not await request.is_disconnected() and security.camera is not None and security.camera.vision is vision:
                item = vision.latest_jpeg()
                if item is None or item[0] == last:
                    await asyncio.sleep(0.02)
                    continue
                last, jpg = item
                yield f"--{boundary}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpg)}\r\n\r\n".encode() + jpg + b"\r\n"
                await asyncio.sleep(1 / 24)
        finally:
            vision.stream_clients -= 1

    return StreamingResponse(frames(), media_type=f"multipart/x-mixed-replace; boundary={boundary}", headers={"Cache-Control": "no-store"})
