"""Mitosis detection review tool.

Usage:
    python app.py build [--force]   # scan patches, sample, build manifest.json
    python app.py                   # run the review server

Pathologists open the URL, pick their name, and label each detection.
Results are written to results/results_<rater>.csv .
"""

import csv
import fnmatch
import io
import json
import os
import random
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone

import cv2
from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   send_file, session, url_for)

import boxes
import metrics
import slides as slides_mod

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
MANIFEST_PATH = os.path.join(ROOT, "manifest.json")
RESULTS_DIR = os.path.join(ROOT, "results")
CACHE_DIR = os.path.join(ROOT, "web_cache")
THUMB_CACHE_DIR = os.path.join(ROOT, "thumb_cache")
SELECTION_PATH = os.path.join(RESULTS_DIR, "selection.json")

# Slide-grading (/grade) — separate manifest + results directory so it can
# evolve independently of the patch review.
SLIDES_MANIFEST_PATH = os.path.join(ROOT, "slides_manifest.json")
GRADING_RESULTS_DIR = os.path.join(ROOT, "grading_results")
GRADE_SELECTION_PATH = os.path.join(GRADING_RESULTS_DIR, "selection.json")
GRADE_THUMB_DIR = os.path.join(ROOT, "thumb_cache_slides")

CSV_FIELDS = ["rater", "item_id", "patch_id", "image", "det_index",
              "x", "y", "w", "h", "label", "timestamp", "time_ms"]
GRADE_CSV_FIELDS = ["rater", "slide_id", "slide_name", "label",
                    "timestamp", "time_ms"]
WEB_NATIVE = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

_lock = threading.Lock()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_config():
    with open(CONFIG_PATH) as fh:
        return json.load(fh)


def save_config(new_cfg):
    """Atomically write config.json (temp + rename) and refresh CFG.

    Callers should hold _lock to serialise the read-modify-write."""
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(new_cfg, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, CONFIG_PATH)
    global CFG
    CFG = new_cfg


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------
def _abs(cfg, key):
    p = cfg[key]
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


def _clamp_box(b, w, h):
    x, y, bw, bh = b
    x = max(0, min(int(x), w - 1))
    y = max(0, min(int(y), h - 1))
    bw = max(1, min(int(bw), w - x))
    bh = max(1, min(int(bh), h - y))
    return [x, y, bw, bh]


def _list_candidates(cfg, pdir):
    """Return [(image_relpath, csv_path_or_None)] to sample from.

    paired_csv mode walks the parallel coordinates tree (one CSV per tile);
    other modes list image files and resolve boxes later."""
    if cfg.get("detection_mode") == "paired_csv":
        croot = _abs(cfg, "coords_root")
        if not os.path.isdir(croot):
            sys.exit("coords_root not found: %s" % croot)
        tiles = cfg.get("tiles_subdir", "tiles")
        out = []
        for root, _, files in os.walk(croot):
            for f in files:
                if f.startswith("._") or not f.lower().endswith(".csv"):
                    continue
                csv_path = os.path.join(root, f)
                rel = os.path.relpath(csv_path, croot)
                slide = rel.split(os.sep)[0]
                img_rel = os.path.join(slide, tiles, os.path.basename(rel)[:-4])
                out.append((img_rel, csv_path))
        return out
    exts = {e.lower() for e in cfg["image_extensions"]}
    out = []
    for root, _, files in os.walk(pdir):
        for f in files:
            if f.startswith("._"):
                continue
            if os.path.splitext(f)[1].lower() in exts:
                rel = os.path.relpath(os.path.join(root, f), pdir)
                out.append((rel, None))
    return out


def cohort_for(image_rel, cfg):
    """Cohort name for a patch, by matching its TOP path segment against the
    configured `cohorts` patterns ({cohort_name: [fnmatch patterns]}).

    The top segment is the slide folder (flat layout: <slide>/<tile>.jpg) or a
    cohort folder (nested layout: <cohort>/<slide>/<tile>.jpg) — either works,
    since cohorts are defined by pattern (e.g. "um*" for upmc, or the exact
    cohort folder name). Returns '' when no cohort matches / none configured."""
    cohorts = cfg.get("cohorts") or {}
    if not cohorts:
        return ""
    seg = image_rel.replace(os.sep, "/").split("/")[0]
    for name, patterns in cohorts.items():
        for pat in (patterns or []):
            if fnmatch.fnmatch(seg.lower(), pat.lower()):
                return name
    return ""


