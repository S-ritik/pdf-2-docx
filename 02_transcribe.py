#!/usr/bin/env python3
"""
02_transcribe.py  —  STAGE B: Visual transcription (vision-language model).

Follows the reconstruction-log pipeline, Stage B: read each page IMAGE directly
with a multimodal model (there is no separate OCR engine) and emit not just text
but the TYPOGRAPHIC ROLE of every block, because role drives reconstruction
downstream (Stage D/E).

Model: a vision model from the provider chosen in .env (see llm.py).
    LLM_PROVIDER          groq | ollama
    GROQ_VISION_MODEL     (groq default: meta-llama/llama-4-scout-17b-16e-instruct)
    OLLAMA_VISION_MODEL   (ollama default: llama3.2-vision)

Usage:
    python3 02_transcribe.py [--workdir work]

Input :  work/manifest.json (+ page images from Stage A)
Output:  work/transcription.json   {pages:[{page:int, blocks:[ <block> ]}]}

Block schema (the model is asked to produce exactly this):
    {"type":"title",        "text": str, "size":"display"|"large"}
    {"type":"heading",      "text": str, "level": 1|2|3}
    {"type":"running_head", "text": str, "page_number": str|null}
    {"type":"body",         "text": str, "bold_lead": str|null}   # run-in bold heading at start, if any
    {"type":"list_item",    "label": str, "text": str, "indent": int}
    {"type":"table",        "rows": [[str,...],...], "header": bool, "borders": bool, "vertical_header": bool}
    {"type":"footnote",     "marker": str, "text": str}
    {"type":"image",        "bbox": [x0,y0,x1,y1], "caption": str|null}  # bbox normalised 0..1
    {"type":"form",         "lines": [str,...]}   # field/letter layout kept line-by-line
"""
import argparse, json, re
from pathlib import Path
from dotenv import load_dotenv
import llm

load_dotenv()   # Reads .env

PROMPT = r"""You are a precise document-transcription engine. Transcribe the attached page image of a printed book/manual EXACTLY as printed.

Output ONLY a JSON array of blocks in top-to-bottom reading order — no prose, no markdown, no code fences.

Block types (choose the one that fits each block):
- {"type":"title","text":"...","size":"display"}              large centred title
- {"type":"heading","text":"...","level":1}                   section/chapter heading (level 1 = biggest)
- {"type":"running_head","text":"...","page_number":"13"}     small header line at the very top (page_number may be null)
- {"type":"body","text":"...","bold_lead":null}               paragraph; a leading bold run-in heading goes in bold_lead, the rest in text
- {"type":"list_item","label":"(i)","text":"...","indent":1}  labelled/numbered entry; label = bullet/number as printed
- {"type":"table","rows":[["c1","c2"]],"header":true,"borders":true,"vertical_header":false}  table; header=true if first row is a header; borders=true ONLY if ruled lines are printed; vertical_header=true ONLY if the column headings are printed rotated/vertical (sideways) while the page itself is upright
- {"type":"footnote","marker":"1","text":"..."}               footnote, usually below a rule
- {"type":"image","bbox":[0.12,0.30,0.88,0.62],"caption":null}  a non-text graphic; bbox=[left,top,right,bottom] as fractions 0..1 of the page
- {"type":"form","lines":["FROM ...","Shri ........","To","The Collector ..."]}  a form/letter layout; keep each printed line as-is, including dotted fill-in lines

OPTIONAL rich text (use ONLY where the styling is clearly visible, else omit):
- A body block may use "runs" instead of "text" to keep inline styling:
  {"type":"body","runs":[{"text":"see ","i":false},{"text":"Form No.5","i":true},{"text":" below"}]}
  per run flags: "b" bold, "i" italic, "u" underline, "sup" superscript, "sub" subscript. Omit a flag when false.

READING ORDER & LAYOUT:
- Multi-column pages: read each column fully top-to-bottom; finish the entire left column before starting the right one. Never interleave columns across a line.
- One paragraph = one body block. Join its printed lines into a single "text" string separated by single spaces; do not keep line breaks.
- A word broken across two lines by a hyphen is a line-wrap artifact: rejoin it into the whole word and drop that hyphen. Keep genuine hyphens in compound words (e.g. "post-war").
- LISTS: emit ONE list_item per labelled entry — never merge several (a)(b)(c) entries into one block. Use "indent" for nesting depth (0,1,2…) so sub-items stay under their parent.
- Tables: capture EVERY cell, including empty ones as "". Every row must have the same number of cells; never merge columns or silently drop blanks. Emit one table block per table. Set "borders" from what you SEE (true only if ruled lines are printed).
- IMAGES: emit an "image" block (in reading order, with its bbox) for any non-text graphic — photograph, figure, diagram, chart, map, logo, seal, stamp or signature. Do NOT use image blocks for ordinary text, headings or tables. Transcribe any caption text as a separate body block.
- FORMS: for a printed form, application or letter template (fields, blanks, dotted lines, From/To headers), use a "form" block and keep each line verbatim in "lines".
- Keep the running head, page number and any footer separate from body text. Lines under a bottom rule are footnotes, not body.
- Reproduce special characters exactly: § ¶ © — – " " ' ' and all accented letters.

RULES:
1. Verbatim: reproduce spelling, punctuation, numbers, dates, citations and section numbers EXACTLY — correct nothing.
2. Encode emphasis via the fields above (bold_lead for run-in bold; "runs" for inline italic/underline/etc.; titles/headings are already typed).
3. Illegible glyphs -> the literal token [unclear].
4. Never invent, summarise, reorder, merge or omit printed content.
5. Emit one well-formed JSON array (no trailing commas) and nothing else."""


