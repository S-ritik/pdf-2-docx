#!/usr/bin/env bash
#
# run.sh — general PDF -> editable DOCX pipeline (any scanned PDF).
# Implements the reconstruction-log stages A,B,C,G,D+E,F (+ optional R). Models
# are routed by tier in .env (strong/mid/cheap -> openai|gemini / groq / ollama;
# see llm.py), with cloud rate-limiting and 429 retry built in.
#
#   ./run.sh SOURCE.pdf
#
# Execution order (G runs before D+E so the build uses the measured typeface):
#   01_ingest.py      Stage A   render the selected pages + manifest
#   02_transcribe.py  Stage B   VISION model -> role-tagged blocks
#   03_correct.py     Stage C   TEXT model   -> apply correction rules
#   04_typography.py  Stage G   measure size/leading/margins/face
#   05_assemble.py    Stage D+E build the .docx (python-docx)
#   06_verify.py      Stage F   validate + render + parity + fidelity + diffs
#   07_refine.py      Stage R   (optional) visual compare -> JSON patch -> rebuild
#
# Config: everything lives in .env (tiers, models, keys, page range, rate limits,
# WORKDIR/OUTDIR/DPI). This script reads those values from .env directly; an
# exported shell variable of the same name still overrides .env for one run.
#   tiers: STRONG_PROVIDER / MID_PROVIDER / CHEAP_PROVIDER  (+ legacy LLM_PROVIDER)
#   PAGE_FROM / PAGE_TO    INGEST_DPI / MEASURE_DPI    WORKDIR / OUTDIR
#   REFINE=1  REFINE_PASSES=1   (enable the optional Stage R refine)
set -euo pipefail

SRC="${1:?usage: ./run.sh SOURCE.pdf}"
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

# Pull run-script config from .env (shell env wins; python-dotenv handles quoting).
eval "$(python3 - <<'PY'
import os, shlex
from dotenv import dotenv_values
v = {**dotenv_values(".env"), **os.environ}          # exported shell vars override .env
def g(k, d): return (str(v.get(k) or "").strip() or d)
for k, d in [("WORKDIR", "work"), ("OUTDIR", "out"), ("INGEST_DPI", "200"),
             ("MEASURE_DPI", "300"), ("REFINE", "0"), ("REFINE_PASSES", "1"),
             ("LLM_PROVIDER", "groq"), ("MID_PROVIDER", ""), ("STRONG_PROVIDER", "")]:
    print("%s=%s" % (k, shlex.quote(g(k, d))))
PY
)"
MID="${MID_PROVIDER:-$LLM_PROVIDER}"
STRONG="${STRONG_PROVIDER:-gemini}"
echo "== config: workdir=$WORKDIR outdir=$OUTDIR dpi=$INGEST_DPI/$MEASURE_DPI  mid=$MID strong=$STRONG =="

#echo "== deps =="
#pip install groq openai ollama pymupdf python-docx pillow numpy python-dotenv --quiet 2>/dev/null \
#  || pip install groq openai ollama pymupdf python-docx pillow numpy python-dotenv --break-system-packages --quiet

echo "== Stage A: ingest (page range from .env PAGE_FROM/PAGE_TO) =="
python3 01_ingest.py "$SRC" --workdir "$WORKDIR" --dpi "$INGEST_DPI"

echo "== Stage B: transcribe (mid: $MID) =="
python3 02_transcribe.py --workdir "$WORKDIR"

echo "== Stage C: correct (mid: $MID) =="
python3 03_correct.py --workdir "$WORKDIR"

echo "== Stage G: typography =="
python3 04_typography.py "$SRC" --workdir "$WORKDIR" --measure-dpi "$MEASURE_DPI"

echo "== Stage D+E: assemble =="
python3 05_assemble.py --workdir "$WORKDIR" --out "$OUTDIR/output.docx"

echo "== Stage F: verify =="
python3 06_verify.py --workdir "$WORKDIR" --docx "$OUTDIR/output.docx" --diff || true

# Optional Stage R: visual self-correction on the pages Stage F flagged.
# Enable with REFINE=1 in .env and a strong tier (STRONG_PROVIDER + its API key).
if [ "$REFINE" = "1" ]; then
  echo "== Stage R: refine (strong: $STRONG) =="
  python3 07_refine.py --workdir "$WORKDIR" --auto --passes "$REFINE_PASSES" --out "$OUTDIR/output.docx"
  echo "== Stage F: re-verify =="
  python3 06_verify.py --workdir "$WORKDIR" --docx "$OUTDIR/output.docx" --diff || true
fi

echo "DONE -> $OUTDIR/output.docx"
