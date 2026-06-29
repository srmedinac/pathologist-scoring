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

// ---- SETTINGS (all live; no restart) -------------------------------------
const DM_SET = JSON.parse(($("dm-settings-data") || { textContent: "{}" }).textContent || "{}");

function emailRow(val, isMe) {
  const row = document.createElement("div"); row.className = "dm-erow";
  const inp = document.createElement("input");
  inp.type = "email"; inp.value = val || ""; inp.placeholder = "name@example.org";
  if (isMe) inp.readOnly = true;
  row.appendChild(inp);
  if (isMe) {
    const y = document.createElement("span"); y.className = "dm-you"; y.textContent = "(you)";
    row.appendChild(y);
  } else {
    const b = document.createElement("button");
    b.type = "button"; b.className = "dm-erm"; b.textContent = "×"; b.title = "remove";
    b.onclick = () => row.remove();
    row.appendChild(b);
  }
  return row;
}
function renderEmails(id, emails, me) {
  const c = $(id); if (!c) return; c.innerHTML = "";
  (emails || []).forEach((e) => c.appendChild(emailRow(e, me && e.toLowerCase() === me.toLowerCase())));
  const add = document.createElement("button");
  add.type = "button"; add.className = "dm-eadd"; add.textContent = "+ add email";
  add.onclick = () => { const r = emailRow("", false); c.insertBefore(r, add); r.querySelector("input").focus(); };
  c.appendChild(add);
}
function collectEmails(id) {
  return Array.from($(id).querySelectorAll(".dm-erow input")).map((i) => i.value.trim()).filter(Boolean);
}
function siblingRow(s) {
  const row = document.createElement("div"); row.className = "dm-erow";
  const n = document.createElement("input");
  n.type = "text"; n.placeholder = "name (e.g. Tumor Buds)"; n.value = (s && s.name) || "";
  n.style.fontFamily = "var(--sans)"; n.style.flex = "0 0 34%";
  const u = document.createElement("input");
  u.type = "text"; u.placeholder = "https://…/admin"; u.value = (s && s.url) || "";
  const b = document.createElement("button");
  b.type = "button"; b.className = "dm-erm"; b.textContent = "×"; b.onclick = () => row.remove();
  row.append(n, u, b); return row;
}
function renderSiblings(list) {
  const c = $("set-siblings"); if (!c) return; c.innerHTML = "";
  (list || []).forEach((s) => c.appendChild(siblingRow(s)));
  const add = document.createElement("button");
  add.type = "button"; add.className = "dm-eadd"; add.textContent = "+ add study link";
  add.onclick = () => { const r = siblingRow(null); c.insertBefore(r, add); };
  c.appendChild(add);
}
function collectSiblings() {
  return Array.from($("set-siblings").querySelectorAll(".dm-erow")).map((r) => {
    const ins = r.querySelectorAll("input");
    return { name: ins[0].value.trim(), url: ins[1].value.trim() };
  }).filter((s) => s.url);
}

if ($("panel-settings")) {
  renderEmails("set-admins", DM_SET.admins, DM_SET.me);
  renderEmails("set-curators", DM_SET.curators, null);
  renderSiblings(DM_SET.siblings);

  document.querySelectorAll("#panel-settings .set-save").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const sec = btn.dataset.section;
      let body = {};
      if (sec === "text") {
        body = { study_title: $("set-title").value.trim(), instructions: $("set-instructions").value };
      } else if (sec === "labels") {
        const m = {};
        document.querySelectorAll("#set-labels .set-label").forEach((i) => { m[i.dataset.opt] = i.value.trim(); });
        body = { answer_labels: m };
      } else if (sec === "access") {
        body = { admin_emails: collectEmails("set-admins"), curator_emails: collectEmails("set-curators") };
      } else if (sec === "display") {
        body = { shuffle_per_rater: $("set-shuffle").checked, data_manager: $("set-datalink").checked,
                 sibling_studies: collectSiblings() };
      }
      btn.disabled = true;
      try {
        await api("/admin/data/settings", body);
        setMsg("Saved — applied live, no restart.", true);
        setTimeout(() => location.reload(), 900);     // refresh against current config
      } catch (e) {
        setMsg("Error: " + e.message, false);
        btn.disabled = false;
      }
    });
  });
}

