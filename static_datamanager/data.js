// Superadmin data-manager: assign raters to cohorts, organize cohort
// definitions, upload slides, rebuild the manifest. All server state lives in
// config.json + the manifest; this just drives the four tabs.

const $ = (id) => document.getElementById(id);

function setMsg(text, ok) {
  const el = $("msg");
  el.textContent = text;
  el.classList.toggle("ok", !!ok);
  el.classList.toggle("err", !ok);
  if (ok) setTimeout(() => { if (el.textContent === text) el.textContent = ""; }, 4000);
}

async function api(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  let data = {};
  try { data = await r.json(); } catch (_) {}
  if (!r.ok) throw new Error(data.error || ("HTTP " + r.status));
  return data;
}

// ---- tabs ----------------------------------------------------------------
document.querySelectorAll(".dm-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".dm-tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".dm-panel").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    $("panel-" + tab.dataset.panel).classList.add("active");
  });
});

// ---- ASSIGN --------------------------------------------------------------
document.querySelectorAll("#assign-table .assign-save").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const tr = btn.closest("tr");
    const rater = tr.dataset.rater;
    const cohorts = Array.from(tr.querySelectorAll(".dm-cohort-cb"))
      .filter((cb) => cb.checked).map((cb) => cb.value);
    btn.disabled = true;
    try {
      await api("/admin/data/assign", { rater, cohorts });
      setMsg(`${rater} → ` + (cohorts.length ? cohorts.join(", ") : "all cohorts"), true);
    } catch (e) {
      setMsg(`Error saving ${rater}: ${e.message}`, false);
    } finally {
      btn.disabled = false;
    }
  });
});

// ---- ORGANIZE ------------------------------------------------------------
function parsePatterns(s) {
  return s.split(",").map((p) => p.trim()).filter(Boolean);
}

document.querySelectorAll("#cohorts-table tr[data-cohort]").forEach((tr) => {
  const name = tr.dataset.cohort;
  const editBtn = tr.querySelector(".cohort-edit");
  const renameBtn = tr.querySelector(".cohort-rename");
  const deleteBtn = tr.querySelector(".cohort-delete");

  editBtn && editBtn.addEventListener("click", async () => {
    const patterns = parsePatterns(tr.querySelector(".dm-patterns").value);
    editBtn.disabled = true;
    try {
      await api("/admin/data/cohort", { action: "edit", name, patterns });
      setMsg(`Saved patterns for ${name}. Use "Apply changes" to update the review set.`, true);
      setTimeout(() => location.reload(), 800);
    } catch (e) {
      setMsg(`Error: ${e.message}`, false);
      editBtn.disabled = false;
    }
  });

  renameBtn && renameBtn.addEventListener("click", async () => {
    const new_name = prompt(`Rename cohort "${name}" to:`, name);
    if (!new_name || new_name === name) return;
    try {
      await api("/admin/data/cohort", { action: "rename", name, new_name });
      setMsg(`Renamed ${name} → ${new_name} (assignments updated).`, true);
      setTimeout(() => location.reload(), 800);
    } catch (e) {
      setMsg(`Error: ${e.message}`, false);
    }
  });

  deleteBtn && deleteBtn.addEventListener("click", async () => {
    if (!confirm(`Delete cohort "${name}"? Slides stay on disk; raters assigned `
                 + `only to it will revert to seeing all cohorts.`)) return;
    try {
      await api("/admin/data/cohort", { action: "delete", name });
      setMsg(`Deleted ${name}.`, true);
      setTimeout(() => location.reload(), 800);
    } catch (e) {
      setMsg(`Error: ${e.message}`, false);
    }
  });
});

const cohortAdd = $("cohort-add");
cohortAdd && cohortAdd.addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = $("new-cohort-name").value.trim();
  const patterns = parsePatterns($("new-cohort-patterns").value);
  const submit = e.submitter || e.target.querySelector("button");
  submit.disabled = true;
  try {
    await api("/admin/data/cohort", { action: "create", name, patterns });
    setMsg(`Added cohort ${name}.`, true);
    setTimeout(() => location.reload(), 800);
  } catch (err) {
    setMsg(`Error: ${err.message}`, false);
    submit.disabled = false;
  }
});

// ---- UPLOAD (a cohort, one slide per request) ----------------------------
const uploadForm = $("upload-form");
const CAP = 95 * 1024 * 1024;                     // per-slide cap (under CF's 100 MB)
const MARKERS = new Set(["labels", "clean", "images"]);

function relSegs(f) { return (f.webkitRelativePath || f.name).split("/"); }

