#!/usr/bin/env python3
"""
06_verify.py  —  STAGE F: Rendering & validation.

Follows the reconstruction-log pipeline, Stage F / Section 15:
  - validate the OOXML (full XSD validation if the docx-skill validator is
    available via OFFICE_VALIDATOR or a local path; otherwise a structural check)
  - render the docx -> PDF (LibreOffice headless)
  - page-count PARITY check against the source (Section 5)
  - optional source-vs-output visual diff composites

In addition to OOXML validity + page parity, a FIDELITY report compares the
intended content (work/corrected.json) against what actually landed in the DOCX:
text coverage, table count, image count, run styles (bold/italic/underline/
super/subscript) and pages that failed earlier stages. This catches missing
structure/formatting, not just missing plain text.

Usage:
    python3 06_verify.py [--workdir work] [--docx out/output.docx] [--diff]

Reads work/manifest.json for the expected page count and the source path.
"""
import argparse, collections, json, os, io, shutil, subprocess, sys, zipfile
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()   # Reads .env


def load_json(path):
    """Read JSON tolerating legacy cp1252/latin-1 files from older runs."""
    p = Path(path)
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return json.loads(p.read_text(encoding=enc))
        except UnicodeDecodeError:
            continue
    return json.loads(p.read_text(encoding="utf-8", errors="replace"))


def _norm(s):
    return " ".join(str(s).split()).lower()


def _tokens(s):
    return collections.Counter(_norm(s).split())


def expected_inventory(corrected):
    """What the corrected blocks say SHOULD be in the document."""
    inv = collections.Counter()
    texts, err_pages = [], []
    for pg in corrected["pages"]:
        if pg.get("error"):
            err_pages.append(pg["page"])
        for b in pg["blocks"]:
            t = b.get("type"); inv[t] += 1
            if t == "table":
                for r in b.get("rows", []):
                    texts.extend(str(c) for c in r)
            elif t == "form":
                texts.extend(b.get("lines", []))
            elif t == "image":
                continue
            else:
                if b.get("runs"):
                    for rn in b["runs"]:
                        texts.append(rn.get("text", ""))
                        for f in ("b", "i", "u", "sup", "sub"):
                            if rn.get(f):
                                inv["run_" + f] += 1
                else:
                    if b.get("bold_lead"):
                        texts.append(b["bold_lead"]); inv["run_b"] += 1
                    texts.append(b.get("text", ""))
                    if b.get("label"):
                        texts.append(b["label"])
    return inv, " ".join(texts), err_pages


def page_text(pg):
    """Concatenated text of one page's blocks (for per-page coverage scoring)."""
    parts = []
    for b in pg["blocks"]:
        t = b.get("type")
        if t == "table":
            for r in b.get("rows", []):
                if isinstance(r, list):
                    parts.extend(str(c) for c in r)
        elif t == "form":
            parts.extend(b.get("lines", []))
        elif t == "image":
            continue
        else:
            if b.get("runs"):
                parts.extend(rn.get("text", "") for rn in b["runs"])
            parts.append(b.get("text", ""))
            if b.get("bold_lead"):
                parts.append(b["bold_lead"])
            if b.get("label"):
                parts.append(b["label"])
    return " ".join(parts)


def docx_inventory(docx_path):
    """What actually exists in the produced DOCX."""
    from docx import Document
    from docx.oxml.ns import qn
    doc = Document(docx_path)
    blips = len(doc.element.findall(".//" + qn("a:blip")))
    runs = collections.Counter()
    texts = []
    for p in doc.paragraphs:
        for r in p.runs:
            if r.bold: runs["run_b"] += 1
            if r.italic: runs["run_i"] += 1
            if r.underline: runs["run_u"] += 1
            if r.font.superscript: runs["run_sup"] += 1
            if r.font.subscript: runs["run_sub"] += 1
        texts.append(p.text)
    for t in doc.tables:
        for row in t.rows:
            for c in row.cells:
                texts.append(c.text)
    return {"images": blips, "tables": len(doc.tables), "runs": runs}, " ".join(texts)


