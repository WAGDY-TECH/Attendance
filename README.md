# Smart Student Attendance

Face-recognition attendance system: live camera scanning, a student registry, and a dashboard
(stats, recent attendance, 7-day chart, activity feed, CSV export).
Stack: **Python (Flask) · OpenCV · SQLite · HTML/CSS/JS** (no front-end build step).

## Quick start

```bash
pip install -r requirements.txt
python scripts/download_models.py      # one-time: YuNet + SFace models (~40 MB) into ./models
python app.py                          # http://127.0.0.1:5000
```

Open **http://localhost:5000** (browsers only allow camera access on `localhost` or HTTPS).

1. **Enroll student** - name, admission number, 1-5 clear photos (camera or upload).
   More photos at slightly different angles improve accuracy.
2. **Start scanning** - the browser camera sends a frame every ~0.7 s; recognised students are
   marked present once per day. Boxes: green = recognised, orange = unknown.
3. **Mark present** (Students panel) is available when someone can't be recognised.
4. **Export CSV** for any date (UTF-8 with BOM, opens correctly in Excel, Arabic names included).

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `FACE_ENGINE` | `auto` | `opencv` (YuNet+SFace), `dlib` (`face_recognition`), or `auto` (tries opencv, then dlib) |
| `MATCH_THRESHOLD` | engine default | Override the match score. opencv: cosine similarity, default 0.363 (higher = stricter). dlib: `1 - distance`, default 0.5 |
| `ADMIN_PASSWORD` | unset | If set, the whole app requires HTTP Basic auth (any username) |
| `HOST` / `PORT` | `127.0.0.1` / `5000` | Bind address. A non-local `HOST` without `ADMIN_PASSWORD` logs a warning |
| `ATTENDANCE_DATA_DIR` | `./data` | SQLite DB + student thumbnails |
| `ATTENDANCE_MODELS_DIR` | `./models` | ONNX models |

Production: `waitress-serve --threads=4 --call app:create_app` behind an HTTPS reverse proxy
(keep the `Host` header intact - cross-origin POSTs are rejected by comparing `Origin` to `Host`).
Run a single process; SQLite is configured for concurrent threads (WAL).

## Design notes

- Recognition runs **server-side** on frames from the browser camera, so the same code works on a
  laptop, a classroom PC or a tablet, with no desktop window (`cv2.imshow`) involved.
- Embeddings are stored per engine; switching `FACE_ENGINE` requires re-enrolling (the Students
  panel flags such students with "Re-enroll").
- Attendance is unique per student per day (DB constraint), so repeated sightings are harmless.
- Deleting a student permanently removes their face data, photo and attendance history.
- Security: parameterised SQL, upload size/dimension limits, strict CSP (no inline scripts),
  Origin check on state-changing requests, CSV formula-injection neutralisation, output rendered
  with `textContent` only.

## Tests

```bash
pip install pytest
python -m pytest tests -q              # API/DB/logic, uses a deterministic fake engine
python tests/e2e_ui.py [out_dir]       # optional: full UI in headless Chrome (needs playwright)
```

## Limitations - please read

- **No liveness detection**: a photo of a student held up to the camera can be recognised.
  Supervise scanning or add an anti-spoofing step before relying on this for anything high-stakes.
- Accuracy depends on lighting, camera resolution and face size (faces under 32 px are ignored).
  Tune `MATCH_THRESHOLD` with real data from your classroom; raise it if you see wrong matches.
- This stores biometric data about (often minor) students. Get consent, restrict access
  (`ADMIN_PASSWORD` + HTTPS), and follow your school's / country's data-protection rules.
