#!/usr/bin/env bash
#
# run.sh — general PDF -> editable DOCX pipeline (any scanned PDF).
# Implements the reconstruction-log stages A,B,C,G,D+E,F using Groq models.
#
#   ./run.sh SOURCE.pdf
#
# Execution order (G runs before D+E so the build uses the measured typeface):
#   01_ingest.py      Stage A   render pages + manifest
#   02_transcribe.py  Stage B   Groq VISION model -> role-tagged blocks
#   03_correct.py     Stage C   Groq TEXT model   -> apply correction rules
#   04_typography.py  Stage G   measure size/leading/margins/face
#   05_assemble.py    Stage D+E build the .docx (python-docx)
#   06_verify.py      Stage F   validate + render + page-count parity + diffs
#
# Config (env, with defaults):
#   GROQ_VISION_MODEL=meta-llama/llama-4-scout-17b-16e-instruct
#   GROQ_TEXT_MODEL=llama-3.3-70b-versatile
#   GROQ_API_KEY=...   (required; set it in .env)
#   INGEST_DPI=200     MEASURE_DPI=300
#   WORKDIR=work       OUTDIR=out
set -euo pipefail

SRC="${1:?usage: ./run.sh SOURCE.pdf}"
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
WORKDIR="${WORKDIR:-work}"
OUTDIR="${OUTDIR:-out}"
INGEST_DPI="${INGEST_DPI:-200}"
MEASURE_DPI="${MEASURE_DPI:-300}"

echo "== deps =="
pip install groq pymupdf python-docx pillow numpy python-dotenv --quiet 2>/dev/null \
  || pip install groq pymupdf python-docx pillow numpy python-dotenv --break-system-packages --quiet

echo "== Stage A: ingest =="
python3 01_ingest.py "$SRC" --workdir "$WORKDIR" --dpi "$INGEST_DPI"

echo "== Stage B: transcribe (vision: ${GROQ_VISION_MODEL:-meta-llama/llama-4-scout-17b-16e-instruct}) =="
python3 02_transcribe.py --workdir "$WORKDIR"

echo "== Stage C: correct (text: ${GROQ_TEXT_MODEL:-llama-3.3-70b-versatile}) =="
python3 03_correct.py --workdir "$WORKDIR"

echo "== Stage G: typography =="
python3 04_typography.py "$SRC" --workdir "$WORKDIR" --measure-dpi "$MEASURE_DPI"

echo "== Stage D+E: assemble =="
python3 05_assemble.py --workdir "$WORKDIR" --out "$OUTDIR/output.docx"

echo "== Stage F: verify =="
python3 06_verify.py --workdir "$WORKDIR" --docx "$OUTDIR/output.docx" --diff || true

echo "DONE -> $OUTDIR/output.docx"