def compute_manifest(cfg):
    """Sample n_patches tiles and resolve their detections, RETURNING the
    manifest dict WITHOUT writing it to disk. Shared by build_manifest (which
    writes) and the data-manager's dry-run preview (which must not clobber the
    live manifest.json).

    patch_id is content-addressed (boxes.stable_pid of the relative image path)
    so a rebuild after adding slides/cohorts never renumbers existing patches —
    prior raters' results and the curation selection stay valid. n_patches == 0
    means "all candidates"."""
    pdir = _abs(cfg, "patches_dir")
    if not os.path.isdir(pdir):
        sys.exit("patches_dir not found: %s" % pdir)
    cfg["_patches_dir_abs"] = pdir         # used by yolo_subdir lookup
    mode = cfg.get("detection_mode")

    cands = _list_candidates(cfg, pdir)
    cands.sort()                                     # deterministic across rebuilds
    random.Random(cfg["seed"]).shuffle(cands)

    coord_index = {} if mode == "paired_csv" else boxes.build_coord_index(pdir, cfg)
    target = cfg["n_patches"] or len(cands)        # n_patches == 0 -> all candidates
    patches, skipped = [], 0

    for img_rel, csv_path in cands:
        if len(patches) >= target:
            break
        full = os.path.join(pdir, img_rel)
        if not os.path.exists(full):
            skipped += 1
            continue
        if mode == "paired_csv":               # read cheap CSV before the image
            dets = boxes.read_paired_csv(csv_path)
            if not dets:
                skipped += 1
                continue
            img = boxes.read_image(full)
        else:
            img = boxes.read_image(full)
            if img is None:
                skipped += 1
                continue
            dets = ([None] if mode == "whole_patch"
                    else boxes.detections_for_image(img_rel, full, img,
                                                    cfg, coord_index))
        if img is None or not dets:
            skipped += 1
            continue
        h, w = img.shape[:2]
        img_norm = img_rel.replace(os.sep, "/")
        patches.append({
            "patch_id": boxes.stable_pid(img_norm),
            "image": img_norm,
            "cohort": cohort_for(img_rel, cfg),
            "w": w, "h": h,
            "detections": [
                {"det_index": k,
                 "bbox": None if d is None else _clamp_box(d, w, h)}
                for k, d in enumerate(dets)],
        })

    return {
        "created": now_iso(), "seed": cfg["seed"],
        "n_requested": target, "n_patches": len(patches),
        "n_detections": sum(len(p["detections"]) for p in patches),
        "detection_mode": mode,
        "candidates_scanned": len(cands), "skipped": skipped,
        "patches": patches,
    }


def build_manifest(cfg, force=False):
    """Build the manifest AND persist it atomically to MANIFEST_PATH. The dry-run
    preview calls compute_manifest() directly to avoid this write."""
    manifest = compute_manifest(cfg)
    tmp = MANIFEST_PATH + ".tmp"                    # atomic: no truncated manifest
    with open(tmp, "w") as fh:
        json.dump(manifest, fh, indent=1)
    os.replace(tmp, MANIFEST_PATH)
    print("manifest: %d tiles, %d detections (scanned %d candidates, "
          "skipped %d with no image / no boxes)"
          % (manifest["n_patches"], manifest["n_detections"],
             manifest["candidates_scanned"], manifest["skipped"]))
    return manifest


def load_manifest():
    with open(MANIFEST_PATH) as fh:
        return json.load(fh)


def build_item_index(manifest):
    """item_id -> {patch_id, image, det_index, bbox} for every detection.

    Factored out of startup so the data-manager rebuild can build a fresh index
    against a new manifest and hot-swap MANIFEST + ITEM_INDEX together."""
    idx = {}
    for p in manifest["patches"]:
        for d in p["detections"]:
            idx["%s_d%d" % (p["patch_id"], d["det_index"])] = {
                "patch_id": p["patch_id"], "image": p["image"],
                "det_index": d["det_index"], "bbox": d["bbox"]}
    return idx


def apply_manifest(new_manifest):
    """Hot-swap the live MANIFEST + ITEM_INDEX together under _lock so concurrent
    readers never observe a manifest/index mismatch. Rebinding a module global is
    a single atomic store under the GIL; we build the new index fully first, then
    rebind both names. Used by the data-manager's in-process rebuild. Because
    patch_ids are content-addressed, unchanged slides keep their ids so existing
    rater results re-link automatically. SKIPPED/RESULTS are keyed by patch_id and
    are intentionally NOT touched here."""
    global MANIFEST, ITEM_INDEX
    new_index = build_item_index(new_manifest)
    with _lock:
        MANIFEST = new_manifest
        ITEM_INDEX = new_index


# --------------------------------------------------------------------------
# review-item ordering
# --------------------------------------------------------------------------
def rater_items(manifest, cfg, rater):
    """Flat, per-rater-ordered list of review items. Same patches & boxes for
    everyone; patch order is shuffled per rater to remove order bias.
    Patches in SKIPPED (admin curation) are filtered out before shuffling.

    When `rater_cohorts` assigns this rater a set of cohorts, they see ONLY
    patches from those cohorts. A rater with no entry sees all patches — so a
    deployment with no cohorts configured behaves exactly as before."""
    rc = cfg.get("rater_cohorts") or {}
    assigned = rc.get(rater)          # None -> all cohorts; list -> only those
    order = [i for i, p in enumerate(manifest["patches"])
             if p["patch_id"] not in SKIPPED
             and (assigned is None or p.get("cohort", "") in assigned)]
    if cfg.get("shuffle_per_rater", True):
        ridx = cfg["raters"].index(rater)
        random.Random(cfg["seed"] * 100003 + ridx + 1).shuffle(order)

    items, total = [], len(order)
    for pos, pi in enumerate(order):
        p = manifest["patches"][pi]
        nd = len(p["detections"])
        for d in p["detections"]:
            bb = d["bbox"]
            frac = None
            if bb is not None:
                frac = [round(bb[0] / p["w"], 6), round(bb[1] / p["h"], 6),
                        round(bb[2] / p["w"], 6), round(bb[3] / p["h"], 6)]
            items.append({
                "item_id": "%s_d%d" % (p["patch_id"], d["det_index"]),
                "patch_id": p["patch_id"], "image": p["image"],
                "det_index": d["det_index"], "n_in_patch": nd,
                "patch_pos": pos + 1, "n_patches": total,
                "bbox": bb, "bbox_frac": frac,
            })
    return items


# --------------------------------------------------------------------------
# results storage  (in-memory, mirrored to one CSV per rater)
# --------------------------------------------------------------------------
RESULTS = {}  # rater -> {item_id -> row dict}
SKIPPED = set()  # patch_ids excluded by admin curation


