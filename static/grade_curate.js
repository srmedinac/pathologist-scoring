/* Slide-curation page — same click-to-skip/keep semantics as the patch
 * curator (curate.js), grouped by case. */
(function () {
  "use strict";
  const stats = document.getElementById("stats");
  const cells = new Map();

  document.querySelectorAll(".slide-cell").forEach((c) => {
    cells.set(c.dataset.sid, c);
    c.addEventListener("click", () => toggle(c));
  });

  function api(path, opts) {
    return fetch(path, opts || {})
      .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); });
  }

  function setStats(kept, total) {
    stats.innerHTML =
      `<b>${kept}</b> kept &nbsp;·&nbsp; ${total - kept} skipped &nbsp;·&nbsp; ${total} total ` +
      `&nbsp;·&nbsp; <span class="muted">click a thumbnail to toggle</span>`;
  }

  function applyState(skippedSet) {
    cells.forEach((cell, sid) =>
      cell.classList.toggle("skipped", skippedSet.has(sid)));
  }

  function toggle(cell) {
    const sid = cell.dataset.sid;
    const willKeep = cell.classList.contains("skipped");
    cell.classList.toggle("skipped", !willKeep);
    api("/admin/grade/curate/toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slide_id: sid, keep: willKeep }),
    }).then((d) => setStats(d.kept, cells.size))
      .catch(() => {
        cell.classList.toggle("skipped");                  // revert
        alert("Couldn't save — check the connection.");
      });
  }

  function bulk(action) {
    return api("/admin/grade/curate/bulk", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    }).then((d) => {
      // re-fetch state for correctness (esp. keep_first_per_case which is
      // server-decided which slide stays in each case)
      return api("/admin/grade/curate/state").then((s) => {
        applyState(new Set(s.skipped));
        setStats(s.kept, s.total);
      });
    });
  }

  document.getElementById("bulk-keep").addEventListener("click", () => {
    if (confirm("Keep all slides? Clears every skip you've marked.")) bulk("keep_all");
  });
  document.getElementById("bulk-one").addEventListener("click", () => {
    if (confirm("Keep only the first slide of each case (e.g. _1.1)?\n" +
                "Subsequent slides will be skipped.")) bulk("keep_first_per_case");
  });
  document.getElementById("bulk-skip").addEventListener("click", () => {
    if (confirm("Skip all? Then click each thumbnail to opt in.")) bulk("skip_all");
  });

  api("/admin/grade/curate/state").then((d) => {
    applyState(new Set(d.skipped));
    setStats(d.kept, d.total);
  });
})();
