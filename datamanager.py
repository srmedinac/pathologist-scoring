"""Superadmin data-manager — upload / organize / assign, all from the web UI.

A self-contained Flask blueprint that drops into any detection-review deployment
(mitosis, tumorbuds, future studies). Register it with one line at the bottom of
app.py:

    import datamanager, sys
    datamanager.register(app, sys.modules[__name__])

…and add one is_admin-gated link to admin.html. No other shared file is touched,
so the module copies cleanly across siblings.

Capabilities (all gated on the app's is_admin()):
  • Assign   — edit `rater_cohorts` (rater -> [cohorts]); RESTART-FREE (save_config
               hot-swaps CFG and rater_items reads it live).
  • Organize — create / rename / delete / edit cohort DEFINITIONS (fnmatch globs).
               Never moves or renames slide folders on disk (stable_pid hashes the
               relative path, so a move would re-id patches and orphan results).
  • Upload   — one slide as a .zip (images + labels/), zip-slip guarded, into a NEW
               folder under patches_dir. Re-upload into an existing folder is
               rejected (a label change would re-map already-answered detections).
  • Rebuild  — in-process background manifest rebuild + atomic hot-swap of the live
               MANIFEST + ITEM_INDEX. HARD-GATED (409) until a deployment with
               positional-id results has been migrated to stable ids.

Design notes:
  • The running app is launched as `python app.py`, so its module is __main__, not
    `app`. register() captures the LIVE module reference (`_APP`); routes read
    `_APP.MANIFEST` / `_APP.CFG` as live attributes on every request so a hot-swap
    is immediately visible. Never `import app` (that would load a second copy).
  • The hot-swap itself lives in `_APP.apply_manifest` (rebinds both globals under
    `_APP._lock`); this module only calls into it.
"""

import copy
import os
import re
import shutil
import threading
import zipfile
from datetime import datetime, timezone

from flask import Blueprint, abort, jsonify, render_template, request

bp = Blueprint("datamanager", __name__,
               template_folder="templates_datamanager",
               static_folder="static_datamanager",
               static_url_path="/static/datamanager")

# ~95 MB — above a typical ~50 MB slide zip, but kept *under* the Cloudflare 100 MB
# cap (incl. multipart overhead) so the in-app 413 fires before CF rejects the body.
MAX_UPLOAD_BYTES = 95 * 1024 * 1024
_POSITIONAL = re.compile(r"^p[0-9]{4}$")          # old positional patch_id form
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,128}$")
_COHORT_NAME = re.compile(r"^[A-Za-z0-9_\-]+$")

_APP = None                                       # set by register() to the live module

# Background-rebuild status, polled by the UI. One rebuild at a time.
_REBUILD = {"state": "idle", "message": "", "started": "", "finished": "",
            "n_patches": None, "n_detections": None, "added": None, "removed": None}
_REBUILD_LOCK = threading.Lock()                  # guards starting a rebuild


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _require_admin():
    if not _APP.is_admin():
        abort(401)


# --------------------------------------------------------------------------
# helpers — slide folders on disk and how they map to cohorts
# --------------------------------------------------------------------------
def _patches_root():
    p = _APP.CFG.get("patches_dir", "")
    return p if os.path.isabs(p) else os.path.join(_APP.ROOT, p)


def _slide_folders():
    """Sorted top-level directory names under patches_dir (the slide folders /
    cohort folders that cohort_for keys off). Empty list if the dir is missing
    (e.g. an unmounted data drive) — the UI degrades gracefully."""
    root = _patches_root()
    if not os.path.isdir(root):
        return []
    out = []
    for name in os.listdir(root):
        if name.startswith(".") or name.startswith("._"):
            continue
        if os.path.isdir(os.path.join(root, name)):
            out.append(name)
    return sorted(out)


def _match_cohorts(cfg, folders=None):
    """Map each slide folder to its cohort using the SAME top-segment fnmatch
    rule as app.cohort_for, so the UI shows exactly what a rebuild will bake in.
    Returns {"by_cohort": {cohort: [folders]}, "unmatched": [folders]}."""
    if folders is None:
        folders = _slide_folders()
    by_cohort, unmatched = {c: [] for c in (cfg.get("cohorts") or {})}, []
    for f in folders:
        c = _APP.cohort_for(f, cfg)       # f is the top segment itself
        if c:
            by_cohort.setdefault(c, []).append(f)
        else:
            unmatched.append(f)
    return {"by_cohort": by_cohort, "unmatched": unmatched}


