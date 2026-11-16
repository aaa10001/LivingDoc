"""Analyse bsk UI screenshots without a vision model.

The audit agent can read only numbers, not pixels, so this script turns each PNG
into objective layout evidence:

  * ink coverage      — how much of the frame is non-background (blank / broken render)
  * empty bands       — tall horizontal stretches that are pure background (dead space)
  * edge clipping     — content touching the right/bottom edge with no gutter
  * column gutter     — left margin of the first ink column (does the grid hold)
  * palette           — dominant colours, to confirm theming actually applied

Usage:
    python scripts/ui_shot_audit.py var/ui-audit/*.png
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image


def analyse(path: Path) -> dict:
    img = Image.open(path).convert("RGB")
    w, h = img.size
    px = img.load()

    # Sample every 2nd pixel: cheap, and plenty for band/coverage statistics.
    step = 2
    bg = px[1, 1]  # top-left corner is background on these pages

    def is_ink(x: int, y: int) -> bool:
        return abs(px[x, y][0] - bg[0]) + abs(px[x, y][1] - bg[1]) + abs(px[x, y][2] - bg[2]) > 24

    rows, cols = [], []
    ink_total = 0
    for y in range(0, h, step):
        row_ink = sum(1 for x in range(0, w, step) if is_ink(x, y))
        rows.append(row_ink)
        ink_total += row_ink
    for x in range(0, w, step):
        cols.append(sum(1 for y in range(0, h, step) if is_ink(x, y)))

    sampled = (len(rows) * len(cols))
    empty_rows = [i for i, v in enumerate(rows) if v == 0]
    # Longest run of pure-background rows (dead vertical space inside content).
    longest, run, start, best = 0, 0, 0, 0
    for i, v in enumerate(rows):
        if v == 0:
            run += 1
            if run == 1:
                start = i
            if run > longest:
                longest, best = run, start
        else:
            run = 0

    first_col = next((i for i, v in enumerate(cols) if v), None)
    last_col = next((i for i, v in reversed(list(enumerate(cols))) if v), None)
    first_row = next((i for i, v in enumerate(rows) if v), None)
    last_row = next((i for i, v in reversed(list(enumerate(rows))) if v), None)

    quant = img.quantize(colors=6, method=Image.Quantize.FASTOCTREE)
    palette = quant.getpalette()[: 6 * 3]
    counts = sorted(quant.getcolors() or [], reverse=True)
    top = [
        {"rgb": list(palette[i * 3 : i * 3 + 3]), "share": round(c / (w * h), 3)}
        for c, i in counts[:4]
    ]

    return {
        "file": path.name,
        "size": f"{w}x{h}",
        "ink_ratio": round(ink_total / sampled, 4),
        "ink_rows_first_last": [first_row * step if first_row is not None else None, last_row * step if last_row is not None else None],
        "ink_cols_first_last": [first_col * step if first_col is not None else None, last_col * step if last_col is not None else None],
        "empty_row_pct": round(len(empty_rows) / len(rows), 3),
        "longest_blank_band_px": longest * step,
        "blank_band_starts_at_y": best * step,
        "right_gutter_px": (w - 1 - last_col * step) if last_col is not None else w,
        "left_margin_px": first_col * step if first_col is not None else None,
        "palette": top,
    }


def main(argv: list[str]) -> int:
    paths = [Path(p) for p in argv[1:]]
    if not paths:
        print("usage: python scripts/ui_shot_audit.py <png>...", file=sys.stderr)
        return 2
    out = []
    for p in paths:
        if not p.exists():
            out.append({"file": p.name, "error": "missing"})
            continue
        out.append(analyse(p))
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))