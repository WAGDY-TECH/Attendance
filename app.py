"""Smart Student Attendance - Flask application (API + dashboard)."""
from __future__ import annotations

import csv
import hmac
import io
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import cv2
import numpy as np
from flask import (Blueprint, Flask, Response, current_app, jsonify,
                   render_template, request, send_from_directory)
from PIL import Image
from werkzeug.exceptions import HTTPException

import storage
from engine import EngineUnavailable, FaceEngine, load_engine

BASE_DIR = Path(__file__).resolve().parent
log = logging.getLogger("attendance")

DAY_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
ADMISSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
MAX_PHOTOS = 5
MAX_FACES_PER_FRAME = 60
THUMB_SIZE = 160
CSV_DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

bp = Blueprint("main", __name__)


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# --------------------------------------------------------------------------- helpers
def now() -> datetime:
    return current_app.config["NOW"]()


def get_engine() -> FaceEngine | None:
    return current_app.extensions["face_engine"]


def require_engine() -> FaceEngine:
    engine = get_engine()
    if engine is None:
        raise ApiError(503, "Face recognition engine unavailable: "
                       + current_app.extensions["face_engine_error"])
    return engine


def parse_day(value: str | None, default: str) -> str:
    if not value:
        return default
    if not DAY_RE.match(value):
        raise ApiError(400, "Invalid date, expected YYYY-MM-DD")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ApiError(400, "Invalid date") from None
    return value


def clean_name(raw: str | None) -> str:
    name = " ".join((raw or "").split())
    name = "".join(ch for ch in name if ch.isprintable())
    if not 2 <= len(name) <= 80:
        raise ApiError(400, "Name must be between 2 and 80 characters")
    return name


def clean_admission(raw: str | None) -> str:
    value = (raw or "").strip()
    if not ADMISSION_RE.match(value):
        raise ApiError(400, "Admission number must be 1-32 characters: letters, digits, '-' or '_'")
    return value.upper()


def decode_image(data: bytes) -> np.ndarray:
    cfg = current_app.config
    try:
        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
    except Exception:
        raise ApiError(400, "File is not a valid image") from None
    if width * height > cfg["MAX_IMAGE_PIXELS"]:
        raise ApiError(413, "Image dimensions are too large")
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ApiError(400, "Unsupported image format (use JPEG, PNG or WebP)")
    h, w = img.shape[:2]
    longest = max(h, w)
    if longest > cfg["MAX_IMAGE_SIDE"]:
        scale = cfg["MAX_IMAGE_SIDE"] / longest
        img = cv2.resize(img, (max(1, round(w * scale)), max(1, round(h * scale))),
                         interpolation=cv2.INTER_AREA)
    return img


def normalised_box(box, width: int, height: int) -> dict:
    x, y, w, h = box
    return {"x": round(x / width, 4), "y": round(y / height, 4),
            "w": round(w / width, 4), "h": round(h / height, 4)}


def face_thumbnail(img: np.ndarray, box, size: int = THUMB_SIZE) -> np.ndarray:
    """Square crop around the face (with margin), resized to ``size``."""
    height, width = img.shape[:2]
    x, y, w, h = box
    side = int(min(max(w, h) * 1.6, height, width))
    cx, cy = x + w / 2, y + h / 2
    x0 = int(min(max(0, cx - side / 2), width - side))
    y0 = int(min(max(0, cy - side / 2), height - side))
    crop = img[y0:y0 + side, x0:x0 + side]
    return cv2.resize(crop, (size, size), interpolation=cv2.INTER_AREA)


def match_embeddings(engine: FaceEngine, db, embeddings):
    """Return ``[(student_id | None, score)]`` for each embedding (best match each)."""
    rows = db.execute("SELECT student_id, vector FROM embeddings WHERE engine = ?",
                      (engine.name,)).fetchall()
    if not rows:
        return [(None, 0.0) for _ in embeddings]
    ids = np.array([r["student_id"] for r in rows])
    matrix = np.stack([np.frombuffer(r["vector"], dtype=np.float32) for r in rows])
    results = []
    for embedding in embeddings:
        scores = engine.score(matrix, embedding)
        best = int(np.argmax(scores))
        score = float(scores[best])
        results.append((int(ids[best]), score) if score >= engine.threshold else (None, score))
    return results