def load_selection():
    global SKIPPED
    SKIPPED = set()
    if os.path.exists(SELECTION_PATH):
        try:
            with open(SELECTION_PATH) as fh:
                SKIPPED = set(json.load(fh).get("skipped", []))
        except (OSError, ValueError):
            pass


def save_selection():
    os.makedirs(RESULTS_DIR, exist_ok=True)
    tmp = SELECTION_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"skipped": sorted(SKIPPED), "updated": now_iso()},
                  fh, indent=1)
    os.replace(tmp, SELECTION_PATH)


def results_path(rater):
    return os.path.join(RESULTS_DIR, "results_%s.csv" % rater)


def load_results(cfg):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    for rater in cfg["raters"]:
        RESULTS[rater] = {}
        path = results_path(rater)
        if os.path.exists(path):
            with open(path, newline="") as fh:
                for row in csv.DictReader(fh):
                    RESULTS[rater][row["item_id"]] = row


def write_results(rater):
    tmp = results_path(rater) + ".tmp"
    rows = sorted(RESULTS[rater].values(), key=lambda r: r["timestamp"])
    with open(tmp, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    os.replace(tmp, results_path(rater))


# --------------------------------------------------------------------------
# slide-grading manifest + results
# --------------------------------------------------------------------------
SLIDES = {"slides": [], "n_slides": 0}    # slides_manifest.json
SLIDE_INDEX = {}                          # slide_id -> manifest record
GRADES = {}                               # rater -> {slide_id -> row dict}
GRADE_SKIPPED = set()                     # slide_ids excluded by admin curation


def load_slides_manifest():
    global SLIDES, SLIDE_INDEX
    SLIDES = slides_mod.load_manifest(SLIDES_MANIFEST_PATH)
    SLIDE_INDEX = {s["slide_id"]: s for s in SLIDES["slides"]}


def load_grade_selection():
    global GRADE_SKIPPED
    GRADE_SKIPPED = set()
    if os.path.exists(GRADE_SELECTION_PATH):
        try:
            with open(GRADE_SELECTION_PATH) as fh:
                GRADE_SKIPPED = set(json.load(fh).get("skipped", []))
        except (OSError, ValueError):
            pass


def save_grade_selection():
    os.makedirs(GRADING_RESULTS_DIR, exist_ok=True)
    tmp = GRADE_SELECTION_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"skipped": sorted(GRADE_SKIPPED), "updated": now_iso()},
                  fh, indent=1)
    os.replace(tmp, GRADE_SELECTION_PATH)


def grade_results_path(rater):
    return os.path.join(GRADING_RESULTS_DIR, "results_%s.csv" % rater)


def load_grades(cfg):
    os.makedirs(GRADING_RESULTS_DIR, exist_ok=True)
    for rater in cfg["raters"]:
        GRADES[rater] = {}
        path = grade_results_path(rater)
        if os.path.exists(path):
            with open(path, newline="") as fh:
                for row in csv.DictReader(fh):
                    GRADES[rater][row["slide_id"]] = row


def write_grades(rater):
    tmp = grade_results_path(rater) + ".tmp"
    rows = sorted(GRADES[rater].values(), key=lambda r: r["timestamp"])
    with open(tmp, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=GRADE_CSV_FIELDS)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    os.replace(tmp, grade_results_path(rater))


def grade_items_for_rater(cfg, rater):
    """Per-rater-shuffled list of slides to grade. Same set for everyone;
    order shuffled per rater to remove order effects, using a different
    seed multiplier than the patch shuffle so the two streams don't
    inherit the same ordering. Slides in GRADE_SKIPPED (admin curation)
    are dropped before shuffling."""
    order = [i for i, s in enumerate(SLIDES["slides"])
             if s["slide_id"] not in GRADE_SKIPPED]
    if cfg.get("shuffle_per_rater", True) and order:
        ridx = cfg["raters"].index(rater) if rater in cfg["raters"] else 0
        random.Random(cfg["seed"] * 200003 + ridx + 1).shuffle(order)
    out = []
    for pos, i in enumerate(order):
        s = SLIDES["slides"][i]
        out.append({
            "slide_id": s["slide_id"],
            "name": s.get("name") or os.path.basename(s["path"]),
            "pos": pos + 1, "total": len(order),
        })
    return out


def slide_abspath(rec):
    sd = CFG.get("slides_dir") or ROOT
    sd = sd if os.path.isabs(sd) else os.path.join(ROOT, sd)
    return slides_mod.resolve_slide_path(rec, sd)


# --------------------------------------------------------------------------
# app
# --------------------------------------------------------------------------
CFG = load_config()
app = Flask(__name__)
app.secret_key = CFG.get("secret_key", "dev-secret")


@app.context_processor
def inject_auth():
    """Expose the CF-Access-verified email + admin flag to every template
    so the auth pill (sign-out link) can be rendered without each route
    threading it through. sibling_studies is a config-driven list of
    {name, url} hops shown in admin nav for switching between studies.
    grade_enabled is true once a slides_manifest.json has been built — used
    to gate the 'Slide grading' nav links. is_curator/can_curate gate the
    curation UI for the narrow curator role. data_enabled gates the superadmin
    data-manager link (also is_admin-gated); set "data_manager": false in config
    to hide it for a deployment."""
    return {"auth_email": cf_access_email(), "is_admin": is_admin(),
            "is_curator": is_curator(), "can_curate": can_curate(),
            "sibling_studies": CFG.get("sibling_studies", []),
            "grade_enabled": SLIDES.get("n_slides", 0) > 0,
            "data_enabled": CFG.get("data_manager", True)}

