// Live metrics dashboard: poll /admin/metrics/data every 5s and re-render.
// The selected GT rater persists locally and is sent back as ?gt=.

const POLL_MS = 5000;
const GT_KEY  = "mitosis_admin_gt";

const fmt3   = v => (v == null ? "—" : v.toFixed(3));
const fmtPct = v => (v == null ? "—" : (v * 100).toFixed(1) + "%");

const $ = id => document.getElementById(id);

function loadGT() {
  return localStorage.getItem(GT_KEY) || "";
}
function saveGT(v) {
  if (v) localStorage.setItem(GT_KEY, v);
  else   localStorage.removeItem(GT_KEY);
}

async function refresh() {
  const gt = $("gt-select").value || loadGT();
  const url = "/admin/metrics/data" + (gt ? "?gt=" + encodeURIComponent(gt) : "");
  try {
    const r = await fetch(url, {cache: "no-store"});
    if (!r.ok) throw new Error("HTTP " + r.status);
    render(await r.json());
  } catch (e) {
    $("updated").textContent = "fetch error: " + e.message;
  }
}

function render(d) {
  // ---- GT dropdown ----------------------------------------------------
  const sel = $("gt-select");
  const wanted = sel.value || loadGT();
  sel.innerHTML = '<option value="">— pick a rater —</option>';
  for (const r of d.active) {
    const o = document.createElement("option");
    o.value = r;
    o.textContent = r + (r === d.gt_rater ? "  (GT)" : "");
    if (r === wanted) o.selected = true;
    sel.appendChild(o);
  }

  // ---- Headline -------------------------------------------------------
  const h = d.headline;
  if (!h) {
    $("headline-box").innerHTML =
      '<span class="muted">Pick a ground-truth rater above to compute F1.</span>';
  } else {
    $("headline-box").innerHTML = `
      <div class="headline-grid">
        <div><div class="hl-label">GT</div><div class="hl-val">${h.rater}</div></div>
        <div><div class="hl-label">n (yes/no)</div><div class="hl-val">${h.n_yn}</div></div>
        <div><div class="hl-label">TP / FP</div><div class="hl-val">${h.tp} / ${h.fp}</div></div>
        <div><div class="hl-label">Precision</div><div class="hl-val">${fmtPct(h.precision)}</div></div>
        <div><div class="hl-label">F1</div><div class="hl-val hl-big">${fmt3(h.f1)}</div></div>
      </div>
      <div class="muted" style="margin-top:8px; font-size:12px">
        Recall = ${fmtPct(h.recall)} by construction (no model negatives in
        the candidate pool) — interpret precision, not F1, as the
        substantive number.
      </div>`;
  }

  // ---- Per-rater F1 ---------------------------------------------------
  const f1tb = $("f1-rows");
  f1tb.innerHTML = "";
  if (!d.f1.length) {
    f1tb.innerHTML = `<tr><td colspan="9" class="muted">No labels submitted yet.</td></tr>`;
  }
  for (const row of d.f1) {
    const tr = document.createElement("tr");
    if (row.is_gt) tr.className = "row-gt";
    tr.innerHTML = `
      <td>${row.rater}${row.is_gt ? ' <span class="badge">GT</span>' : ""}</td>
      <td>${row.n_yn}</td>
      <td>${row.tp}</td>
      <td>${row.fp}</td>
      <td>${row.fn}</td>
      <td>${fmtPct(row.precision)}</td>
      <td>${fmtPct(row.recall)}</td>
      <td><b>${fmt3(row.f1)}</b></td>
      <td>${row.unsure}</td>`;
    f1tb.appendChild(tr);
  }

  // ---- Pairwise kappa -------------------------------------------------
  const pt = $("pair-rows");
  pt.innerHTML = "";
  if (!d.pairs.length) {
    pt.innerHTML = `<tr><td colspan="4" class="muted">Need at least one rater with labels.</td></tr>`;
  }
  for (const p of d.pairs) {
    const tr = document.createElement("tr");
    if (p.b === "model") tr.className = "row-model";
    const nNote = (p.n_yn !== p.n_common) ? ` <span class="muted">(yn ${p.n_yn})</span>` : "";
    tr.innerHTML = `
      <td>${p.a} vs ${p.b}</td>
      <td>${fmt3(p.k_yn)} <span class="muted">${p.k_yn_label || ""}</span></td>
      <td>${fmt3(p.k_all)} <span class="muted">${p.k_all_label || ""}</span></td>
      <td>${p.n_common}${nNote}</td>`;
    pt.appendChild(tr);
  }

  // ---- Fleiss ---------------------------------------------------------
  const f = d.fleiss;
  if (!f || (f.n_all === 0 && f.n_yn === 0)) {
    $("fleiss-box").innerHTML = '<span class="muted">Not enough overlap across raters yet.</span>';
  } else {
    $("fleiss-box").innerHTML = `
      Across <b>{${f.raters.join(", ")}}</b> on fully-overlapping items:
      <b>κ<sub>yes/no</sub> = ${fmt3(f.k_yn)}</b> (n=${f.n_yn}),
      κ<sub>all cats</sub> = ${fmt3(f.k_all)} (n=${f.n_all}).`;
  }

  // ---- Distributions --------------------------------------------------
  const dt = $("dist-rows");
  dt.innerHTML = "";
  for (const r of d.distributions) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${r.rater}</td><td>${r.n}</td>
                    <td>${r.yes}</td><td>${r.no}</td><td>${r.unsure}</td>`;
    dt.appendChild(tr);
  }

  $("updated").textContent = "updated " + new Date().toLocaleTimeString();
}

$("gt-select").addEventListener("change", () => {
  saveGT($("gt-select").value);
  refresh();
});

refresh();
setInterval(refresh, POLL_MS);
