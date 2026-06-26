/* Slide-grading front-end: OpenSeadragon viewer + same answer/progress flow
 * as review.js. Each slide is one item; the grade is one of HIGH / LOW /
 * UNSURE; answers persist server-side as soon as a button is clicked. */
(() => {
  "use strict";
  const META = window.GRADE_META;

  const $ = (id) => document.getElementById(id);
  const S = { items: [], answers: {}, options: [], idx: 0, t0: 0 };
  let viewer = null;

  // ---- fetch helpers ------------------------------------------------------
  async function api(url, body) {
    const opts = { headers: { "Content-Type": "application/json" } };
    if (body !== undefined) { opts.method = "POST"; opts.body = JSON.stringify(body); }
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error(url + " → " + r.status);
    return r.json();
  }

  // ---- init ---------------------------------------------------------------
  async function init() {
    const data = await api("/api/grade/session");
    S.items = data.items || [];
    S.answers = data.answers || {};
    S.options = data.options || [];
    if (!S.items.length) {
      $("empty-overlay").classList.remove("hidden");
      $("progress-text").textContent = "0 slides";
      return;
    }
    initViewer();
    buildButtons();
    S.idx = firstUngraded();
    render();
    bindKeys();
  }

  function firstUngraded() {
    for (let i = 0; i < S.items.length; i++) {
      if (!(S.items[i].slide_id in S.answers)) return i;
    }
    return 0;
  }

  // ---- OpenSeadragon ------------------------------------------------------
  function initViewer() {
    viewer = OpenSeadragon({
      id: "osd",
      prefixUrl: "/static/openseadragon/images/",
      showNavigator: true,
      navigatorPosition: "TOP_RIGHT",
      navigatorSizeRatio: 0.14,
      imageLoaderLimit: 12,        // more parallel CIFS tile fetches
      timeout: 60000,
      maxZoomPixelRatio: 2.5,
      zoomPerScroll: 1.35,
      gestureSettingsMouse: { clickToZoom: false, dblClickToZoom: true },
      animationTime: 0.5,
      blendTime: 0.1,
      showRotationControl: true,
      preserveViewport: false,
    });
    viewer.scalebar({
      type: OpenSeadragon.ScalebarType.MICROSCOPY,
      pixelsPerMeter: 0,
      location: OpenSeadragon.ScalebarLocation.BOTTOM_RIGHT,
      xOffset: 12, yOffset: 38,
      stayInsideImage: false,
      color: "#fff", fontColor: "#fff",
      backgroundColor: "rgba(0,0,0,.55)",
      barThickness: 3,
    });
  }

  function buildTileSource(meta) {
    return {
      width: meta.width,
      height: meta.height,
      tileSize: meta.tile_size,
      tileOverlap: meta.tile_overlap,
      minLevel: 0,
      maxLevel: meta.levels - 1,
      getTileUrl: (level, x, y) =>
        `/grade/tile/${encodeURIComponent(meta.slide_id)}/${level}/${x}/${y}.jpg`,
    };
  }

  async function openCurrentSlide() {
    const it = S.items[S.idx];
    $("slide-name").textContent = it.name;
    $("grade-loading").hidden = false;

    // Placeholder: drop the cached thumbnail behind the OSD canvas so the
    // user sees the slide immediately while real tiles stream in.
    const osd = $("osd");
    osd.style.backgroundImage =
      `url("/grade/thumb/${encodeURIComponent(it.slide_id)}.jpg?size=1024")`;

    try {
      const meta = await api(`/api/grade/slide/${encodeURIComponent(it.slide_id)}`);
      const bits = [];
      if (meta.objective) bits.push(`${meta.objective}×`);
      if (meta.mpp_x) bits.push(`${meta.mpp_x.toFixed(3)} µm/px`);
      bits.push(`${meta.width.toLocaleString()}×${meta.height.toLocaleString()}`);
      if (meta.vendor) bits.push(meta.vendor);
      $("slide-meta").textContent = bits.join("  ·  ");

      const opened = new Promise((res) => viewer.addOnceHandler("open", res));
      viewer.open(buildTileSource(meta));
      await opened;
      const mpp = meta.mpp_x || meta.mpp_y;
      viewer.scalebar({ pixelsPerMeter: mpp ? 1e6 / mpp : 0 });
    } catch (e) {
      console.error(e);
      $("slide-meta").textContent = "(could not load this slide)";
    } finally {
      $("grade-loading").hidden = true;
      S.t0 = performance.now();
    }
    prefetchNext();
  }

  // Warm the next slide's handle + thumbnail in the background, so when the
  // rater hits → the meta + first tiles are already cached. Fire and forget.
  let lastPrefetch = null;
  function prefetchNext() {
    if (S.items.length < 2) return;
    const next = S.items[(S.idx + 1) % S.items.length];
    if (next.slide_id === lastPrefetch) return;
    lastPrefetch = next.slide_id;
    // hitting /api/grade/slide forces openslide_open server-side (HandleCache hit on Next click)
    fetch(`/api/grade/slide/${encodeURIComponent(next.slide_id)}`, { keepalive: true })
      .catch(() => {});
    // and prewarm the placeholder image
    const im = new Image();
    im.src = `/grade/thumb/${encodeURIComponent(next.slide_id)}.jpg?size=1024`;
  }

  // ---- answer buttons -----------------------------------------------------
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

  function highlightChosen() {
    const it = S.items[S.idx];
    const chosen = S.answers[it.slide_id];
    const wrap = $("answers");
    wrap.classList.toggle("answered", !!chosen);
    [...wrap.querySelectorAll(".ans-btn")].forEach((b) => {
      b.classList.toggle("chosen", b.dataset.opt === chosen);
    });
  }

  // ---- render -------------------------------------------------------------
  function render() {
    const total = S.items.length;
    const done = Object.keys(S.answers).length;
    const it = S.items[S.idx];
    $("progress-text").textContent =
      `slide ${S.idx + 1} / ${total}  ·  ${done} graded`;
    $("progress-fill").style.width = (100 * done / total).toFixed(1) + "%";
    highlightChosen();
    openCurrentSlide();
  }

  function answer(opt) {
    const it = S.items[S.idx];
    const dt = Math.round(performance.now() - (S.t0 || performance.now()));
    S.answers[it.slide_id] = opt;
    highlightChosen();
    api("/api/grade/answer", {
      slide_id: it.slide_id, label: opt, time_ms: dt,
    }).catch(() => alert("Could not save that grade — check your connection."));
    if (Object.keys(S.answers).length >= S.items.length) {
      // all graded — but auto-advance if there's still a next unanswered
      const nxt = firstUngraded();
      if (S.answers[S.items[nxt].slide_id]) {
        showDone();
        return;
      }
      S.idx = nxt;
    } else {
      // jump to the next ungraded slide (wrapping)
      const start = S.idx;
      for (let k = 1; k <= S.items.length; k++) {
        const i = (start + k) % S.items.length;
        if (!(S.items[i].slide_id in S.answers)) { S.idx = i; break; }
      }
    }
    render();
  }

  function showDone() {
    const overlay = $("done-overlay");
    overlay.classList.remove("hidden");
    $("done-text").textContent =
      `${S.items.length} slides graded.`;
    $("review-again").onclick = () => {
      overlay.classList.add("hidden");
      S.idx = 0;
      render();
    };
  }

  function go(d) {
    const n = S.items.length;
    S.idx = ((S.idx + d) % n + n) % n;
    render();
  }

  // ---- keys ---------------------------------------------------------------
  function bindKeys() {
    document.addEventListener("keydown", (e) => {
      if (e.target.tagName === "INPUT") return;
      const k = e.key.toLowerCase();
      if (k === "arrowleft")  { go(-1); e.preventDefault(); return; }
      if (k === "arrowright") { go(1);  e.preventDefault(); return; }
      for (const opt of S.options) {
        const m = META[opt] || { key: opt[0] };
        if (k === m.key) { answer(opt); e.preventDefault(); return; }
      }
    });
    $("back-btn").addEventListener("click", () => go(-1));
    $("next-btn").addEventListener("click", () => go(1));
    if (Object.keys(S.answers).length >= S.items.length && S.items.length) {
      showDone();
    }
  }

  init().catch((e) => {
    console.error(e);
    $("progress-text").textContent = "error loading session";
  });
})();
