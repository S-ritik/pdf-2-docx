#!/usr/bin/env python3
"""
04_typography.py  —  STAGE G: Typography matching (measurement).

Follows the reconstruction-log pipeline, Stage G / Section 12. Measures the
source's real type metrics so the assembler (Stage E) can match them:
  - LEADING  : row projection profile -> median baseline-to-baseline
  - SIZE     : x-height of the dense lowercase zone / face x-height ratio
  - MARGINS  : text-block bounding box on the page
  - FACE     : serif vs sans-serif, classified by a Groq vision model on a
               zoomed swatch (offline fallback: a stroke-contrast heuristic)

Runs BEFORE assembly so the build uses the right face/size from the start
(the original session measured after a first build; measuring first is the clean
generalisation and yields the same matched result).

    GROQ_VISION_MODEL    (default: meta-llama/llama-4-scout-17b-16e-instruct)  # face ID only
    GROQ_API_KEY         (required for face ID; falls back to a heuristic if unset)
    TYPO_FONT_OVERRIDE   (optional: force e.g. "Arial" / "Times New Roman")

Usage:
    python3 04_typography.py SOURCE.pdf [--workdir work] [--measure-dpi 300]

Output: work/typography.json
"""
import argparse, json, os, io
from pathlib import Path
import numpy as np
import fitz
from PIL import Image
from dotenv import load_dotenv
import llm

load_dotenv()   # Reads .env


def env_page(name):
    """Parse a 1-based page bound from the environment; blank/0/invalid -> None."""
    v = os.environ.get(name, "").strip()
    return int(v) if v.isdigit() and int(v) > 0 else None

# x-height as a fraction of em, per face (used to convert measured x-height -> point size)
XHEIGHT_RATIO = {"Arial": 0.519, "Helvetica": 0.523, "Times New Roman": 0.448,
                 "Georgia": 0.481, "Verdana": 0.545, "_default": 0.50}
# natural single-line height as a fraction of em, per face (sanity cross-check only)
LINE_HEIGHT = {"Arial": 1.150, "Times New Roman": 1.150, "_default": 1.150}

MEASURE_DPI_DEFAULT = 300


def otsu(gray):
    hist, _ = np.histogram(gray, 256, (0, 256))
    tot = gray.size; sumT = (np.arange(256) * hist).sum()
    wB = sB = vmax = 0.0; thr = 128
    for t in range(256):
        wB += hist[t]
        if wB == 0: continue
        wF = tot - wB
        if wF == 0: break
        sB += t * hist[t]; mB = sB / wB; mF = (sumT - sB) / wF
        v = wB * wF * (mB - mF) ** 2
        if v > vmax: vmax = v; thr = t
    return thr


def page_ink(doc, idx, dpi):
    pix = doc[idx].get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), colorspace=fitz.csGRAY)
    a = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width)
    return (a < otsu(a)).astype(np.int32), pix.height, pix.width


def line_bands(ink, row_frac=0.06, min_px=6):
    rs = ink.sum(1)
    is_text = rs > rs.max() * row_frac
    bands, s = [], None
    for y, t in enumerate(is_text):
        if t and s is None: s = y
        elif not t and s is not None: bands.append((s, y - 1)); s = None
    if s is not None: bands.append((s, ink.shape[0] - 1))
    return [(t, b) for t, b in bands if b - t >= min_px], rs


def measure_page(ink, H, W, dpi):
    pt = lambda px: px * 72.0 / dpi
    bands, rs = line_bands(ink)
    if len(bands) < 8:
        return None  # too sparse to trust (title/letter pages)
    cents = np.array([(t + b) / 2 for t, b in bands])
    gaps = np.diff(cents); m = np.median(gaps)
    keep = gaps[(gaps > 0.6 * m) & (gaps < 1.6 * m)]
    leading = pt(np.median(keep))
    xhs = []
    for t, b in bands:
        sub = rs[t:b + 1]
        if sub.max() <= 0: continue
        dense = np.where(sub > 0.55 * sub.max())[0]
        if len(dense): xhs.append(dense.max() - dense.min() + 1)
    xheight = pt(np.median(xhs))
    # margins (skip top 12% = header/page number)
    body = ink[int(H * 0.12):, :]
    cols = body.sum(0); xs = np.where(cols > cols.max() * 0.04)[0]
    left, right = pt(xs.min()), pt(W - xs.max())
    ys = np.where(rs > rs.max() * 0.06)[0]
    top, bottom = pt(ys.min()), pt(H - ys.max())
    return dict(leading=leading, xheight=xheight, left=left, right=right, top=top, bottom=bottom,
                nlines=len(bands))


