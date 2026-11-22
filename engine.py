"""Face detection / recognition engines.

Two interchangeable back-ends are supported:

* ``opencv``  - OpenCV YuNet (detector) + SFace (recognizer). Pure OpenCV, no dlib
                 needed. Requires two ONNX files, see ``scripts/download_models.py``.
* ``dlib``    - the ``face_recognition`` package (dlib HOG + ResNet), as in the
                 original idea. Optional, needs dlib to be installed.

Embeddings are only comparable within one engine, so every stored embedding is
tagged with ``engine.name`` and only matched against the same engine.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

DETECTOR_FILE = "face_detection_yunet_2023mar.onnx"
RECOGNIZER_FILE = "face_recognition_sface_2021dec.onnx"
# A real ONNX model is megabytes; an HTML error page saved by mistake is tiny.
MIN_MODEL_BYTES = 100_000
MIN_FACE_PX = 32


class EngineUnavailable(RuntimeError):
    """Raised when an engine cannot be created (missing package or model)."""


@dataclass(frozen=True)
class Face:
    box: tuple[int, int, int, int]  # x, y, w, h in pixels of the analysed image
    embedding: np.ndarray  # float32 vector


class FaceEngine:
    """Base class. ``score`` is "higher is better"; a match needs ``>= threshold``."""

    name = "base"
    threshold = 0.5

    def __init__(self) -> None:
        # Detectors keep per-call state (input size), so serialise access.
        self._lock = threading.Lock()

    def detect(self, image_bgr: np.ndarray) -> list[Face]:
        with self._lock:
            return self._detect(image_bgr)

    def _detect(self, image_bgr: np.ndarray) -> list[Face]:  # pragma: no cover
        raise NotImplementedError

    def score(self, known: np.ndarray, probe: np.ndarray) -> np.ndarray:  # pragma: no cover
        raise NotImplementedError


def _clamp_box(x: float, y: float, w: float, h: float, width: int, height: int):
    x0 = int(max(0, min(width - 1, round(x))))
    y0 = int(max(0, min(height - 1, round(y))))
    x1 = int(max(x0 + 1, min(width, round(x + w))))
    y1 = int(max(y0 + 1, min(height, round(y + h))))
    return x0, y0, x1 - x0, y1 - y0


class OpenCVEngine(FaceEngine):
    """YuNet detection + SFace embeddings (cosine similarity)."""

    name = "opencv-sface"
    threshold = 0.363  # cosine threshold recommended by the SFace authors

    def __init__(self, models_dir: Path) -> None:
        super().__init__()
        import cv2

        det_path = Path(models_dir) / DETECTOR_FILE
        rec_path = Path(models_dir) / RECOGNIZER_FILE
        for path in (det_path, rec_path):
            if not path.is_file() or path.stat().st_size < MIN_MODEL_BYTES:
                raise EngineUnavailable(
                    f"model file missing or invalid: {path.name} "
                    f"(run `python scripts/download_models.py`)"
                )
        try:
            self._detector = cv2.FaceDetectorYN.create(
                str(det_path), "", (320, 320), 0.85, 0.3, 500
            )
            self._recognizer = cv2.FaceRecognizerSF.create(str(rec_path), "")
        except Exception as exc:  # cv2.error and friends
            raise EngineUnavailable(f"could not load OpenCV models: {exc}") from exc

    def _detect(self, image_bgr: np.ndarray) -> list[Face]:
        height, width = image_bgr.shape[:2]
        self._detector.setInputSize((width, height))
        _, rows = self._detector.detect(image_bgr)
        faces: list[Face] = []
        if rows is None:
            return faces
        for row in rows:
            x, y, w, h = _clamp_box(row[0], row[1], row[2], row[3], width, height)
            if min(w, h) < MIN_FACE_PX:
                continue
            aligned = self._recognizer.alignCrop(image_bgr, row)
            vec = self._recognizer.feature(aligned).flatten().astype(np.float32)
            norm = float(np.linalg.norm(vec))
            if not np.isfinite(norm) or norm == 0.0:
                continue
            faces.append(Face((x, y, w, h), vec / norm))
        return faces

    def score(self, known: np.ndarray, probe: np.ndarray) -> np.ndarray:
        return known @ probe  # both L2-normalised -> cosine similarity


class DlibEngine(FaceEngine):
    """``face_recognition`` package. score = 1 - euclidean distance."""

    name = "dlib-face-recognition"
    threshold = 0.5  # i.e. distance <= 0.5 (library default 0.6 is more permissive)

    def __init__(self) -> None:
        super().__init__()
        try:
            import face_recognition  # noqa: F401
        except ImportError as exc:
            raise EngineUnavailable("the `face_recognition` package is not installed") from exc
        self._fr = face_recognition

    def _detect(self, image_bgr: np.ndarray) -> list[Face]:
        import cv2

        height, width = image_bgr.shape[:2]
        rgb = np.ascontiguousarray(cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB))
        locations = self._fr.face_locations(rgb, model="hog")
        encodings = self._fr.face_encodings(rgb, known_face_locations=locations)
        faces: list[Face] = []
        for (top, right, bottom, left), enc in zip(locations, encodings):
            x, y, w, h = _clamp_box(left, top, right - left, bottom - top, width, height)
            if min(w, h) < MIN_FACE_PX:
                continue
            faces.append(Face((x, y, w, h), np.asarray(enc, dtype=np.float32)))
        return faces

    def score(self, known: np.ndarray, probe: np.ndarray) -> np.ndarray:
        return 1.0 - np.linalg.norm(known - probe, axis=1)


def load_engine(preference: str, models_dir: Path) -> FaceEngine:
    """Create an engine. ``preference``: ``auto`` | ``opencv`` | ``dlib``."""
    builders = {
        "opencv": lambda: OpenCVEngine(models_dir),
        "dlib": DlibEngine,
    }
    if preference != "auto" and preference not in builders:
        raise ValueError(f"unknown FACE_ENGINE {preference!r}; use auto, opencv or dlib")
    order = ["opencv", "dlib"] if preference == "auto" else [preference]
    errors = []
    for key in order:
        try:
            return builders[key]()
        except EngineUnavailable as exc:
            errors.append(f"{key}: {exc}")
    raise EngineUnavailable("; ".join(errors))
