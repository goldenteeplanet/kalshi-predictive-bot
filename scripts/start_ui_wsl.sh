#!/usr/bin/env bash
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec .venv/bin/python -m uvicorn kalshi_predictor.ui.app:app --host 127.0.0.1 --port 8080
