# General PDF → editable DOCX pipeline (Groq)

A generalised, model-driven reimplementation of the reconstruction pipeline
described in `Reconstruction_Log_and_Pipeline_Summary.md`. Unlike the original
build scripts (which hard-coded one book's text), this runs on **any** scanned
PDF: it reads the pages with a **Groq vision model**, corrects them with a
**Groq LLM**, measures the source typography, and assembles a faithful,
page-aligned `.docx`.

**One file per pipeline step.**

| File | Stage (in the summary) | What it does | Model |
|------|------------------------|--------------|-------|
| `01_ingest.py`     | **A** Ingestion        | render pages → images + manifest; detect text layer | — |
| `02_transcribe.py` | **B** Visual transcription | vision model → role-tagged JSON blocks | Groq vision |
| `03_correct.py`    | **C** Error correction | apply the 6-class taxonomy + rules; preserve-by-default; `[unclear]` | Groq text |
| `04_typography.py` | **G** Typography       | measure leading / size / margins / face (serif vs sans) | Groq vision (face only) |
| `05_assemble.py`   | **D+E** Structure + assembly | map blocks → docx primitives; 1 page-block/page with hard page break | — |
| `06_verify.py`     | **F** Verify           | OOXML validate + render + page-count parity + diffs | — |
| `run.sh`           | orchestrator           | runs 01→02→03→04→05→06 in sequence | — |

> Execution order runs **G before D+E** so the build uses the measured typeface
> from the start (the original session measured *after* a first build because its
> first build used a guessed font; measuring first is the clean generalisation and
> reaches the same matched result). Data dependency: `{C, G} → E`.

## Requirements
- **A Groq API key** — create one at <https://console.groq.com/keys> and put it
  in `.env` as `GROQ_API_KEY=...`. The pipeline uses a vision model and a text model:
  ```bash
  GROQ_VISION_MODEL=meta-llama/llama-4-scout-17b-16e-instruct  # or meta-llama/llama-4-maverick-17b-128e-instruct
  GROQ_TEXT_MODEL=llama-3.3-70b-versatile                      # or llama-3.1-8b-instant, openai/gpt-oss-120b …
  ```
- **Python 3** + `pip install groq pymupdf python-docx pillow numpy python-dotenv`
- **LibreOffice** (`soffice`) on PATH for the render/parity step.

## Run
```bash
./run.sh SOURCE.pdf
# output -> out/output.docx   (intermediates in work/)
```

Configure via env (`.env` is read automatically):
```bash
GROQ_API_KEY=...                      # required
GROQ_VISION_MODEL=meta-llama/llama-4-scout-17b-16e-instruct
GROQ_TEXT_MODEL=llama-3.3-70b-versatile
INGEST_DPI=200   MEASURE_DPI=300
TYPO_FONT_OVERRIDE="Times New Roman"  # force a face instead of auto-detect
WORKDIR=work     OUTDIR=out
```

## Run a single stage
Each file is standalone:
```bash
python3 01_ingest.py SOURCE.pdf --workdir work --dpi 200
python3 02_transcribe.py --workdir work
python3 03_correct.py --workdir work
python3 04_typography.py SOURCE.pdf --workdir work --measure-dpi 300
python3 05_assemble.py --workdir work --out out/output.docx
python3 06_verify.py --workdir work --docx out/output.docx --diff
```

## Data passed between stages (in `work/`)
```
manifest.json        page geometry, DPI, page count, text-layer flag
pages/page-*.png     one render per page (Stage A)
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
{"type":"list_item","label":"(i)","text":"...","indent":1}
{"type":"table","rows":[["c1","c2"],...],"header":true}
{"type":"footnote","marker":"1","text":"..."}
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
