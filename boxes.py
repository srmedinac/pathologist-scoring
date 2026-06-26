"""Detection sources for review patches.

A "detection" is an axis-aligned box [x, y, w, h] in image pixels.

Priority (detection_mode == "auto"):
  1. a global coordinates CSV (config "coords_csv")
  2. a sibling YOLO .txt file  (same basename as the image)
  3. a sibling .json sidecar   (same basename as the image)
  4. green-box auto-detection straight from the patch image

The green-box detector is a best-effort fallback for patches that only have
the boxes drawn on. Verify it on real patches with the /preview page before a
study, and tune the "green" block in config.json if needed.
"""

import csv
import json
import os

import cv2
import numpy as np


# --------------------------------------------------------------------------
# image loading
# --------------------------------------------------------------------------
def read_image(path):
    """Return a BGR uint8 image, or None if it cannot be read."""
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is not None:
        return img
    try:  # fallback for less common formats / multi-page tiff
        from PIL import Image
        pil = Image.open(path).convert("RGB")
        return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    except Exception:
        return None


# --------------------------------------------------------------------------
# green-box auto-detection
# --------------------------------------------------------------------------
def _iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    return inter / float(aw * ah + bw * bh - inter)


def _merge(boxes, thr):
    boxes = [list(b) for b in boxes]
    changed = True
    while changed:
        changed = False
        out, used = [], [False] * len(boxes)
        for i in range(len(boxes)):
            if used[i]:
                continue
            cur = boxes[i]
            for j in range(i + 1, len(boxes)):
                if used[j]:
                    continue
                if _iou(cur, boxes[j]) > thr:
                    x = min(cur[0], boxes[j][0])
                    y = min(cur[1], boxes[j][1])
                    x2 = max(cur[0] + cur[2], boxes[j][0] + boxes[j][2])
                    y2 = max(cur[1] + cur[3], boxes[j][1] + boxes[j][3])
                    cur = [x, y, x2 - x, y2 - y]
                    used[j] = True
                    changed = True
            used[i] = True
            out.append(cur)
        boxes = out
    return boxes


def detect_green_boxes(img, g):
    """Find green rectangle outlines drawn on a patch.

    Each box is the outer contour of a green ring: a rectangular shape
    (high "rectangularity") with a mostly-empty interior. That pair of
    tests separates a detection outline from a solid green label tag
    (full interior) and from text / noise (not rectangular).

    Known limitation: two genuinely overlapping outlines merge into one
    non-rectangular contour and are dropped -- rare for post-NMS detector
    output. Verify on real patches via /preview; use coordinates if wrong.
    """
    h, w = img.shape[:2]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(
        hsv,
        np.array([g["hue_min"], g["sat_min"], g["val_min"]], np.uint8),
        np.array([g["hue_max"], 255, 255], np.uint8),
    )
    if g.get("close_iter", 0):                           # bridge tiny gaps only
        mask = cv2.morphologyEx(
            mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8),
            iterations=g["close_iter"],
        )
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    min_px = g["min_box_px"]
    max_w, max_h = w * g["max_box_frac"], h * g["max_box_frac"]
    for c in contours:
        x, y, bw, bh = cv2.boundingRect(c)
        if bw < min_px or bh < min_px or bw > max_w or bh > max_h:
            continue
        if max(bw / bh, bh / bw) > g["max_aspect"]:
            continue
        if cv2.contourArea(c) / float(bw * bh) < g["min_rectangularity"]:
            continue                                     # not a rectangle
        m = max(3, int(round(min(bw, bh) * 0.18)))
        inner = mask[y + m:y + bh - m, x + m:x + bw - m]
        if inner.size and (inner > 0).mean() > g["max_inner_fill"]:
            continue                                     # solid -> label tag
        boxes.append([int(x), int(y), int(bw), int(bh)])

    boxes = _merge(boxes, g["merge_iou"])
    boxes.sort(key=lambda b: (b[1], b[0]))
    return boxes


# --------------------------------------------------------------------------
# coordinate files
# --------------------------------------------------------------------------
def _to_xywh(vals, w, h):
    """Convert one detection record to pixel [x, y, w, h]."""
    a, b, c, d = [float(v) for v in vals[:4]]
    normalized = max(a, b, c, d) <= 1.5
    if normalized:
        a, c = a * w, c * w
        b, d = b * h, d * h
    # heuristic: (cx, cy, w, h) if the box stays inside the image, else corners
    if c <= w and d <= h and a - c / 2 >= -1 and b - d / 2 >= -1:
        return [a - c / 2, b - d / 2, c, d]          # centre form
    if c > a and d > b:
        return [a, b, c - a, d - b]                  # x1,y1,x2,y2 form
    return [a, b, c, d]                              # already x,y,w,h


def read_yolo_txt(txt_path, w, h):
    """Axis-aligned boxes [x, y, w, h] in pixels from a YOLO label file.

    Handles two normalized formats per line, distinguished by column count:
      * plain detection  : cls cx cy w h [conf]           (4 or 5 values after cls)
      * oriented/polygon : cls x1 y1 x2 y2 ... [conf]      (>=6 values, an even
        number of point coords, with an optional trailing conf making it odd)
    For polygons/oriented boxes we take the axis-aligned bounding box of the
    points — that is what the review highlight draws."""
    out = []
    with open(txt_path) as fh:
        for line in fh:
            p = line.split()
            if len(p) < 5:
                continue
            vals = [float(v) for v in p[1:]]
            n = len(vals)
            if n in (4, 5):                          # cls cx cy w h [conf]
                cx, cy, bw, bh = vals[:4]
                out.append([(cx - bw / 2) * w, (cy - bh / 2) * h, bw * w, bh * h])
            else:                                    # polygon / OBB -> AABB
                pts = vals[:-1] if n % 2 else vals   # drop trailing conf if odd
                xs, ys = pts[0::2], pts[1::2]
                if not xs or not ys:
                    continue
                x0, y0 = min(xs) * w, min(ys) * h
                out.append([x0, y0, (max(xs) - min(xs)) * w, (max(ys) - min(ys)) * h])
    return [[int(v) for v in b] for b in out]


