import base64
import io
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import create_app  # noqa: E402
from engine import EngineUnavailable, load_engine  # noqa: E402
from fake_engine import (BLUE, CYAN, GREEN, MAGENTA, RED, YELLOW,  # noqa: E402
                         FakeEngine, make_image)


class Clock:
    def __init__(self, dt):
        self.dt = dt

    def __call__(self):
        return self.dt


@pytest.fixture
def clock():
    return Clock(datetime(2026, 11, 22, 8, 15, 0))


@pytest.fixture
def app(tmp_path, clock):
    return create_app({"DATA_DIR": tmp_path, "NOW": clock, "TESTING": True}, engine=FakeEngine())


@pytest.fixture
def client(app):
    return app.test_client()


def enroll(client, name, adm, *colours_per_photo):
    data = {"name": name, "admission_no": adm}
    data["photos"] = [(io.BytesIO(make_image(*c) if isinstance(c, tuple) else c), f"p{i}.jpg")
                      for i, c in enumerate(colours_per_photo)]
    return client.post("/api/students", data=data, content_type="multipart/form-data")


def recognize(client, image):
    return client.post("/api/recognize", data={"image": (io.BytesIO(image), "f.jpg")},
                       content_type="multipart/form-data")


def dash(client):
    return client.get("/api/dashboard").get_json()


# ------------------------------------------------------------------ basics
def test_empty_dashboard(client):
    d = dash(client)
    assert d["stats"] == {"total": 0, "present": 0, "absent": 0, "rate": 0.0}
    assert d["date"] == "2026-11-22" and d["engine"]["ready"] is True
    assert [h["present"] for h in d["history"]] == [0] * 7
    assert d["history"][-1]["day"] == "2026-11-22" and d["history"][0]["day"] == "2026-11-16"


def test_index_and_headers(client):
    r = client.get("/")
    assert r.status_code == 200 and b"Smart Attendance" in r.data
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "script-src 'self'" in r.headers["Content-Security-Policy"]
    assert client.get("/api/health").get_json() == {"status": "ok", "engine": "fake-colour"}


# ------------------------------------------------------------------ enrolment
def test_enroll_ok_and_photo(client):
    r = enroll(client, "  John   Mwangi ", "stu001", (RED,), (RED,))
    assert r.status_code == 201, r.get_json()
    body = r.get_json()
    assert body["name"] == "John Mwangi" and body["admission_no"] == "STU001"
    photo = client.get(f"/photos/{body['id']}.jpg")
    assert photo.status_code == 200 and photo.mimetype == "image/jpeg"
    assert photo.data[:2] == b"\xff\xd8"
    assert dash(client)["students"][0]["samples"] == 2


@pytest.mark.parametrize("name,adm", [("", "S1"), ("A", "S1"), ("x" * 81, "S1"),
                                      ("Ok Name", ""), ("Ok Name", "bad id"),
                                      ("Ok Name", "../etc"), ("Ok Name", "-lead"),
                                      ("Ok Name", "x" * 33)])
def test_enroll_validation(client, name, adm):
    r = enroll(client, name, adm, (RED,))
    assert r.status_code == 400
    assert dash(client)["stats"]["total"] == 0


def test_enroll_rejects_bad_photos(client):
    assert enroll(client, "No Face", "S1", (), ).status_code == 422          # black image
    assert enroll(client, "Two Faces", "S2", (RED, GREEN)).status_code == 422
    assert enroll(client, "Garbage", "S3", b"not an image").status_code == 400
    assert enroll(client, "Different", "S4", (RED,), (GREEN,)).status_code == 422
    r = client.post("/api/students", data={"name": "No Photos", "admission_no": "S5"},
                    content_type="multipart/form-data")
    assert r.status_code == 400
    assert enroll(client, "Too Many", "S6", *[(RED,)] * 6).status_code == 400
    assert dash(client)["stats"]["total"] == 0


def test_enroll_duplicates(client):
    assert enroll(client, "John", "S1", (RED,)).status_code == 201
    r = enroll(client, "Other", "s1", (GREEN,))               # same admission, case-insens.
    assert r.status_code == 409
    r = enroll(client, "Clone", "S2", (RED,))                 # same face, new admission
    assert r.status_code == 409 and "John" in r.get_json()["error"]
    assert dash(client)["stats"]["total"] == 1


def test_delete_student_cascades(client, app, tmp_path):
    sid = enroll(client, "John", "S1", (RED,)).get_json()["id"]
    recognize(client, make_image(RED))
    assert client.delete(f"/api/students/{sid}").status_code == 200
    assert client.delete(f"/api/students/{sid}").status_code == 404
    d = dash(client)
    assert d["stats"]["total"] == 0 and d["activity"] == []
    assert client.get(f"/photos/{sid}.jpg").status_code == 404
    # the face can be enrolled again afterwards
    assert enroll(client, "John Again", "S1", (RED,)).status_code == 201


