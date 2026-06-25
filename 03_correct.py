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

Input :  work/transcription.json
Output:  work/corrected.json   (same shape, text fields cleaned)
         work/changes.json     (per-page log of what was changed vs preserved)
"""
import argparse, json, re
from pathlib import Path
from dotenv import load_dotenv
import llm

load_dotenv()   # Reads .env

RULES = r"""You are a conservative proof-reading engine restoring a faithful reproduction of a printed legal/administrative manual. You receive the JSON blocks of ONE page, transcribed verbatim from a scan. Fix ONLY genuine scan/transcription artifacts; PRESERVE the printed edition's own quirks.

ERROR TAXONOMY (classify each suspicious token, then act):
  1 High-confidence single-token misread that context forces        -> CORRECT
  2 Word wrongly split or merged / broken spacing                   -> CORRECT (re-join or split)
  3 Stray spacing or punctuation artifact from scanning             -> CORRECT
  4 Archaic/odd spelling actually PRINTED in the source             -> PRESERVE (e.g. bonafied, pre-Buddist, Gujrat)
  5 Number, date, citation, section/paragraph/Act/Form number, name -> PRESERVE (unless obviously garbled)
  6 Illegible ([unclear])                                           -> KEEP as [unclear]

CARDINAL RULE: the printed page is the only source of truth. When unsure whether a token is a misread (class 1) or a real printed typo (class 4), PRESERVE it. Correct only when the glyphs are clear AND context forces a single reading.

COMMON SCAN ARTIFACTS (classes 1-3) — fix only when context makes the intended word unambiguous:
  - glyph misreads inside words: "rn"->"m" (rnay->may), "vv"->"w", "c"->"e", "ii"->"u", "1"->"l"/"I" inside a word (1ist->list)
  - merged words: "ofthe"->"of the", "shallbe"->"shall be"
  - split words: "to gether"->"together", "Gov ernment"->"Government"
  - spacing/punctuation: " ,"->",", a double space->single space, a stray "|" or bullet glyph at a line edge removed
Leave anything not covered above untouched.

CONSTRAINTS:
  - Keep the JSON structure, block types and field names EXACTLY as given.
  - Edit text only inside the "text" and "bold_lead" fields.
  - Never alter numbers, dates, citations, section/paragraph refs, Form/Act numbers or names.
  - Never paraphrase, reorder, merge, split or delete blocks.

OUTPUT: return ONLY this JSON object (no prose, no code fences):
{"blocks":[ ...same blocks, text corrected... ],
 "changes":[ {"before":"...","after":"...","class":1,"reason":"..."} ]}
Record in "changes" only the blocks you actually modified; if nothing changed, return "changes":[]."""


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


def correct_page(blocks):
    payload = json.dumps({"blocks": blocks}, ensure_ascii=False)
    raw = llm.chat_text(
        messages=[
            {"role": "system", "content": RULES},
            {"role": "user", "content": "Page blocks:\n" + payload},
        ],
        temperature=0,
        json_object=True,
    )
    obj = extract_json_object(raw)
    if obj is None or "blocks" not in obj:
        # fail safe: if the model misbehaves, keep the original blocks unchanged
        return blocks, [{"note": "model output unparseable; page left unchanged"}]
    return obj["blocks"], obj.get("changes", [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default="work")
    args = ap.parse_args()
    work = Path(args.workdir)
    trans = json.loads((work / "transcription.json").read_text())

    corrected = {"pages": []}
    changelog = {"pages": []}
    for page in trans["pages"]:
        print(f"  correcting page {page['page']} with {llm.PROVIDER}:{llm.text_model()} ...", flush=True)
        new_blocks, changes = correct_page(page["blocks"])
        corrected["pages"].append({"page": page["page"], "blocks": new_blocks})
        changelog["pages"].append({"page": page["page"], "changes": changes})
        (work / "corrected.json").write_text(json.dumps(corrected, indent=2, ensure_ascii=False))
        (work / "changes.json").write_text(json.dumps(changelog, indent=2, ensure_ascii=False))

    n = sum(len(p["changes"]) for p in changelog["pages"])
    print(f"\nStage C done: {n} correction(s) logged -> {work/'corrected.json'}, {work/'changes.json'}")


if __name__ == "__main__":
    main()
