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
    {"type":"table",        "rows": [[str,...],...], "header": bool}
    {"type":"footnote",     "marker": str, "text": str}
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
- {"type":"table","rows":[["c1","c2"]],"header":true}         ruled table; header=true if the first row is a header
- {"type":"footnote","marker":"1","text":"..."}               footnote, usually below a rule

READING ORDER & LAYOUT:
- Multi-column pages: read each column fully top-to-bottom; finish the entire left column before starting the right one. Never interleave columns across a line.
- One paragraph = one body block. Join its printed lines into a single "text" string separated by single spaces; do not keep line breaks.
- A word broken across two lines by a hyphen is a line-wrap artifact: rejoin it into the whole word and drop that hyphen. Keep genuine hyphens in compound words (e.g. "post-war").
- Tables: capture EVERY cell, including empty ones as "". Every row must have the same number of cells; never merge columns or silently drop blanks. Emit one table block per ruled table.
- Keep the running head, page number and any footer separate from body text. Lines under a bottom rule are footnotes, not body.
- Reproduce special characters exactly: § ¶ © — – " " ' ' and all accented letters.

RULES:
1. Verbatim: reproduce spelling, punctuation, numbers, dates, citations and section numbers EXACTLY — correct nothing.
2. Encode emphasis only via the fields above (bold_lead for run-in bold; titles/headings are already typed).
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


def transcribe_page(image_path):
    raw = llm.chat_vision(PROMPT, image_path, temperature=0, max_tokens=8000)
    blocks = extract_json_array(raw)
    if blocks is None:
        # robust fallback: keep the raw text so nothing is lost
        blocks = [{"type": "body", "text": raw.strip(), "bold_lead": None}]
    return blocks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default="work")
    args = ap.parse_args()
    work = Path(args.workdir)
    manifest = json.loads((work / "manifest.json").read_text())

    out = {"pages": []}
    for pg in manifest["pages"]:
        print(f"  transcribing page {pg['index']} "
              f"with {llm.PROVIDER}:{llm.vision_model()} ...", flush=True)
        blocks = transcribe_page(pg["image"])
        out["pages"].append({"page": pg["index"], "blocks": blocks})
        # checkpoint after every page (long jobs survive interruption)
        (work / "transcription.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))

    print(f"\nStage B done: transcription -> {work/'transcription.json'}")


if __name__ == "__main__":
    main()
