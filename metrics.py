"""Live dashboard metrics: F1 (model vs. a ground-truth rater) and
Cohen / Fleiss kappa across raters.

The upstream detector proposed every candidate that pathologists review,
so the model's prediction is implicit "yes" on every shown detection.
That makes recall trivially 1.0 (no model negatives in the candidate pool),
so F1 collapses to 2P/(P+1); precision is the meaningful number.
"""

from itertools import combinations


def kappa_label(k):
    if k is None:
        return ""
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
    po = sum(1 for a, b in pairs if a == b) / n
    pe = 0.0
    for c in cats:
        pa = sum(1 for a, _ in pairs if a == c) / n
        pb = sum(1 for _, b in pairs if b == c) / n
        pe += pa * pb
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def fleiss_kappa(rows, cats):
    """rows: list of dicts category->count, each row summing to the same n."""
    rows = [r for r in rows if sum(r.values()) >= 2]
    if not rows:
        return None
    n = sum(rows[0].values())
    if any(sum(r.values()) != n for r in rows):
        return None
    N = len(rows)
    p_j = {c: sum(r.get(c, 0) for r in rows) / (N * n) for c in cats}
    P_bar = sum((sum(r.get(c, 0) ** 2 for c in cats) - n) / (n * (n - 1))
                for r in rows) / N
    P_e = sum(v * v for v in p_j.values())
    return 1.0 if P_e == 1 else (P_bar - P_e) / (1 - P_e)


def _f1_for(labels_r, cats):
    """Treat the model as 'yes' on every item this rater answered; return
    confusion + precision/recall/F1 (yes/no items only)."""
    yn = [lab for lab in labels_r.values() if lab in ("yes", "no")]
    tp = sum(1 for lab in yn if lab == "yes")
    fp = sum(1 for lab in yn if lab == "no")
    fn = 0   # model never predicts "no" on a candidate
    prec = tp / (tp + fp) if (tp + fp) else None
    rec = 1.0 if (tp + fn) else None
    f1 = (2 * prec * rec / (prec + rec)
          if prec is not None and rec is not None and (prec + rec) else None)
    unsure = sum(1 for lab in labels_r.values() if lab == "unsure")
    return {"n_yn": len(yn), "tp": tp, "fp": fp, "fn": fn,
            "precision": prec, "recall": rec, "f1": f1, "unsure": unsure}


def compute(results, raters, cats, kept_ids=None, gt_rater=None):
    """Build the dashboard payload.

    results   : {rater -> {item_id -> row dict (with 'label' + 'patch_id')}}
    raters    : config rater list (order preserved in UI)
    cats      : config answer_options (e.g. ['yes','no','unsure'])
    kept_ids  : set of patch_ids in the curated study scope (or None for all)
    gt_rater  : rater chosen as ground truth in the UI; None => no F1 yet
    """
    # rater -> {item_id -> label}, restricted to curated patches
    labels = {}
    for r in raters:
        rd = {}
        for iid, row in results.get(r, {}).items():
            if kept_ids is None or row["patch_id"] in kept_ids:
                rd[iid] = row["label"]
        labels[r] = rd

    counts = {r: len(labels[r]) for r in raters}
    active = [r for r in raters if counts[r] > 0]

    # --- Model F1 against every active rater (so admin can compare frames) ---
    f1_rows = []
    for r in active:
        m = _f1_for(labels[r], cats)
        f1_rows.append({"rater": r, "is_gt": (r == gt_rater), **m})

    # The headline number: model vs. the chosen GT.
    headline = None
    if gt_rater and gt_rater in active:
        headline = {"rater": gt_rater, **_f1_for(labels[gt_rater], cats)}

    # --- Pairwise Cohen's kappa: rater vs rater, and rater vs model ---
    pair_rows = []
    for a, b in combinations(active, 2):
        common = set(labels[a]) & set(labels[b])
        la, lb = labels[a], labels[b]
        yn = [(la[i], lb[i]) for i in common
              if la[i] in ("yes", "no") and lb[i] in ("yes", "no")]
        allp = [(la[i], lb[i]) for i in common]
        pair_rows.append(_pair_row(a, b, len(common), len(yn),
                                   cohen_kappa(yn, ["yes", "no"]),
                                   cohen_kappa(allp, cats)))
    for r in active:
        la = labels[r]
        n_common = len(la)
        yn = [(la[i], "yes") for i in la if la[i] in ("yes", "no")]
        allp = [(la[i], "yes") for i in la]
        pair_rows.append(_pair_row(r, "model", n_common, len(yn),
                                   cohen_kappa(yn, ["yes", "no"]),
                                   cohen_kappa(allp, cats)))

    # --- Fleiss' kappa across active human raters (full overlap only) ---
    fleiss_info = None
    if len(active) >= 2:
        full = set.intersection(*(set(labels[r]) for r in active))
        rows_all = [{c: sum(1 for r in active if labels[r][i] == c) for c in cats}
                    for i in full]
        rows_yn  = [{c: sum(1 for r in active if labels[r][i] == c)
                     for c in ("yes", "no")}
                    for i in full
                    if all(labels[r][i] in ("yes", "no") for r in active)]
        fleiss_info = {
            "raters": list(active),
            "n_all": len(rows_all),
            "n_yn":  len(rows_yn),
            "k_all": fleiss_kappa(rows_all, cats),
            "k_yn":  fleiss_kappa(rows_yn, ["yes", "no"]),
        }

    # --- Label distributions on each rater's submitted set ---
    dist_rows = []
    for r in active:
        c = {x: sum(1 for lab in labels[r].values() if lab == x) for x in cats}
        dist_rows.append({"rater": r, "n": counts[r], **c})

    return {
        "active": active,
        "gt_rater": gt_rater if gt_rater in active else None,
        "headline": headline,
        "f1": f1_rows,
        "pairs": pair_rows,
        "fleiss": fleiss_info,
        "distributions": dist_rows,
    }


def _pair_row(a, b, n_common, n_yn, k_yn, k_all):
    return {"a": a, "b": b, "n_common": n_common, "n_yn": n_yn,
            "k_yn": k_yn, "k_yn_label": kappa_label(k_yn),
            "k_all": k_all, "k_all_label": kappa_label(k_all)}
