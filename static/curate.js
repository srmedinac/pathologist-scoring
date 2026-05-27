(function () {
  "use strict";
  const grid = document.getElementById("grid");
  const stats = document.getElementById("stats");
  const cells = new Map();

  // hover-magnifier popup ------------------------------------------------
  const mag = document.createElement("div");
  mag.className = "magnifier";
  mag.innerHTML = '<img alt="" draggable="false"><div class="mag-cap"></div>';
  document.body.appendChild(mag);
  const magImg = mag.querySelector("img");
  const magCap = mag.querySelector(".mag-cap");
  let hoverPid = null;
  let hoverTimer = null;

  function showMag(cell, e) {
    const pid = cell.dataset.pid;
    if (hoverPid !== pid) {
      hoverPid = pid;
      const ndet = (cell.querySelector(".cell-ndet") || {}).textContent || "";
      magCap.innerHTML = `<b>${pid}</b><span>${ndet}</span>`;
      magImg.src = `/preview_img/${pid}?w=860`;
    }
    clearTimeout(hoverTimer);
    hoverTimer = setTimeout(() => {
      mag.classList.add("visible");
      positionMag(e);
    }, 180);
  }
  function hideMag() {
    clearTimeout(hoverTimer);
    hoverPid = null;
    mag.classList.remove("visible");
  }
  function positionMag(e) {
    if (!mag.classList.contains("visible")) return;
    const W = window.innerWidth, H = window.innerHeight;
    const mw = mag.offsetWidth || 620, mh = mag.offsetHeight || 620;
    const pad = 16;
    let x = e.clientX + pad;
    if (x + mw > W - pad) x = e.clientX - mw - pad;
    let y = e.clientY + pad;
    if (y + mh > H - pad) y = e.clientY - mh - pad;
    x = Math.max(pad, Math.min(W - mw - pad, x));
    y = Math.max(pad, Math.min(H - mh - pad, y));
    mag.style.left = x + "px";
    mag.style.top = y + "px";
  }
  window.addEventListener("scroll", hideMag, true);
  window.addEventListener("resize", hideMag);

  // cells ----------------------------------------------------------------
  grid.querySelectorAll(".curate-cell").forEach((c) => {
    cells.set(c.dataset.pid, c);
    c.addEventListener("click", () => toggle(c));
    c.addEventListener("mouseenter", (e) => showMag(c, e));
    c.addEventListener("mousemove", positionMag);
    c.addEventListener("mouseleave", hideMag);
  });

  function api(path, opts) {
    return fetch(path, opts || {})
      .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); });
  }

  function setStats(kept, total) {
    stats.innerHTML =
      `<b>${kept}</b> kept &nbsp;·&nbsp; ${total - kept} skipped &nbsp;·&nbsp; ${total} total &nbsp;·&nbsp; ` +
      `<span class="muted">target ≈ 200 — click a tile to skip / keep</span>`;
    stats.classList.toggle("at-target", kept >= 150 && kept <= 250);
  }

  function applyState(skippedSet) {
    cells.forEach((cell, pid) =>
      cell.classList.toggle("skipped", skippedSet.has(pid)));
  }

  function toggle(cell) {
    const pid = cell.dataset.pid;
    const willKeep = cell.classList.contains("skipped");   // currently skipped → keep
    cell.classList.toggle("skipped", !willKeep);
    api("/admin/curate/toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ patch_id: pid, keep: willKeep }),
    }).then((d) => setStats(d.kept, cells.size))
      .catch(() => {
        cell.classList.toggle("skipped");                  // revert
        alert("Couldn't save — check the connection.");
      });
  }

  function bulk(action) {
    api("/admin/curate/bulk", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    }).then((d) => {
      applyState(action === "skip_all" ? new Set([...cells.keys()]) : new Set());
      setStats(d.kept, cells.size);
    });
  }

  document.getElementById("bulk-keep").addEventListener("click", () => {
    if (confirm("Keep all? This clears every skip you've marked.")) bulk("keep_all");
  });
  document.getElementById("bulk-skip").addEventListener("click", () => {
    if (confirm("Skip all? Then click each tile you want to keep.")) bulk("skip_all");
  });

  api("/admin/curate/state").then((d) => {
    applyState(new Set(d.skipped));
    setStats(d.kept, d.total);
  });
})();
