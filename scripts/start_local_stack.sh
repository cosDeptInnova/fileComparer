#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo '1) Arranca Redis en otra terminal: redis-server'
echo '2) API: ./start_api.sh'
echo '3) Worker: ./start_worker.sh'