if not os.path.exists(MANIFEST_PATH):
    print("No manifest.json found - run:  python app.py build")
    MANIFEST = {"patches": [], "n_patches": 0, "n_detections": 0}
else:
    MANIFEST = load_manifest()
ITEM_INDEX = build_item_index(MANIFEST)  # item_id -> patch + detection info
load_results(CFG)
load_selection()
load_slides_manifest()
load_grades(CFG)
load_grade_selection()


def _prewarm_slides_bg():
    """Open every slide once and cache its 1024 px placeholder thumbnail
    on disk. Runs once per process in a daemon thread so app startup isn't
    blocked. The expensive bit is the first OpenSlide read off CIFS — once
    a slide handle is in HandleCache, subsequent tile reads are warm."""
    import time
    os.makedirs(GRADE_THUMB_DIR, exist_ok=True)
    for s in SLIDES["slides"]:
        sid = s["slide_id"]
        # check the placeholder cache first so a restart with all thumbs on
        # disk doesn't re-open every slide (still nice to warm the handles
        # but the IO win goes to the user's first request)
        safe = "".join(c if c.isalnum() or c in "_.-" else "_" for c in sid)
        cache_path = os.path.join(GRADE_THUMB_DIR, "%s_w%d.jpg" % (safe, 1024))
        try:
            path = slide_abspath(s)
            if not os.path.exists(path):
                continue
            entry = slides_mod.open_slide(path)
            if not os.path.exists(cache_path):
                # same locking discipline as the live thumb route
                with entry["lock"]:
                    img = entry["osl"].get_thumbnail((1024, 1024))
                img.convert("RGB").save(cache_path, "JPEG", quality=82)
        except Exception:
            # one bad slide shouldn't kill the prewarmer
            app.logger.exception("prewarm failed for %s", sid)
        time.sleep(0.05)        # yield so we don't starve real requests


threading.Thread(target=_prewarm_slides_bg, daemon=True,
                 name="slide-prewarm").start()


def patches_root():
    pdir = CFG["patches_dir"]
    return pdir if os.path.isabs(pdir) else os.path.join(ROOT, pdir)


def current_rater():
    return session.get("rater")


def cf_access_email():
    """Email Cloudflare Access verified for this request, or '' if absent.

    Set by the cloudflared tunnel after the user passes the Access policy.
    Flask binds 127.0.0.1, so this header can only originate from cloudflared
    on this host — no spoofing path from off-machine."""
    return (request.headers.get("Cf-Access-Authenticated-User-Email") or "").lower()


def is_admin():
    """True iff the Access-verified email is on the admin_emails allowlist."""
    email = cf_access_email()
    if not email:
        return False
    allowed = {e.lower() for e in CFG.get("admin_emails", [])}
    return email in allowed


def is_curator():
    """True iff the email is on curator_emails — a *narrowly* scoped role that
    grants ONLY access to /admin/curate and the preview-image route it depends
    on. Curators see no metrics, no rater list, no data-manager, no other admin
    pages. For pathologist-graders who also prune the patch set but shouldn't
    see everyone's review progress."""
    email = cf_access_email()
    if not email:
        return False
    allowed = {e.lower() for e in CFG.get("curator_emails", [])}
    return email in allowed


def can_curate():
    """Admin OR curator. The /admin/curate* endpoints + preview_img use this
    rather than is_admin() so curators can run the curation workflow."""
    return is_admin() or is_curator()


def _authorized_emails():
    """Union of every email the app config has named — admins, mapped raters,
    and (if configured) curators. The before_request gate uses this so an
    email that was NOT explicitly added by the owner can't reach ANY route,
    even read-only ones. Cloudflare Access alone is not enough: the Access
    allowlist might legitimately include people who shouldn't see this
    study's data (e.g. across studies sharing a tenant)."""
    s = {e.lower() for e in CFG.get("admin_emails", [])}
    s.update(e.lower() for e in CFG.get("curator_emails", []))
    s.update(e.lower() for e in (CFG.get("rater_emails") or {}).keys())
    return s


@app.before_request
def _access_gate():
    """Fail-closed: any CF-Access email that isn't explicitly named in the
    config gets 403, regardless of which route they hit. Static assets are
    gated too — they belong to the study, not the world."""
    if not _authorized_emails() & {cf_access_email()}:
        abort(403)


def rater_for_email(email):
    """Rater name that this CF-Access-verified email is bound to, or None.

    Used to bypass the radio picker for mapped raters."""
    if not email:
        return None
    return (CFG.get("rater_emails") or {}).get(email.lower())


# ---- auth ----------------------------------------------------------------
@app.route("/", methods=["GET", "POST"])
def login():
    email = cf_access_email()
    auto_rater = rater_for_email(email)

    if request.method == "POST":
        rater = request.form.get("rater", "").strip()
        if rater not in CFG["raters"]:
            return render_template("login.html", cfg=CFG, error="Pick your name.",
                                   auth_email=email, is_admin=is_admin(),
                                   auto_rater=auto_rater)
        session["rater"] = rater
        return redirect(url_for("review"))

    if current_rater():
        return redirect(url_for("review"))

    # Email is bound to a rater AND user isn't also an admin who might want
    # to choose between /review and /admin → jump straight to /review.
    if auto_rater and not is_admin():
        session["rater"] = auto_rater
        return redirect(url_for("review"))

    return render_template("login.html", cfg=CFG, error=None,
                           auth_email=email, is_admin=is_admin(),
                           auto_rater=auto_rater)


