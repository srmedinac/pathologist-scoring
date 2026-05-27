"""Generate synthetic H&E-style patches with green detection boxes.

For local testing only -- it lets you exercise the whole app without the real
dataset. Run:  python make_test_data.py
"""

import json
import os
import random

import cv2
import numpy as np

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "patches")
random.seed(7)
np.random.seed(7)


def make_patch(size=512):
    img = np.full((size, size, 3), (235, 220, 240), np.uint8)        # pink-ish
    img = cv2.add(img, (np.random.randn(size, size, 3) * 8).astype(np.int16)
                  .clip(-40, 40).astype(np.uint8))
    for _ in range(random.randint(40, 90)):                          # cell nuclei
        c = (random.randint(0, size), random.randint(0, size))
        cv2.circle(img, c, random.randint(4, 11),
                   (random.randint(90, 150), random.randint(40, 90),
                    random.randint(110, 170)), -1)
    return cv2.GaussianBlur(img, (3, 3), 0)


def _overlaps(a, b, gap=8):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return not (ax + aw + gap < bx or bx + bw + gap < ax or
                ay + ah + gap < by or by + bh + gap < ay)


def add_boxes(img, count, label_first=False):
    """Draw `count` non-overlapping green outline boxes (post-NMS style)."""
    size = img.shape[0]
    placed = []
    for k in range(count):
        for _ in range(200):
            bw, bh = random.randint(34, 80), random.randint(34, 80)
            x = random.randint(6, size - bw - 6)
            y = random.randint(20, size - bh - 6)
            box = [x, y, bw, bh]
            if not any(_overlaps(box, p) for p in placed):
                break
        else:
            break
        placed.append(box)
        cv2.rectangle(img, (x, y), (x + bw, y + bh), (0, 255, 0), 2)
        if label_first and k == 0:                  # ultralytics-style label tag
            cv2.rectangle(img, (x, y - 13), (x + 30, y - 2), (0, 255, 0), -1)
            cv2.putText(img, "%.2f" % random.uniform(.5, .99), (x + 1, y - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.32, (0, 0, 0), 1)
    return placed


def main():
    for sub in ("slide_A", "slide_B", "slide_C"):
        os.makedirs(os.path.join(OUT, sub), exist_ok=True)
    n, truth = 30, {}
    for i in range(n):
        sub = ("slide_A", "slide_B", "slide_C")[i % 3]
        img = make_patch()
        placed = add_boxes(img, random.choice([1, 1, 1, 2, 3]),
                           label_first=(random.random() < 0.4))
        rel = "%s/patch_%03d.png" % (sub, i)
        cv2.imwrite(os.path.join(OUT, sub, "patch_%03d.png" % i), img)
        truth[rel] = placed
    with open(os.path.join(os.path.dirname(OUT), "truth.json"), "w") as fh:
        json.dump(truth, fh)
    print("wrote %d synthetic patches to %s" % (n, OUT))


if __name__ == "__main__":
    main()
