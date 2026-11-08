"""Owner face enrollment and checks for AIRLOCK. Everything stays on this Mac.

  airlock_face.py status                         is anyone enrolled, how many samples, which thresholds
  airlock_face.py enroll --i-consent [--add]     the owner enrolls (about 30 s; look at the camera, then turn a little each way)
  airlock_face.py verify [--seconds 20]          live: what the camera would report about each face, with the scores
  airlock_face.py delete --yes                   overwrite and remove the owner template
  airlock_face.py calibrate --label owner --i-consent                     held-out owner samples (not added to the template)
  airlock_face.py calibrate --label visitor --volunteer v1 --i-consent    a consenting visitor; only scores are kept
  airlock_face.py report [--holdout v3,v4] [--apply]                      thresholds from those scores
  airlock_face.py bench [--seconds 10]           face detection speed on live frames; nothing is compared or kept

The template (data/airlock/owner_face.json, mode 0600) holds embeddings only, never a picture. Calibration keeps
only similarity scores and labels (data/airlock/face_calibration.jsonl), never a visitor's embedding or picture.
The camera light is on while a command uses the camera.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.vision.airlock.enrollment import (  # noqa: E402
    FaceEnrollment,
    delete_template,
    load_template,
    save_template,
    template_path,
)
from app.vision.airlock.faces import (  # noqa: E402
    QualityConfig,
    SFaceEmbedder,
    Thresholds,
    YuNetFaceDetector,
    assess,
    similarity,
    verdict,
)
from app.vision.webcam import CameraSource  # noqa: E402

DATA = BACKEND.parent / "data"
MODELS = BACKEND / "models"
CALIBRATION = DATA / "airlock" / "face_calibration.jsonl"


def frames(source: str, seconds: float):
    from app.security.device_lock import camera_lock

    lock = camera_lock(DATA)
    lock.acquire()  # DeviceBusy if the backend or another script is using the camera
    cam = CameraSource(source, 640, 480)
    cam.start()
    last, deadline = 0, time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            item = cam.read_latest(last, timeout=1.0)
            if item is None:
                if cam.state not in ("live", "opening"):
                    print(f"camera: {cam.state} ({cam.reason})")
                continue
            last, bgr, t = item
            yield t, bgr
    finally:
        cam.stop()
        lock.release()


def cmd_status(args) -> int:
    path = template_path(DATA)
    tpl = load_template(path)
    if tpl is None:
        print(f"nobody is enrolled ({path} does not exist)")
        return 0
    mode = oct(path.stat().st_mode & 0o777)
    th = tpl.thresholds
    print(f"enrolled: {tpl.samples.shape[0]} samples {tpl.poses}, {tpl.sessions} session(s), model {tpl.model}; file {path} mode {mode}")
    print(f"thresholds: match >= {th.match:.3f}, unrecognized <= {th.reject:.3f}, {'calibrated' if th.calibrated else 'default, not calibrated'}")
    if tpl.calibration:
        print(f"calibration: {json.dumps(tpl.calibration)}")
    return 0


def cmd_enroll(args) -> int:
    if not args.i_consent:
        print("Enrollment keeps embeddings of your face on this Mac. Run again with --i-consent to start.")
        return 2
    path = template_path(DATA)
    existing = load_template(path) if args.add else None
    if path.exists() and not args.add:
        print("an owner is already enrolled: use --add to add samples (other lighting), or delete first")
        return 2
    enrol = FaceEnrollment(YuNetFaceDetector(MODELS), SFaceEmbedder(MODELS))
    print("Enrollment started. Sit where you normally work, alone in view. Look at the camera, then turn your head a little each way.")
    hint = None
    for t, bgr in frames(args.source, args.seconds):
        p = enrol.offer(bgr, t)
        if p["hint"] != hint:
            hint = p["hint"]
            print(f"  {p['accepted']}/{p['needed']}  {hint}")
        if p["complete"]:
            break
    p = enrol.progress(None)
    print(f"accepted {p['accepted']}/{p['needed']} {p['poses']}; not used: {p['rejected']}")
    if not p["complete"]:
        print("not enough samples: nothing saved. Try brighter light, or move closer to the camera.")
        return 1
    save_template(path, enrol.template(existing))
    print(f"saved {path} (mode 0600): embeddings only. Thresholds are the conservative default until you run calibrate and report.")
    return 0


def cmd_verify(args) -> int:
    tpl = load_template(template_path(DATA))
    if tpl is None:
        print("nobody is enrolled")
        return 2
    det, emb = YuNetFaceDetector(MODELS), SFaceEmbedder(MODELS)
    q = QualityConfig()
    last_print = 0.0
    for t, bgr in frames(args.source, args.seconds):
        faces = det.detect(bgr)
        if t - last_print < 0.5:
            continue
        last_print = t
        if not faces:
            print("  no face")
            continue
        parts = []
        for f in sorted(faces, key=lambda f: f.box.area, reverse=True)[:3]:
            e, aligned = emb.embed(bgr, f)
            problem = assess(f, bgr.shape[1], aligned, q)
            score = similarity(e, tpl.samples)
            parts.append(f"{verdict(score, problem, tpl.thresholds):<8} score {score:.3f}{'' if problem is None else ' (' + problem + ')'}")
        print("  " + " | ".join(parts))
    return 0


def cmd_delete(args) -> int:
    if not args.yes:
        print("this removes the owner template; run again with --yes")
        return 2
    print("deleted" if delete_template(template_path(DATA)) else "nobody was enrolled")
    return 0


def cmd_calibrate(args) -> int:
    if not args.i_consent:
        print("Run with --i-consent once the person in view has agreed. Only similarity scores are kept.")
        return 2
    if args.label == "visitor" and not args.volunteer:
        print("--volunteer ID is required for visitors (a label such as v1, never a name)")
        return 2
    tpl = load_template(template_path(DATA))
    if tpl is None:
        print("enroll the owner first")
        return 2
    det, emb = YuNetFaceDetector(MODELS), SFaceEmbedder(MODELS)
    q = QualityConfig()
    session = time.strftime("%Y%m%d-%H%M%S")
    CALIBRATION.parent.mkdir(parents=True, exist_ok=True)
    kept = 0
    last = 0.0
    with open(CALIBRATION, "a") as fh:
        for t, bgr in frames(args.source, args.seconds):
            if t - last < 0.3:
                continue
            faces = det.detect(bgr)
            if len(faces) != 1:
                continue  # one consenting person at a time
            last = t
            e, aligned = emb.embed(bgr, faces[0])
            problem = assess(faces[0], bgr.shape[1], aligned, q)
            rec = {"t": round(time.time(), 2), "session": session, "label": args.label, "volunteer": args.volunteer or "owner",
                   "condition": args.condition, "score": round(similarity(e, tpl.samples), 4), "quality": problem}
            fh.write(json.dumps(rec) + "\n")
            kept += 1
    print(f"kept {kept} scores for {args.label} {args.volunteer or ''} ({args.condition}) in {CALIBRATION}")
    return 0


def _rates(owner, visitors, th: Thresholds):
    """False owner match per visitor sample; owner called unrecognized; owner matched."""
    fm = sum(1 for s in visitors if s >= th.match)
    fr = sum(1 for s in owner if s <= th.reject)
    om = sum(1 for s in owner if s >= th.match)
    return fm, fr, om


def cmd_report(args) -> int:
    try:
        rows = [json.loads(line) for line in CALIBRATION.read_text().splitlines() if line.strip()]
    except FileNotFoundError:
        print("no calibration scores yet")
        return 2
    good = [r for r in rows if r["quality"] is None]
    holdout = {v for v in (args.holdout or "").split(",") if v}
    owner = [r["score"] for r in good if r["label"] == "owner"]
    vis_cal = [r["score"] for r in good if r["label"] == "visitor" and r["volunteer"] not in holdout]
    vis_out = [r["score"] for r in good if r["label"] == "visitor" and r["volunteer"] in holdout]
    vols = sorted({r["volunteer"] for r in good if r["label"] == "visitor"})
    print(f"usable samples: owner {len(owner)}, visitors {len(vis_cal)} for calibration + {len(vis_out)} held out; volunteers {vols}; "
          f"{len(rows) - len(good)} samples failed quality")
    if len(owner) < 10 or len(vis_cal) < 10:
        print("too few samples to set thresholds (need at least 10 owner and 10 visitor samples)")
        return 1
    owner.sort()
    o_p05 = owner[max(0, int(len(owner) * 0.05) - 1)]
    v_max = max(vis_cal)
    th = Thresholds(match=round(max(0.40, v_max + 0.05), 3), reject=round(max(0.10, min(0.30, o_p05 - 0.05)), 3), calibrated=True)
    if th.reject >= th.match:
        th.reject = round(th.match - 0.05, 3)
    fm, fr, om = _rates(owner, vis_cal, th)
    print(f"owner scores: min {owner[0]:.3f}, 5th pct {o_p05:.3f}, median {owner[len(owner) // 2]:.3f}; visitor max {v_max:.3f}")
    print(f"proposed: match >= {th.match}, unrecognized <= {th.reject}")
    print(f"  calibration set: false owner matches {fm}/{len(vis_cal)} visitor samples; owner matched {om}/{len(owner)}; owner called unrecognized {fr}/{len(owner)}")
    out = {"owner": len(owner), "visitors": len(vis_cal), "volunteers": [v for v in vols if v not in holdout], "false_owner_matches": fm,
           "owner_matched": om, "owner_rejected": fr, "at": int(time.time())}
    if vis_out:
        fm2, _, _ = _rates([], vis_out, th)
        print(f"  held out ({sorted(holdout)}): false owner matches {fm2}/{len(vis_out)}")
        out.update(holdout=sorted(holdout), holdout_samples=len(vis_out), holdout_false_owner_matches=fm2)
    print("Small sample: these rates describe these people in this room, not a population.")
    if args.apply:
        path = template_path(DATA)
        tpl = load_template(path)
        tpl.thresholds, tpl.calibration = th, out
        save_template(path, tpl)
        print(f"applied to {path}")
    return 0


def cmd_bench(args) -> int:
    det = YuNetFaceDetector(MODELS)
    ms, found, n = [], 0, 0
    for _t, bgr in frames(args.source, args.seconds):
        t0 = time.perf_counter()
        faces = det.detect(bgr)
        ms.append((time.perf_counter() - t0) * 1000)
        n += 1
        found += bool(faces)
    ms.sort()
    if not ms:
        print("no frames")
        return 1
    print(f"face detection on {n} live frames: p50 {ms[len(ms) // 2]:.1f} ms, p95 {ms[int(len(ms) * 0.95)]:.1f} ms; a face in {found}/{n} frames. Nothing was compared or kept.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="0")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    e = sub.add_parser("enroll")
    e.add_argument("--i-consent", action="store_true")
    e.add_argument("--add", action="store_true")
    e.add_argument("--seconds", type=float, default=60)
    v = sub.add_parser("verify")
    v.add_argument("--seconds", type=float, default=20)
    d = sub.add_parser("delete")
    d.add_argument("--yes", action="store_true")
    c = sub.add_parser("calibrate")
    c.add_argument("--label", choices=("owner", "visitor"), required=True)
    c.add_argument("--volunteer", default=None)
    c.add_argument("--condition", default="normal", help="normal, low_light, glasses, side, occluded ...")
    c.add_argument("--i-consent", action="store_true")
    c.add_argument("--seconds", type=float, default=15)
    r = sub.add_parser("report")
    r.add_argument("--holdout", default="")
    r.add_argument("--apply", action="store_true")
    b = sub.add_parser("bench")
    b.add_argument("--seconds", type=float, default=10)
    args = ap.parse_args()
    return {"status": cmd_status, "enroll": cmd_enroll, "verify": cmd_verify, "delete": cmd_delete, "calibrate": cmd_calibrate,
            "report": cmd_report, "bench": cmd_bench}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
