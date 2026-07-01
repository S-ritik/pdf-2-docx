#!/usr/bin/env python3
"""
07_refine.py  —  STAGE R: visual self-correction loop.

Closes the loop the rest of the pipeline was missing: render each page's
reconstruction, show it next to the SOURCE page to a STRONG vision model
(REFINE_PROVIDER/REFINE_MODEL, e.g. gemini/openai), and let it emit a small,
machine-applicable JSON PATCH that enriches/fixes the page's blocks
(borders, bold/italic/colour, alignment, a missed heading, a wrong image, …).
Apply the patch, re-assemble, optionally repeat.

Design choices (see the architecture discussion):
  * patches edit the STRUCTURED JSON, never generated code — deterministic,
    reviewable, and a bad patch can never crash the build.
  * each page is rendered in ISOLATION (a one-page docx) so overflow/page-mapping
    never confuses the comparison.
  * bounded: only the requested pages, max --passes, and a patch must actually
    change something or the loop stops.

Usage:
    python3 07_refine.py [--workdir work] [--pages 1,2,5-7] [--passes 1] [--out out/output.docx]

Reads/writes work/corrected.json (a one-time backup -> corrected.prerefine.json).
With --out it also re-assembles the final document afterwards.
"""
import argparse, json, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path
from dotenv import load_dotenv
import llm

load_dotenv()

HERE = Path(__file__).resolve().parent


def load_json(path):
    p = Path(path)
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return json.loads(p.read_text(encoding=enc))
        except UnicodeDecodeError:
            continue
    return json.loads(p.read_text(encoding="utf-8", errors="replace"))


