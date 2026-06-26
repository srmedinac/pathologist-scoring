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
      setMsg(`Saved patterns for ${name}. Rebuild to apply to the review set.`, true);
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

// ---- UPLOAD --------------------------------------------------------------
const uploadForm = $("upload-form");
uploadForm && uploadForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const folder = $("upload-folder").value.trim();
  const file = $("upload-file").files[0];
  if (!file) { setMsg("Choose a .zip file", false); return; }
  const submit = e.submitter || e.target.querySelector("button");
  submit.disabled = true;

  const fd = new FormData();
  fd.append("folder", folder);
  fd.append("file", file);

  const track = $("upload-track"), fill = $("upload-fill");
  track.style.display = "block"; fill.style.width = "0%";

  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/admin/data/upload");
  xhr.upload.onprogress = (ev) => {
    if (ev.lengthComputable) fill.style.width = (100 * ev.loaded / ev.total).toFixed(0) + "%";
  };
  xhr.onload = () => {
    submit.disabled = false;
    let data = {};
    try { data = JSON.parse(xhr.responseText); } catch (_) {}
    if (xhr.status >= 200 && xhr.status < 300) {
      setMsg(`Uploaded ${folder} (${data.n_files} files, cohort: ${data.cohort}). `
             + `Switch to Rebuild to add it.`, true);
      uploadForm.reset();
    } else {
      setMsg(`Upload failed: ${data.error || ("HTTP " + xhr.status)}`, false);
    }
    setTimeout(() => { track.style.display = "none"; }, 1500);
  };
  xhr.onerror = () => { submit.disabled = false; setMsg("Upload failed (network).", false); };
  xhr.send(fd);
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
      setMsg("Rebuild error: " + s.message, false);
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
    setMsg("Could not start rebuild: " + e.message, false);
  }
});

// if a rebuild was already running when the page loaded, resume polling
if (rebuildBtn && /running/.test($("rebuild-status").textContent)) {
  pollTimer = setInterval(pollStatus, 1500);
}
