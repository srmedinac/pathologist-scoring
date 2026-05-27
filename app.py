"""Mitosis detection review tool.

Usage:
    python app.py build [--force]   # scan patches, sample, build manifest.json
    python app.py                   # run the review server

Pathologists open the URL, pick their name, and label each detection.
Results are written to results/results_<rater>.csv .
"""

import csv
import io
import json
import os
import random
import sys
import threading
import time
from datetime import datetime, timezone

import cv2
from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   send_file, session, url_for)

import boxes

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
MANIFEST_PATH = os.path.join(ROOT, "manifest.json")
RESULTS_DIR = os.path.join(ROOT, "results")
CACHE_DIR = os.path.join(ROOT, "web_cache")
THUMB_CACHE_DIR = os.path.join(ROOT, "thumb_cache")
SELECTION_PATH = os.path.join(RESULTS_DIR, "selection.json")

CSV_FIELDS = ["rater", "item_id", "patch_id", "image", "det_index",
              "x", "y", "w", "h", "label", "timestamp", "time_ms"]
WEB_NATIVE = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

_lock = threading.Lock()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_config():
    with open(CONFIG_PATH) as fh:
        return json.load(fh)


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


def build_manifest(cfg, force=False):
    """Sample n_patches tiles and resolve their detections. Run once; the
    saved manifest fixes the exact same set + boxes for every rater."""
    pdir = _abs(cfg, "patches_dir")
    if not os.path.isdir(pdir):
        sys.exit("patches_dir not found: %s" % pdir)
    mode = cfg.get("detection_mode")

    cands = _list_candidates(cfg, pdir)
    cands.sort()                                     # deterministic across rebuilds
    random.Random(cfg["seed"]).shuffle(cands)

    coord_index = {} if mode == "paired_csv" else boxes.build_coord_index(pdir, cfg)
    target = cfg["n_patches"]
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
        patches.append({
            "patch_id": "p%04d" % (len(patches) + 1),
            "image": img_rel.replace(os.sep, "/"),
            "w": w, "h": h,
            "detections": [
                {"det_index": k,
                 "bbox": None if d is None else _clamp_box(d, w, h)}
                for k, d in enumerate(dets)],
        })

    manifest = {
        "created": now_iso(), "seed": cfg["seed"],
        "n_requested": target, "n_patches": len(patches),
        "n_detections": sum(len(p["detections"]) for p in patches),
        "detection_mode": mode,
        "candidates_scanned": len(cands), "skipped": skipped,
        "patches": patches,
    }
    with open(MANIFEST_PATH, "w") as fh:
        json.dump(manifest, fh, indent=1)
    print("manifest: %d tiles, %d detections (scanned %d candidates, "
          "skipped %d with no image / no boxes)"
          % (manifest["n_patches"], manifest["n_detections"],
             len(cands), skipped))
    return manifest


def load_manifest():
    with open(MANIFEST_PATH) as fh:
        return json.load(fh)


# --------------------------------------------------------------------------
# review-item ordering
# --------------------------------------------------------------------------
def rater_items(manifest, cfg, rater):
    """Flat, per-rater-ordered list of review items. Same patches & boxes for
    everyone; patch order is shuffled per rater to remove order bias.
    Patches in SKIPPED (admin curation) are filtered out before shuffling."""
    order = [i for i, p in enumerate(manifest["patches"])
             if p["patch_id"] not in SKIPPED]
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
# app
# --------------------------------------------------------------------------
CFG = load_config()
app = Flask(__name__)
app.secret_key = CFG.get("secret_key", "dev-secret")

if not os.path.exists(MANIFEST_PATH):
    print("No manifest.json found - run:  python app.py build")
    MANIFEST = {"patches": [], "n_patches": 0, "n_detections": 0}
else:
    MANIFEST = load_manifest()
ITEM_INDEX = {}  # item_id -> patch + detection info
for _p in MANIFEST["patches"]:
    for _d in _p["detections"]:
        ITEM_INDEX["%s_d%d" % (_p["patch_id"], _d["det_index"])] = {
            "patch_id": _p["patch_id"], "image": _p["image"],
            "det_index": _d["det_index"], "bbox": _d["bbox"]}
load_results(CFG)
load_selection()


def patches_root():
    pdir = CFG["patches_dir"]
    return pdir if os.path.isabs(pdir) else os.path.join(ROOT, pdir)


def current_rater():
    return session.get("rater")


def is_admin():
    return request.args.get("key") == CFG.get("admin_password")


# ---- auth ----------------------------------------------------------------
@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        rater = request.form.get("rater", "").strip()
        pw = request.form.get("password", "")
        if pw != CFG.get("access_password"):
            return render_template("login.html", cfg=CFG, error="Wrong access code.")
        if rater not in CFG["raters"]:
            return render_template("login.html", cfg=CFG, error="Pick your name.")
        session["rater"] = rater
        return redirect(url_for("review"))
    if current_rater():
        return redirect(url_for("review"))
    return render_template("login.html", cfg=CFG, error=None)


@app.route("/logout")
def logout():
    session.pop("rater", None)
    return redirect(url_for("login"))


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
    if item_id not in ITEM_INDEX or label not in CFG["answer_options"]:
        abort(400)
    info = ITEM_INDEX[item_id]
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
    rows = []
    for rater in CFG["raters"]:
        raw = RESULTS.get(rater, {})
        done = sum(1 for r in raw.values() if r["patch_id"] in kept_ids)
        rows.append({"rater": rater, "done": done, "total": total_dets,
                     "pct": round(100 * done / total_dets, 1) if total_dets else 0})
    return render_template("admin.html", cfg=CFG, rows=rows,
                           manifest=MANIFEST,
                           kept_count=len(kept), skipped_count=len(SKIPPED),
                           total_dets=total_dets,
                           key=CFG.get("admin_password"))


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
    return render_template("preview.html", cfg=CFG, manifest=MANIFEST,
                           key=CFG.get("admin_password"))


@app.route("/preview_img/<patch_id>")
def preview_img(patch_id):
    """Patch with every detection drawn + numbered. ?w= sets the max-side
    target (160-1200). Cached to thumb_cache/ so subsequent loads are instant."""
    if not is_admin():
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
    if not is_admin():
        abort(401)
    return render_template("curate.html", cfg=CFG, manifest=MANIFEST,
                           key=CFG.get("admin_password"))


@app.route("/admin/curate/state")
def admin_curate_state():
    if not is_admin():
        abort(401)
    return jsonify(skipped=sorted(SKIPPED),
                   total=len(MANIFEST["patches"]),
                   kept=len(MANIFEST["patches"]) - len(SKIPPED))


@app.route("/admin/curate/toggle", methods=["POST"])
def admin_curate_toggle():
    if not is_admin():
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
    if not is_admin():
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
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        build_manifest(load_config(), force="--force" in sys.argv)
    else:
        port = int(os.environ.get("PORT", CFG.get("port", 8000)))
        print("Mitosis review running on http://0.0.0.0:%d" % port)
        app.run(host="0.0.0.0", port=port, threaded=True)
