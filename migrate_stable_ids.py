"""One-time migration: positional patch_ids (p%04d) -> content-addressed
stable ids (boxes.stable_pid of the image path).

Rewrites results/results_<rater>.csv (patch_id + item_id columns) and
results/selection.json (skipped list) so they key off the stable id instead of
the old positional id. Run ONCE, BEFORE rebuilding the manifest, with the OLD
positional manifest.json still on disk (it provides patch_id -> image for the
selection list, which — unlike the results CSVs — has no image to self-migrate
from).

Safety (every one of these is load-bearing; the original tumorbuds script had
none of them):
  * run-once guard      — refuses if a _migration_complete sentinel exists, or if
                          the artifacts already carry stable ids.
  * pre-run backup      — copies results/*.csv + selection.json + manifest.json
                          into results/_backup_premigrate_<UTC>/ before any write.
  * resumable two-pass  — the results pass is idempotent (re-derives the id from
                          each row's own image column); the selection pass is NOT
                          (depends on the OLD positional manifest) and is guarded
                          on selection.json's OWN id format, so a crash between
                          passes can be re-run safely.
  * hard abort          — if the manifest is already stable but selection is still
                          positional (old map gone), refuses and points at backup.
  * verification        — asserts every migrated row is self-consistent.
  * --dry-run           — report what would change, write nothing.
  * --root <dir>        — operate on a copy of a deployment (for dry-runs / tests).

Usage:
    python migrate_stable_ids.py --dry-run            # report, write nothing
    python migrate_stable_ids.py                      # migrate in place
    python migrate_stable_ids.py --root /tmp/copy     # migrate a copy
"""
import csv
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone

import boxes

POSITIONAL = re.compile(r"^p[0-9]{4}$")
STABLE = re.compile(r"^p[0-9a-f]{12}$")
SENTINEL = "_migration_complete"


def _ts():
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _csv_files(res_dir):
    return [f for f in sorted(os.listdir(res_dir))
            if f.startswith("results_") and f.endswith(".csv")]


def _sniff_csv_ids(res_dir):
    """Return ('positional'|'stable'|'mixed'|'empty') across all results rows."""
    seen = set()
    for fn in _csv_files(res_dir):
        with open(os.path.join(res_dir, fn), newline="") as fh:
            for row in csv.DictReader(fh):
                pid = row.get("patch_id", "")
                if POSITIONAL.match(pid):
                    seen.add("positional")
                elif STABLE.match(pid):
                    seen.add("stable")
    if not seen:
        return "empty"
    if len(seen) == 2:
        return "mixed"
    return seen.pop()


def _sniff_list_ids(ids):
    seen = set()
    for pid in ids:
        if POSITIONAL.match(pid):
            seen.add("positional")
        elif STABLE.match(pid):
            seen.add("stable")
    if not seen:
        return "empty"
    if len(seen) == 2:
        return "mixed"
    return seen.pop()