def fidelity_report(work, docx_path, manifest):
    """Compare intended content (corrected.json) vs the produced DOCX. Returns
    True if no hard failure (missing pages or dropped images/tables)."""
    cj = work / "corrected.json"
    if not cj.is_file():
        print("   (no corrected.json; skipping fidelity report)")
        return True
    corrected = load_json(cj)
    inv, exp_text, err_pages = expected_inventory(corrected)
    act, act_text = docx_inventory(docx_path)

    ok = True
    def line(label, status, detail=""):
        print(f"   [{status:4}] {label}: {detail}")

    # stage drift: did any source page disappear between ingest and correction?
    src_pages = {p["index"] for p in manifest.get("pages", [])}
    have_pages = {p["page"] for p in corrected["pages"]}
    missing = sorted(src_pages - have_pages)
    if missing:
        ok = False
        line("pages carried through", "FAIL",
             f"{len(missing)} of {len(src_pages)} source page(s) missing from corrected.json: {missing}")
    else:
        line("pages carried through", "OK", f"{len(have_pages)} page(s)")

    # text coverage: fraction of expected tokens present in the output (multiset)
    exp_tok, act_tok = _tokens(exp_text), _tokens(act_text)
    have = sum(min(c, act_tok.get(w, 0)) for w, c in exp_tok.items())
    coverage = have / max(sum(exp_tok.values()), 1)

    cov_status = "OK" if coverage >= 0.95 else ("WARN" if coverage >= 0.85 else "FAIL")
    if cov_status == "FAIL":
        ok = False
    line("text coverage", cov_status, f"{coverage:.1%} of expected tokens present")

    exp_tables = inv.get("table", 0)
    line("tables", "OK" if act["tables"] >= exp_tables else "WARN",
         f"{act['tables']} in docx / {exp_tables} expected")

    exp_imgs = inv.get("image", 0)
    if exp_imgs:
        img_status = "OK" if act["images"] >= exp_imgs else "FAIL"
        if img_status == "FAIL":
            ok = False
        line("images", img_status, f"{act['images']} embedded / {exp_imgs} expected")
    else:
        line("images", "OK", "none expected")

    # run styles: warn if a style is expected but entirely absent from the output
    for f, name in [("run_b", "bold"), ("run_i", "italic"), ("run_u", "underline"),
                    ("run_sup", "superscript"), ("run_sub", "subscript")]:
        e = inv.get(f, 0); a = act["runs"].get(f, 0)
        if e:
            line(f"style {name}", "OK" if a > 0 else "WARN", f"{a} runs / {e} expected")

    if err_pages:
        ok = False
        line("failed pages", "FAIL", f"{len(err_pages)} page(s) kept as fallback: {err_pages}")
    else:
        line("failed pages", "OK", "none")

    # per-page refine targets (consumed by 07_refine.py --auto): a page is worth a
    # visual pass if it failed, dropped text (low recall vs the output), or carries
    # structure (table/form/image) where borders/styling/crops commonly need fixes.
    targets, scores = [], {}
    for pg in corrected["pages"]:
        ptok = _tokens(page_text(pg))
        rec = (sum(min(c, act_tok.get(w, 0)) for w, c in ptok.items())
               / max(sum(ptok.values()), 1))
        scores[pg["page"]] = round(rec, 3)
        structural = any(b.get("type") in ("table", "form", "image") for b in pg["blocks"])
        if pg.get("error") or rec < 0.90 or structural:
            targets.append(pg["page"])
    (work / "refine_targets.json").write_text(
        json.dumps({"targets": targets, "scores": scores}, indent=2), encoding="utf-8")
    line("refine targets", "OK", f"{len(targets)} page(s) flagged -> refine_targets.json")

    return ok


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
    soffice = (shutil.which("soffice") or shutil.which("libreoffice")
               or shutil.which(r"C:\Program Files\LibreOffice\program\soffice.exe"))
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
    manifest = load_json(work / "manifest.json")
    docx_path = Path(args.docx)
    assert docx_path.is_file(), f"docx not found: {docx_path}"

    ok = True

    print("[1] OOXML validation")
    ok &= validate_ooxml(docx_path)

    print("[2] fidelity report (intended content vs DOCX)")
    ok &= fidelity_report(work, docx_path, manifest)

    print("[3] render DOCX -> PDF")
    pdf = render_pdf(docx_path, docx_path.parent)
    if pdf:
        print(f"   rendered -> {pdf}")

        print("[4] page-count parity")
        import fitz
        n = fitz.open(pdf).page_count
        expected = manifest["page_count"]
        status = "OK" if n == expected else "MISMATCH"
        print(f"   output pages = {n}, expected (selected) pages = {expected}  -> {status}")
        ok &= (n == expected)

        if args.diff:
            print("[5] visual diff composites")
            from PIL import Image
            src = fitz.open(manifest["source"])
            out = fitz.open(pdf)
            mpages = manifest["pages"]                      # output page j <-> source page mpages[j]["index"]
            outdir = Path("out") / "diff"; outdir.mkdir(parents=True, exist_ok=True)
            k = min(len(mpages), out.page_count)

            def render(doc, idx):
                pix = doc[idx].get_pixmap(matrix=fitz.Matrix(1.3, 1.3))
                return Image.open(io.BytesIO(pix.tobytes("png")))

            for j in sorted(set([0, k // 4, k // 2, 3 * k // 4, k - 1])):
                src_idx = mpages[j]["index"] - 1           # real 0-based source page
                s, o = render(src, src_idx), render(out, j)
                cv = Image.new("RGB", (s.width + o.width + 20, max(s.height, o.height)), "white")
                cv.paste(s, (0, 0)); cv.paste(o, (s.width + 20, 0))
                cv.save(outdir / f"cmp_page_{mpages[j]['index']:04d}.png")
            print(f"   composites -> {outdir} (LEFT=source, RIGHT=output)")

    print("\nStage F done:", "ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
