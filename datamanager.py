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
import json
import os
import re
import shutil
import threading
from datetime import datetime, timezone

from flask import Blueprint, abort, jsonify, render_template, request

bp = Blueprint("datamanager", __name__,
               template_folder="templates_datamanager",
               static_folder="static_datamanager",
               static_url_path="/static/datamanager")

# Per-SLIDE request cap. A cohort is uploaded one slide per request; each must
# stay under Cloudflare's 100 MB free-plan body limit. 95 MB leaves headroom for
# multipart overhead so the in-app 413 fires before CF rejects the body.
MAX_UPLOAD_BYTES = 95 * 1024 * 1024
_POSITIONAL = re.compile(r"^p[0-9]{4}$")          # old positional patch_id form
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,128}$")
_COHORT_NAME = re.compile(r"^[A-Za-z0-9_\-]+$")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_APP = None                                       # set by register() to the live module

# Background-rebuild status, polled by the UI. One rebuild at a time.
_REBUILD = {"state": "idle", "message": "", "started": "", "finished": "",
            "n_patches": None, "n_detections": None, "added": None, "removed": None}
_REBUILD_LOCK = threading.Lock()                  # guards starting a rebuild

# Background dry-run (Apply preview) status — computes the would-be manifest
# WITHOUT writing manifest.json or swapping the live MANIFEST/ITEM_INDEX.
_PREVIEW = {"state": "idle", "message": "", "started": "", "finished": "",
            "added": None, "removed": None, "orphaned": None, "n_patches": None,
            "n_detections": None, "orphaned_ids": []}
_PREVIEW_LOCK = threading.Lock()


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
        me=_APP.cf_access_email(),
        answer_options=cfg.get("answer_options", []),
        answer_labels=cfg.get("answer_labels") or {},
        admin_emails=cfg.get("admin_emails", []),
        curator_emails=cfg.get("curator_emails", []),
        sibling_studies=cfg.get("sibling_studies", []),
        advanced=_advanced_config(cfg),
    )


# Read-only "Advanced" view: build-time / locked keys, shown but NOT editable
# here (they re-scan data or need a restart). ORDERED allowlist of (key, note);
# iterating this — never cfg.items() — is what stops other keys leaking in.
_REBUILD_NOTE = "Edit config.json, restart the app, then Apply changes."
_RESTART_NOTE = "Edit config.json, then restart the app."
_ADVANCED_KEYS = [
    ("patches_dir", _REBUILD_NOTE), ("n_patches", _REBUILD_NOTE),
    ("seed", "Sampling seed — held fixed for study integrity. " + _RESTART_NOTE),
    ("detection_mode", _REBUILD_NOTE), ("coords_csv", _REBUILD_NOTE),
    ("coords_root", _REBUILD_NOTE), ("tiles_subdir", _REBUILD_NOTE),
    ("labels_subdir", _REBUILD_NOTE), ("image_extensions", _REBUILD_NOTE),
    ("green", _REBUILD_NOTE),
    ("slides_dir", "Slide folder for /grade. " + _RESTART_NOTE + " Slide list is built via the build_slides CLI."),
    ("secret_key", "Session-signing key. " + _RESTART_NOTE),
    ("port", "Listening port. " + _RESTART_NOTE),
]


def _advanced_config(cfg):
    """Build the read-only Advanced rows from the allowlist. secret_key is
    redacted FIRST (its value never enters the result); dict/list values are
    JSON-rendered so they're valid to copy back into config.json."""
    rows = []
    for key, note in _ADVANCED_KEYS:
        if key == "secret_key":
            display = "(set)" if cfg.get("secret_key") else "(not set)"
        elif key not in cfg:
            display = "(unset)"
        else:
            v = cfg[key]
            display = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
        rows.append({"key": key, "display": display, "note": note})
    return rows


def _clean_emails(lst):
    """Lowercase, dedupe, and validate a list of emails (raises ValueError)."""
    out, seen = [], set()
    for e in lst or []:
        e = (e or "").strip().lower()
        if not e:
            continue
        if not _EMAIL_RE.match(e):
            raise ValueError("not a valid email: %s" % e)
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