def _cohort_patch_counts():
    """{cohort: n_patches} over the current live manifest."""
    counts = {}
    for p in _APP.MANIFEST.get("patches", []):
        c = p.get("cohort", "") or "(unassigned)"
        counts[c] = counts.get(c, 0) + 1
    return counts


def _rater_progress():
    """{rater: answered_count} restricted to the kept set, for the Assign tab."""
    kept = {p["patch_id"] for p in _APP.MANIFEST.get("patches", [])
            if p["patch_id"] not in _APP.SKIPPED}
    out = {}
    for rater in _APP.CFG.get("raters", []):
        rows = _APP.RESULTS.get(rater, {})
        out[rater] = sum(1 for r in rows.values() if r.get("patch_id") in kept)
    return out


def _is_unmigrated():
    """True iff this deployment still has positional-id (p%04d) results/selection
    that have NOT been migrated to content-addressed stable ids. Rebuilding the
    manifest while this is true would emit stable ids and orphan every prior
    answer + the curation selection — so the rebuild route refuses (409) until a
    migration has run. A `_migration_complete` sentinel forces 'migrated'."""
    if os.path.exists(os.path.join(_APP.RESULTS_DIR, "_migration_complete")):
        return False
    for rows in _APP.RESULTS.values():
        for r in rows.values():
            if _POSITIONAL.match(r.get("patch_id", "") or ""):
                return True
    for pid in _APP.SKIPPED:
        if _POSITIONAL.match(pid or ""):
            return True
    return False


# --------------------------------------------------------------------------
# page
# --------------------------------------------------------------------------
@bp.route("/admin/data")
def data():
    _require_admin()
    cfg = _APP.CFG
    mode = cfg.get("detection_mode", "auto")
    return render_template(
        "data.html", cfg=cfg,
        patches_dir=_patches_root(),
        detection_mode=mode,
        upload_enabled=(mode != "paired_csv"),
        raters=cfg.get("raters", []),
        rater_cohorts=cfg.get("rater_cohorts") or {},
        cohorts=cfg.get("cohorts") or {},
        cohort_counts=_cohort_patch_counts(),
        match=_match_cohorts(cfg),
        progress=_rater_progress(),
        unmigrated=_is_unmigrated(),
        rebuild=dict((k, v) for k, v in _REBUILD.items() if not k.startswith("_")),
    )


# --------------------------------------------------------------------------
# ASSIGN — rater_cohorts (restart-free)
# --------------------------------------------------------------------------
@bp.route("/admin/data/assign", methods=["POST"])
def assign():
    _require_admin()
    data = request.get_json(force=True) or {}
    rater = (data.get("rater") or "").strip()
    cohorts = data.get("cohorts") or []
    if not isinstance(cohorts, list):
        return jsonify(error="cohorts must be a list"), 400
    with _APP._lock:
        fresh = _APP.load_config()
        if rater not in fresh.get("raters", []):
            return jsonify(error="unknown rater '%s'" % rater), 400
        defined = set(fresh.get("cohorts") or {})
        bad = [c for c in cohorts if c not in defined]
        if bad:
            return jsonify(error="unknown cohort(s): %s" % ", ".join(bad)), 400
        rc = fresh.setdefault("rater_cohorts", {})
        if cohorts:
            rc[rater] = cohorts          # only these cohorts
        else:
            rc.pop(rater, None)          # empty -> no entry -> sees ALL cohorts
        _APP.save_config(fresh)
    # restart-free: rater_items reads CFG live on the rater's next /api/session
    return jsonify(ok=True, rater_cohorts=_APP.CFG.get("rater_cohorts") or {})