// Group the picked files into slides. Returns { slideName: [{file, relpath}] }.
// Two layouts: a cohort folder (root/<slide>/labels/…) or a single slide folder
// (root/labels/… → the picked folder itself is the slide).
function groupSlides(files) {
  const single = files.some(f => { const s = relSegs(f); return s.length >= 2 && MARKERS.has(s[1].toLowerCase()); });
  const slides = {};
  for (const f of files) {
    const s = relSegs(f);
    let name, rel;
    if (single) { name = s[0]; rel = s.slice(1).join("/"); }
    else { if (s.length < 3) continue; name = s[1]; rel = s.slice(2).join("/"); }
    if (!rel) continue;
    (slides[name] = slides[name] || []).push({ file: f, relpath: rel });
  }
  return slides;
}

uploadForm && uploadForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const files = Array.from($("upload-dir").files || []);
  const dest = $("upload-dest").value.trim();
  if (!files.length) { setMsg("Pick a folder of slides", false); return; }

  const slides = groupSlides(files);
  const names = Object.keys(slides).sort();
  if (!names.length) {
    setMsg("No slides found — expected <slide>/labels/*.txt under the picked folder.", false);
    return;
  }

  const submit = e.submitter || e.target.querySelector("button");
  submit.disabled = true;
  const log = $("upload-log"); log.style.display = "block"; log.textContent = "";
  const line = (t) => { log.textContent += t + "\n"; log.scrollTop = log.scrollHeight; };
  const mb = (b) => (b / 1048576).toFixed(1);

  line(`Found ${names.length} slide(s)${dest ? " → under " + dest : ""}.`);
  let ok = 0, skip = 0, fail = 0;
  for (let i = 0; i < names.length; i++) {
    const name = names[i], grp = slides[name];
    const size = grp.reduce((a, b) => a + b.file.size, 0);
    const tag = `[${i + 1}/${names.length}] ${name}`;
    if (size > CAP) { line(`${tag}: SKIP — ${mb(size)} MB > 95 MB; copy to server directly`); skip++; continue; }
    const fd = new FormData();
    fd.append("slide", name);
    if (dest) fd.append("dest", dest);
    for (const it of grp) { fd.append("file", it.file); fd.append("relpath", it.relpath); }
    line(`${tag}: uploading ${grp.length} files (${mb(size)} MB)…`);
    try {
      const r = await fetch("/admin/data/upload", { method: "POST", body: fd });
      const d = await r.json().catch(() => ({}));
      if (r.ok) { ok++; line(`${tag}: ✓ done (cohort: ${d.cohort})`); }
      else { fail++; line(`${tag}: ✗ ${d.error || ("HTTP " + r.status)}`); }
    } catch (err) { fail++; line(`${tag}: ✗ ${err.message}`); }
  }
  line(`\nDone — ${ok} uploaded, ${skip} skipped, ${fail} failed. Now go to "Apply changes" to add them.`);
  setMsg(`Cohort upload: ${ok} uploaded, ${skip} skipped, ${fail} failed`, fail === 0);
  submit.disabled = false;
});

// ---- REBUILD -------------------------------------------------------------
const rebuildBtn = $("rebuild-btn");
let pollTimer = null;

function renderStatus(s) {
  let txt = s.state + (s.message ? " — " + s.message : "");
  $("rebuild-status").textContent = txt;
}

async function pollStatus() {
  try {
    const r = await fetch("/admin/data/rebuild/status");
    const s = await r.json();
    renderStatus(s);
    if (s.state === "running") return;          // keep polling
    clearInterval(pollTimer); pollTimer = null;
    rebuildBtn.disabled = false;
    if (s.state === "done") {
      setMsg(s.message, true);
      setTimeout(() => location.reload(), 1500);  // refresh counts/match
    } else if (s.state === "error") {
      setMsg("Apply changes failed: " + s.message, false);
    }
  } catch (_) { /* transient */ }
}

rebuildBtn && rebuildBtn.addEventListener("click", async () => {
  if (window.DM && window.DM.unmigrated) return;
  rebuildBtn.disabled = true;
  try {
    await api("/admin/data/rebuild", {});
    renderStatus({ state: "running", message: "building manifest…" });
    if (!pollTimer) pollTimer = setInterval(pollStatus, 1500);
  } catch (e) {
    rebuildBtn.disabled = false;
    setMsg("Could not apply changes: " + e.message, false);
  }
});

// if a rebuild was already running when the page loaded, resume polling
if (rebuildBtn && /running/.test($("rebuild-status").textContent)) {
  pollTimer = setInterval(pollStatus, 1500);
}