@bp.route("/admin/data/settings", methods=["POST"])
def settings():
    """Edit study config LIVE — every key here is read per-request, so a
    save_config() hot-swap takes effect on the next request with NO restart and
    NO rebuild. Only the CHANGED keys are sent; each is validated + guarded."""
    _require_admin()
    data = request.get_json(force=True) or {}
    with _APP._lock:
        fresh = _APP.load_config()

        if "study_title" in data:
            t = (data.get("study_title") or "").strip()
            if not t:
                return jsonify(error="study title can't be empty"), 400
            if len(t) > 120:
                return jsonify(error="study title too long (max 120 chars)"), 400
            fresh["study_title"] = t

        if "instructions" in data:
            ins = (data.get("instructions") or "").strip()
            if len(ins) > 4000:
                return jsonify(error="instructions too long (max 4000 chars)"), 400
            fresh["instructions"] = ins

        if "answer_labels" in data:                       # cosmetic button wording
            lbls = data.get("answer_labels") or {}
            if not isinstance(lbls, dict):
                return jsonify(error="answer_labels must be an object"), 400
            opts = set(fresh.get("answer_options") or [])
            fresh["answer_labels"] = {k: (str(v)[:60]).strip()
                                      for k, v in lbls.items() if k in opts}

        if "admin_emails" in data:                        # self-lockout guard
            try:
                admins = _clean_emails(data.get("admin_emails"))
            except ValueError as e:
                return jsonify(error=str(e)), 400
            if not admins:
                return jsonify(error="there must be at least one admin"), 400
            me = (_APP.cf_access_email() or "").lower()
            if me and me not in admins:
                return jsonify(error="you can't remove your own admin access (%s) "
                                     "— add another admin first or keep yourself." % me), 400
            fresh["admin_emails"] = admins

        if "curator_emails" in data:
            try:
                fresh["curator_emails"] = _clean_emails(data.get("curator_emails"))
            except ValueError as e:
                return jsonify(error=str(e)), 400

        if "shuffle_per_rater" in data:
            fresh["shuffle_per_rater"] = bool(data.get("shuffle_per_rater"))
        if "data_manager" in data:
            fresh["data_manager"] = bool(data.get("data_manager"))

        if "sibling_studies" in data:
            sibs = data.get("sibling_studies") or []
            if not isinstance(sibs, list):
                return jsonify(error="sibling_studies must be a list"), 400
            clean = []
            for s in sibs:
                name = (s.get("name") or "").strip()
                url = (s.get("url") or "").strip()
                if not name and not url:
                    continue
                if not (url.startswith("http://") or url.startswith("https://")):
                    return jsonify(error="study link URL must start with http:// or https://"), 400
                clean.append({"name": name or url, "url": url})
            fresh["sibling_studies"] = clean

        _APP.save_config(fresh)
    return jsonify(ok=True, note="applied live — no restart needed")


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


@bp.route("/admin/data/assign_bulk", methods=["POST"])
def assign_bulk():
    """Apply many rater->cohort assignments in ONE all-or-nothing save_config
    pass. Body {assignments: {rater: [cohorts]}}. Only the raters present in the
    payload are touched (the client sends only rows the operator changed), so a
    concurrent per-row edit to an untouched rater survives. Restart-free."""
    _require_admin()
    data = request.get_json(force=True) or {}
    assignments = data.get("assignments")
    if not isinstance(assignments, dict):
        return jsonify(error="assignments must be an object"), 400
    with _APP._lock:
        fresh = _APP.load_config()
        known = set(fresh.get("raters", []))
        defined = set(fresh.get("cohorts") or {})
        bad_raters = [r for r in assignments if r not in known]
        bad_type = [r for r, c in assignments.items() if not isinstance(c, list)]
        bad_cohorts = sorted({c for cl in assignments.values() if isinstance(cl, list)
                              for c in cl if c not in defined})
        if bad_type:
            return jsonify(error="cohorts must be lists for: %s" % ", ".join(bad_type)), 400
        if bad_raters:
            return jsonify(error="unknown rater(s): %s" % ", ".join(bad_raters)), 400
        if bad_cohorts:
            return jsonify(error="unknown cohort(s): %s" % ", ".join(bad_cohorts)), 400
        rc = fresh.setdefault("rater_cohorts", {})
        for rater, cohorts in assignments.items():
            if cohorts:
                rc[rater] = cohorts
            else:
                rc.pop(rater, None)          # empty -> sees ALL cohorts
        _APP.save_config(fresh)
    return jsonify(ok=True, n=len(assignments),
                   rater_cohorts=_APP.CFG.get("rater_cohorts") or {})


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


