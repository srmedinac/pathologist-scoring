"""Slide-level grading: open WSIs and serve DeepZoom tiles.

This is the WSI side of the mitosis app (the /grade interface, distinct from
the /review patch interface). It exists so pathologists can assign one
slide-level label per case ("mitosis high" vs "mitosis low") next to their
patch-level work, with results aggregated alongside the patch results.

Tile serving mirrors the wsi-viewer's pattern: OpenSlide + DeepZoomGenerator,
per-slide handle cache (DeepZoomGenerator.get_tile is not thread-safe so each
handle carries its own lock), in-memory tile cache.

A "slide" in the grading manifest is identified by a stable slide_id picked
by whoever built the manifest; paths are resolved through slides_dir at open
time and never sent to the client.
"""

import io
import json
import os
import threading
from collections import OrderedDict

from openslide import OpenSlide, OpenSlideError
import openslide
from openslide.deepzoom import DeepZoomGenerator


# DeepZoom tile geometry — OSD-friendly defaults, matches the wsi-viewer so a
# rater's browser will warm-cache cleanly if they cross-navigate.
TILE_SIZE = 254
TILE_OVERLAP = 1
TILE_QUALITY = 80

# Cache sizes: each open slide holds one OS file handle + mmap. Opening an
# NDPI over CIFS is the dominant cost (handful of seconds, cold), so make
# the cache large enough that the WHOLE study stays hot once touched —
# eviction triggers a close() on the OpenSlide handle, and libopenslide
# 3.4.x's hamamatsu reader has a known race (jpeg_do_destroy assertion +
# g_mutex_clear on locked mutex) when close() runs while another thread
# is mid-tile-or-thumbnail. Big-enough cache → eviction never happens
# during normal use. Tiles are ~10-50KB; 2048 ≈ 60 MB max.
HANDLE_CACHE_SIZE = 150
TILE_CACHE_SIZE = 2048


# --------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------
def manifest_paths(root):
    return os.path.join(root, "slides_manifest.json")


def load_manifest(path):
    """Return {"slides": [...], "created": ..., "n_slides": ...} or empty."""
    if not os.path.exists(path):
        return {"slides": [], "n_slides": 0, "created": None}
    with open(path) as fh:
        m = json.load(fh)
    m.setdefault("slides", [])
    m["n_slides"] = len(m["slides"])
    return m


def resolve_slide_path(rec, slides_dir):
    """Absolute path on disk for a manifest record. rec['path'] may be
    absolute or relative to slides_dir."""
    p = rec["path"]
    return p if os.path.isabs(p) else os.path.join(slides_dir, p)


# --------------------------------------------------------------------------
# caches
# --------------------------------------------------------------------------
class HandleCache:
    """abspath -> {osl, dz, lock}. LRU eviction; double-open race tolerated."""

    def __init__(self, capacity):
        self.capacity = capacity
        self._d = OrderedDict()
        self._guard = threading.Lock()

    def get(self, path):
        key = str(path)
        with self._guard:
            entry = self._d.get(key)
            if entry is not None:
                self._d.move_to_end(key)
                return entry
        # open outside the guard — opening a slide does file I/O
        osl = OpenSlide(str(path))
        dz = DeepZoomGenerator(osl, tile_size=TILE_SIZE,
                               overlap=TILE_OVERLAP, limit_bounds=True)
        entry = {"osl": osl, "dz": dz, "lock": threading.Lock()}
        evicted = None
        with self._guard:
            if key in self._d:
                osl.close()
                self._d.move_to_end(key)
                return self._d[key]
            self._d[key] = entry
            if len(self._d) > self.capacity:
                _, evicted = self._d.popitem(last=False)
        # Close the evicted handle while holding its per-entry lock —
        # otherwise we race a tile/thumbnail render in another thread and
        # libopenslide 3.4.x's hamamatsu vendor reader crashes the process
        # ("jpeg_do_destroy: restart_marker_users == 0" + mutex assertion).
        if evicted is not None:
            try:
                with evicted["lock"]:
                    evicted["osl"].close()
            except Exception:
                pass
        return entry


