# WhatsApp Relay on Hermes

Source for `whatsapp-relay.zip`: a Hermes plugin plus a Docker setup that links a dedicated WhatsApp
number to its own Hermes container on the VPS, forwards files from one number to three groups, and
tracks the daily ₹2,000 credits from statement screenshots.

The setup guide for the server is [`whatsapp-relay/README.md`](whatsapp-relay/README.md); that file
ships inside the zip.

## Layout

| Path | What |
|---|---|
| `whatsapp-relay/` | The package, exactly as it is zipped and unzipped on the VPS |
| `whatsapp-relay/setup.sh` | Every setup step (questions, pairing, groups, schedules, status, logs, update) |
| `whatsapp-relay/docker-compose.yml` | The `hermes-whatsapp` container (official `nousresearch/hermes-agent` image) |
| `whatsapp-relay/plugin/` | The Hermes plugin: message hook, forwarding, statements, ledger, `deposit_tracker` tool |
| `whatsapp-relay/scripts/` | The two no-AI schedule scripts, and `setup.sh`'s config helper |
| `tests/` | Unit tests (`pytest`) and the end-to-end container test |
| `build-zip.sh` | Builds `dist/whatsapp-relay.zip` |

## Build the zip

```bash
bash build-zip.sh                        # -> dist/whatsapp-relay.zip (setup.sh asks the questions)
bash build-zip.sh path/to/settings.json  # a personal zip: setup.sh uses those settings, asks only the Gemini key
```

A `settings.json` holds someone's phone numbers, groups and accounts in the same shape as
`config.example.json`. It goes into that person's zip only; `.gitignore` keeps it out of the repo.

## Tests

```bash
pip install pytest httpx
python3 -m pytest tests                  # unit tests, a few seconds
python3 tests/e2e/run_e2e.py             # needs Docker; several minutes
```

The end-to-end test runs the real `setup.sh` against the official Hermes image, swaps the Baileys
WhatsApp bridge for a fake one that speaks the same HTTP API (`tests/e2e/fake_bridge.js`, which also
stands in for Gemini), and drives WhatsApp-shaped messages through the real gateway, adapter and
plugin hook. What it can't cover is the live WhatsApp link itself: QR pairing, real group IDs, and a
real Gemini read of a real statement are checked on the VPS during setup steps 3 to 7.