@app.route("/logout")
def logout():
    # Clear *all* Flask session data, then end the Cloudflare Access
    # session too. CF requires returnTo to be a fully-qualified absolute
    # URL on the app's own domain (a relative '/' is rejected as
    # invalid), so build it from the host + forwarded scheme cloudflared
    # passes through. Landing back on the app root lets the next OTP
    # cycle start from a clean origin request.
    session.clear()
    proto = request.headers.get("X-Forwarded-Proto", "https")
    return_to = "%s://%s/" % (proto, request.host)
    return redirect("/cdn-cgi/access/logout?returnTo=%s"
                    % urllib.parse.quote(return_to, safe=""))


@app.route("/review")
def review():
    if not current_rater():
        return redirect(url_for("login"))
    return render_template("review.html", cfg=CFG, rater=current_rater())


# ---- review API ----------------------------------------------------------
@app.route("/api/session")
def api_session():
    rater = current_rater()
    if not rater:
        abort(401)
    items = rater_items(MANIFEST, CFG, rater)
    answers = {k: v["label"] for k, v in RESULTS.get(rater, {}).items()}
    client = [{k: it[k] for k in ("item_id", "patch_id", "det_index",
                                  "n_in_patch", "patch_pos", "n_patches",
                                  "bbox_frac")} for it in items]
    return jsonify(rater=rater, options=CFG["answer_options"],
                   items=client, answers=answers)


@app.route("/api/answer", methods=["POST"])
def api_answer():
    rater = current_rater()
    if not rater:
        abort(401)
    data = request.get_json(force=True)
    item_id = data.get("item_id")
    label = data.get("label")
    # single read of the (atomically rebound) ITEM_INDEX so a concurrent
    # data-manager hot-swap can't slip between a membership test and a lookup.
    info = ITEM_INDEX.get(item_id)
    if info is None or label not in CFG["answer_options"]:
        abort(400)
    bb = info["bbox"] or ["", "", "", ""]
    row = {"rater": rater, "item_id": item_id, "patch_id": info["patch_id"],
           "image": info["image"], "det_index": info["det_index"],
           "x": bb[0], "y": bb[1], "w": bb[2], "h": bb[3], "label": label,
           "timestamp": now_iso(), "time_ms": int(data.get("time_ms", 0))}
    with _lock:
        RESULTS.setdefault(rater, {})[item_id] = row
        write_results(rater)
    return jsonify(ok=True)


# ---- image serving -------------------------------------------------------
def _safe_patch_path(patch_id):
    p = next((x for x in MANIFEST["patches"] if x["patch_id"] == patch_id), None)
    if p is None:
        abort(404)
    full = os.path.normpath(os.path.join(patches_root(), p["image"]))
    if not full.startswith(os.path.normpath(patches_root())):
        abort(403)
    if not os.path.exists(full):
        abort(404)
    return p, full


@app.route("/img/<patch_id>")
def img(patch_id):
    if not current_rater() and not is_admin():
        abort(401)
    p, full = _safe_patch_path(patch_id)
    if os.path.splitext(full)[1].lower() in WEB_NATIVE:
        return send_file(full)
    os.makedirs(CACHE_DIR, exist_ok=True)            # convert tiff/etc to png
    cached = os.path.join(CACHE_DIR, patch_id + ".png")
    if not os.path.exists(cached):
        im = boxes.read_image(full)
        if im is None:
            abort(404)
        cv2.imwrite(cached, im)
    return send_file(cached)


# ---- admin / preview -----------------------------------------------------
@app.route("/admin")
def admin():
    if not is_admin():
        abort(401)
    kept = [p for p in MANIFEST["patches"] if p["patch_id"] not in SKIPPED]
    kept_ids = {p["patch_id"] for p in kept}
    total_dets = sum(len(p["detections"]) for p in kept)
    rc = CFG.get("rater_cohorts") or {}
    # detections-per-cohort over the kept set (for the summary line)
    cohort_dets = {}
    for p in kept:
        c = p.get("cohort", "") or "(unassigned)"
        cohort_dets[c] = cohort_dets.get(c, 0) + len(p["detections"])
    rows = []
    for rater in CFG["raters"]:
        raw = RESULTS.get(rater, {})
        assigned = rc.get(rater)
        if assigned is None:
            total = total_dets
            cohorts_label = "all"
        else:
            total = sum(len(p["detections"]) for p in kept
                        if p.get("cohort", "") in assigned)
            cohorts_label = ", ".join(assigned) or "(none yet)"
        done = sum(1 for r in raw.values() if r["patch_id"] in kept_ids)
        rows.append({"rater": rater, "done": done, "total": total,
                     "pct": round(100 * done / total, 1) if total else 0,
                     "cohorts": cohorts_label})
    return render_template("admin.html", cfg=CFG, rows=rows,
                           manifest=MANIFEST, cohort_dets=cohort_dets,
                           kept_count=len(kept), skipped_count=len(SKIPPED),
                           total_dets=total_dets)


@app.route("/admin/metrics")
def admin_metrics():
    if not is_admin():
        abort(401)
    return render_template("metrics.html", cfg=CFG)