def migrate(root, dry_run=False):
    res_dir = os.path.join(root, "results")
    manifest_path = os.path.join(root, "manifest.json")
    sel_path = os.path.join(res_dir, "selection.json")
    sentinel_path = os.path.join(res_dir, SENTINEL)

    def log(msg):
        print(("[dry-run] " if dry_run else "") + msg)

    if not os.path.isdir(res_dir):
        sys.exit("no results/ dir under %s" % root)

    # ---- run-once guard --------------------------------------------------
    if os.path.exists(sentinel_path):
        sys.exit("ABORT: %s exists — migration already completed for %s"
                 % (SENTINEL, root))

    csv_state = _sniff_csv_ids(res_dir)
    manifest = json.load(open(manifest_path)) if os.path.exists(manifest_path) else {"patches": []}
    manifest_ids = [p["patch_id"] for p in manifest["patches"]]
    manifest_state = _sniff_list_ids(manifest_ids)
    sel = json.load(open(sel_path)) if os.path.exists(sel_path) else {"skipped": []}
    sel_state = _sniff_list_ids(sel.get("skipped", []))

    log("csv ids: %s | manifest ids: %s | selection ids: %s"
        % (csv_state, manifest_state, sel_state))

    if csv_state == "stable" and sel_state in ("stable", "empty"):
        sys.exit("ABORT: results + selection already on stable ids — nothing to do "
                 "(write the %s sentinel manually if you want to silence this)." % SENTINEL)

    # the selection pass needs the OLD positional manifest to map positional ids.
    if sel_state == "positional" and manifest_state == "stable":
        sys.exit("ABORT: selection.json is positional but manifest.json is already "
                 "stable — the position->image map is gone, so the curation list "
                 "cannot be remapped. Restore manifest.json from a pre-rebuild "
                 "backup, then re-run.")

    old2img = {p["patch_id"]: p["image"] for p in manifest["patches"]}

    # ---- backup ----------------------------------------------------------
    if not dry_run:
        bdir = os.path.join(res_dir, "_backup_premigrate_" + _ts())
        os.makedirs(bdir, exist_ok=True)
        for fn in _csv_files(res_dir):
            shutil.copy2(os.path.join(res_dir, fn), os.path.join(bdir, fn))
        if os.path.exists(sel_path):
            shutil.copy2(sel_path, os.path.join(bdir, "selection.json"))
        if os.path.exists(manifest_path):
            shutil.copy2(manifest_path, os.path.join(bdir, "manifest.json"))
        log("backed up results + selection + manifest -> %s" % bdir)

    # ---- pass 1: results CSVs (idempotent, keyed off each row's image) ----
    for fn in _csv_files(res_dir):
        path = os.path.join(res_dir, fn)
        rows = list(csv.DictReader(open(path, newline="")))
        if not rows:
            log("%s: empty, skipped" % fn)
            continue
        fields = list(rows[0].keys())
        changed = 0
        for r in rows:
            pid = boxes.stable_pid(r["image"])
            new_item = "%s_d%s" % (pid, r["det_index"])
            if r["patch_id"] != pid or r["item_id"] != new_item:
                changed += 1
            r["patch_id"] = pid
            r["item_id"] = new_item
        # verify self-consistency
        for r in rows:
            assert r["patch_id"] == boxes.stable_pid(r["image"]), fn
            assert r["item_id"] == "%s_d%s" % (r["patch_id"], r["det_index"]), fn
            assert not POSITIONAL.match(r["patch_id"]), fn
        if not dry_run:
            with open(path, "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=fields)
                w.writeheader()
                w.writerows(rows)
        log("%s: %d rows (%d rewritten)" % (fn, len(rows), changed))

    # ---- pass 2: selection.json (NON-idempotent; guard on its own format) -
    if os.path.exists(sel_path):
        if sel_state == "stable":
            log("selection.json: already stable, skipped")
        elif sel_state == "empty":
            log("selection.json: empty skip list, nothing to map")
        else:  # positional (manifest confirmed positional above)
            old_sk = sel.get("skipped", [])
            new_sk, miss = [], 0
            for pid in old_sk:
                img = old2img.get(pid)
                if img is None:
                    miss += 1
                    continue
                new_sk.append(boxes.stable_pid(img))
            sel["skipped"] = sorted(set(new_sk))
            if not dry_run:
                with open(sel_path, "w") as fh:
                    json.dump(sel, fh, indent=1)
            log("selection: %d -> %d skipped (%d unmapped%s)"
                % (len(old_sk), len(sel["skipped"]), miss,
                   " — REVIEW: those curation decisions could not be carried over"
                   if miss else ""))

    # ---- sentinel --------------------------------------------------------
    if not dry_run:
        with open(sentinel_path, "w") as fh:
            fh.write(_ts() + "\n")
        log("wrote %s — next step: rebuild the manifest (python app.py build), "
            "then restart the app." % SENTINEL)
    else:
        log("dry-run complete — no files written. Re-run without --dry-run to apply.")


if __name__ == "__main__":
    args = sys.argv[1:]
    dry = "--dry-run" in args
    root = os.path.dirname(os.path.abspath(__file__))
    if "--root" in args:
        root = args[args.index("--root") + 1]
    migrate(root, dry_run=dry)
