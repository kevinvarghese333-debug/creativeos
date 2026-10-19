"""Two accounts in one statement batch, late payments, images-only forwarding, groups found by name,
and settings changed from WhatsApp. The statement rows are laid out like real 1-17 Oct ledger photos."""

from __future__ import annotations

import json
import sqlite3
from datetime import date

from conftest import FIRM, G1, G2, G3, OWNER, SOURCE, STRANGER, drive, event, jid, media_file, write_config
from test_relay import gemini, statement_from  # noqa: F401  (fixture + helper)

from plugin import ledger as L
from plugin import relay as R

from statement_rows import ACCOUNT_A, ACCOUNT_B, ACCOUNTS, rows


def two_accounts(home, **extra):
    write_config(home, statement__sender_numbers=[FIRM], statement__match_keywords=[], statement__batch_seconds=0,
                 statement__accounts=ACCOUNTS,
                 calendar={"weekly_off": ["sunday"], "holidays": ["2026-10-02"]}, **extra)


def test_two_ledgers_in_one_batch(relay, home, gemini):  # noqa: F811
    two_accounts(home)
    relay.reload()
    gemini.will_return(rows(ACCOUNT_A, "1001", 1) + rows(ACCOUNT_B, "1,002.00", 2))
    statement_from(relay, home, n=1)
    (msg,) = relay.adapter_obj.texts_to(jid(OWNER))
    asha, ravi = msg.split("**Ravi**")
    assert "**Asha** (A/C 1001) · Thu 1 Oct – Fri 16 Oct" in asha
    assert "Matching credits: 13 (₹26,000)" in asha and "9 (×2)" in asha
    assert "⏰ Paid late: 8 Oct (on 9 Oct)" in asha and "No credit on" not in asha
    assert "(A/C 1002) · Thu 1 Oct – Sat 17 Oct" in ravi
    assert "Matching credits: 13 (₹26,000)" in ravi and "⚠️ No credit on: Thu 1 Oct" in ravi
    assert "1 Oct ₹1,500 rent" in ravi
    assert "October 2026 so far: Asha ₹26,000 · Ravi ₹26,000 · together **₹52,000**" in msg

    report = relay.ledger.month_report(2026, 10, today=date(2026, 10, 18))
    assert "**Asha** (A/C 1001)" in report and "**Ravi** (A/C 1002)" in report
    assert "Paid late: 8 Oct (on 9 Oct)" in report and "Together: **₹52,000** from 26 credits" in report

    # The same two photos again: nothing is counted twice.
    gemini.will_return(rows(ACCOUNT_A, "1001", 1) + rows(ACCOUNT_B, "1002", 2))
    statement_from(relay, home, n=1)
    assert relay.adapter_obj.texts_to(jid(OWNER))[-1].count("new 0, already had 13") == 2


def test_rows_attributed_by_screenshot_and_other_accounts_ignored(relay, home, gemini):  # noqa: F811
    two_accounts(home)
    relay.reload()
    kev = rows(ACCOUNT_A[:3], "1001", 1) + [{**r, "account": ""} for r in rows(ACCOUNT_A[6:8], "", 1)]
    other = rows([(5, 2000, 0, "", "1")], "7777", 3)
    gemini.will_return(kev + other)
    statement_from(relay, home)
    msg = relay.adapter_obj.texts_to(jid(OWNER))[0]
    assert "Matching credits: 3 (₹6,000)" in msg, "the unlabelled 5 Oct row joins Asha's screenshot"
    assert "Ignored rows for other accounts: A/C 7777" in msg and "Ravi" not in msg.split("so far")[0]


def test_final_total_waits_for_every_account(relay, home, gemini, monkeypatch):  # noqa: F811
    two_accounts(home)
    relay.reload()
    monkeypatch.setenv("RELAY_NOW", "2026-11-03T10:00:00")
    gemini.will_return(rows([(31, 2000, 0, "", "1")], "1001", 1))
    statement_from(relay, home)
    assert not any("Final total" in t for t in relay.adapter_obj.texts_to(jid(OWNER)))
    gemini.will_return(rows([(31, 2000, 0, "", "2")], "1002", 1))
    statement_from(relay, home)
    assert any("Final total: October 2026" in t for t in relay.adapter_obj.texts_to(jid(OWNER)))


