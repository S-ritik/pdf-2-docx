#!/usr/bin/env python3
"""
03_correct.py  —  STAGE C: Error detection & correction.

Follows the reconstruction-log pipeline, Stage C / Sections 6,7,10,11:
apply the six-class error taxonomy and correction rules with the TEXT model of
the provider chosen in .env (see llm.py). The default bias is PRESERVE; only
high-confidence, context-forced scan/transcription artifacts are corrected.
Numbers, dates, citations, section numbers and names are never altered.
Illegible text stays [unclear].

Model:
    LLM_PROVIDER        groq | ollama
    GROQ_TEXT_MODEL     (groq default: llama-3.3-70b-versatile)
    OLLAMA_TEXT_MODEL   (ollama default: llama3.1)

Usage:
    python3 03_correct.py [--workdir work]

A verified-readings GLOSSARY (glossary.py) is injected into every page's prompt
so confirmed source-correct oddities are never "fixed" and confirmed OCR errors
are always applied; confirmed fixes accumulate across pages for consistency.

Input :  work/transcription.json   (+ glossary.json seed at project root)
Output:  work/corrected.json   (same shape, text fields cleaned)
         work/changes.json     (per-page log of what was changed vs preserved)
         work/glossary.json    (living glossary, seeded from ./glossary.json)
"""
import argparse, json, os, re
from pathlib import Path
from dotenv import load_dotenv
import llm
import glossary as G

load_dotenv()   # Reads .env

# Cap how many glossary entries are injected per page so a large accumulated
# glossary can't blow up the prompt (corrections are also page-filtered upstream).
GLOSSARY_MAX = int(os.environ.get("GLOSSARY_MAX", "60"))

RULES = r"""
You are an expert document reconstruction proof-reading engine whose sole purpose is to restore an editable DOCX so that it matches the original printed PDF page as closely as possible.

The PRINTED PDF PAGE is the ONLY source of truth.

You receive the JSON blocks extracted from ONE page of a DOCX reconstruction. Your job is to correct only genuine OCR, transcription, formatting and layout reconstruction errors while faithfully preserving the printed edition.

########################################################################
SOURCE OF TRUTH
########################################################################

The PDF page is always correct.

Never preserve an error from the DOCX if the PDF clearly differs.

Never invent text.

Never modernize language.

Never improve grammar.

Never rewrite sentences.

Never paraphrase.

Never "fix" genuine mistakes that exist in the printed edition.

If uncertain whether something is a printed quirk or an OCR error,
PRESERVE IT.

########################################################################
ERROR TAXONOMY
########################################################################

Class 1
High-confidence OCR character misread

Examples

rn → m
vv → w
ii → u
1 → l
I → l
0 → O
5 → S
8 → B

Action:
CORRECT

------------------------------------------------------------

Class 2
Broken word segmentation

Examples

Gov ernment
to gether
there fore

shallbe
ofthe
inthe

Action:
CORRECT

------------------------------------------------------------

Class 3
Spacing or punctuation artifact

Examples

double spaces

space before comma

space before period

missing punctuation

duplicate punctuation

stray OCR glyphs

Action:
CORRECT

------------------------------------------------------------

Class 4
Printed spelling or typography that genuinely exists

Examples

bonafied

pre-Buddist

Gujrat

olde spellings

historic punctuation

Action:
PRESERVE

------------------------------------------------------------

Class 5
Legal identifiers

Examples

Act numbers

Rule numbers

Form numbers

Section numbers

Dates

Case citations

Serial numbers

Registration numbers

Names

Addresses

Action:
PRESERVE unless obviously unreadable.

------------------------------------------------------------

Class 6
Unreadable scan

Action:
Leave as

[unclear]

########################################################################
TEXT CORRECTIONS
########################################################################

Correct ONLY when context forces one interpretation.

Check

• spelling

• punctuation

• capitalization

• Unicode symbols

• ligatures

• superscripts

• subscripts

• mathematical symbols

• special characters

• broken hyphenation

• merged words

• split words

Never rewrite sentences.

########################################################################
LAYOUT CORRECTIONS
########################################################################

Also inspect whether reconstruction differs from the PDF.

Correct if necessary

• paragraph alignment

• indentation

• paragraph spacing

• line spacing

• page margins

• page width

• section spacing

• page breaks

• column layout

• text box position

• image position

• caption position

• page number placement

• header alignment

• footer alignment

########################################################################
FONT CORRECTIONS
########################################################################

Restore

• font size

• bold

• italic

• underline

• strike-through

• font family (approximate)

• small caps

• superscript

• subscript

• text color

########################################################################
TABLE CORRECTIONS
########################################################################

Restore

• row count

• column count

• merged cells

• split cells

• borders

• border thickness

• shading

• alignment

• column widths

• row heights

• padding

Never change table content unless OCR is wrong.

########################################################################
LIST CORRECTIONS
########################################################################

Restore

• numbering

• bullets

• indentation

• hanging indent

• numbering style

########################################################################
IMAGE CORRECTIONS
########################################################################

Restore

• position

• dimensions

• wrapping

• captions

Never recreate image contents.

########################################################################
HEADERS / FOOTERS
########################################################################

Restore

• page number

• running title

• footer text

• separator lines

########################################################################
CONSTRAINTS
########################################################################

Keep the JSON structure EXACTLY.

Never change field names.

Never add fields.

Never remove fields.

Only modify

"text"

"bold_lead"

and any formatting/layout/style properties already present in the JSON.

Never merge blocks.

Never split blocks.

Never reorder blocks.

Never invent missing paragraphs.

########################################################################
OUTPUT
########################################################################

Return ONLY valid JSON.

{
  "blocks":[
      ...corrected blocks...
  ],
  "changes":[
      {
          "before":"...",
          "after":"...",
          "class":1,
          "reason":"OCR misread"
      }
  ]
}

Record ONLY actual modifications.

If nothing changed

"changes":[]
"""



