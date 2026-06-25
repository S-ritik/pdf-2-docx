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

Usage:
    python3 01_ingest.py SOURCE.pdf [--workdir work] [--dpi 200]

Outputs:
    work/pages/page-0001.png ...        one image per page
    work/manifest.json                  {source, page_count, page_w_pt, page_h_pt, dpi, has_text_layer, pages:[...]}
"""
import argparse, json, sys
from pathlib import Path
import fitz  # PyMuPDF
from dotenv import load_dotenv

load_dotenv()   # Reads .env


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--workdir", default="work")
    ap.add_argument("--dpi", type=int, default=200,
                    help="render DPI for transcription images (200 is a good default for VLMs)")
    args = ap.parse_args()

    src = Path(args.source)
    assert src.is_file(), f"source not found: {src}"
    work = Path(args.workdir)
    pages_dir = work / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(src)
    zoom = args.dpi / 72.0
    pages_meta = []
    text_pages = 0

    for i in range(doc.page_count):
        page = doc[i]
        rect = page.rect
        # text-layer probe: born-digital pages return real text here, scans return ''
        has_text = len(page.get_text().strip()) > 0
        text_pages += int(has_text)

        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        img_path = pages_dir / f"page-{i+1:04d}.png"
        pix.save(img_path)

        pages_meta.append({
            "index": i + 1,
            "image": str(img_path),
            "width_pt": round(rect.width, 2),
            "height_pt": round(rect.height, 2),
            "has_text_layer": has_text,
        })
        print(f"  page {i+1:>3}/{doc.page_count}  {rect.width:.0f}x{rect.height:.0f}pt  "
              f"text_layer={'yes' if has_text else 'no'}")

    # page geometry for the document is taken from page 1 (assume uniform; common for books)
    manifest = {
        "source": str(src),
        "page_count": doc.page_count,
        "dpi": args.dpi,
        "page_w_pt": pages_meta[0]["width_pt"],
        "page_h_pt": pages_meta[0]["height_pt"],
        "has_text_layer": text_pages > doc.page_count // 2,  # majority vote
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
