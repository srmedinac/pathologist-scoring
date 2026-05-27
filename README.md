# pathologist-scoring

A small web app for 2–3 reviewers to score detections from a **YOLO-style
object detector** on image patches, one detection at a time. Built for
pathology (confirming mitosis detections on whole-slide-image tiles) but
works for any *patches + per-image bounding boxes* review study.

Reviewers click **Yes / No / Unsure** (mouse or keyboard). Per-reviewer
CSVs feed straight into inter-rater κ and model-precision analysis.

## What you get

- **Same fixed sample** for every reviewer (random with a fixed seed) +
  per-reviewer patch-order shuffle to remove order bias.
- **Resume** — answers save on every click; close the browser, come back.
- **Admin curation page** — browse the candidate pool as thumbnails with
  a hover-magnifier, click to exclude bad tiles; reviewers see only the
  kept set.
- Four ways to source detection boxes (see *Detection modes* below):
  - `paired_csv` — one CSV per image in a parallel folder tree
    (e.g. ultralytics inference output)
  - `coords` — sibling YOLO `.txt`/`.json` files or one global CSV
  - `green` — auto-extract green boxes already drawn on the image
  - `whole_patch` — single Yes/No per image, no box highlight
- `analyze.py` — pairwise Cohen's κ, Fleiss' κ, percent agreement, and
  majority-vote model precision; merged wide CSV.

## Quick start

```bash
git clone https://github.com/srmedinac/pathologist-scoring.git
cd pathologist-scoring
pip install -r requirements.txt

# 1. configure -- copy and edit
cp config.example.json config.json
#    set: raters, access_password, admin_password, secret_key,
#         patches_dir, detection_mode (+ mode-specific fields)

# 2. (optional) try with synthetic data first to verify the install
python make_test_data.py     # writes 30 fake patches into data/patches/
python app.py build          # sample n_patches, build manifest.json

# 3. inspect the boxes before sharing
python app.py
#  → http://localhost:8000/preview?key=<admin_password>

# 4. curate (optional): trim the candidate pool to the tiles you want
#  → http://localhost:8000/admin/curate?key=<admin_password>

# 5. share the URL — reviewers pick their name and enter access_password
```

## Detection modes

Set `"detection_mode"` in `config.json`.

| mode | use it when | extra config |
|---|---|---|
| `paired_csv` | YOLO inference output is a parallel folder tree, one CSV per image | `coords_root`, `tiles_subdir` |
| `auto` | Try coords files first, fall back to green-box detection | `coords_csv` (optional) |
| `coords` | One CSV of all boxes, or sibling `.txt`/`.json` per image | `coords_csv` (optional) |
| `green` | Patches already have green boxes drawn on them; no coords available | tune the `green` block, **verify on `/preview`** |
| `whole_patch` | One Yes/No per whole image, no box highlight | — |

### `paired_csv` layout

```
patches_dir/
  <slide_id>/
    <tiles_subdir>/
      <tile_name>.jpeg

coords_root/
  <slide_id>/
    <tile_name>.jpeg.csv      # detections for that tile
```

Each per-tile CSV has columns `top_left_x, top_left_y, bottom_right_x,
bottom_right_y` (pixel coordinates within the tile). Tiles without a CSV
or with a header-only CSV are skipped during sampling.

### YOLO / coords / green

- **YOLO `.txt`** — one file per image, same basename, lines
  `class cx cy w h` (normalized).
- **Generic CSV** — set `coords_csv`. The reader picks up `image`/
  `filename` + `x,y,w,h` or `x1,y1,x2,y2` columns automatically.
- **Green-box auto-detection** — works on baked-in outlines, *not* solid
  label tags. Always preview before relying on it; tune `hue_*`,
  `min_box_px`, `max_inner_fill`, `min_rectangularity`, etc.

## Curation

`http://<host>:<port>/admin/curate?key=<admin_password>`

- Thumbnail grid of every patch in the manifest with detection boxes
  drawn and numbered.
- **Hover** any tile → big magnifier popup follows the cursor for a
  closer look (with the boxes still on it).
- **Click** a tile to *skip* it (grayscale + red `SKIPPED`). Click again
  to keep.
- Saves automatically to `results/selection.json`. Reviewers see only
  kept tiles.
- "Keep all" / "Skip all" buttons for bulk reset.
- The counter turns green when you're in the 150–250 range — a hint when
  you're at typical study size.

## Results and analysis

Each reviewer writes one row per detection to
`results/results_<rater>.csv`:

```
rater, item_id, patch_id, image, det_index,
x, y, w, h, label, timestamp, time_ms
```

After collecting, run:

```bash
python analyze.py
```

Outputs `results/summary.txt` (κ + precision report) and
`results/merged.csv` (one row per detection, one column per rater for
easy spreadsheet work).

## Configuration reference

| key | meaning |
|---|---|
| `study_title`, `instructions` | shown on the login and review screens |
| `patches_dir` | folder with the patch images (recursive) |
| `detection_mode` | one of `paired_csv` / `auto` / `coords` / `green` / `whole_patch` |
| `coords_root`, `tiles_subdir` | (paired_csv) parallel CSV tree + per-slide images subdir |
| `coords_csv` | (auto/coords) single CSV of detections |
| `green` | (green/auto) HSV + shape thresholds — verify in `/preview` |
| `n_patches` | candidate-pool size (typical: 300–1000; curate down later) |
| `seed` | fixes which tiles are sampled — keep stable for reproducibility |
| `raters` | exact reviewer names shown on login (case- and spelling-sensitive) |
| `answer_options` | typically `["yes","no","unsure"]` |
| `shuffle_per_rater` | per-reviewer patch-order shuffle to remove order bias |
| `access_password` | reviewer login |
| `admin_password` | `/admin`, `/preview`, `/admin/curate` (via `?key=…`) |
| `secret_key` | Flask session key — generate one: `python -c 'import secrets;print(secrets.token_urlsafe(32))'` |
| `port` | server port (also honours the `$PORT` env var) |

## Public access — your responsibility

The app binds `0.0.0.0`, so it serves any device on the local network out
of the box. To reach reviewers off-site, `DEPLOY.md` includes example
commands for a [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/)
— **read it before doing this.**

If you expose the app on the internet you are accountable for:

- **De-identifying the data first.** Strip patient identifiers, slide
  labels, and anything baked into image pixels or filenames.
- **Ethics / IRB / data-use agreements** if your data demands it.
- **Auth strength.** `access_password` is a shared deterrent, not real
  authentication. Treat anyone with the URL + the code as a trusted
  participant.
- **Uptime.** Cloudflare *quick* tunnels (`cloudflared tunnel --url …`)
  are free and unsupported — URLs rotate on restart, with no SLA. Use a
  named tunnel + your own domain, a reverse proxy with proper auth, or a
  hosted setup if your study needs reliability.

The repo does not endorse any specific hosting; the deploy docs are
examples.

## Project layout

```
app.py              # Flask app, manifest builder, all routes
boxes.py            # detection sources: paired_csv, YOLO .txt, green-detect
analyze.py          # κ + precision summary from results CSVs
make_test_data.py   # synthetic patches for sanity-testing the pipeline
templates/          # login, review, admin, curate, preview pages
static/             # style.css, review.js, curate.js
config.example.json # copy → config.json (gitignored), then edit
Dockerfile          # for hosted deployments (HF Spaces, Render, …)
DEPLOY.md           # deployment notes + caveats
```

## License

MIT — see `LICENSE`.