def stable_pid(image_rel):
    """Deterministic patch id from a patch's relative image path.

    Content-addressed (not positional) so rebuilding the manifest after adding
    new slides/cohorts never renumbers existing patches — prior raters' results
    and the curation selection stay valid across rebuilds."""
    import hashlib
    norm = image_rel.replace(os.sep, "/")
    return "p" + hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


def read_json_sidecar(json_path, w, h):
    with open(json_path) as fh:
        data = json.load(fh)
    recs = data.get("boxes", data) if isinstance(data, dict) else data
    out = []
    for r in recs:
        if isinstance(r, dict):
            vals = [r.get(k) for k in ("x", "y", "w", "h")]
            if None in vals:
                vals = [r.get(k) for k in ("x1", "y1", "x2", "y2")]
        else:
            vals = r
        out.append([int(v) for v in _to_xywh(vals, w, h)])
    return out


def read_paired_csv(path):
    """Read one per-tile detection CSV from a parallel coordinates tree.

    Expected columns: top_left_x, top_left_y, bottom_right_x, bottom_right_y
    (pixel coordinates within the tile). Falls back to the first four columns
    if those headers are absent. Returns a list of [x, y, w, h].
    """
    out = []
    keys = ("top_left_x", "top_left_y", "bottom_right_x", "bottom_right_y")
    try:
        with open(path, newline="") as fh:
            reader = csv.DictReader(fh)
            cols = {c.lower().strip(): c for c in (reader.fieldnames or [])}
            for row in reader:
                try:
                    if all(k in cols for k in keys):
                        x1, y1, x2, y2 = (float(row[cols[k]]) for k in keys)
                    else:
                        x1, y1, x2, y2 = (float(v) for v in list(row.values())[:4])
                except (ValueError, TypeError):
                    continue
                x, y = min(x1, x2), min(y1, y2)
                w, h = abs(x2 - x1), abs(y2 - y1)
                if w >= 1 and h >= 1:
                    out.append([int(round(x)), int(round(y)),
                                int(round(w)), int(round(h))])
    except (OSError, csv.Error):
        pass
    return out


def build_coord_index(patches_dir, cfg):
    """Parse a global coordinates CSV into {image_relpath_lower: [raw_rows]}."""
    path = cfg.get("coords_csv", "")
    if not path:
        return {}
    if not os.path.isabs(path):
        path = os.path.join(patches_dir, path) if not os.path.exists(path) else path
    if not os.path.exists(path):
        return {}
    index = {}
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        cols = {c.lower(): c for c in (reader.fieldnames or [])}

        def pick(*names):
            for nm in names:
                if nm in cols:
                    return cols[nm]
            return None

        img_c = pick("image", "filename", "file", "patch", "name")
        quad = [pick("x", "xmin", "x1"), pick("y", "ymin", "y1"),
                pick("w", "width", "x2", "xmax"), pick("h", "height", "y2", "ymax")]
        for row in reader:
            if not img_c or None in quad:
                break
            key = os.path.basename(str(row[img_c])).lower()
            index.setdefault(key, []).append([row[c] for c in quad])
    return index


def yolo_subdir_path(patches_dir, fullpath, labels_subdir):
    """For a patch at <patches_dir>/<slide>/.../<stem>.<ext>, return the
    YOLO label path <patches_dir>/<slide>/<labels_subdir>/<stem>.txt.

    Matches the conventional YOLO layout where each slide folder has a
    sibling labels/ subdir; works whether patches live directly under the
    slide folder or one level deeper (e.g. <slide>/images/<patch>.jpeg)."""
    rel = os.path.relpath(fullpath, patches_dir)
    parts = rel.split(os.sep)
    if len(parts) < 2:
        return None
    stem = os.path.splitext(parts[-1])[0]
    return os.path.join(patches_dir, parts[0], labels_subdir, stem + ".txt")


def detections_for_image(relpath, fullpath, img, cfg, coord_index):
    """Return a list of [x, y, w, h] boxes for one patch image."""
    h, w = img.shape[:2]
    mode = cfg.get("detection_mode", "auto")
    if mode == "whole_patch":
        return []  # caller substitutes a single None detection

    if mode == "yolo_subdir":
        pdir = cfg.get("_patches_dir_abs")        # set by build_manifest
        sub = cfg.get("labels_subdir", "labels")
        tx = yolo_subdir_path(pdir, fullpath, sub) if pdir else None
        if tx and os.path.exists(tx):
            return read_yolo_txt(tx, w, h)
        return []

    if mode in ("auto", "coords"):
        key = os.path.basename(relpath).lower()
        if key in coord_index:
            return [[int(v) for v in _to_xywh(r, w, h)] for r in coord_index[key]]
        stem = os.path.splitext(fullpath)[0]
        if os.path.exists(stem + ".txt"):
            return read_yolo_txt(stem + ".txt", w, h)
        if os.path.exists(stem + ".json"):
            return read_json_sidecar(stem + ".json", w, h)
        if mode == "coords":
            return []

    return detect_green_boxes(img, cfg["green"])
