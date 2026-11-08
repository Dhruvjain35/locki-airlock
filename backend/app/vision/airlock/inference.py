"""Inference adapters for the AIRLOCK camera. The pipeline only sees ``detect(rgb, ts_ms)``.

``mac_mediapipe``  MediaPipe Tasks on the Mac: EfficientDet-Lite0 (COCO, int8) for people. Faces use
                   OpenCV's YuNet and hands reuse LOCKED IN's MediaPipe hand landmarker.
``opencv_dnn``     OpenCV's DNN module only, for machines without MediaPipe (the Jetson). Faces work
                   here today (YuNet). A person detector for it is not chosen yet: see docs/airlock/VISION.md.

Models come from ``scripts/download_models.py --airlock-vision`` (about 42 MB, checksummed).
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from .geometry import Box
from .observations import PersonDet

os.environ.setdefault("GLOG_minloglevel", "2")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")

PERSON_MODEL = "efficientdet_lite0.tflite"
YUNET_MODEL = "face_detection_yunet_2023mar.onnx"
SFACE_MODEL = "face_recognition_sface_2021dec.onnx"


class ModelMissing(RuntimeError):
    pass


def require_model(models_dir: Path, name: str) -> Path:
    path = Path(models_dir) / name
    if not path.exists():
        raise ModelMissing(f"{name} not found in {models_dir}; run `python scripts/download_models.py --airlock-vision` while online")
    return path


class MediaPipePersonDetector:
    """People only (COCO "person"), up to ``max_people`` per frame. VIDEO mode: timestamps must increase."""

    name = "efficientdet_lite0"
    inference = "mac_mediapipe"

    def __init__(self, models_dir: Path, min_score: float = 0.35, max_people: int = 5):
        import mediapipe as mp
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision as mp_vision

        self._mp = mp
        options = mp_vision.ObjectDetectorOptions(
            base_options=mp_python.BaseOptions(model_asset_path=str(require_model(models_dir, PERSON_MODEL))),
            running_mode=mp_vision.RunningMode.VIDEO,
            max_results=max_people,
            score_threshold=min_score,
            category_allowlist=["person"],
        )
        self._detector = mp_vision.ObjectDetector.create_from_options(options)
        self._last_ts = -1

    def detect(self, rgb: np.ndarray, ts_ms: int) -> list[PersonDet]:
        ts_ms = max(int(ts_ms), self._last_ts + 1)
        self._last_ts = ts_ms
        h, w = rgb.shape[:2]
        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
        result = self._detector.detect_for_video(image, ts_ms)
        out = []
        for det in result.detections:
            bb = det.bounding_box
            score = float(det.categories[0].score) if det.categories else 0.0
            x, y = max(0.0, bb.origin_x / w), max(0.0, bb.origin_y / h)
            bw, bh = min(1.0 - x, bb.width / w), min(1.0 - y, bb.height / h)
            out.append(PersonDet(Box(x, y, bw, bh), score))
        return out

    def close(self) -> None:
        self._detector.close()


def frame_stats(bgr: np.ndarray):
    """Grey mean and spread of an 80x60 copy: about 0.1 ms, run on every frame for the obstruction check."""
    import cv2

    from .observations import FrameStats

    small = cv2.resize(bgr, (80, 60), interpolation=cv2.INTER_AREA)
    grey = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    mean, std = cv2.meanStdDev(grey)
    return FrameStats(float(mean[0][0]), float(std[0][0]))
