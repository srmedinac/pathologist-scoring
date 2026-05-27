"""Summarise the pathologist review: agreement, kappa, model precision.

Run after collecting results:  python analyze.py
Reads results/results_<rater>.csv, writes results/merged.csv + results/summary.txt
"""

import csv
import json
import os
from itertools import combinations

ROOT = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(ROOT, "results")


def kappa_label(k):
    for thr, name in [(0.81, "almost perfect"), (0.61, "substantial"),
                      (0.41, "moderate"), (0.21, "fair"), (0.0, "slight")]:
        if k >= thr:
            return name
    return "poor"


def cohen_kappa(pairs, cats):
    """pairs: list of (label_a, label_b) for items both raters labelled."""
    n = len(pairs)
    if n == 0:
        return None
    agree = sum(1 for a, b in pairs if a == b) / n
    pe = 0.0
    for c in cats:
        pa = sum(1 for a, _ in pairs if a == c) / n
        pb = sum(1 for _, b in pairs if b == c) / n
        pe += pa * pb
    return 1.0 if pe == 1 else (agree - pe) / (1 - pe)


def fleiss_kappa(rows, cats):
    """rows: list of dicts category->count, each row summing to the same n."""
    rows = [r for r in rows if sum(r.values()) >= 2]
    if not rows:
        return None
    n = sum(rows[0].values())
    if any(sum(r.values()) != n for r in rows):
        return None                       # needs equal raters per item
    N = len(rows)
    p_j = {c: sum(r.get(c, 0) for r in rows) / (N * n) for c in cats}
    P_bar = sum((sum(r.get(c, 0) ** 2 for c in cats) - n) / (n * (n - 1))
                for r in rows) / N
    P_e = sum(v * v for v in p_j.values())
    return 1.0 if P_e == 1 else (P_bar - P_e) / (1 - P_e)


def main():
    cfg = json.load(open(os.path.join(ROOT, "config.json")))
    raters, cats = cfg["raters"], cfg["answer_options"]

    data = {}                              # item_id -> {rater: label}
    meta = {}                              # item_id -> (patch_id, image, det)
    for r in raters:
        path = os.path.join(RESULTS_DIR, "results_%s.csv" % r)
        if not os.path.exists(path):
            continue
        for row in csv.DictReader(open(path, newline="")):
            data.setdefault(row["item_id"], {})[r] = row["label"]
            meta[row["item_id"]] = (row["patch_id"], row["image"], row["det_index"])

    if not data:
        print("No results found in %s" % RESULTS_DIR)
        return

    out = ["Mitosis review summary", "=" * 60,
           "items with >=1 label: %d" % len(data)]

    # per-rater counts
    out.append("\nPer-rater label counts:")
    for r in raters:
        labs = [d[r] for d in data.values() if r in d]
        line = "  %-16s n=%-4d " % (r, len(labs))
        line += "  ".join("%s=%d" % (c, labs.count(c)) for c in cats)
        ynl = [x for x in labs if x in ("yes", "no")]
        if ynl:
            line += "   model precision (yes/[yes+no]) = %.1f%%" % (
                100 * ynl.count("yes") / len(ynl))
        out.append(line)

    # pairwise Cohen kappa
    out.append("\nPairwise Cohen's kappa:")
    for a, b in combinations(raters, 2):
        both = [(d[a], d[b]) for d in data.values() if a in d and b in d]
        k_all = cohen_kappa(both, cats)
        yn = [(x, y) for x, y in both if x in ("yes", "no") and y in ("yes", "no")]
        k_yn = cohen_kappa(yn, ["yes", "no"])
        out.append("  %s vs %s" % (a, b))
        if k_all is not None:
            out.append("      all categories : k=%.3f (%s, n=%d)"
                       % (k_all, kappa_label(k_all), len(both)))
        if k_yn is not None:
            out.append("      yes/no only    : k=%.3f (%s, n=%d)"
                       % (k_yn, kappa_label(k_yn), len(yn)))

    # Fleiss kappa (items labelled by every rater)
    full = [d for d in data.values() if all(r in d for r in raters)]
    rows = [{c: sum(1 for r in raters if d[r] == c) for c in cats} for d in full]
    fk = fleiss_kappa(rows, cats)
    out.append("\nFleiss' kappa (items rated by all %d raters, n=%d):"
               % (len(raters), len(full)))
    out.append("  k=%.3f (%s)" % (fk, kappa_label(fk)) if fk is not None
               else "  not enough complete items")

    # majority-vote model precision
    if full:
        maj_yes = 0
        for d in full:
            votes = [d[r] for r in raters]
            if votes.count("yes") > len(raters) / 2:
                maj_yes += 1
        out.append("\nMajority-vote model precision: %.1f%% (%d/%d)"
                   % (100 * maj_yes / len(full), maj_yes, len(full)))

    report = "\n".join(out)
    print(report)
    with open(os.path.join(RESULTS_DIR, "summary.txt"), "w") as fh:
        fh.write(report + "\n")

    # merged wide CSV
    merged = os.path.join(RESULTS_DIR, "merged.csv")
    with open(merged, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["item_id", "patch_id", "image", "det_index"] + raters)
        for item_id in sorted(data):
            pid, image, det = meta[item_id]
            w.writerow([item_id, pid, image, det]
                       + [data[item_id].get(r, "") for r in raters])
    print("\nwrote %s and results/summary.txt" % merged)


if __name__ == "__main__":
    main()