# ------------------------------------------------------------------ recognition
def test_recognize_marks_once(client, clock):
    enroll(client, "John", "S1", (RED,))
    enroll(client, "Mary", "S2", (GREEN,))
    r = recognize(client, make_image(RED, BLUE))
    assert r.status_code == 200
    faces = r.get_json()["faces"]
    assert len(faces) == 2
    john, unknown = faces
    assert john["matched"] and john["new"] and john["student"]["name"] == "John"
    assert not unknown["matched"] and unknown["student"] is None and not unknown["new"]
    assert 0 <= john["box"]["x"] <= 1 and 0 < john["box"]["w"] <= 1

    again = recognize(client, make_image(RED)).get_json()["faces"][0]
    assert again["matched"] and again["new"] is False

    d = dash(client)
    assert d["stats"] == {"total": 2, "present": 1, "absent": 1, "rate": 50.0}
    assert [a["name"] for a in d["attendance"]] == ["John"]
    assert d["attendance"][0]["check_in"] == "08:15:00"
    assert d["history"][-1]["present"] == 1


def test_same_student_twice_in_frame_reports_new_once(client):
    enroll(client, "John", "S1", (RED,))
    faces = recognize(client, make_image(RED, RED)).get_json()["faces"]
    assert [f["new"] for f in faces] == [True, False]
    assert dash(client)["stats"]["present"] == 1


def test_recognize_no_faces_and_errors(client):
    assert recognize(client, make_image()).get_json() == {"faces": []}
    assert client.post("/api/recognize").status_code == 400
    assert recognize(client, b"junk").status_code == 400


def test_new_day_resets_presence(client, clock):
    enroll(client, "John", "S1", (RED,))
    recognize(client, make_image(RED))
    clock.dt = datetime(2026, 11, 23, 9, 0, 0)
    d = dash(client)
    assert d["stats"]["present"] == 0 and d["stats"]["absent"] == 1
    assert recognize(client, make_image(RED)).get_json()["faces"][0]["new"] is True
    d = dash(client)
    assert [h["present"] for h in d["history"]][-2:] == [1, 1]
    assert [a["day"] for a in d["activity"]] == ["2026-11-23", "2026-11-22"]


def test_rate_rounding(client):
    colours = [RED, GREEN, BLUE]
    for i, c in enumerate(colours):
        assert enroll(client, f"Student {i}", f"S{i}", (c,)).status_code == 201
    recognize(client, make_image(RED))
    assert dash(client)["stats"]["rate"] == 33.3


# ------------------------------------------------------------------ manual attendance
def test_manual_attendance(client):
    sid = enroll(client, "John", "S1", (RED,)).get_json()["id"]
    r = client.post("/api/attendance/manual", json={"student_id": sid})
    assert r.status_code == 200 and r.get_json()["new"] is True
    assert client.post("/api/attendance/manual", json={"student_id": sid}).get_json()["new"] is False
    assert dash(client)["attendance"][0]["source"] == "manual"
    assert client.post("/api/attendance/manual", json={"student_id": 999}).status_code == 404
    for bad in ({}, {"student_id": "1"}, {"student_id": True}, {"student_id": 1.5}, [1]):
        assert client.post("/api/attendance/manual", json=bad).status_code == 400
    assert client.post("/api/attendance/manual", data="nope").status_code == 400


# ------------------------------------------------------------------ export
def test_export_csv(client):
    enroll(client, "=HYPERLINK(\"x\")", "S1", (RED,))
    enroll(client, "Mary مريم", "S2", (GREEN,))
    recognize(client, make_image(GREEN))
    r = client.get("/api/export.csv")
    assert r.status_code == 200
    assert r.headers["Content-Disposition"] == 'attachment; filename="attendance_2026-11-22.csv"'
    text = r.data.decode("utf-8-sig")
    lines = text.strip().splitlines()
    assert lines[0] == "Admission No,Name,Date,Status,Check-In,Source"
    assert lines[1].startswith("S1,\"'=HYPERLINK")            # formula neutralised
    assert "S2,Mary مريم,2026-11-22,Present,08:15:00,face" in text
    assert client.get("/api/export.csv?date=2026-01-01").data.decode("utf-8-sig").count("Absent") == 2
    for bad in ("2026-13-45", "abc", "2026-1-1", "٢٠٢٦-٠١-٠١"):
        assert client.get("/api/export.csv", query_string={"date": bad}).status_code == 400


# ------------------------------------------------------------------ security & limits
def test_auth(tmp_path, clock):
    app = create_app({"DATA_DIR": tmp_path, "NOW": clock, "ADMIN_PASSWORD": "s3cret"},
                     engine=FakeEngine())
    c = app.test_client()
    assert c.get("/api/dashboard").status_code == 401
    assert c.get("/").headers.get("WWW-Authenticate", "").startswith("Basic")
    good = {"Authorization": "Basic " + base64.b64encode(b"admin:s3cret").decode()}
    bad = {"Authorization": "Basic " + base64.b64encode(b"admin:wrong").decode()}
    assert c.get("/api/dashboard", headers=bad).status_code == 401
    assert c.get("/api/dashboard", headers=good).status_code == 200


