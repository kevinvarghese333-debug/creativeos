# WhatsApp Relay on Hermes

A dedicated WhatsApp number, linked to its own Hermes in a Docker container (`hermes-whatsapp`) on the VPS. A small plugin inside Hermes does three jobs:

1. **Forwards files.** What the source numbers send to the bot goes to your three groups with its original caption: only pictures, or also PDFs, videos and voice notes, as you choose. Plain rules, no AI, no cost.
2. **Tracks the ₹2,000 credits.** Statement screenshots, photos or PDFs are read with one Gemini call. Every ₹2,000 credit goes into a local ledger, kept separately for each account, and you get a WhatsApp confirmation listing the days credited, any day paid late, and any working day without a credit.
3. **Keeps you on schedule.** A nudge if 7 days pass with no statement, a month total on the last evening of the month, and a final total once the statement covering the month's last working day arrives.

You can also ask the bot normal questions ("how much came in this month?") and Hermes answers from the same ledger, using the free Gemini key.

## What happens to each message sent to the bot number

| Message | What happens |
|---|---|
| A picture from a source number | Forwarded to the groups. The bot never replies to that person. |
| Any other file from a source number | Forwarded only if `forward_kinds` includes it (see Settings). |
| Text from a source number | Ignored. |
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

## Step 2: Run setup

```bash
bash setup.sh
```

**If your zip came with a `settings.json`** (your numbers, groups and accounts already filled in), setup shows those settings and asks only for your **Gemini key**.

Otherwise it asks:

1. Your own number
2. The source number (Enter to skip; more can be added from WhatsApp)
3. The firm's number (Enter to skip if you'll forward statements yourself)
4. A word that appears in the ₹2,000 entry, such as the account's last 4 digits (Enter to skip and count every ₹2,000 credit)
5. Your Gemini key

Numbers with country code (`919847012345`); a plain 10-digit mobile also works. The first run downloads Hermes (a minute or two).
**Expected:** `Saved ...config.json`, `Done.`, then `Next: bash setup.sh pair`.

Everything except your own number and the Gemini key can be changed later from WhatsApp: send `!settings` to the bot.

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

## Step 5: Find the three groups

```bash
bash setup.sh groups
```

When the group names are in your settings (`target_group_names`), the bot finds each group by itself the next time anyone posts in it, and messages you privately: *✅ Found AREA MANAGERS (1 of 3)*. Leave the command running; once all are found it stops looking at group messages and lists them. Busy groups are found within minutes. For a quiet group, type `!addgroup` inside it from your own phone. The bot never writes anything in a group except the forwarded files.

Without group names: run `bash setup.sh groups on`, type `!addgroup` inside each group from your own phone, then `bash setup.sh groups off`.

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
| `!settings` | The numbers, accounts and amount in use, and how to change them |
| `!add source 9847012345` / `!remove source …` | Another number whose reports get forwarded / stop forwarding from it |
| `!set firm 9847012345` | Who sends the statements (`!set firm none` to clear) |
| `!add account 1001 Asha` / `!remove account 1001` | Track another account separately / stop tracking it |
| `!set keyword 4417` / `!set amount 2000` | A word the entry must contain / the daily amount |
| `!reload` | Reload config.json (it also reloads by itself) |
| `!help` | Command list |

**Typed inside a group** (only while `bash setup.sh groups on`): `!addgroup`, `!removegroup`, `!groupid`. Only your own number can use them; the answer comes to you privately.

### Example confirmation

```
Statement read (2 files from the firm)

Asha (A/C 1001) · Thu 1 Oct – Fri 16 Oct
Matching credits: 13 (₹26,000) · new 13, already had 0
Credited on: 1, 3, 5, 6, 7, 9 (×2), 10, 12, 13, 14, 15, 16 Oct
⏰ Paid late: 8 Oct (on 9 Oct)
✅ Every working day in this range has its credit.

Ravi (A/C 1002) · Thu 1 Oct – Sat 17 Oct
Matching credits: 13 (₹26,000) · new 13, already had 0
⚠️ No credit on: Thu 1 Oct
Other credits seen (not counted): 1 Oct ₹1,500 rent

October 2026 so far: Asha ₹26,000 · Ravi ₹26,000 · together ₹52,000
```

If a missing day was a holiday, send `!holiday` with that date and it drops off every report.

## How credits are counted

Gemini only reads the rows (date, description, amount, credit or debit, reference, and which screenshot the row is on). The rules are plain code:

- Only credits count, never debits. The amount must be exactly ₹2,000.
- When accounts are set (`!add account 1001 Asha`), each row goes to the account printed in its statement's header, and each account gets its own check and total. Rows for other accounts are ignored and mentioned.
- A day with two credits pays for the earliest earlier working day that had none, shown as "paid late" rather than missing (like the second ₹2,000 on 9 Oct marked "8-10").
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
| `forwarding.forward_kinds` | all four | Which files are forwarded: `image`, `document`, `video`, `audio`. `["image"]` = pictures only |
| `forwarding.target_group_names` | `[]` | Group names `bash setup.sh groups` finds by itself (emoji and capitals don't matter) |
| `statement.accounts` | `[]` | Accounts tracked separately, e.g. `[{"number": "1001", "name": "Asha"}]` |
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
| A screenshot got a chat reply instead of being read | It had a caption. Send statement screenshots with no caption (or start the caption with "statement"). |
| The bot's messages land in your "Message yourself" chat, and `!status` gets no answer | The bot is linked to your own number. WhatsApp hides what you type from the bot's own account, so your number must be a different phone. Run `bash setup.sh questions`, type your other number at question 1 and press Enter for the rest. `bash setup.sh status` warns about this. |
| A group isn't found | Run `bash setup.sh groups` again and type `!addgroup` inside that group from your phone. If still nothing, check the bot number is a member of it. Last resort: send any message in the group while `bash setup.sh groups on` is active, run `bash setup.sh logs`, and copy the `chat=...@g.us` ID into `target_groups` in `data/whatsapp-relay/config.json`. |
| Files reach two groups but not the third | That group may let only admins send messages (common in Communities). Make the bot number an admin there, or allow all members to send. |
| WhatsApp logged out | `bash setup.sh pair` and scan again from the bot phone. |
| `docker compose` not found | Docker Compose isn't installed on the VPS; ask whoever manages the server. |

## Files

Everything lives in `data/` next to this README. Back up the whole agent by copying that folder.

| Path (inside `data/`) | What |
|---|---|
| `whatsapp-relay/config.json` | Settings (numbers, groups, accounts, amount); `settings.json` from the zip is copied in here |
| `whatsapp-relay/groups.json` | Groups added with `!addgroup` |
| `whatsapp-relay/ledger.db` | The credits ledger |
| `whatsapp-relay/statements/` | Every statement image received |
| `whatsapp-relay/forward-log.jsonl` | One line per forward, with any errors |
| `plugins/whatsapp-relay/`, `scripts/relay_*.py` | Plugin code and schedule scripts (copied in by `setup.sh`) |
| `.env`, `config.yaml` | Hermes settings (Gemini key, WhatsApp mode, model) |
| `whatsapp/session/` | The WhatsApp link. Treat it like a password; never share it. |

The container has no ports open and its own Docker network, so it doesn't touch your other Hermes setups or the team's projects. Never start a second Hermes container on the same `data` folder.