# --------------------------------------------------------------------------
# ORGANIZE — cohort definitions (glob patterns only; never touches disk)
# --------------------------------------------------------------------------
@bp.route("/admin/data/cohort", methods=["POST"])
def cohort():
    _require_admin()
    data = request.get_json(force=True) or {}
    action = (data.get("action") or "").strip()
    name = (data.get("name") or "").strip()

    with _APP._lock:
        fresh = _APP.load_config()
        cohorts = fresh.setdefault("cohorts", {})
        rc = fresh.setdefault("rater_cohorts", {})

        if action in ("create", "edit"):
            patterns = data.get("patterns") or []
            if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
                return jsonify(error="patterns must be a list of strings"), 400
            patterns = [p.strip() for p in patterns if p.strip()]
            if not _COHORT_NAME.match(name):
                return jsonify(error="cohort name must be alphanumeric / _ / -"), 400
            if not patterns:
                return jsonify(error="give at least one glob pattern (e.g. um*)"), 400
            if action == "create" and name in cohorts:
                return jsonify(error="cohort '%s' already exists" % name), 400
            if action == "edit" and name not in cohorts:
                return jsonify(error="cohort '%s' does not exist" % name), 400
            cohorts[name] = patterns

        elif action == "rename":
            new_name = (data.get("new_name") or "").strip()
            if name not in cohorts:
                return jsonify(error="cohort '%s' does not exist" % name), 400
            if not _COHORT_NAME.match(new_name):
                return jsonify(error="new name must be alphanumeric / _ / -"), 400
            if new_name in cohorts:
                return jsonify(error="cohort '%s' already exists" % new_name), 400
            cohorts[new_name] = cohorts.pop(name)
            for r in list(rc):            # cascade the rename into assignments
                rc[r] = [new_name if c == name else c for c in rc[r]]

        elif action == "delete":
            if name not in cohorts:
                return jsonify(error="cohort '%s' does not exist" % name), 400
            cohorts.pop(name)
            for r in list(rc):            # strip dangling refs
                rc[r] = [c for c in rc[r] if c != name]
                if not rc[r]:
                    rc.pop(r)             # empty list -> sees all again

        else:
            return jsonify(error="unknown action '%s'" % action), 400

        _APP.save_config(fresh)

    return jsonify(ok=True,
                   cohorts=_APP.CFG.get("cohorts") or {},
                   rater_cohorts=_APP.CFG.get("rater_cohorts") or {},
                   match=_match_cohorts(_APP.CFG),
                   note="cohort membership updates on the next rebuild")


# --------------------------------------------------------------------------
# UPLOAD — one slide as a zip, into a NEW folder, zip-slip guarded
# --------------------------------------------------------------------------
def _extract_zip_guarded(zf, dest_root):
    """Extract a ZipFile into dest_root, rejecting any member that would escape
    it (absolute paths, '..', or symlinks). Returns the count of files written."""
    dest_root = os.path.realpath(dest_root)
    n = 0
    for info in zf.infolist():
        nm = info.filename
        if nm.endswith("/"):
            continue                                   # directory entry
        if nm.startswith("/") or nm.startswith("\\") or os.path.isabs(nm):
            raise ValueError("absolute path in zip: %r" % nm)
        parts = nm.replace("\\", "/").split("/")
        if any(p == ".." for p in parts):
            raise ValueError("parent traversal in zip: %r" % nm)
        if (info.external_attr >> 16) & 0o170000 == 0o120000:   # symlink member
            raise ValueError("symlink member in zip: %r" % nm)
        target = os.path.realpath(os.path.join(dest_root, nm))
        if target != dest_root and not target.startswith(dest_root + os.sep):
            raise ValueError("zip member escapes destination: %r" % nm)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
        n += 1
    return n


