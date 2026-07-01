# General PDF → editable DOCX pipeline (Groq or Ollama)

A generalised, model-driven reimplementation of the reconstruction pipeline
described in `Reconstruction_Log_and_Pipeline_Summary.md`. Unlike the original
build scripts (which hard-coded one book's text), this runs on **any** scanned
PDF: it reads the pages with a **vision model**, corrects them with a **text
LLM**, measures the source typography, and assembles a faithful, page-aligned
`.docx`. The model backend is swappable — set `LLM_PROVIDER` to `groq`,
`openai`, `gemini`, or `ollama` in `.env` (all stages share `llm.py`).

**One file per pipeline step** (plus `llm.py`, the shared provider helper).

| File | Stage (in the summary) | What it does | Model |
|------|------------------------|--------------|-------|
| `01_ingest.py`     | **A** Ingestion        | render the selected pages → images + manifest; detect text layer | — |
| `02_transcribe.py` | **B** Visual transcription | vision model → role-tagged JSON blocks | vision |
| `03_correct.py`    | **C** Error correction | apply the 6-class taxonomy + rules; preserve-by-default; `[unclear]` | text |
| `04_typography.py` | **G** Typography       | measure leading / size / margins / face (serif vs sans) | vision (face only) |
| `05_assemble.py`   | **D+E** Structure + assembly | map blocks → docx primitives; 1 page-block/page with hard page break | — |
| `06_verify.py`     | **F** Verify           | OOXML validate + render + page-count parity + diffs | — |
| `llm.py`           | shared helper          | reads `LLM_PROVIDER`; dispatches vision/text calls to Groq or Ollama | — |
| `run.sh`           | orchestrator           | runs 01→02→03→04→05→06 in sequence | — |

> Execution order runs **G before D+E** so the build uses the measured typeface
> from the start (the original session measured *after* a first build because its
> first build used a guessed font; measuring first is the clean generalisation and
> reaches the same matched result). Data dependency: `{C, G} → E`.

## Requirements
- **A model backend** chosen with `LLM_PROVIDER` (set the matching key in `.env`):
  | Provider | `LLM_PROVIDER` | SDK | Key | Vision / Text defaults |
  |----------|---------------|-----|-----|------------------------|
  | Groq   | `groq`   | `groq`   | `GROQ_API_KEY`   | `…/llama-4-scout-17b…` / `llama-3.3-70b-versatile` |
  | OpenAI | `openai` | `openai` | `OPENAI_API_KEY` | `gpt-4o-mini` / `gpt-4o-mini` |
  | Gemini | `gemini` | `openai` | `GEMINI_API_KEY` | `gemini-2.0-flash` / `gemini-2.0-flash` |
  | Ollama | `ollama` | `ollama` | — (local)        | `llama3.2-vision` / `llama3.1` |

  Gemini runs through its **OpenAI-compatibility endpoint**, so it needs the
  `openai` SDK (not `google-generativeai`). For Ollama, pull the models first
  (`ollama pull llama3.2-vision`, `ollama pull llama3.1`).
- **Python 3** + `pip install pymupdf python-docx pillow numpy python-dotenv`
  plus the one SDK for your provider (`groq`, `openai`, or `ollama`).
- **LibreOffice** (`soffice`) on PATH for the render/parity step.

## Run
```bash
./run.sh SOURCE.pdf
# output -> out/output.docx   (intermediates in work/)
```

Configure via env (`.env` is read automatically):
```bash
LLM_PROVIDER=groq                     # groq | openai | gemini | ollama
# --- groq ---
GROQ_API_KEY=...
GROQ_VISION_MODEL=meta-llama/llama-4-scout-17b-16e-instruct
GROQ_TEXT_MODEL=llama-3.3-70b-versatile
# --- openai ---
OPENAI_API_KEY=...
OPENAI_VISION_MODEL=gpt-4o-mini
OPENAI_TEXT_MODEL=gpt-4o-mini
# --- gemini (OpenAI-compatible endpoint) ---
GEMINI_API_KEY=...
GEMINI_VISION_MODEL=gemini-2.0-flash
GEMINI_TEXT_MODEL=gemini-2.0-flash
# --- ollama ---
OLLAMA_VISION_MODEL=llama3.2-vision
OLLAMA_TEXT_MODEL=llama3.1
#OLLAMA_HOST=http://localhost:11434    # or https://ollama.com (+ OLLAMA_API_KEY)
# --- page range to convert (1-based, inclusive; blank = whole PDF) ---
PAGE_FROM=                            # e.g. 5
PAGE_TO=                              # e.g. 20
INGEST_DPI=200   MEASURE_DPI=300
TYPO_FONT_OVERRIDE="Times New Roman"  # force a face instead of auto-detect
WORKDIR=work     OUTDIR=out
```

## Run a single stage
Each file is standalone:
```bash
python3 01_ingest.py SOURCE.pdf --workdir work --dpi 250 [--from 5 --to 20]
python3 02_transcribe.py --workdir work
python3 03_correct.py --workdir work
python3 04_typography.py SOURCE.pdf --workdir work --measure-dpi 250
python3 05_assemble.py --workdir work --out out/output.docx
python3 06_verify.py --workdir work --docx out/output.docx --diff
```
`--from/--to` override `PAGE_FROM/PAGE_TO`; the range is fixed at ingestion, so
stages 02–06 automatically operate only on the selected pages.

## Data passed between stages (in `work/`)
```
manifest.json        geometry, DPI, page_count (selected), page_range, source_page_count, text-layer flag
pages/page-*.png     one render per selected page (Stage A)
transcription.json   {pages:[{page, blocks:[ ... ]}]}            (Stage B)
corrected.json       same shape, text cleaned                   (Stage C)
changes.json         per-page correction log                    (Stage C)
typography.json      {font_family,size_pt,leading_pt,margins…}  (Stage G)
out/output.docx      the deliverable                            (Stage E)
out/output.pdf       render used for the parity check           (Stage F)
out/diff/*.png       source-vs-output composites (with --diff)  (Stage F)
```

## Block schema (the transcription contract)
```jsonc
{"type":"title","text":"...","size":"display"}
{"type":"heading","text":"...","level":1}
{"type":"running_head","text":"...","page_number":"13"}
{"type":"body","text":"...","bold_lead":null}      // run-in bold heading in bold_lead
{"type":"body","runs":[{"text":"see ","i":false},{"text":"Form 5","i":true}]} // inline styling: b/i/u/sup/sub
{"type":"list_item","label":"(i)","text":"...","indent":1}   // one block per item; indent = nesting
{"type":"table","rows":[["c1","c2"],...],"header":true,"borders":true} // borders only if ruled; "shading":"#EEE" optional
{"type":"footnote","marker":"1","text":"..."}
{"type":"image","bbox":[0.1,0.3,0.9,0.6],"caption":null}  // bbox 0..1; cropped from the page render
{"type":"form","lines":["FROM ....","To","  The Collector"]} // form/letter layout kept line-by-line
```

## OOXML validation
`06_verify.py` runs full XSD validation if the docx-skill validator is available
(set `OFFICE_VALIDATOR=/path/to/validate.py`, or drop it at
`validation/office/validate.py`); otherwise it does a structural check (valid
zip + `word/document.xml` + opens in python-docx).

## What this pipeline can and cannot do (honest)
It faithfully implements the *stages and logic* of the summary for any PDF, but
output fidelity is bounded by the **vision model's transcription accuracy** —
complex tables, dense multi-column pages, and degraded scans are where errors
concentrate. The typography step gives **robust estimates** (size to ~0.5pt,
face as serif/sans), not exact foundry/point values; a scan carries no embedded
font metadata. Page-count parity is reported, not forced: if the model's
transcription wraps differently from the source, a page may overflow — that is
surfaced by Stage F, not silently hidden. These limits mirror the
"What cannot reliably be obtained" section of the reconstruction log.
