#!/usr/bin/env bash
# Builds dist/whatsapp-relay.zip: the package that gets copied to the VPS and unzipped there.
#
#   bash build-zip.sh                         the plain package (setup.sh asks the questions)
#   bash build-zip.sh path/to/settings.json   a personal package: setup.sh uses those settings
set -euo pipefail
cd "$(dirname "$0")"
settings="${1:-}"
rm -rf dist && mkdir -p dist
find whatsapp-relay -name __pycache__ -type d -prune -exec rm -rf {} +
zip -qr dist/whatsapp-relay.zip whatsapp-relay \
  -x 'whatsapp-relay/data/*' '*/__pycache__/*' '*.pyc' '*/.ruff_cache/*' '*/.pytest_cache/*' 'whatsapp-relay/settings.json'
if [[ -n "$settings" ]]; then
  python3 -c 'import json,re,sys; json.loads(re.sub(r"(?m)^\s*//.*$", "", open(sys.argv[1]).read()))' "$settings"
  stage="$(mktemp -d)"
  mkdir -p "$stage/whatsapp-relay"
  cp "$settings" "$stage/whatsapp-relay/settings.json"
  (cd "$stage" && zip -q "$OLDPWD/dist/whatsapp-relay.zip" whatsapp-relay/settings.json)
  rm -rf "$stage"
fi
unzip -l dist/whatsapp-relay.zip
