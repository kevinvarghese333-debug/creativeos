#!/usr/bin/env bash
# WhatsApp relay on Hermes: every setup step, run from the unzipped whatsapp-relay folder.
#
#   bash setup.sh               set up (uses settings.json when the package has one; asks for the Gemini key)
#   bash setup.sh questions     answer the setup questions again (Enter keeps old answers)
#   bash setup.sh pair          link the bot's WhatsApp by scanning a QR code
#   bash setup.sh groups        find the forwarding groups by name, then stop looking at groups
#   bash setup.sh groups on     let the bot see group messages, so !addgroup works
#   bash setup.sh groups off    back to ignoring groups, once the groups are added
#   bash setup.sh schedules     turn on the 7-day nudge and the month-end total
#   bash setup.sh status        is it running, is WhatsApp connected
#   bash setup.sh logs          follow the relay's log lines (Ctrl+C to stop)
#   bash setup.sh update        after unzipping a newer package: new code, latest Hermes, restart
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="$HERE/data"
CONTAINER=hermes-whatsapp
SERVICE=hermes-whatsapp
IMAGE=nousresearch/hermes-agent:latest
GEMINI_MODEL=gemini-flash-latest
GEMINI_BASE_URL=https://generativelanguage.googleapis.com/v1beta
# The container user that owns ./data; docker-compose.yml reads the same two variables.
export HERMES_UID="${HERMES_UID:-10000}" HERMES_GID="${HERMES_GID:-10000}"
SETUP_LOG="$DATA/whatsapp-relay/setup.log"

say() { printf '%s\n' "$*"; }
err() { printf '%s\n' "$*" >&2; }
die() { err "✗ $*"; exit 1; }

# ------------------------------------------------------------------ docker helpers
compose() { docker compose -f "$HERE/docker-compose.yml" --project-directory "$HERE" "$@"; }
hx() { docker exec "$CONTAINER" hermes "$@"; }  # the hermes CLI inside the running container

need_docker() {
  command -v docker >/dev/null 2>&1 || die "Docker isn't installed on this server."
  docker compose version >/dev/null 2>&1 \
    || die "docker compose not found: Docker Compose isn't installed on the VPS; ask whoever manages the server."
  docker info >/dev/null 2>&1 || die "Can't reach Docker. Run this as root (or a user in the docker group)."
}

container_state() { docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo "missing"; }

wait_ready() {
  for _ in $(seq 1 90); do
    if docker exec "$CONTAINER" hermes --version >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  die "Hermes didn't start in time. See: docker logs --tail 50 $CONTAINER"
}

# After a (re)start: wait until the gateway reports "running" in gateway_state.json, newer than $1.
# A gateway that refuses to start (bad setting) records why; show that instead of carrying on.
wait_gateway() {
  local result
  result="$(docker exec -i "$CONTAINER" python3 - "$1" <<'PY' 2>/dev/null || true
import json, sys, time
since, seen_start, last = sys.argv[1], False, {}
for _ in range(90):
    try:
        with open("/opt/data/gateway_state.json", encoding="utf-8") as fh:
            last = json.load(fh)
    except (OSError, ValueError):
        last = {}
    state = last.get("gateway_state")
    if str(last.get("updated_at") or "") > since:
        if state in ("running", "degraded"):
            print(state)
            sys.exit(0)
        if state == "starting":
            seen_start = True
        elif state == "startup_failed" and seen_start:
            break
    time.sleep(2)
print(f"failed|{last.get('gateway_state')}|{last.get('exit_reason') or 'no reason recorded'}")
PY
)"
  case "$result" in
    running) return 0 ;;
    degraded) err "Note: Hermes started, but reports a problem with a platform. Check: bash setup.sh status" ;;
    failed\|*) die "Hermes didn't start: ${result##*|}. See: docker logs --tail 50 $CONTAINER" ;;
    *) err "Note: couldn't confirm Hermes started. Check: bash setup.sh status" ;;
  esac
}

