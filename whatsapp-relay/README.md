# WhatsApp Relay on Hermes

A dedicated WhatsApp number, linked to its own Hermes in a Docker container (`hermes-whatsapp`) on the VPS. A small plugin inside Hermes does three jobs:

1. **Forwards files.** Anything the source number sends to the bot (photo, PDF, document, video, voice note) goes to your three groups with its original caption. Plain rules, no AI, no cost.
2. **Tracks the ₹2,000 credits.** Statement screenshots or PDFs are read with one Gemini call. Every ₹2,000 credit goes into a local ledger, and you get a WhatsApp confirmation listing the days credited and any working day without a credit.
3. **Keeps you on schedule.** A nudge if 7 days pass with no statement, a month total on the last evening of the month, and a final total once the statement covering the month's last working day arrives.

You can also ask the bot normal questions ("how much came in this month?") and Hermes answers from the same ledger, using the free Gemini key.

## What happens to each message sent to the bot number

| Message | What happens |
|---|---|
| A file from the source number | Forwarded to the groups. The bot never replies to that person. |
| Text from the source number | Ignored. |
| A screenshot or PDF from the firm | Read as a statement; you get the confirmation. The bot never replies to the firm. |
| A screenshot or PDF from you, no caption | Read as a statement. |
| A photo from you with a caption | Goes to Hermes as a normal question. |
| A message from you starting with `!` | A relay command (list below). |
| Anything else from you | Normal chat with Hermes. |
| Anyone else | No reply at all. |
| Messages inside the groups | Ignored. |

Screenshots that arrive within 45 seconds of each other are read together in one Gemini call.

---

## Before you start