@bp.route("/admin/data/upload", methods=["POST"])
def upload():
    _require_admin()
    mode = _APP.CFG.get("detection_mode", "auto")
    if mode == "paired_csv":
        return jsonify(error="Upload is not supported in paired_csv mode "
                             "(candidates come from coords_root, not patches_dir). "
                             "Add paired_csv data out-of-band."), 400

    # scope the size limit to THIS route (no global MAX_CONTENT_LENGTH side-effect)
    if request.content_length and request.content_length > MAX_UPLOAD_BYTES:
        return jsonify(error="upload too large (limit %d MB)"
                             % (MAX_UPLOAD_BYTES // (1024 * 1024))), 413

    folder = (request.form.get("folder") or "").strip()
    if "/" in folder or "\\" in folder or not _SAFE_SEGMENT.match(folder):
        return jsonify(error="folder name must be a single safe segment "
                             "(letters, digits, space, _ . -)"), 400
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify(error="no file uploaded"), 400
    if not f.filename.lower().endswith(".zip"):
        return jsonify(error="upload a .zip of one slide (images + labels/)"), 400

    root = _patches_root()
    if not os.path.isdir(root):
        return jsonify(error="patches_dir does not exist on disk: %s" % root), 400
    dest = os.path.join(root, folder)
    if os.path.exists(dest):
        return jsonify(error="folder '%s' already exists — re-uploading into an "
                             "existing slide is not allowed (it would re-map "
                             "already-answered detections). Use a new name." % folder), 409

    tmp_zip = dest + ".upload.zip"
    try:
        f.save(tmp_zip)
        with zipfile.ZipFile(tmp_zip) as zf:
            if zf.testzip() is not None:
                return jsonify(error="corrupt zip"), 400
            os.makedirs(dest, exist_ok=True)
            n = _extract_zip_guarded(zf, dest)
    except zipfile.BadZipFile:
        _rmtree_quiet(dest)
        return jsonify(error="not a valid zip file"), 400
    except ValueError as e:                            # zip-slip guard tripped
        _rmtree_quiet(dest)
        return jsonify(error="rejected unsafe zip: %s" % e), 400
    finally:
        _unlink_quiet(tmp_zip)

    return jsonify(ok=True, folder=folder, n_files=n,
                   cohort=_APP.cohort_for(folder, _APP.CFG) or "(unmatched)",
                   note="run Rebuild to add these patches to the review set")


def _unlink_quiet(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _rmtree_quiet(path):
    try:
        shutil.rmtree(path)
    except OSError:
        pass


# --------------------------------------------------------------------------
# REBUILD — in-process background manifest rebuild + atomic hot-swap
# --------------------------------------------------------------------------
def _snapshot_manifest():
    if not os.path.exists(_APP.MANIFEST_PATH):
        return
    bak = _APP.MANIFEST_PATH + ".bak-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    try:
        shutil.copy2(_APP.MANIFEST_PATH, bak)
    except OSError:
        pass


def _run_rebuild_bg():
    try:
        prior = _APP.MANIFEST
        prior_ids = {p["patch_id"] for p in prior.get("patches", [])}

        root = _patches_root()
        if not os.path.isdir(root):
            raise RuntimeError("patches_dir does not exist: %s" % root)

        _snapshot_manifest()                           # recovery + diff baseline

        # build on a deep copy so build_manifest's in-place cfg mutation
        # (_patches_dir_abs) and any throw never leak into the live CFG
        new_cfg = copy.deepcopy(_APP.CFG)
        new_manifest = _APP.build_manifest(new_cfg)    # writes manifest.json atomically

        _APP.apply_manifest(new_manifest)              # swap MANIFEST+ITEM_INDEX under _lock

        new_ids = {p["patch_id"] for p in new_manifest.get("patches", [])}
        _REBUILD.update(state="done", finished=_now(),
                        message="rebuilt: %d patches (%d added, %d removed)" % (
                            new_manifest["n_patches"],
                            len(new_ids - prior_ids), len(prior_ids - new_ids)),
                        n_patches=new_manifest["n_patches"],
                        n_detections=new_manifest["n_detections"],
                        added=len(new_ids - prior_ids),
                        removed=len(prior_ids - new_ids))
    except BaseException as e:                          # incl. SystemExit from build_manifest
        _REBUILD.update(state="error", finished=_now(),
                        message="%s: %s" % (type(e).__name__, e))
    finally:
        _REBUILD["_running"] = False


@bp.route("/admin/data/rebuild", methods=["POST"])
def rebuild():
    _require_admin()
    if _is_unmigrated():
        return jsonify(error="This deployment still has positional-id results. "
                             "Run the stable-id migration before rebuilding, or "
                             "the rebuild would orphan every prior answer."), 409
    with _REBUILD_LOCK:
        if _REBUILD.get("_running"):
            return jsonify(error="a rebuild is already running"), 409
        _REBUILD.clear()
        _REBUILD.update(state="running", message="building manifest…",
                        started=_now(), finished="", _running=True,
                        n_patches=None, n_detections=None, added=None, removed=None)
    threading.Thread(target=_run_rebuild_bg, daemon=True,
                     name="datamanager-rebuild").start()
    return jsonify(ok=True, state="running"), 202


@bp.route("/admin/data/rebuild/status")
def rebuild_status():
    _require_admin()
    return jsonify({k: v for k, v in _REBUILD.items() if not k.startswith("_")})


# --------------------------------------------------------------------------
def register(flask_app, app_module):
    """Wire the blueprint onto the Flask app and capture the live app module.

    `app_module` must be the RUNNING module (pass sys.modules[__name__] from
    app.py) — not `import app`, which would load a second copy when app.py is
    executed as __main__."""
    global _APP
    _APP = app_module
    flask_app.register_blueprint(bp)