def test_settle_days():
    days = {date(2026, 10, d) for d in (7, 8, 9, 10)}
    counts = {date(2026, 10, 7): 1, date(2026, 10, 9): 2}
    unpaid, late = L.settle_days(date(2026, 10, 7), date(2026, 10, 10), counts, days, days)
    assert late == [(date(2026, 10, 8), date(2026, 10, 9))] and unpaid == [date(2026, 10, 10)]


def test_account_matching():
    accts = L.tracked_accounts({"statement": {"accounts": [{"number": "1,001.00"}, "1002"]}})
    assert [a["number"] for a in accts] == ["1001", "1002"] and accts[0]["name"] == "A/C 1001"
    assert L.match_account("SB A/C-1,001.00", accts) == "1001"
    assert L.match_account("XXXX1002", accts) == "1002"
    assert L.match_account("4417", accts) is None and L.match_account("", accts) is None


def test_old_ledger_is_upgraded(tmp_path):
    db = tmp_path / "ledger.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE credits (id INTEGER PRIMARY KEY, entry_date TEXT NOT NULL, amount REAL NOT NULL,"
                      " description TEXT, reference TEXT, dedupe_key TEXT UNIQUE, statement_id INTEGER, added_at TEXT NOT NULL);"
                      "CREATE TABLE coverage (statement_id INTEGER NOT NULL, day_from TEXT NOT NULL, day_to TEXT NOT NULL);"
                      "INSERT INTO credits VALUES (1, '2026-10-05', 2000, '', 'N1', 'k', 1, '2026-10-06');")
    con.commit()
    con.close()
    led = L.Ledger(db, L.DEFAULT_CONFIG)
    assert led.month_summary(2026, 10, today=date(2026, 10, 18))["count"] == 1


# ---------------------------------------------------------------- forwarding only images
def test_only_images_forwarded(relay, home):
    write_config(home, forwarding__source_numbers=[SOURCE, "919800000003"], forwarding__forward_kinds=["image"],
                 forwarding__delay_between_sends_seconds=0)
    relay.reload()
    relay.save_groups([{"id": G1, "name": "Office A"}])
    drive(relay, event(SOURCE, "Gold Outstanding as on 17-10-2026", media=[(media_file(home, "img_1.jpg"), "image/jpeg")], media_type="image"))
    drive(relay, event("919800000003", "", media=[(media_file(home, "doc_1_shot.png"), "image/png")], media_type="document",
                       file_name="shot.png"))
    drive(relay, event(SOURCE, "", media=[(media_file(home, "doc_2_r.pdf"), "application/pdf")], media_type="document"))
    drive(relay, event(SOURCE, "", media=[(media_file(home, "aud_1.ogg"), "audio/ogg")], media_type="ptt"))
    drive(relay, event(SOURCE, "Ok Sir"))
    sent = relay.adapter_obj.sent
    assert [(s["kind"], s["caption"]) for s in sent] == [("image", "Gold Outstanding as on 17-10-2026"), ("document", None)]
    assert not relay.adapter_obj.texts_to(jid(OWNER)), "skipped files are not reported to the owner"


def test_file_category():
    assert R.file_category("document", [("/x/a.pdf", "application/pdf")]) == "document"
    assert R.file_category("document", [("/x/a.png", "image/png")]) == "image"
    assert R.file_category("ptt", [("/x/a.ogg", "audio/ogg")]) == "audio"
    assert R.file_category("gif", [("/x/a.mp4", "video/mp4")]) == "video"


