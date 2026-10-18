#!/usr/bin/env bash
# Builds dist/whatsapp-relay.zip: the package that gets copied to the VPS and unzipped there.
set -euo pipefail
cd "$(dirname "$0")"
rm -rf dist && mkdir -p dist
find whatsapp-relay -name __pycache__ -type d -prune -exec rm -rf {} +
zip -qr dist/whatsapp-relay.zip whatsapp-relay -x 'whatsapp-relay/data/*' '*/__pycache__/*' '*.pyc' '*/.ruff_cache/*' '*/.pytest_cache/*'
unzip -l dist/whatsapp-relay.zip