@app.route("/admin/metrics/data")
def admin_metrics_data():
    if not is_admin():
        abort(401)
    kept_ids = {p["patch_id"] for p in MANIFEST["patches"]
                if p["patch_id"] not in SKIPPED}
    gt = request.args.get("gt") or None
    # snapshot under lock — avoids racing concurrent api_answer writes
    with _lock:
        snapshot = {r: dict(RESULTS.get(r, {})) for r in CFG["raters"]}
    payload = metrics.compute(snapshot, CFG["raters"], CFG["answer_options"],
                              kept_ids=kept_ids, gt_rater=gt)
    payload["server_time"] = now_iso()
    return jsonify(payload)


@app.route("/admin/raters")
def admin_raters():
    if not is_admin():
        abort(401)
    emails = CFG.get("rater_emails") or {}
    name_to_email = {v: k for k, v in emails.items()}
    rows = [{
        "name": r,
        "email": name_to_email.get(r, ""),
        "answers": len(RESULTS.get(r, {})),
    } for r in CFG["raters"]]
    return render_template("raters.html", cfg=CFG, rows=rows)


def _normalise_rater_name(n):
    return (n or "").strip().lower()


def _normalise_email(e):
    return (e or "").strip().lower()


@app.route("/admin/raters/add", methods=["POST"])
def admin_raters_add():
    if not is_admin():
        abort(401)
    data = request.get_json(force=True) or {}
    name = _normalise_rater_name(data.get("name"))
    email = _normalise_email(data.get("email"))

    if not name or not name.replace("_", "").isalnum():
        return jsonify(error="name must be alphanumeric / underscore only"), 400
    if not email or "@" not in email or "." not in email.split("@")[-1]:
        return jsonify(error="not a valid email"), 400

    with _lock:
        fresh = load_config()
        if name in fresh["raters"]:
            return jsonify(error="rater '%s' already exists" % name), 400
        if email in (fresh.get("rater_emails") or {}):
            return jsonify(
                error="email already mapped to '%s'" % fresh["rater_emails"][email]
            ), 400
        fresh["raters"].append(name)
        fresh.setdefault("rater_emails", {})[email] = name
        save_config(fresh)
        RESULTS.setdefault(name, {})
    return jsonify(ok=True)


@app.route("/admin/raters/email", methods=["POST"])
def admin_raters_set_email():
    """Set/clear the email bound to an existing rater."""
    if not is_admin():
        abort(401)
    data = request.get_json(force=True) or {}
    name = _normalise_rater_name(data.get("name"))
    email = _normalise_email(data.get("email"))

    if name not in CFG["raters"]:
        return jsonify(error="unknown rater"), 400
    if email and ("@" not in email or "." not in email.split("@")[-1]):
        return jsonify(error="not a valid email"), 400

    with _lock:
        fresh = load_config()
        emails = fresh.setdefault("rater_emails", {})
        # drop any previous mapping pointing at this rater
        for k in [k for k, v in emails.items() if v == name]:
            del emails[k]
        if email:
            if email in emails:
                return jsonify(
                    error="email already mapped to '%s'" % emails[email]
                ), 400
            emails[email] = name
        save_config(fresh)
    return jsonify(ok=True)


@app.route("/admin/results/<rater>.csv")
def admin_csv(rater):
    if not is_admin() or rater not in CFG["raters"]:
        abort(401)
    if not os.path.exists(results_path(rater)):
        return "no results yet", 404
    return send_file(results_path(rater), as_attachment=True,
                     download_name="results_%s.csv" % rater)


@app.route("/preview")
def preview():
    if not is_admin():
        abort(401)
    return render_template("preview.html", cfg=CFG, manifest=MANIFEST)