# ---------------------------------------------------------------- groups found by name
def test_groups_found_by_name(relay, home):
    write_config(home, forwarding__source_numbers=[SOURCE],
                 forwarding__target_group_names=["AREA MANAGERS", "Asst Area Managers", "MANAGERS"])
    relay.reload()
    family = "120363000000000009@g.us"
    relay.adapter_obj.names.update({G1: "AREA MANAGERS ♻️⚧️", G2: "Asst Area Managers", G3: "🎯MANAGERS🎯", family: "Family"})
    drive(relay, event(STRANGER, "Thiruvankulam", group=G1))
    drive(relay, event(STRANGER, "photo", group=G1))
    drive(relay, event(STRANGER, "hi", group=family))
    notes = relay.adapter_obj.texts_to(jid(OWNER))
    assert notes == ["✅ Found AREA MANAGERS ♻️⚧️ (1 of 3). Reports will be forwarded there.\n"
                     "Still looking for: Asst Area Managers, MANAGERS"]
    drive(relay, event(STRANGER, "Ashtamichira", group=G2))
    drive(relay, event(OWNER, "morning", group=G3))
    assert relay.adapter_obj.texts_to(jid(OWNER))[-1].endswith("All groups found.")
    assert [g["id"] for g in relay.target_groups()] == [G1, G2, G3]
    assert not any(relay.adapter_obj.texts_to(g) for g in (G1, G2, G3, family)), "never writes in a group"
    drive(relay, event(OWNER, "!status"))
    assert "Groups not found yet" not in relay.adapter_obj.texts_to(jid(OWNER))[-1]


def test_group_key():
    assert L.group_key("AREA MANAGERS ♻️⚧️") == "area managers"
    assert L.group_key("🎯MANAGERS🎯") == "managers"
    assert L.group_key("Asst  Area Managers") == "asst area managers"


# ---------------------------------------------------------------- settings from WhatsApp
def settings(relay, home):
    return json.loads((home / "whatsapp-relay/config.json").read_text())


def say(relay, text):
    drive(relay, event(OWNER, text))
    return relay.adapter_obj.texts_to(jid(OWNER))[-1]


def test_settings_commands(relay, home):
    assert "Source numbers: 919847099999" in say(relay, "!settings")
    assert say(relay, "!add source 9800000003, +91 98000 00005").startswith("✅ Saved.")
    assert settings(relay, home)["forwarding"]["source_numbers"] == [SOURCE, "919800000003", "919800000005"]
    say(relay, "!remove source 9800000003")
    assert settings(relay, home)["forwarding"]["source_numbers"] == [SOURCE, "919800000005"]
    assert "isn't in the list" in say(relay, "!remove source 9000000000")
    say(relay, "!set firm none")
    assert settings(relay, home)["statement"]["sender_numbers"] == []
    say(relay, "!add account 1001 Asha")
    say(relay, "!add account 1002 Ravi")
    say(relay, "!add account 1001 Kev")
    assert settings(relay, home)["statement"]["accounts"] == [{"number": "1002", "name": "Ravi"}, {"number": "1001", "name": "Kev"}]
    say(relay, "!remove account 1002")
    assert [a["number"] for a in relay.ledger.accounts()] == ["1001"]
    say(relay, "!set keyword none")
    say(relay, "!set amount ₹2,500")
    cfg = settings(relay, home)
    assert cfg["statement"]["match_keywords"] == [] and cfg["statement"]["expected_amount"] == 2500
    assert "doesn't look like a phone number" in say(relay, "!add source 12345")
    assert "your own number" in say(relay, f"!set firm {OWNER}")
    assert "Use !set amount" in say(relay, "!add amount 5")
    assert "!add source" in say(relay, "!set colour blue")
    # Messages from a newly added source are forwarded straight away.
    relay.save_groups([{"id": G1, "name": "Office A"}])
    drive(relay, event("919800000005", "x", media=[(media_file(home, "img_9.jpg"), "image/jpeg")], media_type="image"))
    assert relay.adapter_obj.sent[-1]["chat"] == G1