@bp.route("/admin/data/cohort/preview")
def cohort_preview():
    """Live, non-mutating preview of which slide folders a cohort would OWN with
    candidate patterns. Substitutes the patterns into the LIVE cohorts dict (in
    place for an existing cohort, appended for a new one) and runs the production
    app.cohort_for, so cross-cohort first-match-wins is reproduced exactly — the
    preview can't claim a folder another cohort already owns.
    Query: ?name=<cohort>&pat=<glob>&pat=...  (read-only; no save, no rebuild)."""
    _require_admin()
    name = (request.args.get("name") or "_preview").strip() or "_preview"
    pats = [p.strip() for p in request.args.getlist("pat") if p.strip()]
    cohorts = dict(_APP.CFG.get("cohorts") or {})     # preserves insertion order
    cohorts[name] = pats                              # in-place if exists, else appended
    probe = {"cohorts": cohorts}
    folders = _slide_folders()
    matched = [f for f in folders if _APP.cohort_for(f, probe) == name]
    unassigned = sum(1 for f in folders if not _APP.cohort_for(f, probe))
    return jsonify(matched=matched, matched_count=len(matched),
                   unassigned=unassigned, total=len(folders))


# --------------------------------------------------------------------------
# UPLOAD — a cohort, ONE SLIDE PER REQUEST (each under the Cloudflare 100 MB cap)
#
# The browser picks a cohort folder and POSTs one request per slide (multipart:
# many files + a parallel `relpath` for each). The server writes each file under
# patches_dir/[dest/]<slide>/<relpath>, refusing to overwrite an existing slide
# folder (a re-upload would re-map already-answered detections). Slides larger
# than the cap are skipped client-side; copy those in out-of-band + Rebuild.
# --------------------------------------------------------------------------
def _safe_rel_target(root, relpath):
    """Validated absolute target for a relative path under root — rejects
    absolute paths, '..' traversal, and anything escaping root (zip-slip style)."""
    if not relpath or relpath.startswith("/") or relpath.startswith("\\") or os.path.isabs(relpath):
        raise ValueError("absolute/empty path: %r" % relpath)
    parts = relpath.replace("\\", "/").split("/")
    if any(p in ("..", "") for p in parts):
        raise ValueError("bad path segment: %r" % relpath)
    rroot = os.path.realpath(root)
    target = os.path.realpath(os.path.join(rroot, relpath))
    if target != rroot and not target.startswith(rroot + os.sep):
        raise ValueError("escapes destination: %r" % relpath)
    return target


