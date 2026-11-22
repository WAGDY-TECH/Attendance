"""Deterministic stand-in engine for tests: a "face" is a bright coloured rectangle on a
dark background and its identity is its colour. Lets us test the whole application
(DB, API, matching logic, UI) without ML models."""
from __future__ import annotations

import cv2
import numpy as np

from engine import Face, FaceEngine

RED, GREEN, BLUE = (0, 0, 255), (0, 255, 0), (255, 0, 0)
YELLOW, MAGENTA, CYAN = (0, 255, 255), (255, 0, 255), (255, 255, 0)


class FakeEngine(FaceEngine):
    name = "fake-colour"
    threshold = 0.9

    def _detect(self, image_bgr):
        h, w = image_bgr.shape[:2]
        # max channel (not grayscale: pure blue has luma ~29 and would be missed)
        mask = (image_bgr.max(axis=2) > 60).astype(np.uint8) * 255
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        faces = []
        for c in sorted(contours, key=lambda c: cv2.boundingRect(c)[0]):
            x, y, bw, bh = cv2.boundingRect(c)
            if bw < 32 or bh < 32:
                continue
            inner = image_bgr[y + 4:y + bh - 4, x + 4:x + bw - 4]
            mean = inner.reshape(-1, 3).mean(axis=0)
            vec = (mean - mean.mean()).astype(np.float32)
            norm = float(np.linalg.norm(vec))
            if norm == 0:
                continue
            faces.append(Face((x, y, bw, bh), vec / norm))
        return faces

    def score(self, known, probe):
        return known @ probe


def make_image(*colours, size=(480, 640), jpeg=True):
    """Image with one 120px square per colour, left to right."""
    h, w = size
    img = np.zeros((h, w, 3), np.uint8)
    for i, colour in enumerate(colours):
        x = 40 + i * 160
        img[100:220, x:x + 120] = colour
    if not jpeg:
        return img
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    assert ok
    return buf.tobytes()