def test_cross_origin_post_blocked(client):
    sid = enroll(client, "John", "S1", (RED,)).get_json()["id"]
    evil = {"Origin": "https://evil.example"}
    assert client.post("/api/attendance/manual", json={"student_id": sid}, headers=evil).status_code == 403
    assert client.delete(f"/api/students/{sid}", headers=evil).status_code == 403
    same = {"Origin": "http://localhost"}
    assert client.post("/api/attendance/manual", json={"student_id": sid}, headers=same).status_code == 200


def test_upload_limits(tmp_path, clock):
    app = create_app({"DATA_DIR": tmp_path, "NOW": clock, "MAX_CONTENT_LENGTH": 50_000,
                      "MAX_IMAGE_PIXELS": 500_000}, engine=FakeEngine())
    c = app.test_client()
    big = b"\xff" * 60_000
    r = c.post("/api/recognize", data={"image": (io.BytesIO(big), "f.jpg")},
               content_type="multipart/form-data")
    assert r.status_code == 413 and r.get_json()["error"] == "Upload too large"
    huge = make_image(RED, size=(1000, 1000))                       # 1 MP > 0.5 MP limit
    assert len(huge) < 50_000
    r = c.post("/api/recognize", data={"image": (io.BytesIO(huge), "f.jpg")},
               content_type="multipart/form-data")
    assert r.status_code == 413


def test_large_image_is_downscaled(tmp_path, clock):
    app = create_app({"DATA_DIR": tmp_path, "NOW": clock, "MAX_IMAGE_SIDE": 400}, engine=FakeEngine())
    c = app.test_client()
    assert enroll(c, "John", "S1", (RED,)).status_code == 201       # 640px image -> 400px
    f = recognize(c, make_image(RED)).get_json()["faces"][0]
    assert f["matched"] and 0 < f["box"]["x"] < 1


def test_unknown_routes_and_methods(client):
    assert client.get("/api/nope").get_json() == {"error": "The requested URL was not found on the server. If you entered the URL manually please check your spelling and try again."}
    assert client.get("/api/recognize").status_code == 405
    assert client.get("/photos/abc.jpg").status_code == 404


# ------------------------------------------------------------------ engine availability
def test_degraded_mode_without_engine(tmp_path, clock, monkeypatch):
    monkeypatch.setenv("ATTENDANCE_MODELS_DIR", str(tmp_path / "nomodels"))
    app = create_app({"DATA_DIR": tmp_path, "NOW": clock, "FACE_ENGINE": "opencv"})
    c = app.test_client()
    d = dash(c)
    assert d["engine"]["ready"] is False and "download_models" in d["engine"]["error"]
    assert c.post("/api/recognize").status_code == 503
    assert enroll(c, "John", "S1", (RED,)).status_code == 503
    assert c.get("/api/health").get_json()["engine"] is None


def test_load_engine_errors(tmp_path):
    with pytest.raises(EngineUnavailable) as e:
        load_engine("auto", tmp_path)
    assert "opencv" in str(e.value) and "dlib" in str(e.value)
    with pytest.raises(ValueError):
        load_engine("bogus", tmp_path)
    (tmp_path / "face_detection_yunet_2023mar.onnx").write_text("<html>403</html>")
    (tmp_path / "face_recognition_sface_2021dec.onnx").write_text("<html>403</html>")
    with pytest.raises(EngineUnavailable):                           # tiny/HTML files rejected
        load_engine("opencv", tmp_path)


def test_threshold_override(tmp_path, clock):
    eng = FakeEngine()
    create_app({"DATA_DIR": tmp_path, "NOW": clock, "MATCH_THRESHOLD": "0.5"}, engine=eng)
    assert eng.threshold == 0.5
    with pytest.raises(ValueError):
        create_app({"DATA_DIR": tmp_path, "NOW": clock, "MATCH_THRESHOLD": "high"}, engine=FakeEngine())


def test_embeddings_are_per_engine(tmp_path, clock):
    app = create_app({"DATA_DIR": tmp_path, "NOW": clock}, engine=FakeEngine())
    c = app.test_client()
    enroll(c, "John", "S1", (RED,))
    other = FakeEngine()
    other.name = "other-engine"
    app2 = create_app({"DATA_DIR": tmp_path, "NOW": clock}, engine=other)
    c2 = app2.test_client()
    assert recognize(c2, make_image(RED)).get_json()["faces"][0]["matched"] is False
    assert dash(c2)["students"][0]["samples"] == 0                    # UI shows "re-enroll"
