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

PROMPT = r"""
You are an expert document reconstruction engine.

Your task is NOT merely OCR.

Your task is to understand the VISUAL STRUCTURE of the page and convert it into a structured representation that can later be reconstructed into an almost identical DOCX.

Return ONLY ONE valid JSON array.

Never return markdown.
Never explain anything.
Never wrap JSON in code fences.

----------------------------------------------------
AVAILABLE BLOCK TYPES
----------------------------------------------------

Running Header

{
"type":"running_head",
"text":"...",
"page_number":"226"
}

Title

{
"type":"title",
"text":"...",
"size":"display"
}

Heading

{
"type":"heading",
"text":"...",
"level":1
}

Body Paragraph

{
"type":"body",
"text":"...",
"bold_lead":null
}

List Item

{
"type":"list_item",
"label":"(i)",
"text":"...",
"indent":1
}

Table

{
"type":"table",
"rows":[
["A","B"],
["C","D"]
],
"header":true
}

Footnote

{
"type":"footnote",
"marker":"1",
"text":"..."
}

----------------------------------------------------
DOCUMENT UNDERSTANDING RULES
----------------------------------------------------

Your primary goal is to preserve DOCUMENT STRUCTURE.

Do NOT flatten the page.

Do NOT merge visually separate blocks.

Treat every visually independent block as an independent JSON block.

----------------------------------------------------
TITLE DETECTION
----------------------------------------------------

A centered standalone line should almost always become either

title

or

heading

NOT body.

Examples

APPENDIX-G

FORM No.31

ADVANCE REMINDER

must become THREE separate blocks if printed separately.

Never merge them into one paragraph.

----------------------------------------------------
FORM DETECTION
----------------------------------------------------

Government forms are NOT paragraphs.

When you see

No ______

Date ______

From ______

To ______

Designation ______

etc.

keep them inside ONE body block but preserve every printed label exactly.

Never rewrite them into continuous English.

Never remove blank fields.

Never invent punctuation.

----------------------------------------------------
TABLE RULES
----------------------------------------------------

If something is visually a table,

return ONE table block.

Every row must contain the same number of columns.

Never merge cells.

Never omit blank cells.

Keep every visible row.

----------------------------------------------------
MULTI COLUMN PAGES
----------------------------------------------------

If the page contains multiple columns,

finish the ENTIRE left column first,

then continue with the next column.

Never mix lines from different columns.

----------------------------------------------------
PARAGRAPHS
----------------------------------------------------

One paragraph = one body block.

Join wrapped lines using spaces.

Remove only line-wrap hyphens.

Keep genuine hyphens.

----------------------------------------------------
HEADERS
----------------------------------------------------

Running heads are always separate.

Page numbers remain inside running_head.

Never merge running heads into titles.

----------------------------------------------------
FOOTNOTES
----------------------------------------------------

Everything below a horizontal rule becomes a footnote.

----------------------------------------------------
VERBATIM RULES
----------------------------------------------------

Do NOT

Correct spelling

Fix grammar

Expand abbreviations

Insert punctuation

Delete punctuation

Summarize

Reorder

Invent text

Guess unclear words

If unreadable,

write

[unclear]

----------------------------------------------------
VERY IMPORTANT
----------------------------------------------------

Visual layout has HIGHER priority than paragraph merging.

If two blocks are visually separated,

they MUST become two JSON blocks.

Preserve the printed hierarchy exactly.

Return ONLY ONE valid JSON array.
"""


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
    manifest = json.loads((work /"manifest.json").read_text())
   
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
