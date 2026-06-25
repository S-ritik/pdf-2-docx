#!/usr/bin/env python3
"""
05_assemble.py  —  STAGES D + E: Structure modelling + programmatic assembly.

Follows the reconstruction-log pipeline, Stages D & E (which the original build
scripts performed together): map each role-tagged block to a docx primitive and
assemble the document so that ONE source page == ONE page-block, with the first
element of each page carrying a hard page break (page alignment, Section 5).
Running heads are inline paragraphs (not Word headers); footnotes are
bottom-of-page small text under a rule (not Word footnotes). The measured
typography (Stage G) is applied: face, size, body leading, and page margins.

Uses python-docx (pure Python; replaces the original Node `docx` builder).

Usage:
    python3 05_assemble.py [--workdir work] [--out out/output.docx]

Input :  work/corrected.json, work/typography.json, work/manifest.json
Output:  out/output.docx
"""
import argparse, json
from pathlib import Path
from docx import Document
from docx.shared import Pt
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING, WD_TAB_ALIGNMENT
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement


# ----------------------------------------------------------------------
# low-level helpers
# ----------------------------------------------------------------------
def set_default_font(doc, face, size_pt):
    style = doc.styles["Normal"]
    style.font.name = face
    style.font.size = Pt(size_pt)
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.get_or_add_rFonts()
    for attr in ("w:ascii", "w:hAnsi", "w:cs"):
        rfonts.set(qn(attr), face)


def set_page(section, w_pt, h_pt, margins_tw):
    section.page_width = Pt(w_pt)
    section.page_height = Pt(h_pt)
    clamp = lambda tw: min(max(tw, 432), 3600)  # 0.3in .. 2.5in
    section.top_margin = Pt(clamp(margins_tw["top"]) / 20)
    section.bottom_margin = Pt(clamp(margins_tw["bottom"]) / 20)
    section.left_margin = Pt(clamp(margins_tw["left"]) / 20)
    section.right_margin = Pt(clamp(margins_tw["right"]) / 20)


def body_leading(p, leading_pt):
    pf = p.paragraph_format
    pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    pf.line_spacing = Pt(leading_pt)


def add_top_border(p):
    pPr = p._p.get_or_add_pPr()
    pbdr = OxmlElement("w:pBdr")
    top = OxmlElement("w:top")
    top.set(qn("w:val"), "single"); top.set(qn("w:sz"), "6")
    top.set(qn("w:space"), "4"); top.set(qn("w:color"), "000000")
    pbdr.append(top); pPr.append(pbdr)


def set_table_borders(table):
    tblPr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single"); el.set(qn("w:sz"), "6")
        el.set(qn("w:space"), "0"); el.set(qn("w:color"), "000000")
        borders.append(el)
    # OOXML requires tblBorders before shd/tblLayout/tblCellMar/tblLook in tblPr
    after = {qn("w:shd"), qn("w:tblLayout"), qn("w:tblCellMar"),
             qn("w:tblLook"), qn("w:tblCaption"), qn("w:tblDescription")}
    ref = next((c for c in tblPr if c.tag in after), None)
    if ref is not None:
        ref.addprevious(borders)
    else:
        tblPr.append(borders)


def fix_settings(doc):
    """python-docx's default settings.xml has <w:zoom> without the required percent."""
    z = doc.settings.element.find(qn("w:zoom"))
    if z is not None and z.get(qn("w:percent")) is None:
        z.set(qn("w:percent"), "100")


# ----------------------------------------------------------------------
# block -> primitive
# ----------------------------------------------------------------------
def emit_title(doc, blk, base):
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run(blk.get("text", ""))
    r.bold = True
    r.font.size = Pt(base * (2.1 if blk.get("size") == "display" else 1.6))
    return p


def emit_heading(doc, blk, base):
    p = doc.add_paragraph()
    lvl = int(blk.get("level", 1))
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER if lvl <= 2 else WD_ALIGN_PARAGRAPH.LEFT
    r = p.add_run(blk.get("text", ""))
    r.bold = True
    r.font.size = Pt(base * {1: 1.4, 2: 1.2}.get(lvl, 1.0))
    return p


def emit_running_head(doc, blk, base, page_index, text_width_pt):
    p = doc.add_paragraph()
    pf = p.paragraph_format
    num = blk.get("page_number")
    title = blk.get("text", "")
    # book convention: even page -> number left, title centred; odd -> title centred, number right
    pf.tab_stops.add_tab_stop(Pt(text_width_pt / 2), WD_TAB_ALIGNMENT.CENTER)
    pf.tab_stops.add_tab_stop(Pt(text_width_pt), WD_TAB_ALIGNMENT.RIGHT)
    if page_index % 2 == 0:
        line = f"{num or ''}\t{title}"
    else:
        line = f"\t{title}\t{num or ''}"
    r = p.add_run(line)
    r.bold = True
    r.font.size = Pt(base * 0.9)
    return p