@app.route("/preview_img/<patch_id>")
def preview_img(patch_id):
    """Patch with every detection drawn + numbered. ?w= sets the max-side
    target (160-1200). Cached to thumb_cache/ so subsequent loads are instant."""
    if not can_curate():
        abort(401)
    try:
        w_req = int(request.args.get("w", 760))
    except ValueError:
        w_req = 760
    w_req = max(160, min(1200, w_req))
    os.makedirs(THUMB_CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(THUMB_CACHE_DIR, "%s_w%d.jpg" % (patch_id, w_req))
    if os.path.exists(cache_path):
        return send_file(cache_path, mimetype="image/jpeg")
    p, full = _safe_patch_path(patch_id)
    im = boxes.read_image(full)
    if im is None:
        abort(404)
    scale = min(1.0, w_req / max(im.shape[:2]))
    if scale < 1.0:
        im = cv2.resize(im, None, fx=scale, fy=scale,
                        interpolation=cv2.INTER_AREA)
    for d in p["detections"]:
        bb = d["bbox"]
        if not bb:
            continue
        x, y, w, h = (int(round(v * scale)) for v in bb)
        cv2.rectangle(im, (x, y), (x + w, y + h), (0, 0, 255), 2)
        cv2.putText(im, str(d["det_index"]), (x, max(12, y - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 255), 2)
    ok, buf = cv2.imencode(".jpg", im, [cv2.IMWRITE_JPEG_QUALITY, 82])
    with open(cache_path, "wb") as fh:
        fh.write(buf.tobytes())
    return send_file(cache_path, mimetype="image/jpeg")


# ---- curation ------------------------------------------------------------
@app.route("/admin/curate")
def admin_curate():
    if not can_curate():
        abort(401)
    return render_template("curate.html", cfg=CFG, manifest=MANIFEST)


@app.route("/admin/curate/state")
def admin_curate_state():
    if not can_curate():
        abort(401)
    return jsonify(skipped=sorted(SKIPPED),
                   total=len(MANIFEST["patches"]),
                   kept=len(MANIFEST["patches"]) - len(SKIPPED))


@app.route("/admin/curate/toggle", methods=["POST"])
def admin_curate_toggle():
    if not can_curate():
        abort(401)
    data = request.get_json(force=True) or {}
    pid, keep = data.get("patch_id"), bool(data.get("keep"))
    valid = {p["patch_id"] for p in MANIFEST["patches"]}
    if pid not in valid:
        abort(400)
    with _lock:
        (SKIPPED.discard if keep else SKIPPED.add)(pid)
        save_selection()
    return jsonify(ok=True, kept=len(valid) - len(SKIPPED))


@app.route("/admin/curate/bulk", methods=["POST"])
def admin_curate_bulk():
    if not can_curate():
        abort(401)
    action = (request.get_json(force=True) or {}).get("action")
    with _lock:
        if action == "keep_all":
            SKIPPED.clear()
        elif action == "skip_all":
            SKIPPED.update(p["patch_id"] for p in MANIFEST["patches"])
        else:
            abort(400)
        save_selection()
    return jsonify(ok=True, kept=len(MANIFEST["patches"]) - len(SKIPPED))


# --------------------------------------------------------------------------
# slide-level grading (/grade)
# --------------------------------------------------------------------------
def _grade_options():
    return CFG.get("grade_options") or ["high", "low", "unsure"]


@app.route("/grade")
def grade():
    if not current_rater():
        return redirect(url_for("login"))
    return render_template("grade.html", cfg=CFG, rater=current_rater())


@app.route("/api/grade/session")
def api_grade_session():
    rater = current_rater()
    if not rater:
        abort(401)
    items = grade_items_for_rater(CFG, rater)
    answers = {sid: row["label"] for sid, row in GRADES.get(rater, {}).items()}
    return jsonify(rater=rater, options=_grade_options(),
                   items=items, answers=answers)


@app.route("/api/grade/answer", methods=["POST"])
def api_grade_answer():
    rater = current_rater()
    if not rater:
        abort(401)
    data = request.get_json(force=True) or {}
    sid = data.get("slide_id")
    label = data.get("label")
    if sid not in SLIDE_INDEX or label not in _grade_options():
        abort(400)
    rec = SLIDE_INDEX[sid]
    row = {"rater": rater, "slide_id": sid,
           "slide_name": rec.get("name") or os.path.basename(rec["path"]),
           "label": label,
           "timestamp": now_iso(),
           "time_ms": int(data.get("time_ms", 0))}
    with _lock:
        GRADES.setdefault(rater, {})[sid] = row
        write_grades(rater)
    return jsonify(ok=True)


def _open_slide_or_404(sid):
    rec = SLIDE_INDEX.get(sid)
    if rec is None:
        abort(404)
    path = slide_abspath(rec)
    if not os.path.exists(path):
        abort(404)
    try:
        return rec, slides_mod.open_slide(path)
    except Exception:
        app.logger.exception("openslide failed for %r", path)
        abort(415)


@app.route("/api/grade/slide/<slide_id>")
def api_grade_slide(slide_id):
    if not current_rater() and not is_admin():
        abort(401)
    rec, entry = _open_slide_or_404(slide_id)
    meta = slides_mod.slide_metadata(
        entry, slide_id, rec.get("name") or os.path.basename(rec["path"]))
    return jsonify(meta)


@app.route("/grade/tile/<slide_id>/<int:level>/<int:col>/<int:row>.jpg")
def grade_tile(slide_id, level, col, row):
    if not current_rater() and not is_admin():
        abort(401)
    _, entry = _open_slide_or_404(slide_id)
    try:
        blob = slides_mod.render_tile(entry, slide_id, level, col, row)
    except ValueError:
        abort(404)                  # edge tile out of range — OSD tolerates
    resp = app.response_class(blob, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/admin/grade")
def admin_grade():
    if not is_admin():
        abort(401)
    opts = _grade_options()
    rows = []
    kept_ids = {s["slide_id"] for s in SLIDES["slides"]
                if s["slide_id"] not in GRADE_SKIPPED}
    total = len(kept_ids)
    for rater in CFG["raters"]:
        rg = GRADES.get(rater, {})
        # only count grades on slides still in the kept set
        done = sum(1 for sid in rg if sid in kept_ids)
        # confusion vs predicted: count pairs (predicted, label)
        cm = {(p, l): 0 for p in opts for l in opts}
        n_predicted = 0
        n_agree = 0
        for sid, row in rg.items():
            rec = SLIDE_INDEX.get(sid) or {}
            pred = (rec.get("predicted_label") or "").lower()
            label = row["label"]
            if pred in opts:
                cm[(pred, label)] = cm.get((pred, label), 0) + 1
                n_predicted += 1
                if pred == label:
                    n_agree += 1
        agreement = (100.0 * n_agree / n_predicted) if n_predicted else None
        rows.append({
            "rater": rater, "done": done, "total": total,
            "pct": round(100 * done / total, 1) if total else 0,
            "agree_n": n_agree, "agree_total": n_predicted,
            "agree_pct": round(agreement, 1) if agreement is not None else None,
            "confusion": cm,
        })
    n_predicted_slides = sum(
        1 for s in SLIDES["slides"]
        if s["slide_id"] in kept_ids and (s.get("predicted_label") or "") in opts)
    return render_template("grade_admin.html", cfg=CFG, rows=rows,
                           options=opts,
                           n_slides=total, n_predicted=n_predicted_slides,
                           n_total=SLIDES["n_slides"],
                           n_skipped=len(GRADE_SKIPPED))


@app.route("/admin/grade/<rater>.csv")
def admin_grade_csv(rater):
    if not is_admin() or rater not in CFG["raters"]:
        abort(401)
    if not os.path.exists(grade_results_path(rater)):
        return "no grading results yet", 404
    return send_file(grade_results_path(rater), as_attachment=True,
                     download_name="grading_results_%s.csv" % rater)


# ---- slide-grading curation ---------------------------------------------
def _slide_groups():
    """Return [(study_id_or_'', [slide_record, …]), …] in manifest order.
    Slides without a study_id fall into their own single-element group so
    the page works whether or not the manifest was built with study_id."""
    groups, order = {}, []
    for s in SLIDES["slides"]:
        k = s.get("study_id") or s["slide_id"]
        if k not in groups:
            order.append(k)
            groups[k] = []
        groups[k].append(s)
    return [(k, groups[k]) for k in order]


@app.route("/admin/grade/curate")
def admin_grade_curate():
    if not is_admin():
        abort(401)
    return render_template("grade_curate.html", cfg=CFG, manifest=SLIDES,
                           groups=_slide_groups())


@app.route("/admin/grade/curate/state")
def admin_grade_curate_state():
    if not is_admin():
        abort(401)
    total = len(SLIDES["slides"])
    return jsonify(skipped=sorted(GRADE_SKIPPED),
                   total=total, kept=total - len(GRADE_SKIPPED))


@app.route("/admin/grade/curate/toggle", methods=["POST"])
def admin_grade_curate_toggle():
    if not is_admin():
        abort(401)
    data = request.get_json(force=True) or {}
    sid, keep = data.get("slide_id"), bool(data.get("keep"))
    if sid not in SLIDE_INDEX:
        abort(400)
    with _lock:
        (GRADE_SKIPPED.discard if keep else GRADE_SKIPPED.add)(sid)
        save_grade_selection()
    return jsonify(ok=True, kept=len(SLIDES["slides"]) - len(GRADE_SKIPPED))


@app.route("/admin/grade/curate/bulk", methods=["POST"])
def admin_grade_curate_bulk():
    if not is_admin():
        abort(401)
    action = (request.get_json(force=True) or {}).get("action")
    with _lock:
        if action == "keep_all":
            GRADE_SKIPPED.clear()
        elif action == "skip_all":
            GRADE_SKIPPED.update(s["slide_id"] for s in SLIDES["slides"])
        elif action == "keep_first_per_case":
            # one slide per study_id — the first occurrence in manifest order
            GRADE_SKIPPED.clear()
            seen = set()
            for s in SLIDES["slides"]:
                k = s.get("study_id") or s["slide_id"]
                if k in seen:
                    GRADE_SKIPPED.add(s["slide_id"])
                else:
                    seen.add(k)
        else:
            abort(400)
        save_grade_selection()
    return jsonify(ok=True, kept=len(SLIDES["slides"]) - len(GRADE_SKIPPED))


@app.route("/grade/thumb/<slide_id>.jpg")
def grade_thumb(slide_id):
    """Slide thumbnail for the curation page. Admin-only, on-disk cached so
    a 405T CIFS share isn't re-read on every page paint."""
    if not is_admin():
        abort(401)
    try:
        size = int(request.args.get("size", 320))
    except ValueError:
        size = 320
    size = max(64, min(size, 1024))
    os.makedirs(GRADE_THUMB_DIR, exist_ok=True)
    # sanitize: slide_id is opaque but lives on disk in the cache name
    safe = "".join(c if c.isalnum() or c in "_.-" else "_" for c in slide_id)
    cache_path = os.path.join(GRADE_THUMB_DIR, "%s_w%d.jpg" % (safe, size))
    if os.path.exists(cache_path):
        return send_file(cache_path, mimetype="image/jpeg")
    rec, entry = _open_slide_or_404(slide_id)
    # Hold the per-handle lock for the whole render: get_thumbnail touches
    # libopenslide internals and must not race a close() from eviction.
    with entry["lock"]:
        img = entry["osl"].get_thumbnail((size, size))
    img.convert("RGB").save(cache_path, "JPEG", quality=82)
    return send_file(cache_path, mimetype="image/jpeg")


# --------------------------------------------------------------------------
# Superadmin data-manager (upload / organize / assign). Pass the LIVE module
# (sys.modules[__name__]) so the blueprint reads + hot-swaps THESE globals, not
# a second imported copy when app.py runs as __main__.
import datamanager
datamanager.register(app, sys.modules[__name__])


# --------------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        build_manifest(load_config(), force="--force" in sys.argv)
    elif len(sys.argv) > 1 and sys.argv[1] == "build_slides":
        if len(sys.argv) < 3:
            sys.exit("usage: python app.py build_slides <slides.csv>")
        cfg = load_config()
        sd = cfg.get("slides_dir") or ROOT
        sd = sd if os.path.isabs(sd) else os.path.join(ROOT, sd)
        m = slides_mod.build_from_csv(sys.argv[2], sd, SLIDES_MANIFEST_PATH)
        print("slides_manifest: %d slides (%d skipped)" %
              (m["n_slides"], len(m.get("skipped", []))))
        for s in m.get("skipped", []):
            print("  skipped:", s)
    else:
        port = int(os.environ.get("PORT", CFG.get("port", 8000)))
        # Bind to localhost only — cloudflared (same machine) reaches us via
        # 127.0.0.1; LAN clients are blocked from bypassing Cloudflare Access.
        print("Mitosis review running on http://127.0.0.1:%d" % port)
        app.run(host="127.0.0.1", port=port, threaded=True)
