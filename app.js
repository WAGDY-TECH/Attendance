(() => {
  "use strict";

  // ------------------------------------------------------------------ config
  const SCAN_INTERVAL_MS = 700;      // minimum gap between frames sent to the server
  const FRAME_MAX_WIDTH = 960;       // frames are downscaled before upload
  const REFRESH_MS = 10000;
  const MAX_PHOTOS = 5;
  const DONUT_CIRCUMFERENCE = 2 * Math.PI * 52;
  const SVG_NS = "http://www.w3.org/2000/svg";

  // ------------------------------------------------------------------ helpers
  const $ = (id) => document.getElementById(id);
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else node.setAttribute(key, value);
    }
    node.append(...children);
    return node;
  }

  async function api(path, options = {}) {
    const response = await fetch(path, { credentials: "same-origin", ...options });
    let data = null;
    if ((response.headers.get("content-type") || "").includes("application/json")) {
      try { data = await response.json(); } catch { data = null; }
    }
    if (!response.ok) {
      const error = new Error((data && data.error) || `Request failed (${response.status})`);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function fmtTime(value) {
    const match = /^(\d{2}):(\d{2})/.exec(value || "");
    if (!match) return "—";
    const hours = Number(match[1]);
    return `${String(hours % 12 || 12).padStart(2, "0")}:${match[2]} ${hours >= 12 ? "PM" : "AM"}`;
  }

  function fmtPercent(value) {
    return `${Number(value).toFixed(1).replace(/\.0$/, "")}%`;
  }

  function parseDay(day) {
    const [y, m, d] = day.split("-").map(Number);
    return new Date(y, m - 1, d);
  }

  function fmtDayLabel(day, today) {
    if (day === today) return "Today";
    return parseDay(day).toLocaleDateString(undefined, { day: "numeric", month: "short" });
  }

  function initials(name) {
    return name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => Array.from(w)[0]).join("").toUpperCase();
  }

  function avatar(student) {
    const wrap = el("span", { class: "avatar" });
    const img = el("img", { alt: "", loading: "lazy" });
    img.addEventListener("error", () => { img.remove(); wrap.textContent = initials(student.name); });
    img.src = `/photos/${student.id}.jpg`;
    wrap.append(img);
    return wrap;
  }

  function toast(message, kind = "info") {
    const box = $("toasts");
    while (box.children.length >= 4) box.firstChild.remove();
    const node = el("div", { class: `toast ${kind}`, text: message });
    box.append(node);
    setTimeout(() => node.remove(), kind === "error" ? 6000 : 3500);
  }

  function canvasToBlob(canvas, quality = 0.85) {
    return new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", quality));
  }

  // ------------------------------------------------------------------ state
  const state = {
    dashboard: null,
    engineReady: false,
    netError: null,
    scanning: false,
    starting: false,
    scanToken: 0,
    abort: null,
    stream: null,
    faces: [],
    clearTimer: null,
    refreshing: false,
    refreshQueued: false,
  };

  // ------------------------------------------------------------------ dashboard rendering
  function renderBanner(dashboard) {
    const banner = $("banner");
    let message = null;
    if (state.netError) message = `Cannot reach the server: ${state.netError}`;
    else if (dashboard && !dashboard.engine.ready) {
      message = `Face recognition is unavailable: ${dashboard.engine.error}`;
    }
    banner.hidden = !message;
    banner.textContent = message || "";
  }

  function renderEngine(engine) {
    const pill = $("enginePill");
    pill.className = `pill ${engine.ready ? "ok" : "err"}`;
    pill.textContent = engine.ready ? `Engine: ${engine.name}` : "Engine: unavailable";
    state.engineReady = engine.ready;
    $("scanBtn").disabled = state.starting || (!engine.ready && !state.scanning);
    $("enrollBtn").disabled = !engine.ready;
  }

  function renderStats(stats) {
    $("statTotal").textContent = stats.total;
    $("statPresent").textContent = stats.present;
    $("statAbsent").textContent = stats.absent;
    $("statRate").textContent = fmtPercent(stats.rate);
    $("legendPresent").textContent = stats.present;
    $("legendAbsent").textContent = stats.absent;
    $("donutText").textContent = fmtPercent(stats.rate);
    const share = stats.total ? stats.present / stats.total : 0;
    $("donutArc").setAttribute(
      "stroke-dasharray", `${(share * DONUT_CIRCUMFERENCE).toFixed(2)} ${DONUT_CIRCUMFERENCE.toFixed(2)}`);
  }

  function renderAttendance(rows) {
    const body = $("attendanceBody");
    body.replaceChildren();
    $("attendanceCount").textContent = rows.length ? `${rows.length} today` : "";
    if (!rows.length) {
      body.append(el("tr", {}, el("td", { class: "empty", colspan: "5", text: "No attendance recorded yet today." })));
      return;
    }
    for (const row of rows) {
      const status = el("span", { class: "badge", text: "Present" });
      if (row.source === "manual") status.title = "Marked manually";
      body.append(el("tr", {},
        el("td", {}, avatar(row)),
        el("td", { text: row.name }),
        el("td", { text: row.admission_no }),
        el("td", { text: fmtTime(row.check_in) }),
        el("td", {}, status)));
    }
  }

  function renderActivity(items, today) {
    const list = $("activityList");
    list.replaceChildren();
    if (!items.length) { list.append(el("li", { class: "empty-note", text: "No activity yet." })); return; }
    for (const item of items) {
      list.append(el("li", {},
        el("span", { class: "check", "aria-hidden": "true", text: "✓" }),
        el("div", {},
          el("div", {}, el("b", { text: item.name }), " checked in"),
          el("div", { class: "when", text: `${fmtDayLabel(item.day, today)} · ${fmtTime(item.check_in)}` }))));
    }
  }

  function renderBars(history, total) {
    const svg = $("bars");
    svg.replaceChildren();
    const top = 8, baseline = 80, height = baseline - top;
    const slot = 280 / history.length, barWidth = Math.min(28, slot - 10);
    const scale = Math.max(total, ...history.map((h) => h.present), 1);
    history.forEach((item, i) => {
      const barHeight = Math.max(item.present ? 2 : 0, (item.present / scale) * height);
      const x = i * slot + (slot - barWidth) / 2;
      const rect = document.createElementNS(SVG_NS, "rect");
      rect.setAttribute("x", x.toFixed(1));
      rect.setAttribute("y", (baseline - barHeight).toFixed(1));
      rect.setAttribute("width", barWidth.toFixed(1));
      rect.setAttribute("height", barHeight.toFixed(1));
      rect.setAttribute("rx", "3");
      const title = document.createElementNS(SVG_NS, "title");
      title.textContent = `${item.day}: ${item.present} present`;
      rect.append(title);
      const label = document.createElementNS(SVG_NS, "text");
      label.setAttribute("x", (i * slot + slot / 2).toFixed(1));
      label.setAttribute("y", "95");
      label.textContent = parseDay(item.day).toLocaleDateString(undefined, { weekday: "short" });
      svg.append(rect, label);
    });
  }

  function renderStudents(students) {
    const list = $("studentList");
    list.replaceChildren();
    $("studentCount").textContent = students.length ? `${students.length} enrolled` : "";
    if (!students.length) {
      list.append(el("li", { class: "empty-note", text: "No students yet. Click “Enroll student” to add one." }));
      return;
    }
    for (const s of students) {
      const badge = s.samples === 0
        ? el("span", { class: "badge warn", text: "Re-enroll", title: "No face data for the current engine" })
        : s.present ? el("span", { class: "badge", text: "Present" }) : el("span", { class: "badge absent", text: "Absent" });
      const actions = el("span", { class: "row-actions" });
      if (!s.present) {
        const mark = el("button", { class: "btn btn-sm", type: "button", text: "Mark present" });
        mark.addEventListener("click", () => markManual(s, mark));
        actions.append(mark, " ");
      }
      const del = el("button", { class: "btn btn-sm btn-icon btn-danger", type: "button",
        "aria-label": `Delete ${s.name}`, title: "Delete student", text: "✕" });
      del.addEventListener("click", () => deleteStudent(s, del));
      actions.append(del);
      list.append(el("li", {}, avatar(s),
        el("div", { class: "who" }, el("b", { text: s.name }), el("span", { text: s.admission_no })),
        badge, actions));
    }
  }

  function render(d) {
    $("todayLabel").textContent = parseDay(d.date).toLocaleDateString(
      undefined, { weekday: "long", year: "numeric", month: "long", day: "numeric" });
    const exportDate = $("exportDate");
    exportDate.max = d.date;
    if (!exportDate.value) exportDate.value = d.date;
    renderEngine(d.engine);
    renderStats(d.stats);
    renderAttendance(d.attendance);
    renderActivity(d.activity, d.date);
    renderBars(d.history, d.stats.total);
    renderStudents(d.students);
    renderBanner(d);
  }

  async function refresh() {
    if (state.refreshing) { state.refreshQueued = true; return; }
    state.refreshing = true;
    try {
      state.dashboard = await api("/api/dashboard");
      state.netError = null;
      render(state.dashboard);
    } catch (error) {
      state.netError = error.message;
      renderBanner(state.dashboard);
    } finally {
      state.refreshing = false;
      if (state.refreshQueued) { state.refreshQueued = false; refresh(); }
    }
  }

  // ------------------------------------------------------------------ student actions
  async function markManual(student, button) {
    button.disabled = true;
    try {
      await api("/api/attendance/manual", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ student_id: student.id }) });
      toast(`${student.name} marked present`, "success");
    } catch (error) {
      toast(error.message, "error");
    }
    refresh();
  }

  async function deleteStudent(student, button) {
    const ok = window.confirm(
      `Delete ${student.name} (${student.admission_no})?\n\nTheir face data, photo and attendance history will be permanently removed.`);
    if (!ok) return;
    button.disabled = true;
    try {
      await api(`/api/students/${student.id}`, { method: "DELETE" });
      toast(`${student.name} deleted`, "success");
    } catch (error) {
      toast(error.message, "error");
    }
    refresh();
  }

  // ------------------------------------------------------------------ live camera + recognition
  const video = $("video");
  const overlay = $("overlay");
  const overlayCtx = overlay.getContext("2d");

  function setStatus(text) { $("scanStatus").textContent = text; }

  function cameraErrorMessage(error) {
    if (!window.isSecureContext) return "Camera needs a secure origin: open http://localhost:5000 or use HTTPS.";
    switch (error && error.name) {
      case "NotAllowedError": return "Camera permission was denied.";
      case "NotFoundError": return "No camera was found on this device.";
      case "NotReadableError": return "The camera is being used by another application.";
      default: return `Could not start the camera (${(error && error.name) || "error"}).`;
    }
  }

  function resizeOverlay() {
    const rect = overlay.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    overlay.width = Math.max(1, Math.round(rect.width * dpr));
    overlay.height = Math.max(1, Math.round(rect.height * dpr));
    drawFaces();
  }

  // The video is letterboxed (object-fit: contain) inside a box whose aspect ratio is
  // set to the camera's, so the video area equals the canvas area and normalised
  // server coordinates map directly onto it.
  function drawFaces() {
    const dpr = window.devicePixelRatio || 1;
    const width = overlay.width / dpr, height = overlay.height / dpr;
    overlayCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
    overlayCtx.clearRect(0, 0, width, height);
    overlayCtx.lineWidth = 2;
    overlayCtx.font = "600 13px system-ui, sans-serif";
    overlayCtx.textBaseline = "middle";
    for (const face of state.faces) {
      const x = face.box.x * width, y = face.box.y * height;
      const w = face.box.w * width, h = face.box.h * height;
      const color = face.matched ? "#2ecc71" : "#ff9f1a";
      overlayCtx.strokeStyle = color;
      overlayCtx.strokeRect(x, y, w, h);
      const label = face.matched ? face.student.name : "Unknown";
      const textWidth = overlayCtx.measureText(label).width + 14;
      const labelY = y - 22 >= 0 ? y - 22 : y + h + 2;
      overlayCtx.fillStyle = color;
      overlayCtx.fillRect(x, labelY, textWidth, 20);
      overlayCtx.fillStyle = "#04120a";
      overlayCtx.fillText(label, x + 7, labelY + 10);
    }
  }

  function setFaces(faces) {
    state.faces = faces;
    drawFaces();
    clearTimeout(state.clearTimer);
    // Never leave stale boxes on screen if responses stop arriving.
    state.clearTimer = setTimeout(() => { state.faces = []; drawFaces(); }, 2500);
  }

  function setScanUi(mode) {
    const on = mode === "scanning";
    $("videoWrap").classList.toggle("on", on);
    $("liveDot").classList.toggle("on", on);
    const button = $("scanBtn");
    button.textContent = on ? "Stop scanning" : "Start scanning";
    button.disabled = mode === "starting" || (!on && !state.engineReady);
    if (mode === "idle") setStatus("Idle");
    if (mode === "starting") setStatus("Starting camera…");
  }

  async function startScanning() {
    if (state.scanning || state.starting) return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      toast("Camera is not available. Open http://localhost:5000 or use HTTPS.", "error");
      return;
    }
    state.starting = true;
    setScanUi("starting");
    try {
      state.stream = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: "user" }, audio: false });
      video.srcObject = state.stream;
      await video.play();
    } catch (error) {
      releaseCamera();
      state.starting = false;
      setScanUi("idle");
      toast(cameraErrorMessage(error), "error");
      return;
    }
    state.starting = false;
    if (video.videoWidth) $("videoWrap").style.aspectRatio = `${video.videoWidth} / ${video.videoHeight}`;
    state.scanning = true;
    const token = ++state.scanToken;
    setScanUi("scanning");
    setStatus("Scanning…");
    resizeOverlay();
    scanLoop(token);
  }

  function releaseCamera() {
    if (state.stream) state.stream.getTracks().forEach((track) => track.stop());
    state.stream = null;
    video.srcObject = null;
  }

  function stopScanning() {
    state.scanning = false;
    state.scanToken += 1;
    if (state.abort) state.abort.abort();
    state.abort = null;
    releaseCamera();
    clearTimeout(state.clearTimer);
    state.faces = [];
    drawFaces();
    setScanUi("idle");
  }

  async function captureFrame(canvas) {
    if (!video.videoWidth || !video.videoHeight) return null;
    const width = Math.min(video.videoWidth, FRAME_MAX_WIDTH);
    canvas.width = width;
    canvas.height = Math.round(video.videoHeight * width / video.videoWidth);
    canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
    return canvasToBlob(canvas, 0.8);
  }

  function handleRecognition(result) {
    setFaces(result.faces);
    const count = result.faces.length;
    setStatus(count ? `${count} face${count === 1 ? "" : "s"} detected` : "No faces in view");
    const fresh = result.faces.filter((face) => face.new);
    for (const face of fresh) toast(`${face.student.name} checked in`, "success");
    if (fresh.length) refresh();
  }

  async function scanLoop(token) {
    const grab = document.createElement("canvas");
    let failures = 0;
    while (state.scanning && token === state.scanToken) {
      const started = performance.now();
      try {
        const blob = await captureFrame(grab);
        if (!blob) { await sleep(300); continue; }
        const form = new FormData();
        form.append("image", blob, "frame.jpg");
        state.abort = new AbortController();
        const result = await api("/api/recognize", { method: "POST", body: form, signal: state.abort.signal });
        if (token !== state.scanToken) break;
        failures = 0;
        handleRecognition(result);
      } catch (error) {
        if (token !== state.scanToken || error.name === "AbortError") break;
        if (error.status === 503 || error.status === 401 || error.status === 403) {
          toast(error.message, "error");
          stopScanning();
          refresh();
          break;
        }
        failures += 1;
        setStatus("Connection problem, retrying…");
        await sleep(Math.min(5000, 1000 * failures));
        continue;
      }
      await sleep(Math.max(0, SCAN_INTERVAL_MS - (performance.now() - started)));
    }
  }

  // ------------------------------------------------------------------ enrolment dialog
  const dialog = $("enrollDialog");
  const enroll = { photos: [], stream: null, busy: false };

  function enrollError(message) {
    const node = $("enrollError");
    node.hidden = !message;
    node.textContent = message || "";
  }

  function renderThumbs() {
    const box = $("enrollThumbs");
    box.replaceChildren();
    enroll.photos.forEach((photo, index) => {
      const img = el("img", { alt: `Photo ${index + 1}` });
      img.src = photo.url;
      const remove = el("button", { type: "button", "aria-label": `Remove photo ${index + 1}`, text: "×" });
      remove.addEventListener("click", () => {
        URL.revokeObjectURL(photo.url);
        enroll.photos.splice(enroll.photos.indexOf(photo), 1);
        renderThumbs();
      });
      box.append(el("div", { class: "thumb" }, img, remove));
    });
  }

  // Re-encode as a bounded JPEG: keeps uploads small and applies EXIF rotation.
  async function normalisePhoto(source) {
    const bitmap = await createImageBitmap(source, { imageOrientation: "from-image" });
    try {
      const scale = Math.min(1, 1280 / Math.max(bitmap.width, bitmap.height));
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(bitmap.width * scale));
      canvas.height = Math.max(1, Math.round(bitmap.height * scale));
      canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      return await canvasToBlob(canvas, 0.9);
    } finally {
      bitmap.close();
    }
  }

  async function addPhoto(source) {
    if (enroll.photos.length >= MAX_PHOTOS) { enrollError(`You can add at most ${MAX_PHOTOS} photos.`); return; }
    try {
      const blob = await normalisePhoto(source);
      if (!blob) throw new Error("encode failed");
      if (enroll.photos.length >= MAX_PHOTOS) return;  // re-check after the await
      enroll.photos.push({ blob, url: URL.createObjectURL(blob) });
      enrollError(null);
      renderThumbs();
    } catch {
      enrollError("That file could not be read as an image (use JPEG, PNG or WebP).");
    }
  }

  function stopEnrollCamera() {
    if (enroll.stream) enroll.stream.getTracks().forEach((track) => track.stop());
    enroll.stream = null;
    $("enrollVideo").srcObject = null;
    $("enrollCamBox").hidden = true;
    $("enrollShotBtn").disabled = true;
    $("enrollCamBtn").textContent = "Use camera";
  }

  async function toggleEnrollCamera() {
    if (enroll.stream) { stopEnrollCamera(); return; }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      enrollError("Camera is not available here. Upload photos instead, or open http://localhost:5000.");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: "user" }, audio: false });
      if (!dialog.open) { stream.getTracks().forEach((t) => t.stop()); return; }  // closed while asking
      enroll.stream = stream;
      const preview = $("enrollVideo");
      preview.srcObject = stream;
      await preview.play();
      $("enrollCamBox").hidden = false;
      $("enrollShotBtn").disabled = false;
      $("enrollCamBtn").textContent = "Stop camera";
      enrollError(null);
    } catch (error) {
      stopEnrollCamera();
      enrollError(cameraErrorMessage(error));
    }
  }

  async function captureEnrollPhoto() {
    const preview = $("enrollVideo");
    if (!preview.videoWidth) return;
    const frame = await createImageBitmap(preview);
    try {
      await addPhoto(frame);
    } finally {
      frame.close();
    }
  }

  function openEnrollDialog() {
    if (state.scanning || state.starting) stopScanning();
    $("enrollForm").reset();
    enroll.photos.forEach((p) => URL.revokeObjectURL(p.url));
    enroll.photos = [];
    renderThumbs();
    enrollError(null);
    $("enrollSubmit").disabled = false;
    enroll.busy = false;
    dialog.showModal();
    $("enrollName").focus();
  }

  async function submitEnroll(event) {
    event.preventDefault();
    if (enroll.busy) return;
    const form = $("enrollForm");
    if (!form.checkValidity()) { form.reportValidity(); return; }
    if (!enroll.photos.length) { enrollError("Add at least one photo of the student."); return; }
    enroll.busy = true;
    $("enrollSubmit").disabled = true;
    enrollError(null);
    const body = new FormData();
    body.append("name", $("enrollName").value);
    body.append("admission_no", $("enrollAdm").value);
    enroll.photos.forEach((photo, i) => body.append("photos", photo.blob, `photo${i + 1}.jpg`));
    try {
      const created = await api("/api/students", { method: "POST", body });
      dialog.close();
      toast(`${created.name} enrolled`, "success");
      refresh();
    } catch (error) {
      enrollError(error.message);
    } finally {
      enroll.busy = false;
      $("enrollSubmit").disabled = false;
    }
  }

  dialog.addEventListener("close", () => {
    stopEnrollCamera();
    enroll.photos.forEach((p) => URL.revokeObjectURL(p.url));
    enroll.photos = [];
  });
  $("enrollCancel").addEventListener("click", () => dialog.close());
  $("enrollForm").addEventListener("submit", submitEnroll);
  $("enrollCamBtn").addEventListener("click", toggleEnrollCamera);
  $("enrollShotBtn").addEventListener("click", captureEnrollPhoto);
  $("enrollFiles").addEventListener("change", async (event) => {
    const files = Array.from(event.target.files || []);
    event.target.value = "";
    for (const file of files) await addPhoto(file);
  });

  // ------------------------------------------------------------------ wiring
  $("scanBtn").addEventListener("click", () => (state.scanning ? stopScanning() : startScanning()));
  $("enrollBtn").addEventListener("click", openEnrollDialog);
  $("exportBtn").addEventListener("click", () => {
    const day = $("exportDate").value;
    const link = el("a", { href: `/api/export.csv${day ? `?date=${encodeURIComponent(day)}` : ""}` });
    document.body.append(link);
    link.click();
    link.remove();
  });
  new ResizeObserver(resizeOverlay).observe($("videoWrap"));
  window.addEventListener("pagehide", () => { releaseCamera(); stopEnrollCamera(); });
  setInterval(() => { if (!document.hidden) refresh(); }, REFRESH_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  refresh();
})();