start_container() {
  local since; since="$(date -u +%Y-%m-%dT%H:%M:%S)"
  compose up -d --quiet-pull >>"$SETUP_LOG" 2>&1 || { tail -n 20 "$SETUP_LOG" >&2; die "Couldn't start the container."; }
  wait_ready
  wait_gateway "$since"
}

restart_container() {
  local since; since="$(date -u +%Y-%m-%dT%H:%M:%S)"
  compose restart "$SERVICE" >>"$SETUP_LOG" 2>&1 || { tail -n 20 "$SETUP_LOG" >&2; die "Couldn't restart the container."; }
  wait_ready
  wait_gateway "$since"
}

# Run a step quietly; on failure show what it printed.
step() {
  local what="$1"; shift
  if ! "$@" >>"$SETUP_LOG" 2>&1; then
    tail -n 15 "$SETUP_LOG" >&2
    die "$what failed (full output in $SETUP_LOG)"
  fi
}

# ------------------------------------------------------------------ files on the host
py() {
  if command -v python3 >/dev/null 2>&1; then
    python3 "$HERE/scripts/setup_helper.py" --data "$DATA" "$@"
  else
    docker run --rm -i --entrypoint /opt/hermes/.venv/bin/python \
      -v "$HERE:/pkg:ro" -v "$DATA:/data" "$IMAGE" /pkg/scripts/setup_helper.py --data /data "$@"
  fi
}

env_value() {  # a value from data/.env ("" when unset or unreadable)
  [[ -r "$DATA/.env" ]] || return 0
  grep -E "^$1=" "$DATA/.env" 2>/dev/null | tail -n 1 | cut -d= -f2- | sed -e 's/^["'\'']//' -e 's/["'\'']$//' || true
}

paired() { [[ -f "$DATA/whatsapp/session/creds.json" || -f "$DATA/platforms/whatsapp/session/creds.json" ]]; }
whatsapp_enabled() { [[ "$(env_value WHATSAPP_ENABLED | tr '[:upper:]' '[:lower:]')" == "true" ]]; }

fix_owner() {  # files we write must belong to the container user, or Hermes can't update them
  [[ "$(id -u)" == "0" ]] || return 0
  chown "$HERMES_UID:$HERMES_GID" "$DATA" 2>/dev/null || true
  chown -R "$HERMES_UID:$HERMES_GID" "$DATA/whatsapp-relay" "$DATA/plugins/whatsapp-relay" 2>/dev/null || true
  chown "$HERMES_UID:$HERMES_GID" "$DATA/plugins" "$DATA/scripts" "$DATA"/scripts/relay_*.py 2>/dev/null || true
}

install_files() {
  mkdir -p "$DATA/plugins/whatsapp-relay" "$DATA/scripts" "$DATA/whatsapp-relay"
  cp "$HERE"/plugin/{__init__.py,relay.py,ledger.py,extractor.py,plugin.yaml} "$DATA/plugins/whatsapp-relay/"
  rm -rf "$DATA/plugins/whatsapp-relay/__pycache__"
  cp "$HERE"/scripts/relay_nudge.py "$HERE"/scripts/relay_month_end.py "$DATA/scripts/"
  fix_owner
}

need_setup() {
  [[ -f "$DATA/whatsapp-relay/config.json" ]] || die "Run 'bash setup.sh' first (it asks the setup questions)."
  need_docker
  mkdir -p "$DATA/whatsapp-relay"
  if [[ "$(container_state)" != "running" ]]; then
    say "Starting the container..."
    start_container
  fi
}