def load_json(path):
    """Read JSON tolerating legacy cp1252/latin-1 files from older runs."""
    p = Path(path)
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return json.loads(p.read_text(encoding=enc))
        except UnicodeDecodeError:
            continue
    return json.loads(p.read_text(encoding="utf-8", errors="replace"))


def extract_json_object(text):
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def correct_page(blocks, gloss_block="", attempts=2):
    """Correct one page's blocks. Never raises: on any model/parse error it
    keeps the original blocks unchanged so the page (and its content) survives
    rather than aborting the whole job and truncating the document.
    gloss_block: verified-readings reminder injected into the system prompt."""
    payload = json.dumps({"blocks": blocks}, ensure_ascii=False)
    system = RULES + ("\n\n" + gloss_block if gloss_block else "")
    last_err = None
    for i in range(attempts):
        try:
            raw = llm.chat_text(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": "Page blocks:\n" + payload},
                ],
                temperature=0,
                json_object=True,
            )
            obj = extract_json_object(raw)
            if obj is None or "blocks" not in obj:
                return blocks, [{"note": "model output unparseable; page left unchanged"}]
            return obj["blocks"], obj.get("changes", [])
        except Exception as e:  # network / rate-limit / provider / context error
            last_err = e
            print(f"    ! attempt {i+1} failed: {e}", flush=True)
    return blocks, [{"note": f"correction failed; page left unchanged ({last_err})"}]


# page keys to carry through Stage C untouched (Stage E / verify need them)
PASSTHROUGH_KEYS = ("error",)


def resolve_glossary(work):
    """Living glossary lives in the workdir; seed it from the project-root
    glossary.json (the curated seed) on first use. GLOSSARY env overrides."""
    gpath = os.environ.get("GLOSSARY") or str(work / "glossary.json")
    if not Path(gpath).exists() and Path("glossary.json").exists():
        g = G.load("glossary.json")           # start from the curated seed
        G.save(g, gpath)
    return gpath, G.load(gpath)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default="work")
    args = ap.parse_args()
    work = Path(args.workdir)
    trans = load_json(work / "transcription.json")

    gpath, gloss = resolve_glossary(work)
    print(f"  glossary: {len(gloss['preserve'])} preserve / {len(gloss['corrections'])} "
          f"corrections -> {gpath}")

    # wrong-tokens already recorded; skipping these keeps add_correction off its
    # conflict path so accumulation needs no exception handling.
    known = {it["wrong"] for it in gloss["corrections"]}
    corrected = {"pages": []}
    changelog = {"pages": []}
    for page in trans["pages"]:
        if page['page'] < 403:
         continue
        print(f"  correcting page {page['page']} with {llm.PROVIDER}:{llm.text_model()} ...", flush=True)
        payload = json.dumps({"blocks": page["blocks"]}, ensure_ascii=False)
        gloss_block = G.to_prompt(gloss, filter_text=payload, max_items=GLOSSARY_MAX)
        new_blocks, changes = correct_page(page["blocks"], gloss_block)
        # accumulate confirmed fixes so later pages stay consistent. Use only
        # string before/after (models sometimes emit lists/objects), and skip a
        # wrong-token already recorded -> add_correction never reaches its
        # conflict path, so no exception handling is needed.
        for c in changes:
            if not isinstance(c, dict):
                continue
            b, a = c.get("before"), c.get("after")
            if not (isinstance(b, str) and isinstance(a, str)):
                continue
            b, a = b.rstrip("\n"), a.rstrip("\n")
            if b and b != a and b not in known:
                G.add_correction(gloss, b, a, str(c.get("reason", "")), page["page"])
                known.add(b)
        page_obj = {"page": page["page"], "blocks": new_blocks}
        for k in PASSTHROUGH_KEYS:
            if k in page:
                page_obj[k] = page[k]
        corrected["pages"].append(page_obj)
        changelog["pages"].append({"page": page["page"], "changes": changes})
        (work / "corrected.json").write_text(
            json.dumps(corrected, indent=2, ensure_ascii=False), encoding="utf-8")
        (work / "changes.json").write_text(
            json.dumps(changelog, indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            G.save(gloss, gpath)               # checkpoint the living glossary
        except ValueError:
            pass

    n = sum(len(p["changes"]) for p in changelog["pages"])
    print(f"\nStage C done: {len(corrected['pages'])} page(s), {n} correction(s) logged "
          f"-> {work/'corrected.json'}, {work/'changes.json'}  "
          f"(glossary: {len(gloss['preserve'])}/{len(gloss['corrections'])})")


if __name__ == "__main__":
    main()
