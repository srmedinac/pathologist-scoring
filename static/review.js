(function () {
  "use strict";
  const META = window.OPTION_META;
  const $ = (id) => document.getElementById(id);

  const S = { items: [], answers: {}, options: [], idx: 0 };
  let zoom = 3.0;
  let shownAt = 0;
  const BASE_W = Math.min(760, Math.max(420, window.innerWidth - 80));

  function api(url, opts) {
    return fetch(url, opts).then((r) => {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    });
  }

  // ---- init -------------------------------------------------------------
  api("/api/session").then((data) => {
    S.items = data.items;
    S.answers = data.answers || {};
    S.options = data.options;
    buildButtons();
    S.idx = firstUnanswered();
    render();
    maybeDone();
  }).catch(() => alert("Could not load the review session. Please reload."));

  function firstUnanswered() {
    for (let i = 0; i < S.items.length; i++) {
      if (!(S.items[i].item_id in S.answers)) return i;
    }
    return Math.max(0, S.items.length - 1);
  }

  function buildButtons() {
    const wrap = $("answers");
    wrap.innerHTML = "";
    S.options.forEach((opt) => {
      const m = META[opt] || { label: opt, cls: "", key: opt[0] };
      const b = document.createElement("button");
      b.className = "ans-btn " + m.cls;
      b.dataset.opt = opt;
      b.innerHTML = m.label + ' <span class="k">[' + m.key.toUpperCase() + "]</span>";
      b.addEventListener("click", () => answer(opt));
      wrap.appendChild(b);
    });
  }

  // ---- rendering --------------------------------------------------------
  function render() {
    if (!S.items.length) {
      $("caption").textContent = "No patches in the manifest.";
      return;
    }
    const it = S.items[S.idx];
    const img = $("patch-img");
    const src = "/img/" + it.patch_id;
    if (img.getAttribute("src") !== src) {
      img.onload = () => { positionHighlight(); centerActiveBox(); };
      img.src = src;
    }
    positionHighlight();
    applyZoom();
    centerActiveBox();

    $("caption").textContent = it.n_in_patch > 1
      ? "Detection " + (it.det_index + 1) + " of " + it.n_in_patch + " in this patch"
      : "Single detection in this patch";

    const done = Object.keys(S.answers).length;
    const total = S.items.length;
    $("progress-fill").style.width = (100 * done / total) + "%";
    $("progress-text").textContent =
      "Patch " + it.patch_pos + "/" + it.n_patches +
      "  ·  item " + (S.idx + 1) + "/" + total +
      "  ·  " + done + " answered";

    const chosen = S.answers[it.item_id];
    const wrap = $("answers");
    wrap.classList.toggle("answered", !!chosen);
    [...wrap.children].forEach((b) =>
      b.classList.toggle("chosen", b.dataset.opt === chosen));

    $("back-btn").disabled = S.idx === 0;
    $("next-btn").disabled = S.idx >= total - 1;
    shownAt = performance.now();
  }

  function positionHighlight() {
    const it = S.items[S.idx];
    const f = it.bbox_frac;
    const hl = $("highlight");
    const dims = document.querySelectorAll(".dim");
    if (!f) {
      hl.style.display = "none";
      dims.forEach((d) => (d.style.display = "none"));
      return;
    }
    const [x, y, w, h] = f;
    const pc = (v) => Math.max(0, v * 100) + "%";
    hl.style.display = "block";
    hl.style.left = pc(x); hl.style.top = pc(y);
    hl.style.width = pc(w); hl.style.height = pc(h);

    setBox(".dim-top", { left: 0, top: 0, width: "100%", height: pc(y) });
    setBox(".dim-bottom", { left: 0, top: pc(y + h), width: "100%", height: pc(1 - y - h) });
    setBox(".dim-left", { left: 0, top: pc(y), width: pc(x), height: pc(h) });
    setBox(".dim-right", { left: pc(x + w), top: pc(y), width: pc(1 - x - w), height: pc(h) });

    const show = $("dim-toggle").checked ? "block" : "none";
    dims.forEach((d) => (d.style.display = show));
  }

  function setBox(sel, st) { Object.assign(document.querySelector(sel).style, st); }

  function applyZoom() {
    $("stage").style.width = (BASE_W * zoom) + "px";
    $("zoom-label").textContent = zoom.toFixed(0) + "×";
  }

  function centerActiveBox() {
    const it = S.items[S.idx];
    if (!it || !it.bbox_frac) return;
    const stage = $("stage"), vp = $("viewport");
    const sw = stage.offsetWidth, sh = stage.offsetHeight;
    if (!sw || !sh) return;
    vp.scrollLeft = (it.bbox_frac[0] + it.bbox_frac[2] / 2) * sw - vp.clientWidth / 2;
    vp.scrollTop = (it.bbox_frac[1] + it.bbox_frac[3] / 2) * sh - vp.clientHeight / 2;
  }

  // ---- actions ----------------------------------------------------------
  function answer(opt) {
    if (!S.items.length) return;
    const it = S.items[S.idx];
    const t = Math.round(performance.now() - shownAt);
    S.answers[it.item_id] = opt;
    render();
    api("/api/answer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ item_id: it.item_id, label: opt, time_ms: t }),
    }).catch(() => alert("Could not save that answer — check your connection."));
    if (Object.keys(S.answers).length >= S.items.length) {
      setTimeout(maybeDone, 150);
    } else {
      setTimeout(() => go(1), 130);
    }
  }

  function go(d) {
    const n = S.idx + d;
    if (n >= 0 && n < S.items.length) { S.idx = n; render(); }
  }

  function maybeDone() {
    if (S.items.length && Object.keys(S.answers).length >= S.items.length) {
      $("done-text").textContent =
        "You reviewed all " + S.items.length + " detections.";
      $("done-overlay").classList.remove("hidden");
    }
  }

  // ---- events -----------------------------------------------------------
  $("back-btn").addEventListener("click", () => go(-1));
  $("next-btn").addEventListener("click", () => go(1));
  $("help-btn").addEventListener("click", () =>
    $("help-box").classList.toggle("hidden"));
  $("dim-toggle").addEventListener("change", positionHighlight);
  $("review-again").addEventListener("click", () => {
    $("done-overlay").classList.add("hidden");
    S.idx = 0;
    render();
  });
  function zoomBy(d) {
    zoom = Math.max(1, Math.min(8, zoom + d));
    applyZoom();
    centerActiveBox();
  }
  document.querySelectorAll(".zoom-btn").forEach((b) => {
    b.addEventListener("click", () => zoomBy(b.dataset.zoom === "in" ? 1 : -1));
  });
  document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT") return;
    const k = e.key.toLowerCase();
    if (k === "arrowleft") { go(-1); e.preventDefault(); return; }
    if (k === "arrowright") { go(1); e.preventDefault(); return; }
    if (k === "+" || k === "=") { zoomBy(1); e.preventDefault(); return; }
    if (k === "-" || k === "_") { zoomBy(-1); e.preventDefault(); return; }
    for (const opt of S.options) {
      const m = META[opt] || { key: opt[0] };
      if (k === m.key) { answer(opt); e.preventDefault(); return; }
    }
  });
})();