def save_json(path, obj):
    Path(path).write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def extract_json_object(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    s, e = text.find("{"), text.rfind("}")
    if s != -1 and e != -1 and e > s:
        try:
            return json.loads(text[s:e + 1])
        except json.JSONDecodeError:
            return None
    return None


def parse_pages(spec, valid):
    """'1,2,5-7' -> sorted set intersected with the pages that exist."""
    if not spec:
        return sorted(valid)
    out = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        elif part:
            out.add(int(part))
    return sorted(out & set(valid))


def soffice():
    return (shutil.which("soffice") or shutil.which("libreoffice")
            or shutil.which(r"C:\Program Files\LibreOffice\program\soffice.exe"))


def render_page_pngs(page, page_meta, typo, tmp, max_imgs=2):
    """Assemble just this page into a one-page docx, render it, and return the
    output PNG paths (reusing 05_assemble so the real renderer is exercised)."""
    wd = Path(tmp)
    save_json(wd / "typography.json", typo)
    save_json(wd / "manifest.json", {
        "page_w_pt": page_meta["_page_w_pt"], "page_h_pt": page_meta["_page_h_pt"],
        "page_count": 1, "pages": [page_meta]})
    save_json(wd / "corrected.json", {"pages": [page]})
    docx = wd / "out.docx"
    r = subprocess.run([sys.executable, str(HERE / "05_assemble.py"),
                        "--workdir", str(wd), "--out", str(docx)],
                       cwd=str(HERE), capture_output=True, text=True)
    if not docx.is_file():
        raise RuntimeError(f"assemble failed: {r.stderr.strip()[-300:]}")
    so = soffice()
    if not so:
        raise RuntimeError("LibreOffice not found (needed to render for the visual compare)")
    subprocess.run([so, "--headless", "--convert-to", "pdf", "--outdir", str(wd), str(docx)],
                   capture_output=True, text=True)
    pdf = wd / "out.pdf"
    if not pdf.is_file():
        raise RuntimeError("render to PDF failed")
    import fitz
    d = fitz.open(pdf)
    pngs = []
    for i in range(min(max_imgs, d.page_count)):
        out = wd / f"out-{i+1}.png"
        d[i].get_pixmap(matrix=fitz.Matrix(140 / 72, 140 / 72)).save(out)
        pngs.append(str(out))
    return pngs


PROMPT = r"""You are a meticulous document-reconstruction reviewer.

IMAGE 1 is the ORIGINAL scanned page (ground truth).
The remaining image(s) are the current DOCX RECONSTRUCTION of that same page.
Below is the JSON list of blocks that produced the reconstruction; each block is
prefixed with its index [i].

Compare the reconstruction to the original and report ONLY genuine, visible
differences. Do NOT rewrite text that is already correct. Prefer the smallest set
of fixes. Reference blocks by their [i] index.

Return ONLY this JSON object:
{"match": true|false,
 "description": "one short sentence describing the original page layout",
 "ops": [ ...zero or more operations... ]}

Allowed operations (use only what is needed):
- {"op":"set_table","block":i,"rules":"ledger","header":true,"vertical_header":false,"col_widths":[0.5,0.3,0.2],"shading":{"0":"#EEEEEE"},"merges":[{"r":0,"c":0,"rowspan":1,"colspan":2}],"cell_size_ratio":0.7,"row_height":90}
- {"op":"set_runs","block":i,"runs":[{"text":"...","b":true,"i":false,"u":false,"color":"#CC0000","highlight":"yellow","strike":false}]}
- {"op":"set_field","block":i,"field":"align","value":"center"}            # left|center|right|justify
- {"op":"insert_block","index":i,"block":{"type":"heading","text":"...","level":1}}   # add a clearly MISSING element
- {"op":"delete_block","block":i}                                          # remove an element not in the original
- {"op":"set_image_bbox","block":i,"bbox":[0.1,0.3,0.9,0.6]}              # fix a figure crop (fractions 0..1)
- {"op":"set_plate","value":true}                                         # page is essentially one full-page image/figure

Table fields:
- "rules": "grid" (all lines) | "ledger" (column rules only, classic register) | "rows" (row rules only) | "box" (outline only). Use what the original actually prints.
- "merges": rectangular spans for super-headers / merged cells (0-based r,c).
- TEXT-FIT levers — if a (vertical) header is clipped or words break mid-syllable, fix it: widen via "col_widths", shrink with "cell_size_ratio" (e.g. 0.7), or raise "row_height" (points).

Rules:
- If the reconstruction already matches well, return {"match":true,"description":"...","ops":[]}.
- Use ruled borders ONLY where the original prints them; "vertical_header" true ONLY if headers are printed sideways.
- Only add runs/colour/bold where the styling is clearly visible in the original.
- Output one valid JSON object, nothing else."""


def numbered_blocks(page):
    return "\n".join(f"[{i}] {json.dumps(b, ensure_ascii=False)[:400]}"
                     for i, b in enumerate(page["blocks"]))


def apply_ops(page, ops):
    """Apply patch operations to a page's blocks. Robust: out-of-range / unknown
    ops are ignored. Returns the number of ops actually applied."""
    blocks = page["blocks"]
    n = len(blocks)
    applied = 0

    def valid(i):
        return isinstance(i, int) and 0 <= i < n

    inserts, deletes = [], []
    for op in ops if isinstance(ops, list) else []:
        if not isinstance(op, dict):
            continue
        kind = op.get("op")
        if kind == "set_plate" and op.get("value", True):
            page["plate"] = True; applied += 1
        elif kind == "set_field" and valid(op.get("block")) and op.get("field"):
            blocks[op["block"]][op["field"]] = op.get("value"); applied += 1
        elif kind == "set_runs" and valid(op.get("block")) and isinstance(op.get("runs"), list):
            b = blocks[op["block"]]; b["runs"] = op["runs"]; b.pop("bold_lead", None); applied += 1
        elif kind == "set_table" and valid(op.get("block")):
            b = blocks[op["block"]]
            for k in ("borders", "rules", "header", "vertical_header", "col_widths",
                      "shading", "merges", "cell_size_ratio", "row_height"):
                if k in op:
                    b[k] = op[k]
            applied += 1
        elif kind == "set_image_bbox" and valid(op.get("block")) and op.get("bbox"):
            blocks[op["block"]]["bbox"] = op["bbox"]; applied += 1
        elif kind == "delete_block" and valid(op.get("block")):
            deletes.append(op["block"])
        elif kind == "insert_block" and isinstance(op.get("block"), dict):
            inserts.append((op.get("index", n), op["block"]))
    # structural edits last: deletes high->low, then inserts low->high
    for i in sorted(set(deletes), reverse=True):
        del blocks[i]; applied += 1
    for idx, blk in sorted(inserts, key=lambda x: x[0]):
        blocks.insert(max(0, min(idx, len(blocks))), blk); applied += 1
    return applied


def refine_page(page, page_meta, typo, passes):
    """Render -> compare -> patch, up to `passes` times. Returns ops-applied total."""
    # how many reconstruction pages to send alongside the source. Cloud models
    # (gemini/openai) handle several images; some local runtimes choke on >2, so
    # default to 1 (source + first reconstruction page = 2 images total).
    k = int(os.environ.get("REFINE_OUT_IMAGES", "2" if llm.REFINE_PROVIDER in ("gemini", "openai") else "1"))
    total = 0
    for p in range(passes):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                pngs = render_page_pngs(page, page_meta, typo, tmp, max_imgs=k)
            except Exception as e:
                print(f"    render failed: {e}")
                return total
            prompt = PROMPT + "\n\nCURRENT BLOCKS:\n" + numbered_blocks(page)
            try:
                raw = llm.vision_multi(prompt, [page_meta["image"]] + pngs,
                                       json_object=True, max_tokens=2000)
            except Exception as e:
                print(f"    compare failed: {e}")
                return total
        patch = extract_json_object(raw) or {}
        if patch.get("description"):
            page["description"] = patch["description"]
        if patch.get("match") and not patch.get("ops"):
            print(f"    pass {p+1}: match")
            return total
        applied = apply_ops(page, patch.get("ops", []))
        total += applied
        print(f"    pass {p+1}: {applied} op(s) applied")
        if applied == 0:
            break
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default="work")
    ap.add_argument("--pages", default="", help="e.g. 1,2,5-7 (default: all)")
    ap.add_argument("--auto", action="store_true",
                    help="refine only the pages Stage F flagged (work/refine_targets.json)")
    ap.add_argument("--passes", type=int, default=1)
    ap.add_argument("--out", default="", help="if set, re-assemble the final docx after refining")
    args = ap.parse_args()
    work = Path(args.workdir)
    corrected = load_json(work / "corrected.json")
    typo = load_json(work / "typography.json")
    manifest = load_json(work / "manifest.json")
    meta = {p["index"]: p for p in manifest["pages"]}
    valid = [p["page"] for p in corrected["pages"]]

    print(f"Refine using {llm.REFINE_PROVIDER}:{llm.refine_model()}  "
          f"(passes={args.passes})")

    if args.pages:
        targets = parse_pages(args.pages, valid)
    elif args.auto:
        tf = work / "refine_targets.json"
        if tf.is_file():
            targets = sorted(set(load_json(tf).get("targets", [])) & set(valid))
            print(f"  auto: {len(targets)} page(s) flagged by Stage F")
        else:
            print("  --auto: no refine_targets.json (run 06_verify first); refining all pages")
            targets = sorted(valid)
    else:
        targets = sorted(valid)
    total = 0
    for page in corrected["pages"]:
        if page["page"] not in targets:
            continue
        pm = dict(meta.get(page["page"], {}))
        if not pm.get("image"):
            print(f"  page {page['page']}: no source render; skipping")
            continue
        pm["_page_w_pt"] = manifest["page_w_pt"]
        pm["_page_h_pt"] = manifest["page_h_pt"]
        print(f"  page {page['page']} ...")
        total += refine_page(page, pm, typo, args.passes)

    bak = work / "corrected.prerefine.json"
    if not bak.exists():
        shutil.copy(work / "corrected.json", bak)
    save_json(work / "corrected.json", corrected)
    print(f"\nStage R done: {total} op(s) applied across {len(targets)} page(s) "
          f"-> {work/'corrected.json'} (backup {bak.name})")

    if args.out:
        print("Re-assembling final document ...")
        subprocess.run([sys.executable, str(HERE / "05_assemble.py"),
                        "--workdir", str(work), "--out", args.out], cwd=str(HERE))


if __name__ == "__main__":
    main()
