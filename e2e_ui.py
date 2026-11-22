"""Optional end-to-end UI test (Chrome + Playwright, fake camera, fake engine).

    python tests/e2e_ui.py [screenshot_dir]

Starts the app with the deterministic FakeEngine, feeds Chrome a fake camera stream that
shows a RED and a BLUE "face", enrols the RED student through the UI, scans, and checks
that attendance is recorded and that no console errors / CSP violations occur.
"""
import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from playwright.sync_api import expect, sync_playwright  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

from app import create_app  # noqa: E402
from fake_engine import BLUE, GREEN, RED, FakeEngine, make_image  # noqa: E402

PORT = 5055
CHROME = os.environ.get("CHROME_PATH", "/usr/bin/google-chrome")


def main():
    shots = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp())
    shots.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp())

    # fake camera: MJPEG stream of one frame with a RED and a BLUE square
    frame = make_image(RED, BLUE)
    (work / "cam.mjpeg").write_bytes(frame * 60)
    (work / "red.jpg").write_bytes(make_image(RED))
    (work / "green.jpg").write_bytes(make_image(GREEN))
    (work / "two.jpg").write_bytes(make_image(RED, GREEN))

    app = create_app({"DATA_DIR": work / "data"}, engine=FakeEngine())
    server = make_server("127.0.0.1", PORT, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{PORT}/"

    problems = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROME, headless=True, args=[
            "--no-sandbox", "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
            f"--use-file-for-fake-video-capture={work / 'cam.mjpeg'}"])
        page = browser.new_context(viewport={"width": 1440, "height": 1000}).new_page()
        # The test deliberately triggers two 422 validation responses; the browser logs
        # those as console errors. Everything else (JS errors, CSP violations) must be clean.
        page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}")
                if m.type in ("error", "warning") and "status of 422" not in m.text else None)
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        page.on("dialog", lambda d: d.accept())

        page.goto(url)
        expect(page.locator("#statTotal")).to_have_text("0")
        expect(page.locator("#enginePill")).to_contain_text("fake-colour")
        expect(page.locator("#attendanceBody")).to_contain_text("No attendance recorded")
        page.screenshot(path=str(shots / "1_empty.png"))

        # --- enrolment: validation errors surface in the dialog
        page.click("#enrollBtn")
        page.click("#enrollSubmit")                                   # empty form -> native validation
        expect(page.locator("#enrollDialog")).to_be_visible()
        page.fill("#enrollName", "John Mwangi")
        page.fill("#enrollAdm", "STU001")
        page.click("#enrollSubmit")
        expect(page.locator("#enrollError")).to_contain_text("at least one photo")
        page.set_input_files("#enrollFiles", str(work / "two.jpg"))   # two faces -> server rejects
        expect(page.locator("#enrollThumbs .thumb")).to_have_count(1)
        page.click("#enrollSubmit")
        expect(page.locator("#enrollError")).to_contain_text("more than one face")
        page.click("#enrollThumbs .thumb button")                      # remove it
        expect(page.locator("#enrollThumbs .thumb")).to_have_count(0)
        page.set_input_files("#enrollFiles", str(work / "red.jpg"))
        expect(page.locator("#enrollThumbs .thumb")).to_have_count(1)
        page.screenshot(path=str(shots / "2_enroll_dialog.png"))
        page.click("#enrollSubmit")
        expect(page.locator("#enrollDialog")).not_to_be_visible()
        expect(page.locator("#statTotal")).to_have_text("1")
        expect(page.locator("#studentList")).to_contain_text("John Mwangi")
        expect(page.locator("#studentList .avatar img")).to_be_visible()

        # --- enrol via the in-dialog camera as well (fake stream shows RED+BLUE -> 2 faces)
        page.click("#enrollBtn")
        page.fill("#enrollName", "Cam User")
        page.fill("#enrollAdm", "STU002")
        page.click("#enrollCamBtn")
        expect(page.locator("#enrollShotBtn")).to_be_enabled()
        page.click("#enrollShotBtn")
        expect(page.locator("#enrollThumbs .thumb")).to_have_count(1)
        page.click("#enrollSubmit")
        expect(page.locator("#enrollError")).to_contain_text("more than one face")
        page.click("#enrollCancel")
        expect(page.locator("#enrollDialog")).not_to_be_visible()

        # --- live scan
        page.click("#scanBtn")
        expect(page.locator("#scanBtn")).to_have_text("Stop scanning")
        expect(page.locator("#toasts")).to_contain_text("John Mwangi checked in", timeout=10000)
        expect(page.locator("#statPresent")).to_have_text("1", timeout=10000)
        expect(page.locator("#statRate")).to_have_text("100%")
        expect(page.locator("#attendanceBody")).to_contain_text("STU001")
        expect(page.locator("#scanStatus")).to_contain_text("2 faces detected")
        px = page.evaluate("""() => { const c = document.getElementById('overlay');
            const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
            let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i]) n++; return n; }""")
        if px == 0:
            problems.append("overlay canvas is empty: no face boxes drawn")
        page.screenshot(path=str(shots / "3_scanning.png"))
        page.click("#scanBtn")
        expect(page.locator("#scanBtn")).to_have_text("Start scanning")
        expect(page.locator("#liveDot")).not_to_have_class("live-dot on")

        # --- manual mark + delete
        page.click("#enrollBtn")
        page.fill("#enrollName", "Mary Wanjiku")
        page.fill("#enrollAdm", "STU003")
        page.set_input_files("#enrollFiles", str(work / "green.jpg"))
        page.click("#enrollSubmit")
        expect(page.locator("#statTotal")).to_have_text("2")
        expect(page.locator("#statRate")).to_have_text("50%")
        page.locator("#studentList li", has_text="Mary Wanjiku").get_by_text("Mark present").click()
        expect(page.locator("#statRate")).to_have_text("100%")
        page.locator("#studentList li", has_text="Mary Wanjiku").get_by_label("Delete Mary Wanjiku").click()
        expect(page.locator("#statTotal")).to_have_text("1")

        # --- XSS safety: names are rendered as text
        page.click("#enrollBtn")
        page.fill("#enrollName", "<img src=x onerror=window.__xss=1>")
        page.fill("#enrollAdm", "XSS1")
        page.set_input_files("#enrollFiles", str(work / "green.jpg"))
        page.click("#enrollSubmit")
        expect(page.locator("#studentList")).to_contain_text("<img src=x")
        if page.evaluate("window.__xss === 1"):
            problems.append("XSS executed")
        page.screenshot(path=str(shots / "4_after.png"), full_page=True)

        # --- export link downloads a CSV
        with page.expect_download() as dl:
            page.click("#exportBtn")
        assert dl.value.suggested_filename.startswith("attendance_"), dl.value.suggested_filename

        # --- narrow viewport renders without horizontal overflow
        page.set_viewport_size({"width": 390, "height": 800})
        page.wait_for_timeout(300)
        overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
        if overflow:
            problems.append("horizontal overflow at 390px width")
        page.screenshot(path=str(shots / "5_mobile.png"), full_page=True)
        browser.close()
    server.shutdown()

    print("screenshots:", shots)
    if problems:
        print("PROBLEMS:")
        for p in problems:
            print(" -", p)
        sys.exit(1)
    print("E2E OK - no console errors, CSP violations or layout overflow")


if __name__ == "__main__":
    main()
