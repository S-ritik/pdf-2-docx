#!/usr/bin/env python3
"""
01_ingest.py  —  STAGE A: Ingestion & page inventory.

Follows the reconstruction-log pipeline, Stage A:
  - render every PDF page to a raster image (for the vision model)
  - record page geometry (points) and DPI
  - detect whether the PDF already has a text layer (a born-digital PDF needs no
    OCR; a scan does) — recorded but the pipeline proceeds either way
  - emit a manifest the downstream stages read

Works on ANY pdf. No model calls in this stage.

A page range can be restricted with PAGE_FROM / PAGE_TO in .env (1-based,
inclusive) or the --from / --to flags; only those pages are rendered and carried
through the rest of the pipeline. Each page keeps its real source page number in
"index" so the verify stage can map back to the original PDF.

Usage:
    python3 01_ingest.py SOURCE.pdf [--workdir work] [--dpi 200] [--from N] [--to M]

Outputs:
    work/pages/page-0001.png ...        one image per selected page
    work/manifest.json                  {source, page_count, page_range, page_w_pt, page_h_pt, dpi, has_text_layer, pages:[...]}
"""
import argparse, json, os
from pathlib import Path
import fitz  # PyMuPDF
from dotenv import load_dotenv
import llm

load_dotenv()   # Reads .env

# optional orientation pre-pass: detect each page's rotation with a cheap model
# (ROTATE_PROVIDER/ROTATE_MODEL, default a local Ollama vision model) and rotate
# the render upright so downstream OCR reads it correctly. Rotated pages are
# flagged so Stage E can lay them out in landscape.
ROTATE_DETECT = os.environ.get("ROTATE_DETECT", "0").strip().lower() in ("1", "true", "yes", "on")


def env_page(name):
    """Parse a 1-based page bound from the environment; blank/0/invalid -> None."""
    v = os.environ.get(name, "").strip()
    return int(v) if v.isdigit() and int(v) > 0 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--workdir", default="work")
    ap.add_argument("--dpi", type=int, default=200,
                    help="render DPI for transcription images (200 is a good default for VLMs)")
    ap.add_argument("--from", dest="start", type=int, default=None,
                    help="first page to convert (1-based; overrides PAGE_FROM)")
    ap.add_argument("--to", dest="end", type=int, default=None,
                    help="last page to convert (1-based, inclusive; overrides PAGE_TO)")
    args = ap.parse_args()

    src = Path(args.source)
    assert src.is_file(), f"source not found: {src}"
    work = Path(args.workdir)
    pages_dir = work / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(src)
    N = doc.page_count

    # page range (1-based, inclusive); CLI flags override .env; blank -> whole doc
    pfrom = args.start or env_page("PAGE_FROM") or 1
    pto = args.end or env_page("PAGE_TO") or N
    pfrom = max(1, min(pfrom, N))
    pto = max(pfrom, min(pto, N))

    zoom = args.dpi / 72.0
    pages_meta = []
    text_pages = 0

    for i in range(pfrom - 1, pto):
        page = doc[i]
        rect = page.rect
        # text-layer probe: born-digital pages return real text here, scans return ''
        has_text = len(page.get_text().strip()) > 0
        text_pages += int(has_text)

        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        img_path = pages_dir / f"page-{i+1:04d}.png"
        pix.save(img_path)

        rotation = llm.autorotate(str(img_path)) if ROTATE_DETECT else 0
        pages_meta.append({
            "index": i + 1,
            "image": str(img_path),
            "width_pt": round(rect.width, 2),
            "height_pt": round(rect.height, 2),
            "has_text_layer": has_text,
            "rotation": rotation,          # clockwise degrees applied to upright the render
        })
        print(f"  page {i+1:>3}/{N}  {rect.width:.0f}x{rect.height:.0f}pt  "
              f"text_layer={'yes' if has_text else 'no'}"
              + (f"  rotated {rotation}deg" if rotation else ""))

    n_sel = len(pages_meta)
    # page geometry for the document is taken from the first selected page (assume uniform)
    manifest = {
        "source": str(src),
        "page_count": n_sel,                 # number of pages actually converted
        "source_page_count": N,              # pages in the original PDF
        "page_range": [pfrom, pto],          # 1-based inclusive selection
        "dpi": args.dpi,
        "page_w_pt": pages_meta[0]["width_pt"],
        "page_h_pt": pages_meta[0]["height_pt"],
        "has_text_layer": text_pages > n_sel // 2,  # majority vote
        "pages": pages_meta,
    }
    (work / "manifest.json").write_text(json.dumps(manifest, indent=2))

    print(f"\nStage A done: {doc.page_count} pages @ {args.dpi} DPI -> {pages_dir}")
    if manifest["has_text_layer"]:
        print("  NOTE: this PDF already has a text layer (born-digital). The pipeline still")
        print("        re-reads it visually, but you could also extract text directly.")
    print(f"  manifest -> {work/'manifest.json'}")


if __name__ == "__main__":
    main()