def emit_body(doc, blk, base, leading):
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    p.paragraph_format.first_line_indent = Pt(base * 1.4)
    lead = blk.get("bold_lead")
    if lead:
        rb = p.add_run(lead.rstrip() + " ")
        rb.bold = True
    p.add_run(blk.get("text", ""))
    body_leading(p, leading)
    return p


def emit_list_item(doc, blk, base, leading):
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    indent = int(blk.get("indent", 1))
    left = base * (1.4 + 1.6 * indent)
    pf = p.paragraph_format
    pf.left_indent = Pt(left)
    pf.first_line_indent = Pt(-(base * 1.6))   # hanging
    pf.tab_stops.add_tab_stop(Pt(left), WD_TAB_ALIGNMENT.LEFT)
    p.add_run(f"{blk.get('label','')}\t{blk.get('text','')}")
    body_leading(p, leading)
    return p


def emit_table(doc, blk, base):
    rows = blk.get("rows", [])
    if not rows:
        return None
    ncols = max(len(r) for r in rows)
    table = doc.add_table(rows=len(rows), cols=ncols)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    set_table_borders(table)
    header = blk.get("header", False)
    for ri, row in enumerate(rows):
        for ci in range(ncols):
            cell = table.cell(ri, ci)
            txt = row[ci] if ci < len(row) else ""
            cp = cell.paragraphs[0]
            run = cp.add_run(str(txt))
            run.font.size = Pt(base)
            if header and ri == 0:
                run.bold = True
    return table


def emit_footnote(doc, blk, base):
    p = doc.add_paragraph()
    add_top_border(p)
    marker = blk.get("marker", "")
    if marker:
        rs = p.add_run(marker)
        rs.font.superscript = True
        rs.font.size = Pt(base * 0.8)
        p.add_run("  ")
    r = p.add_run(blk.get("text", ""))
    r.font.size = Pt(base * 0.8)
    return p


PARAGRAPH_FIRST = {"title", "heading", "running_head", "body", "list_item", "footnote"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default="work")
    ap.add_argument("--out", default="out/output.docx")
    args = ap.parse_args()
    work = Path(args.workdir)
    corrected = json.loads((work / "corrected.json").read_text())
    typo = json.loads((work / "typography.json").read_text())
    manifest = json.loads((work / "manifest.json").read_text())

    base = float(typo["size_pt"])
    leading = float(typo["leading_pt"])
    face = typo["font_family"]
    margins = typo["margins_twips"]
    w_pt, h_pt = manifest["page_w_pt"], manifest["page_h_pt"]
    text_width_pt = w_pt - (margins["left"] + margins["right"]) / 20

    doc = Document()
    set_default_font(doc, face, base)
    set_page(doc.sections[0], w_pt, h_pt, margins)

    for pi, page in enumerate(corrected["pages"]):
        first = True
        for blk in page["blocks"]:
            t = blk.get("type")
            obj = None
            if t == "title":
                obj = emit_title(doc, blk, base)
            elif t == "heading":
                obj = emit_heading(doc, blk, base)
            elif t == "running_head":
                obj = emit_running_head(doc, blk, base, page["page"], text_width_pt)
            elif t == "body":
                obj = emit_body(doc, blk, base, leading)
            elif t == "list_item":
                obj = emit_list_item(doc, blk, base, leading)
            elif t == "table":
                obj = emit_table(doc, blk, base)
            elif t == "footnote":
                obj = emit_footnote(doc, blk, base)
            else:
                continue

            # page alignment: first element of each page (after page 1) forces a page break
            if first and pi > 0:
                if t in PARAGRAPH_FIRST and obj is not None:
                    obj.paragraph_format.page_break_before = True
                else:
                    # table-first page: insert a zero-size break marker before it
                    brk = doc.paragraphs[-1] if doc.paragraphs else None
                    mp = doc.add_paragraph()
                    mp.paragraph_format.page_break_before = True
                    mp.paragraph_format.space_after = Pt(0)
                    for r in mp.runs:
                        r.font.size = Pt(1)
                    obj._tbl.addprevious(mp._p)  # move marker before the table
            first = False

    fix_settings(doc)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    doc.save(args.out)
    print(f"Stage D+E done: {len(corrected['pages'])} page-blocks, "
          f"{face} {base}pt, leading {leading}pt -> {args.out}")


if __name__ == "__main__":
    main()
