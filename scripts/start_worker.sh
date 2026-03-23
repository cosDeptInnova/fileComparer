#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="${PYTHONPATH:-}:$(pwd)"
python -m app.worker --queue "${COMPARE_QUEUE_NAME:-compare}" "$@"