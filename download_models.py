"""Download the two OpenCV face models (YuNet detector + SFace recognizer) into ./models.

    python scripts/download_models.py

Models come from the official OpenCV Model Zoo (https://github.com/opencv/opencv_zoo).
Run this on a machine with internet access. Files are written atomically and rejected
if they look like an HTML error page instead of an ONNX model.
"""
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from engine import DETECTOR_FILE, MIN_MODEL_BYTES, RECOGNIZER_FILE  # noqa: E402

BASE = "https://github.com/opencv/opencv_zoo/raw/main/models"
MODELS = {
    DETECTOR_FILE: f"{BASE}/face_detection_yunet/{DETECTOR_FILE}",
    RECOGNIZER_FILE: f"{BASE}/face_recognition_sface/{RECOGNIZER_FILE}",
}


def download(name: str, url: str, dest_dir: Path) -> None:
    target = dest_dir / name
    if target.is_file() and target.stat().st_size >= MIN_MODEL_BYTES:
        print(f"  {name}: already present")
        return
    print(f"  {name}: downloading ...")
    request = urllib.request.Request(url, headers={"User-Agent": "smart-attendance/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()
    if len(data) < MIN_MODEL_BYTES or data.lstrip()[:1] == b"<":
        raise RuntimeError(f"{name}: download does not look like an ONNX model ({len(data)} bytes)")
    fd, tmp = tempfile.mkstemp(dir=dest_dir, suffix=".part")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    print(f"  {name}: {len(data) / 1e6:.1f} MB OK")


def main() -> int:
    dest = Path(os.environ.get("ATTENDANCE_MODELS_DIR", ROOT / "models"))
    dest.mkdir(parents=True, exist_ok=True)
    print(f"Models directory: {dest}")
    try:
        for name, url in MODELS.items():
            download(name, url, dest)
    except Exception as exc:
        print(f"ERROR: {exc}\nYou can also download the files manually from {BASE} "
              f"and place them in {dest}.", file=sys.stderr)
        return 1
    print("Done. Start the app with: python app.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