def csv_cell(value) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(CSV_DANGEROUS_PREFIXES) else text


# --------------------------------------------------------------------------- routes
@bp.get("/")
def index():
    return render_template("index.html")


@bp.get("/api/health")
def health():
    engine = get_engine()
    return jsonify(status="ok", engine=engine.name if engine else None)


@bp.get("/api/dashboard")
def dashboard():
    db = storage.get_db()
    engine = get_engine()
    current = now()
    today = current.strftime("%Y-%m-%d")

    students = [dict(r) for r in db.execute(
        """SELECT s.id, s.name, s.admission_no, a.check_in, a.source,
                  (SELECT COUNT(*) FROM embeddings e
                    WHERE e.student_id = s.id AND e.engine = ?) AS samples
             FROM students s
             LEFT JOIN attendance a ON a.student_id = s.id AND a.day = ?
            ORDER BY s.admission_no COLLATE NOCASE""",
        (engine.name if engine else "", today))]
    for s in students:
        s["present"] = s["check_in"] is not None

    total = len(students)
    present = sum(1 for s in students if s["present"])
    attendance = sorted((s for s in students if s["present"]),
                        key=lambda s: s["check_in"], reverse=True)[:100]

    activity = [dict(r) for r in db.execute(
        """SELECT s.id AS student_id, s.name, a.day, a.check_in
             FROM attendance a JOIN students s ON s.id = a.student_id
            ORDER BY a.day DESC, a.check_in DESC, a.id DESC LIMIT 8""")]

    days = 7
    first = (current - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    counts = {r["day"]: r["c"] for r in db.execute(
        "SELECT day, COUNT(*) AS c FROM attendance WHERE day BETWEEN ? AND ? GROUP BY day",
        (first, today))}
    history = [{"day": (current - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")}
               for i in range(days)]
    for item in history:
        item["present"] = counts.get(item["day"], 0)

    return jsonify(
        date=today,
        stats={"total": total, "present": present, "absent": total - present,
               "rate": round(present / total * 100, 1) if total else 0.0},
        attendance=attendance,
        activity=activity,
        history=history,
        students=students,
        engine={"ready": engine is not None,
                "name": engine.name if engine else None,
                "error": None if engine else current_app.extensions["face_engine_error"]},
    )


@bp.post("/api/students")
def create_student():
    engine = require_engine()
    name = clean_name(request.form.get("name"))
    admission = clean_admission(request.form.get("admission_no"))
    files = request.files.getlist("photos")
    if not 1 <= len(files) <= MAX_PHOTOS:
        raise ApiError(400, f"Provide between 1 and {MAX_PHOTOS} photos")

    db = storage.get_db()
    if db.execute("SELECT 1 FROM students WHERE admission_no = ?", (admission,)).fetchone():
        raise ApiError(409, "A student with this admission number already exists")

    embeddings: list[np.ndarray] = []
    first_img = first_box = None
    for index, upload in enumerate(files, start=1):
        img = decode_image(upload.read())
        faces = engine.detect(img)
        if not faces:
            raise ApiError(422, f"Photo {index}: no face detected. Use a clear, front-facing photo.")
        if len(faces) > 1:
            raise ApiError(422, f"Photo {index}: more than one face detected. "
                                "Use a photo containing only this student.")
        face = faces[0]
        if embeddings and engine.score(embeddings[0][None, :], face.embedding)[0] < engine.threshold:
            raise ApiError(422, f"Photo {index} appears to show a different person than photo 1.")
        embeddings.append(face.embedding)
        if first_img is None:
            first_img, first_box = img, face.box

    [(existing_id, _)] = match_embeddings(engine, db, [embeddings[0]])
    if existing_id is not None:
        other = db.execute("SELECT name, admission_no FROM students WHERE id = ?",
                           (existing_id,)).fetchone()
        raise ApiError(409, f"This face is already enrolled as {other['name']} ({other['admission_no']})")

    photo_path = None
    try:
        cur = db.execute("INSERT INTO students(admission_no, name, created_at) VALUES (?, ?, ?)",
                         (admission, name, now().strftime("%Y-%m-%d %H:%M:%S")))
        student_id = cur.lastrowid
        db.executemany(
            "INSERT INTO embeddings(student_id, engine, vector) VALUES (?, ?, ?)",
            [(student_id, engine.name, np.asarray(e, dtype=np.float32).tobytes())
             for e in embeddings])
        photo_path = current_app.config["PHOTO_DIR"] / f"{student_id}.jpg"
        thumb = face_thumbnail(first_img, first_box)
        if not cv2.imwrite(str(photo_path), thumb, [cv2.IMWRITE_JPEG_QUALITY, 88]):
            raise ApiError(500, "Could not save the student's photo")
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        if photo_path:
            photo_path.unlink(missing_ok=True)
        raise ApiError(409, "A student with this admission number already exists") from None
    except Exception:
        db.rollback()
        if photo_path:
            photo_path.unlink(missing_ok=True)
        raise
    return jsonify(id=student_id, name=name, admission_no=admission), 201


@bp.delete("/api/students/<int:student_id>")
def delete_student(student_id: int):
    db = storage.get_db()
    cur = db.execute("DELETE FROM students WHERE id = ?", (student_id,))
    db.commit()
    if cur.rowcount == 0:
        raise ApiError(404, "Student not found")
    (current_app.config["PHOTO_DIR"] / f"{student_id}.jpg").unlink(missing_ok=True)
    return jsonify(ok=True)


@bp.post("/api/attendance/manual")
def manual_attendance():
    payload = request.get_json(silent=True)
    student_id = payload.get("student_id") if isinstance(payload, dict) else None
    if not isinstance(student_id, int) or isinstance(student_id, bool):
        raise ApiError(400, "student_id (integer) is required")
    db = storage.get_db()
    row = db.execute("SELECT id, name FROM students WHERE id = ?", (student_id,)).fetchone()
    if row is None:
        raise ApiError(404, "Student not found")
    current = now()
    created = storage.mark_attendance(db, student_id, current.strftime("%Y-%m-%d"),
                                      current.strftime("%H:%M:%S"), "manual", None)
    return jsonify(ok=True, new=created, name=row["name"])


@bp.post("/api/recognize")
def recognize():
    engine = require_engine()
    upload = request.files.get("image")
    if upload is None:
        raise ApiError(400, "image file is required")
    img = decode_image(upload.read())
    height, width = img.shape[:2]
    faces = engine.detect(img)[:MAX_FACES_PER_FRAME]

    db = storage.get_db()
    matches = match_embeddings(engine, db, [f.embedding for f in faces])
    current = now()
    day, clock = current.strftime("%Y-%m-%d"), current.strftime("%H:%M:%S")
    results = []
    for face, (student_id, score) in zip(faces, matches):
        entry = {"box": normalised_box(face.box, width, height), "matched": False,
                 "new": False, "student": None, "score": round(score, 3)}
        if student_id is not None:
            student = db.execute("SELECT id, name, admission_no FROM students WHERE id = ?",
                                 (student_id,)).fetchone()
            if student is not None:  # may have been deleted since the match
                entry["matched"] = True
                entry["student"] = {"id": student["id"], "name": student["name"],
                                    "admission_no": student["admission_no"]}
                # INSERT OR IGNORE: only the first sighting of the day returns True,
                # even if the same student appears twice in one frame.
                entry["new"] = storage.mark_attendance(
                    db, student_id, day, clock, "face", round(score, 3))
        results.append(entry)
    return jsonify(faces=results)


@bp.get("/api/export.csv")
def export_csv():
    db = storage.get_db()
    day = parse_day(request.args.get("date"), now().strftime("%Y-%m-%d"))
    rows = db.execute(
        """SELECT s.admission_no, s.name, a.check_in, a.source
             FROM students s
             LEFT JOIN attendance a ON a.student_id = s.id AND a.day = ?
            ORDER BY s.admission_no COLLATE NOCASE""", (day,)).fetchall()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Admission No", "Name", "Date", "Status", "Check-In", "Source"])
    for r in rows:
        writer.writerow([csv_cell(r["admission_no"]), csv_cell(r["name"]), day,
                         "Present" if r["check_in"] else "Absent",
                         r["check_in"] or "", r["source"] or ""])
    body = "﻿" + buf.getvalue()  # BOM so Excel opens UTF-8 (Arabic) names correctly
    return Response(body, mimetype="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="attendance_{day}.csv"'})


@bp.get("/photos/<int:student_id>.jpg")
def photo(student_id: int):
    return send_from_directory(current_app.config["PHOTO_DIR"], f"{student_id}.jpg",
                               mimetype="image/jpeg", max_age=3600)


# --------------------------------------------------------------------------- factory
def create_app(test_config: dict | None = None, engine: FaceEngine | None = None) -> Flask:
    app = Flask(__name__, template_folder=str(BASE_DIR / "templates"),
                static_folder=str(BASE_DIR / "static"))
    data_dir = Path(os.environ.get("ATTENDANCE_DATA_DIR", BASE_DIR / "data"))
    app.config.update(
        DATA_DIR=data_dir,
        MODELS_DIR=Path(os.environ.get("ATTENDANCE_MODELS_DIR", BASE_DIR / "models")),
        FACE_ENGINE=os.environ.get("FACE_ENGINE", "auto"),
        ADMIN_PASSWORD=os.environ.get("ADMIN_PASSWORD") or None,
        MATCH_THRESHOLD=os.environ.get("MATCH_THRESHOLD") or None,
        MAX_CONTENT_LENGTH=12 * 1024 * 1024,
        MAX_IMAGE_SIDE=1280,
        MAX_IMAGE_PIXELS=40_000_000,
        NOW=datetime.now,
    )
    if test_config:
        app.config.update(test_config)
    data_dir = Path(app.config["DATA_DIR"])
    app.config["DB_PATH"] = Path(app.config.get("DB_PATH") or data_dir / "attendance.db")
    app.config["PHOTO_DIR"] = Path(app.config.get("PHOTO_DIR") or data_dir / "photos")
    app.config["PHOTO_DIR"].mkdir(parents=True, exist_ok=True)
    app.config["DB_PATH"].parent.mkdir(parents=True, exist_ok=True)
    storage.init_db(app.config["DB_PATH"])

    error = ""
    if engine is None:
        try:
            engine = load_engine(app.config["FACE_ENGINE"], Path(app.config["MODELS_DIR"]))
            log.info("Face engine ready: %s", engine.name)
        except EngineUnavailable as exc:
            error = str(exc)
            log.warning("Face engine unavailable: %s", error)
    override = app.config.get("MATCH_THRESHOLD")
    if engine is not None and override not in (None, ""):
        try:
            engine.threshold = float(override)
        except (TypeError, ValueError):
            raise ValueError(f"MATCH_THRESHOLD must be a number, got {override!r}") from None
        log.info("Match threshold overridden to %s", engine.threshold)
    app.extensions["face_engine"] = engine
    app.extensions["face_engine_error"] = error

    app.teardown_appcontext(storage.close_db)
    app.register_blueprint(bp)

    @app.before_request
    def guard():
        password = app.config["ADMIN_PASSWORD"]
        if password:
            auth = request.authorization
            supplied = (auth.password or "") if auth else ""
            if not auth or not hmac.compare_digest(supplied.encode(), password.encode()):
                return Response("Authentication required", 401,
                                {"WWW-Authenticate": 'Basic realm="Attendance"'})
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("Origin")
            if origin is not None and urlparse(origin).netloc != request.host:
                return jsonify(error="Cross-origin request blocked"), 403
        return None

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Permissions-Policy", "camera=(self), microphone=()")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
            "style-src 'self'; script-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if request.path.startswith("/api/"):
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @app.errorhandler(ApiError)
    def handle_api_error(exc: ApiError):
        return jsonify(error=exc.message), exc.status

    @app.errorhandler(HTTPException)
    def handle_http_error(exc: HTTPException):
        if request.path.startswith("/api/"):
            message = "Upload too large" if exc.code == 413 else (exc.description or exc.name)
            return jsonify(error=message), exc.code
        return exc

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception):
        app.logger.exception("Unhandled error")
        if request.path.startswith("/api/"):
            return jsonify(error="Internal server error"), 500
        return Response("Internal server error", 500)

    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    application = create_app()
    host = os.environ.get("HOST", "127.0.0.1")
    if host not in ("127.0.0.1", "localhost", "::1") and not application.config["ADMIN_PASSWORD"]:
        log.warning("Listening on %s without ADMIN_PASSWORD: anyone on the network can "
                    "read student data. Set ADMIN_PASSWORD and use HTTPS.", host)
    application.run(host=host, port=int(os.environ.get("PORT", "5000")), threaded=True)