# ------------------------------------------------------------------ prompts
normalise_number() {
  local d="${1//[^0-9]/}"
  [[ "$d" == 00* ]] && d="${d:2}"
  [[ ${#d} -eq 11 && "$d" == 0[6-9]* ]] && d="91${d:1}"
  [[ ${#d} -eq 10 && "$d" == [6-9]* ]] && d="91$d"
  [[ ${#d} -ge 11 && ${#d} -le 15 ]] || return 1
  printf '%s\n' "$d"
}

# ask_number LABEL CURRENT required|optional  -> prints the answer
ask_number() {
  local label="$1" current="$2" mode="$3" reply norm hint="" eof=0
  if [[ -n "$current" && "$mode" == optional ]]; then hint=" [$current, type - to clear]"
  elif [[ -n "$current" ]]; then hint=" [$current]"; fi
  while true; do
    read -r -p "$label$hint: " reply || eof=1
    reply="${reply//[[:space:]]/}"
    if [[ -z "$reply" ]]; then
      if [[ -n "$current" ]]; then printf '%s\n' "$current"; return 0; fi
      if [[ "$mode" == optional ]]; then printf '\n'; return 0; fi
      [[ $eof == 1 ]] && { err "   No answer given."; return 1; }
      err "   This one is needed."
      continue
    fi
    if [[ "$reply" == "-" && "$mode" == optional ]]; then printf '\n'; return 0; fi
    if norm="$(normalise_number "$reply")"; then printf '%s\n' "$norm"; return 0; fi
    err "   That doesn't look like a phone number. Use the country code, like 919847012345."
    [[ $eof == 1 ]] && return 1
  done
}

ask_keyword() {
  local current="$1" reply hint=" (Enter to skip: every ₹2,000 credit counts)"
  [[ -n "$current" ]] && hint=" [$current, type - to clear]"
  read -r -p "4. A word that appears in the ₹2,000 entry, such as the account's last 4 digits$hint: " reply || true
  reply="$(printf '%s' "$reply" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  if [[ -z "$reply" ]]; then printf '%s\n' "$current"
  elif [[ "$reply" == "-" ]]; then printf '\n'
  else printf '%s\n' "$reply"; fi
}

ask_key() {
  local current="$1" reply hint="" eof=0
  [[ -n "$current" ]] && hint=" [${current:0:4}…${current: -4}]"
  while true; do
    read -r -p "5. Your Gemini key (https://aistudio.google.com/apikey)$hint: " reply || eof=1
    reply="${reply//[[:space:]]/}"
    if [[ -z "$reply" && -n "$current" ]]; then printf '%s\n' "$current"; return 0; fi
    if [[ -n "$reply" ]]; then
      [[ "$reply" == AIza* ]] || err "   (Gemini keys usually start with AIza; using it anyway.)"
      printf '%s\n' "$reply"; return 0
    fi
    [[ $eof == 1 ]] && { err "   No answer given."; return 1; }
    err "   This one is needed."
  done
}

# ------------------------------------------------------------------ commands
cmd_setup() {
  local mode="${1:-}"
  need_docker
  mkdir -p "$DATA/whatsapp-relay"
  local OWNER="" SOURCE="" FIRM="" KEYWORD="" KEY
  say "WhatsApp relay setup"
  py apply-settings
  eval "$(py answers)"
  if [[ -n "$OWNER" && "$mode" != "questions" ]]; then
    # The numbers are already known (settings.json, or an earlier run): only the key is asked.
    say ""
    py summary | sed 's/^/  /'
    say ""
    say "To change these later, send !settings to the bot on WhatsApp (or run: bash setup.sh questions)."
    say ""
    KEY="$(ask_key "$(env_value GOOGLE_API_KEY)")"
  else
    say "Numbers with country code (919847012345); a plain 10-digit mobile also works."
    say "Press Enter to keep an answer shown in [brackets]. Everything but your own number and the"
    say "Gemini key can also be set later from WhatsApp (send !settings to the bot)."
    say ""
    OWNER="$(ask_number "1. Your own WhatsApp number" "$OWNER" required)"
    SOURCE="$(ask_number "2. The source number (whose files get forwarded; Enter to skip)" "$SOURCE" optional)"
    FIRM="$(ask_number "3. The firm's number (who sends the statements; Enter to skip)" "$FIRM" optional)"
    KEYWORD="$(ask_keyword "$KEYWORD")"
    KEY="$(ask_key "$(env_value GOOGLE_API_KEY)")"
    py save --owner "$OWNER" --source "$SOURCE" --firm "$FIRM" --keyword "$KEYWORD"
  fi
  say ""
  say "Saved $DATA/whatsapp-relay/config.json"
  install_files
  : >"$SETUP_LOG"
  if [[ "$(container_state)" == "running" ]]; then
    say "Updating Hermes settings..."
    wait_ready
  else
    say "Starting Hermes (the first run downloads it, a minute or two)..."
    start_container
  fi
  step "Saving the WhatsApp mode" hx config set WHATSAPP_MODE bot
  step "Saving the allowed user" hx config set WHATSAPP_ALLOWED_USERS "$OWNER"
  # Your chat is Hermes' home channel, so it doesn't ask you to type /sethome.
  step "Saving the home chat" hx config set WHATSAPP_HOME_CHANNEL "$OWNER@s.whatsapp.net"
  step "Saving the Gemini key" hx config set GOOGLE_API_KEY "$KEY"
  step "Choosing the Gemini model" hx config set model.provider gemini
  step "Choosing the Gemini model" hx config set model.default "$GEMINI_MODEL"
  step "Choosing the Gemini model" hx config set model.base_url "$GEMINI_BASE_URL"
  step "Setting India time" hx config set timezone Asia/Kolkata
  step "Silencing replies to strangers" hx config set whatsapp.unauthorized_dm_behavior ignore
  step "Plain schedule messages" hx config set cron.wrap_response false
  step "Enabling the plugin" hx plugins enable whatsapp-relay
  fix_owner
  restart_container
  say "Done."
  if paired && whatsapp_enabled; then
    say "Next: send !status to the bot number from your own phone."
  else
    say "Next: bash setup.sh pair"
  fi
}

# WhatsApp ignores what you type from the bot's own account, so the owner must be another number.
warn_if_linked_to_owner() {
  local linked owner
  linked="$(py linked-number 2>/dev/null || true)"
  owner="$(py owner 2>/dev/null || true)"
  [[ -n "$linked" && "$linked" == "$owner" ]] || return 0
  say "⚠️  The bot is linked to your own number ($owner), so it ignores everything you type (!status, !addgroup)."
  say "   Your number must be the phone you message the bot from. Run 'bash setup.sh questions' and change"
  say "   answer 1 (press Enter for the rest), or link the bot's own WhatsApp instead: bash setup.sh pair"
}

whatsapp_health() {  # prints the bridge's connection state, or nothing
  docker exec "$CONTAINER" curl -fsS --max-time 3 http://127.0.0.1:3000/health 2>/dev/null \
    | sed -n 's/.*"status":"\([a-z_]*\)".*/\1/p' || true
}

wait_whatsapp() {
  local state=""
  for _ in $(seq 1 30); do
    state="$(whatsapp_health)"
    [[ "$state" == "connected" ]] && break
    sleep 2
  done
  printf '%s\n' "${state:-not running}"
}

cmd_pair() {
  need_setup
  if paired && whatsapp_enabled; then
    say "Pausing the current WhatsApp link while you re-link..."
    hx gateway stop >>"$SETUP_LOG" 2>&1 || true
  fi
  say ""
  say "Make this window wide (the QR code needs about 60 columns)."
  say "If it asks 'Update allowed users?', press Enter."
  say "The first time, it installs the WhatsApp bridge before the QR code (a few minutes)."
  say "When the QR code appears: on the BOT phone, WhatsApp → Settings → Linked devices → Link a device, and scan."
  say "(Ctrl+C stops waiting for the scan; Hermes is restarted either way.)"
  say ""
  trap 'true' INT  # Ctrl+C ends the wizard, not this script, so Hermes still gets restarted below
  docker exec -it "$CONTAINER" hermes whatsapp || true
  trap - INT
  say ""
  say "Restarting Hermes with WhatsApp..."
  restart_container
  if ! paired; then
    say "WhatsApp isn't linked yet. Run 'bash setup.sh pair' again and scan the QR code."
    return 1
  fi
  local state; state="$(wait_whatsapp)"
  if [[ "$state" == "connected" ]]; then
    say "✅ WhatsApp connected."
    warn_if_linked_to_owner
    say "Next: from your own phone, send !status to the bot number."
  else
    say "WhatsApp is linked but shows '$state' so far. Give it a minute, then: bash setup.sh status"
  fi
}

# Group messages normally never reach Hermes. "on" admits every group the bot is in (an allowlist of
# "*"; Hermes refuses to start with group policy "open" unless every DM sender is allowed too). The
# plugin keeps the bot silent in groups either way; it only acts on your own !addgroup.
# "groups" with no word: open group messages, wait until the bot has found every group named in
# target_group_names (it does when anyone posts in it), then close them again.
cmd_groups_find() {
  need_setup
  local missing found_before now
  missing="$(py missing-groups)"
  if [[ -z "$(py group-names)" ]]; then
    say "No group names are set, so add them by hand:"
    say "  bash setup.sh groups on, then type !addgroup inside each group, then bash setup.sh groups off"
    return 0
  fi
  if [[ -z "$missing" ]]; then
    say "All groups are already found:"
    py groups | sed 's/^/  /'
    return 0
  fi
  step "Opening group messages" hx config set WHATSAPP_GROUP_ALLOWED_USERS '*'
  step "Opening group messages" hx config set WHATSAPP_GROUP_POLICY allowlist
  restart_container
  say "Looking for these groups:"
  printf '%s\n' "$missing" | sed 's/^/  /'
  say "The bot finds a group as soon as anyone posts in it (or type !addgroup in it from your phone)."
  say "It tells you on WhatsApp as each one is found. Leave this running; Ctrl+C stops waiting."
  local stop=0 waited=0
  trap 'stop=1' INT
  found_before="$missing"
  while [[ $stop == 0 && $waited -lt 3600 ]]; do
    sleep 5 || true
    waited=$((waited + 5))
    now="$(py missing-groups)"
    if [[ "$now" != "$found_before" ]]; then
      comm -23 <(sort <<<"$found_before") <(sort <<<"$now") | sed 's/^/  ✅ Found: /'
      found_before="$now"
    fi
    [[ -z "$now" ]] && break
  done
  trap - INT
  step "Closing group messages" hx config set WHATSAPP_GROUP_POLICY pairing
  hx config unset WHATSAPP_GROUP_ALLOWED_USERS >>"$SETUP_LOG" 2>&1 || true
  restart_container
  say "Stopped looking at group messages. Forwarding groups:"
  py groups | sed 's/^/  /'
  now="$(py missing-groups)"
  if [[ -n "$now" ]]; then
    say "Not found yet: $(paste -sd, <<<"$now" | sed 's/,/, /g'). Run 'bash setup.sh groups' again later."
  else
    say "Send !testgroups to the bot to check they all receive messages."
  fi
}

cmd_groups() {
  case "${1:-}" in
    "") cmd_groups_find ;;
    on)
      need_setup
      step "Opening group messages" hx config set WHATSAPP_GROUP_ALLOWED_USERS '*'
      step "Opening group messages" hx config set WHATSAPP_GROUP_POLICY allowlist
      restart_container
      say "Groups on. Wait about 20 seconds, then type !addgroup inside each of the three groups from your own phone."
      say "The bot confirms each one to you privately. Afterwards: bash setup.sh groups off"
      ;;
    off)
      need_setup
      step "Closing group messages" hx config set WHATSAPP_GROUP_POLICY pairing
      hx config unset WHATSAPP_GROUP_ALLOWED_USERS >>"$SETUP_LOG" 2>&1 || true
      restart_container
      say "Groups off. Forwarding groups:"
      py groups | sed 's/^/  /'
      say "Send !testgroups to the bot to check they all receive messages."
      ;;
    *) die "Use: bash setup.sh groups   (or: bash setup.sh groups on / off)" ;;
  esac
}

cron_job_id() {  # the job ID listed just above "Name: <name>" in `hermes cron list`
  awk -v name="$2" '
    /^ +[0-9A-Za-z_-]+ +\[/ { id = $1 }
    $1 == "Name:" && $2 == name { print id; exit }' <<<"$1"
}

cmd_schedules() {
  need_setup
  local owner existing name id
  owner="$(py owner)"
  [[ -n "$owner" ]] || die "Your own number isn't set. Run 'bash setup.sh' first."
  existing="$(hx cron list --all 2>/dev/null || true)"
  for spec in "relay-nudge|15 10 * * *|relay_nudge.py" "relay-month-end|45 20 * * *|relay_month_end.py"; do
    IFS='|' read -r name schedule script <<<"$spec"
    id="$(cron_job_id "$existing" "$name")"
    if [[ -n "$id" ]]; then
      step "Updating $name" hx cron edit "$id" --schedule "$schedule" --deliver "whatsapp:$owner"
      hx cron resume "$id" >>"$SETUP_LOG" 2>&1 || true
      say "• $name already existed (delivery set to $owner)"
    else
      step "Creating $name" hx cron create "$schedule" --no-agent --script "$script" \
        --deliver "whatsapp:$owner" --name "$name"
      say "• $name created"
    fi
  done
  say ""
  hx cron list
}

cmd_status() {
  need_docker
  local state; state="$(container_state)"
  say "Container $CONTAINER: $state"
  if [[ "$state" != "running" ]]; then
    say "Start it with: bash setup.sh update   (or run 'bash setup.sh' if you never finished setup)"
    return 0
  fi
  if ! paired; then
    say "WhatsApp: not linked yet → bash setup.sh pair"
  elif ! whatsapp_enabled; then
    say "WhatsApp: linked but not switched on → bash setup.sh pair"
  else
    local wa; wa="$(whatsapp_health)"
    case "$wa" in
      connected) say "WhatsApp: connected"; warn_if_linked_to_owner ;;
      "") say "WhatsApp: bridge not running yet (it can take a minute after a restart) → bash setup.sh logs" ;;
      *) say "WhatsApp: $wa → if it stays like this, bash setup.sh pair" ;;
    esac
  fi
  local plugins; plugins="$(hx plugins list --enabled --no-bundled 2>/dev/null || true)"
  if grep -q whatsapp-relay <<<"$plugins"; then
    say "Plugin: whatsapp-relay enabled"
  else
    say "Plugin: whatsapp-relay NOT enabled → bash setup.sh"
  fi
  local groups; groups="$(py groups 2>/dev/null || true)"
  if [[ -n "$groups" ]]; then
    say "Forwarding groups: $(grep -c . <<<"$groups")"
    printf '%s\n' "$groups" | sed 's/^/  /'
  else
    say "Forwarding groups: none yet → bash setup.sh groups"
  fi
  if [[ "$(env_value WHATSAPP_GROUP_ALLOWED_USERS)" == "*" && "$(env_value WHATSAPP_GROUP_POLICY)" != "pairing" ]]; then
    say "Group messages: ON (run 'bash setup.sh groups off' once the groups are added)"
  fi
  say ""
  say "Schedules:"
  hx cron list 2>/dev/null | sed 's/^/  /' || say "  (couldn't list)"
}

cmd_logs() {
  need_setup
  say "Following relay log lines (Ctrl+C to stop)..."
  docker exec "$CONTAINER" tail -n 200 -F /opt/data/logs/agent.log 2>/dev/null \
    | grep --line-buffered -iE "relay|whatsapp" || true
}

cmd_update() {
  [[ -f "$DATA/whatsapp-relay/config.json" ]] || die "Nothing to update yet. Run 'bash setup.sh' first."
  need_docker
  mkdir -p "$DATA/whatsapp-relay"
  install_files
  say "Pulling the latest Hermes..."
  step "Pulling the latest Hermes" compose pull --quiet
  step "Starting the container" compose up -d  # recreates it only when the image changed
  wait_ready
  step "Enabling the plugin" hx plugins enable whatsapp-relay
  restart_container
  say "Updated."
  cmd_status
}

usage() { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; }

case "${1:-}" in
  "") cmd_setup ;;
  questions) cmd_setup questions ;;
  pair) cmd_pair ;;
  groups) cmd_groups "${2:-}" ;;
  schedules) cmd_schedules ;;
  status) cmd_status ;;
  logs) cmd_logs ;;
  update) cmd_update ;;
  help|-h|--help) usage ;;
  *) usage; exit 1 ;;
esac