def extract_json_array(text):
    """Pull the first JSON array out of a model response, tolerating fences/prose."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    # find the outermost [...]
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        chunk = text[start:end + 1]
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            pass
    # last resort: whole-page fallback as a single body block
    return None


def transcribe_page(image_path, attempts=2):
    """Transcribe one page. Retries once on transient errors; never raises —
    a page that cannot be transcribed returns a single [unclear] body block so
    the page is preserved (page count stays in parity) instead of aborting the
    whole job."""
    last_err = None
    for i in range(attempts):
        try:
            raw = llm.chat_vision(PROMPT, image_path, temperature=0, max_tokens=8000)
            blocks = extract_json_array(raw)
            if blocks is None:
                # parsed nothing: keep the raw text so the words are not lost
                blocks = [{"type": "body", "text": raw.strip(), "bold_lead": None}]
            return blocks, None
        except Exception as e:  # network / rate-limit / provider error
            last_err = e
            print(f"    ! attempt {i+1} failed: {e}", flush=True)
    return ([{"type": "body", "text": "[unclear]", "bold_lead": None}],
            f"transcription failed: {last_err}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default="work")
    args = ap.parse_args()
    work = Path(args.workdir)
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))

    out = {"pages": []}
    failures = []
    for pg in manifest["pages"]:
        print(f"  transcribing page {pg['index']} "
              f"with {llm.PROVIDER}:{llm.vision_model()} ...", flush=True)
        blocks, err = transcribe_page(pg["image"])
        if err:
            failures.append(pg["index"])
        page_obj = {"page": pg["index"], "blocks": blocks}
        if err:
            page_obj["error"] = err
        out["pages"].append(page_obj)
        # checkpoint after every page (long jobs survive interruption)
        (work / "transcription.json").write_text(
            json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nStage B done: {len(out['pages'])} page(s) -> {work/'transcription.json'}")
    if failures:
        print(f"  WARNING: {len(failures)} page(s) could not be transcribed and were kept "
              f"as [unclear]: {failures}")


if __name__ == "__main__":
    main()