class TileCache:
    def __init__(self, capacity):
        self.capacity = capacity
        self._d = OrderedDict()
        self._guard = threading.Lock()

    def get(self, key):
        with self._guard:
            val = self._d.get(key)
            if val is not None:
                self._d.move_to_end(key)
            return val

    def put(self, key, val):
        with self._guard:
            self._d[key] = val
            self._d.move_to_end(key)
            if len(self._d) > self.capacity:
                self._d.popitem(last=False)


# Module-level singletons. Reset on app reload.
handles = HandleCache(HANDLE_CACHE_SIZE)
tiles = TileCache(TILE_CACHE_SIZE)


# --------------------------------------------------------------------------
# slide ops
# --------------------------------------------------------------------------
def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def open_slide(path):
    """Returns the cached handle entry, or raises OpenSlideError / OSError.

    Caller is responsible for catching and turning into HTTP errors."""
    return handles.get(path)


def slide_metadata(entry, slide_id, name):
    osl, dz = entry["osl"], entry["dz"]
    # Reading .properties / .level_dimensions is cheap but still touches the
    # underlying handle — hold the per-entry lock so we don't race a close()
    # on a parallel eviction.
    with entry["lock"]:
        props = dict(osl.properties)        # snapshot under lock
        w, h = dz.level_dimensions[-1]
    return {
        "slide_id": slide_id,
        "name": name,
        "width": w, "height": h,
        "tile_size": TILE_SIZE,
        "tile_overlap": TILE_OVERLAP,
        "levels": dz.level_count,
        "mpp_x": _f(props.get(openslide.PROPERTY_NAME_MPP_X)),
        "mpp_y": _f(props.get(openslide.PROPERTY_NAME_MPP_Y)),
        "objective": _f(props.get(openslide.PROPERTY_NAME_OBJECTIVE_POWER)),
        "vendor": props.get(openslide.PROPERTY_NAME_VENDOR),
    }


def render_tile(entry, slide_id, level, col, row):
    """Return JPEG bytes for a (level, col, row) tile. Caches on hit."""
    key = (slide_id, level, col, row)
    cached = tiles.get(key)
    if cached is not None:
        return cached
    dz, lock = entry["dz"], entry["lock"]
    with lock:
        img = dz.get_tile(level, (col, row))   # raises ValueError if OOB
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=TILE_QUALITY)
    blob = buf.getvalue()
    tiles.put(key, blob)
    return blob


# --------------------------------------------------------------------------
# build a manifest from a CSV
# --------------------------------------------------------------------------
def build_from_csv(csv_path, slides_dir, out_path, validate=True):
    """Read slides.csv (slide_id,path,predicted_label) and write slides_manifest.json.

    Each row's path is resolved against slides_dir; with validate=True the file is
    opened with OpenSlide so a bad path is caught early. Returns the manifest dict.
    """
    import csv as _csv
    from datetime import datetime, timezone

    rows = []
    seen = set()
    skipped = []
    with open(csv_path, newline="") as fh:
        reader = _csv.DictReader(fh)
        for line in reader:
            sid = (line.get("slide_id") or "").strip()
            path = (line.get("path") or "").strip()
            pred = (line.get("predicted_label") or "").strip().lower() or None
            if not sid or not path:
                skipped.append({"reason": "missing slide_id or path", "row": line})
                continue
            if sid in seen:
                skipped.append({"reason": "duplicate slide_id", "slide_id": sid})
                continue
            seen.add(sid)
            full = path if os.path.isabs(path) else os.path.join(slides_dir, path)
            if not os.path.exists(full):
                skipped.append({"reason": "file not found", "slide_id": sid,
                                "path": full})
                continue
            if validate:
                try:
                    OpenSlide(full).close()
                except (OpenSlideError, OSError) as e:
                    skipped.append({"reason": "openslide error: %s" % e,
                                    "slide_id": sid, "path": full})
                    continue
            rec = {"slide_id": sid, "path": path,
                   "predicted_label": pred,
                   "name": os.path.basename(path)}
            # opaque extras kept verbatim — lets callers tag rows with e.g.
            # a case/study_id for curation grouping without touching code here
            study_id = (line.get("study_id") or "").strip()
            if study_id:
                rec["study_id"] = study_id
            rows.append(rec)

    manifest = {
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_slides": len(rows),
        "slides": rows,
        "skipped": skipped,
    }
    tmp = out_path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(manifest, fh, indent=1)
    os.replace(tmp, out_path)
    return manifest