// ---- COHORT MATCH PREVIEW (live, as you type) ----------------------------
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
async function fetchCohortPreview(name, patterns) {
  const qs = new URLSearchParams();
  if (name) qs.set("name", name);
  patterns.forEach((p) => qs.append("pat", p));
  const r = await fetch("/admin/data/cohort/preview?" + qs.toString());
  return r.ok ? r.json() : null;
}
function renderMatch(el, d) {
  if (!d) { el.textContent = ""; return; }
  el.textContent = (d.matched.join(", ") || "—")
    + "  ·  " + d.matched_count + " matched · " + d.unassigned + " of " + d.total + " folders unassigned";
}
const _previewSeq = new WeakMap();        // per-field latest-wins token
function wireCohortPreview(input, nameFn, target) {
  const run = debounce(async () => {
    const tok = (_previewSeq.get(input) || 0) + 1; _previewSeq.set(input, tok);
    const d = await fetchCohortPreview(nameFn(), parsePatterns(input.value));
    if (_previewSeq.get(input) === tok) renderMatch(target, d);   // drop stale responses
  }, 250);
  input.addEventListener("input", run);
}
document.querySelectorAll("#cohorts-table tr[data-cohort]").forEach((tr) => {
  const inp = tr.querySelector(".dm-patterns"), cell = tr.querySelector(".dm-folderlist");
  if (inp && cell) wireCohortPreview(inp, () => tr.dataset.cohort, cell);
});
(() => {
  const inp = $("new-cohort-patterns"), nameEl = $("new-cohort-name"), tgt = $("new-cohort-preview");
  if (inp && tgt) {
    wireCohortPreview(inp, () => (nameEl && nameEl.value.trim()) || "", tgt);
    if (nameEl) nameEl.addEventListener("input", () => inp.dispatchEvent(new Event("input")));
  }
})();

// ---- BULK ASSIGN (submit only the rows you changed) ----------------------
function rowCohorts(tr) {
  return Array.from(tr.querySelectorAll(".dm-cohort-cb")).filter((cb) => cb.checked)
    .map((cb) => cb.value).sort();
}
const _assignInitial = {};
document.querySelectorAll("#assign-table tr[data-rater]").forEach((tr) => {
  _assignInitial[tr.dataset.rater] = rowCohorts(tr).join("|");
});
const assignAllBtn = $("assign-save-all");
assignAllBtn && assignAllBtn.addEventListener("click", async () => {
  const assignments = {};
  document.querySelectorAll("#assign-table tr[data-rater]").forEach((tr) => {
    const cur = rowCohorts(tr);
    if (cur.join("|") !== _assignInitial[tr.dataset.rater]) assignments[tr.dataset.rater] = cur;
  });
  if (!Object.keys(assignments).length) { setMsg("No assignment changes to save.", true); return; }
  assignAllBtn.disabled = true;
  try {
    const d = await api("/admin/data/assign_bulk", { assignments });
    setMsg(`Saved assignments for ${d.n} rater(s).`, true);
    setTimeout(() => location.reload(), 800);
  } catch (e) { setMsg("Error saving assignments: " + e.message, false); assignAllBtn.disabled = false; }
});

// ---- APPLY DRY-RUN PREVIEW (what would Apply do) -------------------------
const previewBtn = $("preview-btn");
let previewTimer = null;
async function pollPreview() {
  try {
    const s = await (await fetch("/admin/data/preview/status")).json();
    const st = $("preview-status"); st.style.display = "block";
    st.textContent = s.state + (s.message ? " — " + s.message : "");
    if (s.state === "running") return;
    clearInterval(previewTimer); previewTimer = null;
    if (previewBtn) previewBtn.disabled = false;
    const box = $("preview-result"); box.style.display = "block";
    if (s.state === "done") {
      const danger = s.orphaned > 0;
      box.className = danger ? "dm-danger" : "dm-apply-hint";
      box.innerHTML = "Would <b>add " + s.added + "</b> and <b>remove " + s.removed + "</b> patches"
        + " (" + s.n_patches + " total after Apply). "
        + (danger
            ? "<b>⚠ " + s.orphaned + " already-answered patches would be DROPPED</b> — those answers would be orphaned. Review before applying."
            : "No already-answered patches would be dropped.");
    } else if (s.state === "error") {
      box.className = "dm-danger"; box.textContent = "Preview error: " + s.message;
    }
  } catch (_) { /* transient */ }
}
previewBtn && previewBtn.addEventListener("click", async () => {
  if (window.DM && window.DM.unmigrated) return;
  previewBtn.disabled = true;
  $("preview-result").style.display = "none";
  try {
    await api("/admin/data/preview", {});
    const st = $("preview-status"); st.style.display = "block"; st.textContent = "running — scanning…";
    if (!previewTimer) previewTimer = setInterval(pollPreview, 1500);
  } catch (e) { previewBtn.disabled = false; setMsg("Could not start preview: " + e.message, false); }
});
