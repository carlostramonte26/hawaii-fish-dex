#!/bin/bash
cd "$(dirname "$0")" || exit 1
if [ -d .venv ]; then source .venv/bin/activate; fi
python3 scripts/dex.py