@bp.route("/admin/data/upload", methods=["POST"])
def upload():
    """Receive ONE slide (multipart: files[] + parallel relpath[]) and write it
    under patches_dir/[dest/]<slide>/. The client loops this over a cohort."""
    _require_admin()
    mode = _APP.CFG.get("detection_mode", "auto")
    if mode == "paired_csv":
        return jsonify(error="Upload is not supported in paired_csv mode "
                             "(candidates come from coords_root, not patches_dir). "
                             "Add paired_csv data out-of-band."), 400

    # one slide per request; the limit is scoped here (no global MAX_CONTENT_LENGTH)
    if request.content_length and request.content_length > MAX_UPLOAD_BYTES:
        return jsonify(error="this slide is over the %d MB per-request limit "
                             "(Cloudflare caps each upload at 100 MB) — copy it to "
                             "the server directly and Rebuild instead."
                             % (MAX_UPLOAD_BYTES // (1024 * 1024))), 413

    slide = (request.form.get("slide") or "").strip()
    dest = (request.form.get("dest") or "").strip()
    for label, seg in (("slide", slide), ("dest", dest)):
        if seg and ("/" in seg or "\\" in seg or not _SAFE_SEGMENT.match(seg)):
            return jsonify(error="%s name must be a single safe segment "
                                 "(letters, digits, space, _ . -)" % label), 400
    if not slide:
        return jsonify(error="missing slide folder name"), 400

    files = request.files.getlist("file")
    relpaths = request.form.getlist("relpath")
    if not files:
        return jsonify(error="no files in slide '%s'" % slide), 400
    if len(files) != len(relpaths):
        return jsonify(error="file/relpath count mismatch"), 400

    root = _patches_root()
    if not os.path.isdir(root):
        return jsonify(error="patches_dir does not exist on disk: %s" % root), 400
    slide_rel = os.path.join(dest, slide) if dest else slide
    slide_root = os.path.join(root, slide_rel)
    if os.path.exists(slide_root):
        return jsonify(error="slide folder '%s' already exists — re-uploading is "
                             "not allowed (it would re-map already-answered "
                             "detections). Rename or remove it first." % slide_rel), 409

    # validate every target BEFORE writing anything
    try:
        targets = [_safe_rel_target(slide_root, rp) for rp in relpaths]
    except ValueError as e:
        return jsonify(error="rejected unsafe path: %s" % e), 400

    try:
        for f, target in zip(files, targets):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            f.save(target)
    except Exception as e:                             # partial write -> clean up
        _rmtree_quiet(slide_root)
        return jsonify(error="write failed for '%s': %s" % (slide, e)), 500

    # cohort_for keys off the TOP path segment (dest if nested, else the slide)
    cohort = _APP.cohort_for(dest or slide, _APP.CFG)
    return jsonify(ok=True, slide=slide, dest=dest, n_files=len(files),
                   cohort=cohort or "(unmatched)",
                   note="run Rebuild to add these patches to the review set")


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
    if _PREVIEW.get("_running"):
        return jsonify(error="a preview is running — wait for it to finish"), 409
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
# DRY-RUN PREVIEW — what would Apply do? (added / removed / orphaned answers)
# Builds the would-be manifest WITHOUT writing it or swapping the live globals.
# --------------------------------------------------------------------------
def _answered_patch_ids():
    """Set of patch_ids that ANY rater has recorded an answer for. RESULTS is
    nested rater -> {item_id: row}, so we iterate two levels (a flat
    RESULTS.values() scan would yield zero and silently hide all orphans)."""
    answered = set()
    for rater in _APP.CFG.get("raters", []):
        for row in _APP.RESULTS.get(rater, {}).values():
            pid = row.get("patch_id")
            if pid:
                answered.add(pid)
    return answered


def _run_preview_bg():
    try:
        prior_ids = {p["patch_id"] for p in _APP.MANIFEST.get("patches", [])}
        root = _patches_root()
        if not os.path.isdir(root):
            raise RuntimeError("patches_dir does not exist: %s" % root)
        # build the would-be manifest WITHOUT writing it or swapping globals
        new_manifest = _APP.compute_manifest(copy.deepcopy(_APP.CFG))
        new_ids = {p["patch_id"] for p in new_manifest.get("patches", [])}
        answered = _answered_patch_ids()
        added, removed = new_ids - prior_ids, prior_ids - new_ids
        orphaned = sorted(removed & answered)          # answered patches that would drop out
        _PREVIEW.update(state="done", finished=_now(),
                        added=len(added), removed=len(removed),
                        orphaned=len(orphaned), orphaned_ids=orphaned[:200],
                        n_patches=new_manifest["n_patches"],
                        n_detections=new_manifest["n_detections"],
                        message="would add %d, remove %d; %d answered patches would be dropped"
                                % (len(added), len(removed), len(orphaned)))
    except BaseException as e:                          # incl. SystemExit from compute_manifest
        _PREVIEW.update(state="error", finished=_now(),
                        message="%s: %s" % (type(e).__name__, e))
    finally:
        _PREVIEW["_running"] = False


@bp.route("/admin/data/preview", methods=["POST"])
def preview():
    _require_admin()
    if _is_unmigrated():
        return jsonify(error="Migrate to stable ids before previewing a rebuild."), 409
    if _REBUILD.get("_running"):
        return jsonify(error="a rebuild is running — wait for it to finish"), 409
    with _PREVIEW_LOCK:
        if _PREVIEW.get("_running"):
            return jsonify(error="a preview is already running"), 409
        _PREVIEW.clear()
        _PREVIEW.update(state="running", message="scanning…", started=_now(),
                        finished="", _running=True, added=None, removed=None,
                        orphaned=None, n_patches=None, n_detections=None, orphaned_ids=[])
    threading.Thread(target=_run_preview_bg, daemon=True,
                     name="datamanager-preview").start()
    return jsonify(ok=True, state="running"), 202


@bp.route("/admin/data/preview/status")
def preview_status():
    _require_admin()
    return jsonify({k: v for k, v in _PREVIEW.items() if not k.startswith("_")})


# --------------------------------------------------------------------------
def register(flask_app, app_module):
    """Wire the blueprint onto the Flask app and capture the live app module.

    `app_module` must be the RUNNING module (pass sys.modules[__name__] from
    app.py) — not `import app`, which would load a second copy when app.py is
    executed as __main__."""
    global _APP
    _APP = app_module
    flask_app.register_blueprint(bp)