- [ ] The bot phone (new number) has WhatsApp and is a member of all three groups
- [ ] Your own WhatsApp number
- [ ] The source number (whose files get forwarded)
- [ ] The firm's number (who sends the statements)
- [ ] A Gemini key (reuse the existing one from https://aistudio.google.com/apikey)
- [ ] A command line on the VPS (SSH, or Hostinger's Browser terminal)
- [ ] `docker compose version` works on the VPS

## Step 1: Copy the package to the VPS

On the laptop, in the folder with the zip:

```bash
scp whatsapp-relay.zip root@200.234.34.123:~
ssh root@200.234.34.123
unzip -o whatsapp-relay.zip && cd whatsapp-relay
```

`scp` needs the VPS login password (or an SSH key on the laptop). If you've forgotten it, reset it in the Hostinger dashboard: **VPS → Manage → Settings → Root password**, then wait for `ct_set_rootpasswd` to show **Success** under **Backup & Monitoring → Latest Actions**. The Browser terminal can run every other step, but it can't receive the zip from your laptop.

No Hermes password is needed: this container is a fresh Hermes with no login.

## Step 2: Answer the setup questions

```bash
bash setup.sh
```

It asks:

1. Your own number
2. The source number
3. The firm's number (Enter to skip if you'll forward statements yourself)
4. A word that appears in the ₹2,000 entry, such as the account's last 4 digits (Enter to skip and count every ₹2,000 credit)
5. Your Gemini key

Numbers with country code (`919847012345`); a plain 10-digit mobile also works. The first run downloads Hermes (a minute or two).
**Expected:** `Saved ...config.json`, `Done.`, then `Next: bash setup.sh pair`.

## Step 3: Link the bot's WhatsApp

Widen the terminal window first.

```bash
bash setup.sh pair
```

- If it asks **Update allowed users?**, press Enter.
- The first time, it installs the WhatsApp bridge before showing the QR code. That takes a few minutes.
- When the **QR code** appears: on the **bot phone**, WhatsApp → Settings → Linked devices → Link a device, and scan.
- If the QR code expires before you scan, press Ctrl+C and run `bash setup.sh pair` again.

**Expected:** `✅ WhatsApp connected.`

## Step 4: Check it answers

From your own phone, send `!status` to the bot number.
**Expected:** a status reply within a few seconds.

## Step 5: Add the three groups

```bash
bash setup.sh groups on
```

Wait about 20 seconds. From your own phone, type `!addgroup` inside each of the three groups.
**Expected:** after each one, the bot messages you privately: *✅ Added (group name)*. The bot never writes anything in the group itself.

```bash
bash setup.sh groups off
```

Then send `!testgroups` to the bot. **Expected:** a test line appears in all three groups.

## Step 6: Turn on the schedules

```bash
bash setup.sh schedules
```

**Expected:** `relay-nudge` and `relay-month-end` listed as active, with next run times in India time (10:15 and 20:45).

## Step 7: Real test

- Ask the source number to send a file. **Expected:** it appears in all three groups within seconds.
- When the firm sends a statement (or you forward one with no caption). **Expected:** a confirmation about 45 seconds later. Check the first one against the screenshot.

**First month:** October days before setup show as "not in any statement yet". Forward the earlier October statements to the bot (no caption) to fill them in, or ignore it; November starts clean.

## Other setup.sh commands

| Command | Use |
|---|---|
| `bash setup.sh status` | Is it running, is WhatsApp connected, which groups, which schedules |
| `bash setup.sh logs` | Follow the relay's log lines (Ctrl+C to stop) |
| `bash setup.sh update` | After unzipping a newer package: copy the new code, pull the latest Hermes, restart |
| `bash setup.sh` | Change numbers, keyword or key later (Enter keeps old answers) |

---

## Commands (send to the bot number from your phone)

| Command | What it does |
|---|---|
| `!status` | What is set up, last statement, this month's total |
| `!report` | This month's report (`!report 2026-09` for another month) |
| `!holiday 2026-10-20` | Mark non-working days (several at once is fine; `20/10/2026` works too) |
| `!holiday remove 2026-10-20` | Undo a holiday |
| `!holidays` | List holidays |
| `!undo` | Remove the entries from the last statement read |
| `!retry` | Read the last failed statement again |
| `!groups` | List the forwarding groups |
| `!removegroup 2` | Stop forwarding to group 2 of that list |
| `!testgroups` | Post a test line in the groups |
| `!reload` | Reload config.json (it also reloads by itself) |
| `!help` | Command list |

**Typed inside a group** (only while `bash setup.sh groups on`): `!addgroup`, `!removegroup`, `!groupid`. Only your own number can use them; the answer comes to you privately.

### Example confirmation

```
Statement read (2 files from the firm)
Covers: Thu 1 Oct – Wed 7 Oct (8 rows)
Matching credits: 5 (₹10,000) · new 5, already had 0
Credited on: 1, 3, 5, 6, 7 Oct
⚠️ No credit on: Fri 2 Oct
October 2026 so far: ₹10,000 from 5 credits
```

If a missing day was a holiday, send `!holiday` with that date and it drops off every report.

## How credits are counted

Gemini only reads the rows (date, description, amount, credit or debit, reference, and which screenshot the row is on). The rules are plain code:

- Only credits count, never debits. The amount must be exactly ₹2,000.
- If a keyword was given in setup, the entry must contain it.
- The same entry seen twice (overlapping screenshots, the same week sent again) is recorded once.
- A day counts as "checked" only if it is visible in a screenshot. Days between two screenshots that weren't shown are listed separately ("not in these screenshots"), not as missing.
- A row with an impossible date (a misread year) is skipped and mentioned in the confirmation.
- Sundays and added holidays are not expected days.
- Every statement image is kept, so any reading can be checked later. `!undo` reverses the last statement if something was misread.

## Settings

`bash setup.sh` writes `data/whatsapp-relay/config.json`. Edit it with `nano data/whatsapp-relay/config.json`; it reloads by itself on the next message. `config.example.json` in this folder explains every setting.

| Setting | Default | What it does |
|---|---|---|
| `forwarding.caption_prefix` | `""` | Text put in front of the original caption, e.g. `"From office: "` |
| `forwarding.notify_owner` | `"failures"` | `"always"` also confirms every forward; `"never"` stays quiet |
| `forwarding.forward_text_messages` | `false` | `true` forwards plain text from the source number too |
| `forwarding.target_groups` | `[]` | Only for a group `!addgroup` can't add (see Troubleshooting) |
| `statement.expected_amount` / `amount_tolerance` | `2000` / `0` | The credit to look for |
| `statement.match_keywords` | your setup answer | Words the entry must contain |
| `statement.batch_seconds` | `45` | Screenshots this close together are read as one statement |
| `statement.nudge_after_days` / `nudge_repeat_days` | `7` / `2` | When the nudge starts, and how often it repeats |
| `calendar.weekly_off` | `["sunday"]` | Days the firm never credits |

## Costs and risks

- **Costs:** zero software cost. Hermes is open source, the WhatsApp route has no Meta fees, Gemini is on the free tier with no card. Forwarding and the schedules use no AI; each statement batch is one Gemini call. Chatting with the bot uses the same free key, and one reply can take several calls, so long conversations use more of the quota.
- **Ban risk:** WhatsApp doesn't allow automated accounts outside its Business API. Kept low by using a separate number, not messaging people who never wrote to it, and not sending in bulk.
- **Session drops:** keep the bot phone on and online at least every week or two, or WhatsApp unlinks the server. Re-link with `bash setup.sh pair`.
- **Free-tier data use:** Google may use free-tier prompts to improve its products, and statement screenshots go to Gemini.
- **Free-tier limits can change:** if Gemini limits tighten, statements can be retried with `!retry`.

## Troubleshooting

| Problem | Fix |
|---|---|
| `!status` gets no reply | `bash setup.sh status`. If WhatsApp isn't connected, `bash setup.sh pair` again. |
| Files aren't forwarded | Check the source number with `!status`, then `!groups` and `!testgroups`. |
| "Couldn't read the statement" | Usually the Gemini key or free quota. Fix the key with `bash setup.sh`, then `!retry`. |
| A screenshot got a chat reply instead of being read | It was sent while Hermes was still answering you. Wait for the reply, then send it again. |
| `!addgroup` does nothing | Run `bash setup.sh groups on` first and wait 20 seconds. If still nothing, send any message in the group, run `bash setup.sh logs`, and copy the `chat=...@g.us` ID into `target_groups` in `data/whatsapp-relay/config.json`. |
| WhatsApp logged out | `bash setup.sh pair` and scan again from the bot phone. |
| `docker compose` not found | Docker Compose isn't installed on the VPS; ask whoever manages the server. |

## Files

Everything lives in `data/` next to this README. Back up the whole agent by copying that folder.

| Path (inside `data/`) | What |
|---|---|
| `whatsapp-relay/config.json` | Settings (numbers, keyword, amount) |
| `whatsapp-relay/groups.json` | Groups added with `!addgroup` |
| `whatsapp-relay/ledger.db` | The credits ledger |
| `whatsapp-relay/statements/` | Every statement image received |
| `whatsapp-relay/forward-log.jsonl` | One line per forward, with any errors |
| `plugins/whatsapp-relay/`, `scripts/relay_*.py` | Plugin code and schedule scripts (copied in by `setup.sh`) |
| `.env`, `config.yaml` | Hermes settings (Gemini key, WhatsApp mode, model) |
| `whatsapp/session/` | The WhatsApp link. Treat it like a password; never share it. |

The container has no ports open and its own Docker network, so it doesn't touch your other Hermes setups or the team's projects. Never start a second Hermes container on the same `data` folder.