def serif_heuristic(ink):
    """Offline serif/sans guess via stroke-width-contrast.
    Serif faces have higher variation in horizontal ink run-lengths (thin serifs
    + thick stems); sans faces are more monoline -> lower coefficient of variation."""
    runs = []
    H = ink.shape[0]
    for y in range(0, H):
        row = ink[y]
        # horizontal run lengths of ink
        idx = np.where(row == 1)[0]
        if len(idx) < 2: continue
        splits = np.split(idx, np.where(np.diff(idx) != 1)[0] + 1)
        runs.extend(len(s) for s in splits if len(s) >= 1)
    if len(runs) < 50:
        return "Arial"
    runs = np.array(runs)
    cv = runs.std() / max(runs.mean(), 1e-6)
    return "Times New Roman" if cv > 0.95 else "Arial"


def classify_face(doc, dpi):
    """Return a font family name. Prefer a Groq vision call; fall back to heuristic."""
    override = os.environ.get("TYPO_FONT_OVERRIDE")
    if override:
        return override, "override"
    # crop a body swatch from a middle page
    mid = doc.page_count // 2
    pix = doc[mid].get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72))
    im = Image.open(io.BytesIO(pix.tobytes("png")))
    W, Hh = im.size
    swatch_path = "typo_swatch.png"
    im.crop((int(W * 0.12), int(Hh * 0.30), int(W * 0.88), int(Hh * 0.42))).save(swatch_path)
    try:
        from groq import Groq
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY not set")
        client = Groq(api_key=api_key)
        model = os.environ.get("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
        with open(swatch_path, "rb") as f:
            data_uri = "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
        resp = client.chat.completions.create(model=model, temperature=0, max_tokens=4, messages=[{
            "role": "user",
            "content": [
                {"type": "text",
                 "text": "Classify the body typeface in this image. Look at the stroke ends: "
                         "small finishing feet/serifs = serif; clean blunt ends = sans-serif. "
                         "Reply with exactly one word: serif or sans-serif."},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ]}])
        ans = resp.choices[0].message.content.strip().lower()
        if "sans" in ans:
            return "Arial", "vlm"
        if "serif" in ans:
            return "Times New Roman", "vlm"
    except Exception as e:
        print(f"  (vision face-ID unavailable: {e}; using stroke heuristic)")
    ink, H, W = page_ink(doc, mid, dpi)
    return serif_heuristic(ink), "heuristic"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--workdir", default="work")
    ap.add_argument("--measure-dpi", type=int, default=MEASURE_DPI_DEFAULT)
    args = ap.parse_args()
    work = Path(args.workdir); work.mkdir(exist_ok=True)
    doc = fitz.open(args.source)
    dpi = args.measure_dpi
    N = doc.page_count

    # sample interior pages (skip outer 15% — covers/titles/letter pages)
    lo, hi = max(1, int(N * 0.15)), max(2, int(N * 0.85))
    sample = list(range(lo, hi))
    if len(sample) > 10:
        sample = [sample[i] for i in np.linspace(0, len(sample) - 1, 10).astype(int)]

    rows = [measure_page(*page_ink(doc, i, dpi), dpi) for i in sample]
    rows = [r for r in rows if r]
    assert rows, "could not measure any interior page (PDF too sparse?)"

    leading = float(np.median([r["leading"] for r in rows]))
    xheight = float(np.median([r["xheight"] for r in rows]))
    left = float(np.median([r["left"] for r in rows]))
    right = float(np.median([r["right"] for r in rows]))
    top = float(np.median([r["top"] for r in rows]))
    bottom = float(np.median([r["bottom"] for r in rows]))

    face, how = classify_face(doc, dpi)
    ratio = XHEIGHT_RATIO.get(face, XHEIGHT_RATIO["_default"])
    size_pt = xheight / ratio
    # round to nearest 0.5pt (typesetting granularity)
    size_pt = round(size_pt * 2) / 2

    typography = {
        "font_family": face,
        "face_class": "sans-serif" if face in ("Arial", "Helvetica", "Verdana") else "serif",
        "face_detected_by": how,
        "size_pt": size_pt,
        "size_half_points": int(round(size_pt * 2)),
        "leading_pt": round(leading, 2),
        "leading_cross_check_pt": round(size_pt * LINE_HEIGHT.get(face, 1.15), 2),
        "margins_twips": {  # 1 pt = 20 twips
            "top": int(round(top * 20)), "bottom": int(round(bottom * 20)),
            "left": int(round(left * 20)), "right": int(round(right * 20)),
        },
        "measured_from_pages": sample,
        "measure_dpi": dpi,
        "note": "size from x-height; leading from projection profile; face via " + how,
    }
    (work / "typography.json").write_text(json.dumps(typography, indent=2))
    print("Stage G done -> " + str(work / "typography.json"))
    print(json.dumps(typography, indent=2))


if __name__ == "__main__":
    main()
