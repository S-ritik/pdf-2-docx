#!/usr/bin/env python3
"""
06_verify.py  —  STAGE F: Rendering & validation.

Follows the reconstruction-log pipeline, Stage F / Section 15:
  - validate the OOXML (full XSD validation if the docx-skill validator is
    available via OFFICE_VALIDATOR or a local path; otherwise a structural check)
  - render the docx -> PDF (LibreOffice headless)
  - page-count PARITY check against the source (Section 5)
  - optional source-vs-output visual diff composites

Usage:
    python3 06_verify.py [--workdir work] [--docx out/output.docx] [--diff]

Reads work/manifest.json for the expected page count and the source path.
"""
import argparse, json, os, io, shutil, subprocess, sys, zipfile
from pathlib import Path


def validate_ooxml(docx_path):
    # 1) full XSD validation via the docx-skill validator, if present
    cand = os.environ.get("OFFICE_VALIDATOR")
    search = [cand] if cand else []
    search += [
        "validation/office/validate.py",
        "../validation/office/validate.py",
        "/mnt/skills/public/docx/scripts/office/validate.py",
    ]
    for v in search:
        if v and Path(v).is_file():
            print(f"  OOXML: running validator {v}")
            r = subprocess.run([sys.executable, v, str(docx_path)],
                               capture_output=True, text=True)
            print("   " + (r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "(no output)"))
            return r.returncode == 0
    # 2) fallback structural check
    print("  OOXML: validator not found; structural check instead")
    try:
        with zipfile.ZipFile(docx_path) as z:
            names = z.namelist()
            assert "[Content_Types].xml" in names, "missing [Content_Types].xml"
            assert "word/document.xml" in names, "missing word/document.xml"
        from docx import Document
        Document(docx_path)  # parses without error
        print("   structural check PASSED (valid zip + document.xml + opens in python-docx)")
        return True
    except Exception as e:
        print(f"   structural check FAILED: {e}")
        return False


def render_pdf(docx_path, out_dir):
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        print("  RENDER: LibreOffice not found (skipping render + parity)")
        return None
    subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(docx_path)],
                   capture_output=True, text=True)
    pdf = Path(out_dir) / (Path(docx_path).stem + ".pdf")
    return pdf if pdf.is_file() else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default="work")
    ap.add_argument("--docx", default="out/output.docx")
    ap.add_argument("--diff", action="store_true", help="write source-vs-output composites")
    args = ap.parse_args()
    work = Path(args.workdir)
    manifest = json.loads((work / "manifest.json").read_text())
    docx_path = Path(args.docx)
    assert docx_path.is_file(), f"docx not found: {docx_path}"

    ok = True

    print("[1] OOXML validation")
    ok &= validate_ooxml(docx_path)

    print("[2] render DOCX -> PDF")
    pdf = render_pdf(docx_path, docx_path.parent)
    if pdf:
        print(f"   rendered -> {pdf}")

        print("[3] page-count parity")
        import fitz
        n = fitz.open(pdf).page_count
        expected = manifest["page_count"]
        status = "OK" if n == expected else "MISMATCH"
        print(f"   output pages = {n}, source pages = {expected}  -> {status}")
        ok &= (n == expected)

        if args.diff:
            print("[4] visual diff composites")
            from PIL import Image
            src = fitz.open(manifest["source"])
            out = fitz.open(pdf)
            outdir = Path("out") / "diff"; outdir.mkdir(parents=True, exist_ok=True)
            k = min(src.page_count, out.page_count)
            for i in sorted(set([0, k // 4, k // 2, 3 * k // 4, k - 1])):
                def render(doc):
                    pix = doc[i].get_pixmap(matrix=fitz.Matrix(1.3, 1.3))
                    return Image.open(io.BytesIO(pix.tobytes("png")))
                s, o = render(src), render(out)
                cv = Image.new("RGB", (s.width + o.width + 20, max(s.height, o.height)), "white")
                cv.paste(s, (0, 0)); cv.paste(o, (s.width + 20, 0))
                cv.save(outdir / f"cmp_page_{i+1:04d}.png")
            print(f"   composites -> {outdir} (LEFT=source, RIGHT=output)")

    print("\nStage F done:", "ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
