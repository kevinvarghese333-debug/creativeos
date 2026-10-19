"""Unit tests for the relay plugin: message routing, forwarding, groups, statements, commands."""

from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest
from conftest import FIRM, G1, G2, G3, OWNER, SOURCE, STRANGER, drive, event, jid, media_file, write_config

from plugin import extractor as X
from plugin import ledger as L
from plugin import relay as R


# ---------------------------------------------------------------- routing (the table in the notes)
def test_routing_table(relay, home):
    img = media_file(home, "img_aa11bb22cc33.jpg")
    assert drive(relay, event(STRANGER, "hi"))["action"] == "skip", "strangers get silence, and Hermes never sees them"
    lid = event(STRANGER, "hi")
    lid.source.user_id = lid.raw_message["senderId"] = "201234567890123@lid"
    assert drive(relay, lid) is None, "an unresolved LID sender is left to Hermes' own check"
    assert drive(relay, event(OWNER, "what's up?")) is None, "owner chat goes to Hermes"
    assert drive(relay, event(OWNER, "what is this?", media=[(img, "image/jpeg")], media_type="image")) is None, \
        "owner photo with a caption is a question for Hermes"
    assert drive(relay, event(SOURCE, "hello"))["action"] == "skip", "source text is swallowed"
    assert drive(relay, event(FIRM, "here you go"))["action"] == "skip", "the firm never gets a reply"
    assert drive(relay, event(STRANGER, "hello", group=G1))["action"] == "skip", "groups stay silent"
    assert drive(relay, event(OWNER, "!status"))["action"] == "skip"


def test_not_configured_passes_everything(home):
    from plugin.relay import Relay
    write_config(home, owner_number="")
    r = Relay(home)
    assert asyncio.run(r.handle(event(SOURCE, "x"), None)) is None


def test_other_platforms_ignored(relay):
    ev = event(OWNER, "!status")
    ev.source.platform = "telegram"
    assert drive(relay, ev) is None


# ---------------------------------------------------------------- forwarding
def add_groups(relay, *ids):
    relay.save_groups([{"id": g, "name": relay.adapter_obj.names.get(g, ""), "added_at": ""} for g in ids])


def test_photo_forwarded_to_every_group_with_caption(relay, home):
    add_groups(relay, G1, G2, G3)
    img = media_file(home, "img_aa11bb22cc33.jpg")
    assert drive(relay, event(SOURCE, "Invoice 12", media=[(img, "image/jpeg")], media_type="image"))["action"] == "skip"
    sends = relay.adapter_obj.sent
    assert [(s["kind"], s["chat"], s["caption"]) for s in sends] == [("image", g, "Invoice 12") for g in (G1, G2, G3)]
    log = [json.loads(x) for x in (home / "whatsapp-relay/forward-log.jsonl").read_text().splitlines()]
    assert len(log) == 1 and all(s["ok"] for s in log[0]["sends"])


def test_pdf_keeps_original_file_name(relay, home):
    add_groups(relay, G1)
    doc = media_file(home, "doc_0123456789ab_Oct_Letter.pdf")
    drive(relay, event(SOURCE, "", media=[(doc, "application/pdf")], media_type="document", file_name="Oct Letter.pdf"))
    (s,) = relay.adapter_obj.sent
    assert s["kind"] == "document" and s["file_name"] == "Oct Letter.pdf" and s["caption"] is None


def test_document_name_recovered_from_cache_name(relay, home):
    add_groups(relay, G1)
    doc = media_file(home, "doc_0123456789ab_Bill.pdf")
    drive(relay, event(SOURCE, "", media=[(doc, "application/pdf")], media_type="document"))
    assert relay.adapter_obj.sent[0]["file_name"] == "Bill.pdf"


def test_voice_note_and_video(relay, home):
    add_groups(relay, G1)
    aud = media_file(home, "aud_aa11bb22cc33.ogg")
    vid = media_file(home, "vid_aa11bb22cc33.mp4")
    drive(relay, event(SOURCE, "", media=[(aud, "audio/ogg")], media_type="ptt"))
    drive(relay, event(SOURCE, "site visit", media=[(vid, "video/mp4")], media_type="video"))
    assert [(s["kind"], s["caption"]) for s in relay.adapter_obj.sent] == [("audio", None), ("video", "site visit")]


def test_failed_download_tells_owner(relay):
    add_groups(relay, G1)
    ev = event(SOURCE, "[image could not be downloaded]", media_type="image", has_media=True)
    drive(relay, ev)
    (msg,) = relay.adapter_obj.texts_to(jid(OWNER))
    assert "could not be downloaded" in msg and not relay.adapter_obj.texts_to(G1)


def test_failed_send_reported(relay, home):
    add_groups(relay, G1, G2)
    relay.adapter_obj.fail_for.add(G2)
    drive(relay, event(SOURCE, "", media=[(media_file(home, "img_1.jpg"), "image/jpeg")], media_type="image"))
    (msg,) = relay.adapter_obj.texts_to(jid(OWNER))
    assert "failed for 1 of 2" in msg and G2 in msg


def test_no_groups_yet(relay, home):
    drive(relay, event(SOURCE, "", media=[(media_file(home, "img_1.jpg"), "image/jpeg")], media_type="image"))
    (msg,) = relay.adapter_obj.texts_to(jid(OWNER))
    assert "no forwarding groups" in msg and "bash setup.sh groups`" in msg and "!addgroup" in msg


def test_sender_name_formatting_is_dropped(relay, home):
    ev = event(SOURCE, "", media=[(media_file(home, "img_1.jpg"), "image/jpeg")], media_type="image")
    ev.source.user_name = "~Asha K~❤️"
    drive(relay, ev)
    (msg,) = relay.adapter_obj.texts_to(jid(OWNER))
    assert "A file from Asha K❤️ arrived" in msg


def test_notify_always_and_caption_prefix(relay, home):
    write_config(home, forwarding__notify_owner="always", forwarding__caption_prefix="From office: ",
                 forwarding__source_numbers=[SOURCE], forwarding__delay_between_sends_seconds=0)
    relay.reload()
    add_groups(relay, G1)
    drive(relay, event(SOURCE, "Bill", media=[(media_file(home, "img_1.jpg"), "image/jpeg")], media_type="image"))
    assert relay.adapter_obj.sent[0]["caption"] == "From office: Bill"
    assert relay.adapter_obj.texts_to(jid(OWNER))[0].startswith("✅ Forwarded")


def test_ten_digit_numbers_and_lid_free_matching(relay, home):
    write_config(home, owner_number="9847012345", forwarding__source_numbers=["+91 98470-99999"],
                 forwarding__delay_between_sends_seconds=0)
    relay.reload()
    add_groups(relay, G1)
    drive(relay, event(SOURCE, "", media=[(media_file(home, "img_1.jpg"), "image/jpeg")], media_type="image"))
    assert relay.adapter_obj.sent and relay.adapter_obj.sent[0]["chat"] == G1


# ---------------------------------------------------------------- groups
def test_addgroup_flow(relay):
    assert drive(relay, event(OWNER, "!addgroup", group=G1))["action"] == "skip"
    assert relay.adapter_obj.texts_to(jid(OWNER)) == ["✅ Added Office A\nFiles from the source number now go to 1 group."]
    assert not relay.adapter_obj.texts_to(G1), "the bot never writes in the group"
    drive(relay, event(OWNER, "!addgroup", group=G2))
    drive(relay, event(OWNER, "!addgroup", group=G1))
    assert "already a forwarding group" in relay.adapter_obj.texts_to(jid(OWNER))[-1]
    assert [g["name"] for g in relay.target_groups()] == ["Office A", "Office B"]


def test_stranger_cannot_add_group(relay):
    drive(relay, event(STRANGER, "!addgroup", group=G1))
    assert relay.target_groups() == [] and relay.adapter_obj.sent == []


def test_groupid_and_removegroup(relay):
    drive(relay, event(OWNER, "!groupid", group=G3))
    assert relay.adapter_obj.texts_to(jid(OWNER))[-1] == f"Group ID for Office C:\n{G3}"
    drive(relay, event(OWNER, "!addgroup", group=G3))
    drive(relay, event(OWNER, "!removegroup", group=G3))
    assert relay.adapter_obj.texts_to(jid(OWNER))[-1].startswith("Removed Office C")
    assert relay.target_groups() == []


def test_config_groups_merge_and_placeholders_ignored(relay, home):
    write_config(home, forwarding__target_groups=["1203630XXXXXXXXXXX@g.us", G2, G2],
                 forwarding__source_numbers=[SOURCE])
    relay.reload()
    add_groups(relay, G1, G2)
    assert [(g["id"], g["source"]) for g in relay.target_groups()] == [(G2, "config"), (G1, "added")]
    drive(relay, event(OWNER, "!removegroup", group=G2))
    assert "config.json" in relay.adapter_obj.texts_to(jid(OWNER))[-1]


def test_groups_list_and_remove_by_number(relay):
    add_groups(relay, G1, G2, G3)
    drive(relay, event(OWNER, "!groups"))
    listing = relay.adapter_obj.texts_to(jid(OWNER))[-1]
    assert "1. Office A" in listing and "3. Office C" in listing
    drive(relay, event(OWNER, "!removegroup 2"))
    assert relay.adapter_obj.texts_to(jid(OWNER))[-1].startswith("Removed Office B")
    assert [g["id"] for g in relay.target_groups()] == [G1, G3]


def test_testgroups(relay):
    add_groups(relay, G1, G2)
    drive(relay, event(OWNER, "!testgroups"))
    assert relay.adapter_obj.texts_to(G1) and relay.adapter_obj.texts_to(G2)
    assert "✅ Office A" in relay.adapter_obj.texts_to(jid(OWNER))[-1]


def test_corrupt_groups_file_does_not_break_forwarding(relay, home):
    (home / "whatsapp-relay/groups.json").write_text("{not json")
    assert relay.target_groups() == []


# ---------------------------------------------------------------- commands
def test_split_commands():
    assert R.split_commands("!status\n!report 2026-09") == ["!status", "!report 2026-09"]
    assert R.split_commands("!holiday 2026-10-20\n2026-10-21") == ["!holiday 2026-10-20 2026-10-21"]


def test_two_commands_in_one_message(relay):
    drive(relay, event(OWNER, "!holidays\n!groups"))
    texts = relay.adapter_obj.texts_to(jid(OWNER))
    assert len(texts) == 2 and texts[0].startswith("Holidays:") and "No forwarding groups" in texts[1]


def test_status_and_help(relay):
    drive(relay, event(OWNER, "!status"))
    status = relay.adapter_obj.texts_to(jid(OWNER))[-1]
    assert "Relay status" in status and SOURCE in status and FIRM in status and "4417" in status
    drive(relay, event(OWNER, "!help"))
    assert "!addgroup" in relay.adapter_obj.texts_to(jid(OWNER))[-1]
    drive(relay, event(OWNER, "!bogus"))
    assert "Unknown command" in relay.adapter_obj.texts_to(jid(OWNER))[-1]


def test_holiday_commands(relay):
    drive(relay, event(OWNER, "!holiday 2026-10-20 21/10/2026"))
    assert relay.adapter_obj.texts_to(jid(OWNER))[-1] == "Marked as holiday: Tue 20 Oct, Wed 21 Oct"
    drive(relay, event(OWNER, "!holiday remove 2026-10-20"))
    assert relay.ledger.holidays() == {date(2026, 10, 21)}
    drive(relay, event(OWNER, "!holiday tomorrow"))
    assert "Use !holiday" in relay.adapter_obj.texts_to(jid(OWNER))[-1]


# ---------------------------------------------------------------- statements
def row(day, amount=2000, direction="credit", desc="NEFT CR SB XXXX4417 DAILY", ref=None, image=1, month=10, year=2026):
    return {"date": f"{year}-{month:02d}-{day:02d}", "description": desc, "amount": amount, "direction": direction,
            "reference": ref if ref is not None else f"N{month:02d}{day:02d}", "image": image}


@pytest.fixture
def gemini(monkeypatch):
    calls = []
    answers = []

    async def fake_extract(files, **kw):
        calls.append((files, kw))
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(X, "extract", fake_extract)
    return SimpleGemini(calls, answers)


class SimpleGemini:
    def __init__(self, calls, answers):
        self.calls, self.answers = calls, answers

    def will_return(self, *rows_or_results):
        for r in rows_or_results:
            self.answers.append(r if isinstance(r, (dict, Exception)) else {"is_statement": True, "transactions": r})


def statement_from(relay, home, sender=FIRM, n=1, caption=""):
    for i in range(n):
        drive(relay, event(sender, caption, media=[(media_file(home, f"img_{i}.jpg"), "image/jpeg")], media_type="image"))


def test_firm_statement_confirmation(relay, home, gemini):
    gemini.will_return([row(12), row(13), row(15), row(16), row(17), row(14, 500, "debit", "ATM WDL"),
                        row(16, 15000, desc="SALARY OCT", ref="S1"), row(15, 2000, desc="UPI FROM FRIEND", ref="U9")])
    statement_from(relay, home)
    (msg,) = relay.adapter_obj.texts_to(jid(OWNER))
    assert "Statement read** (1 file from the firm)" in msg
    assert "Covers: Mon 12 Oct – Sat 17 Oct (8 rows)" in msg
    assert "Matching credits: 5 (₹10,000) · new 5, already had 0" in msg
    assert "No credit on: Wed 14 Oct" in msg and "SALARY" in msg
    assert "October 2026 so far: **₹10,000** from 5 credits" in msg
    assert not relay.adapter_obj.texts_to(jid(FIRM))
    assert len(list((home / "whatsapp-relay/statements").rglob("*.jpg"))) == 1
    assert "4417" in gemini.calls[0][1]["hint"]


def test_unexpected_reading_still_gets_an_answer(relay, home, gemini, monkeypatch):
    gemini.will_return([row(12)])

    def broken(*a, **kw):
        raise ValueError("odd row")
    monkeypatch.setattr(relay, "apply_result", broken)
    statement_from(relay, home, sender=OWNER)
    (msg,) = relay.adapter_obj.texts_to(jid(OWNER))
    assert "Statement processing hit an error: odd row" in msg and "!retry" in msg
    assert relay.last_failed is not None


def test_screenshots_batched_into_one_call(relay, home, gemini, monkeypatch):
    write_config(home, statement__batch_seconds=0.3, statement__sender_numbers=[FIRM], statement__match_keywords=[])
    relay.reload()
    gemini.will_return([row(1, image=1), row(2, image=2)])

    async def two_quick():
        for i in range(2):
            await relay.handle(event(FIRM, "", media=[(media_file(home, f"img_{i}.jpg"), "image/jpeg")], media_type="image"), relay.gateway)
        while relay.tasks:
            await asyncio.gather(*list(relay.tasks), return_exceptions=True)
    asyncio.run(two_quick())
    assert len(gemini.calls) == 1 and len(gemini.calls[0][0]) == 2
    assert "2 files from the firm" in relay.adapter_obj.texts_to(jid(OWNER))[0]


def test_overlapping_screenshots_counted_once(relay, home, gemini):
    gemini.will_return([row(5, image=1), row(6, image=1), row(6, image=2, desc="NEFT CR SB XXXX4417 DAlLY"), row(7, image=2)])
    statement_from(relay, home)
    assert "Matching credits: 3 (₹6,000)" in relay.adapter_obj.texts_to(jid(OWNER))[0]
    gemini.will_return([row(6), row(7), row(8)])
    statement_from(relay, home)
    assert "new 1, already had 2" in relay.adapter_obj.texts_to(jid(OWNER))[1]
    assert relay.ledger.month_summary(2026, 10)["count"] == 4


def test_misread_year_skipped(relay, home, gemini):
    gemini.will_return([row(5), row(6, year=2062), row(7, year=2025)])
    statement_from(relay, home)
    msg = relay.adapter_obj.texts_to(jid(OWNER))[0]
    assert "Skipped 2 row(s)" in msg and "Matching credits: 1" in msg


def test_gap_between_screenshots_not_called_missing(relay, home, gemini):
    gemini.will_return([row(1, image=1), row(2, image=1), row(8, image=2), row(9, image=2)])
    statement_from(relay, home)
    msg = relay.adapter_obj.texts_to(jid(OWNER))[0]
    assert "No credit on" not in msg
    assert "Not in these screenshots: 3, 5, 6, 7 Oct" in msg  # 4 Oct is a Sunday


def test_keyword_and_amount_rules(relay, home, gemini):
    gemini.will_return([row(5), row(6, desc="NEFT CR OTHER ACCOUNT", ref=""), row(7, 2001), row(8, direction="unknown")])
    statement_from(relay, home)
    assert "Matching credits: 2" in relay.adapter_obj.texts_to(jid(OWNER))[0]


def test_not_a_statement(relay, home, gemini):
    gemini.will_return({"is_statement": False, "transactions": []})
    statement_from(relay, home)
    assert "doesn't look like a bank statement" in relay.adapter_obj.texts_to(jid(OWNER))[0]


def test_owner_statement_needs_no_caption(relay, home, gemini):
    gemini.will_return([row(5)])
    statement_from(relay, home, sender=OWNER)
    assert "from you" in relay.adapter_obj.texts_to(jid(OWNER))[0]
    gemini.will_return([row(6)])
    statement_from(relay, home, sender=OWNER, caption="statement for this week")
    assert len(gemini.calls) == 2


def test_retry_and_undo(relay, home, gemini):
    gemini.will_return(X.ExtractionError("Gemini call failed (HTTP 429: quota)."), [row(5), row(6)])
    statement_from(relay, home)
    assert "Couldn't read the statement" in relay.adapter_obj.texts_to(jid(OWNER))[0]
    drive(relay, event(OWNER, "!retry"))
    texts = relay.adapter_obj.texts_to(jid(OWNER))
    assert texts[1] == "Reading the 1 file(s) again…" and "Matching credits: 2" in texts[2]
    drive(relay, event(OWNER, "!retry"))
    assert relay.adapter_obj.texts_to(jid(OWNER))[-1] == "Nothing to retry."
    drive(relay, event(OWNER, "!undo"))
    assert "and its 2 credit(s)" in relay.adapter_obj.texts_to(jid(OWNER))[-1]
    assert relay.ledger.month_summary(2026, 10)["count"] == 0


def test_holiday_removes_missing_day(relay, home, gemini):
    gemini.will_return([row(12), row(13), row(15)])
    statement_from(relay, home)
    assert "No credit on: Wed 14 Oct" in relay.adapter_obj.texts_to(jid(OWNER))[0]
    drive(relay, event(OWNER, "!holiday 2026-10-14"))
    drive(relay, event(OWNER, "!report"))
    report = relay.adapter_obj.texts_to(jid(OWNER))[-1]
    assert "No credit on" not in report and "Received: **₹6,000** from 3 credits" in report


def test_final_month_total(relay, home, gemini, monkeypatch):
    monkeypatch.setenv("RELAY_NOW", "2026-11-04T11:00:00")
    days = [d for d in range(26, 32) if date(2026, 10, d).weekday() != 6]
    gemini.will_return([row(d) for d in days] + [row(2, month=11), row(3, month=11)])
    statement_from(relay, home)
    texts = relay.adapter_obj.texts_to(jid(OWNER))
    finals = [t for t in texts if "Final total: October 2026" in t]
    assert len(finals) == 1
    gemini.will_return([row(31)])
    statement_from(relay, home)
    assert sum("Final total" in t for t in relay.adapter_obj.texts_to(jid(OWNER))) == 1, "the final total is sent once"


def test_deposit_tracker_tool(relay, home, gemini):
    gemini.will_return([row(12), row(13)])
    statement_from(relay, home)
    out = relay.tool_summary("2026-10")
    (acct,) = out["accounts"]
    assert out["total_rupees_all_accounts"] == 4000 and acct["credits"] == 2 and "2026-10-12" in acct["credited_days"]


# ---------------------------------------------------------------- ledger + time
def test_timezone_fallback(monkeypatch):
    def broken(name):
        raise L.ZoneInfoNotFoundError(name) if hasattr(L, "ZoneInfoNotFoundError") else KeyError(name)
    monkeypatch.setattr(L, "ZoneInfo", broken)
    zone = L.tz({"timezone": "Asia/Kolkata"})
    assert zone.utcoffset(None).total_seconds() == 5.5 * 3600
    monkeypatch.delenv("RELAY_NOW", raising=False)
    assert L.now_local({}).utcoffset().total_seconds() == 5.5 * 3600


def test_inr_grouping():
    assert L.inr(120000) == "₹1,20,000" and L.inr(2000) == "₹2,000" and L.inr(1234567.5) == "₹12,34,567.50"


def test_parse_day():
    assert R.parse_day("20/10/2026") == date(2026, 10, 20)
    assert R.parse_day("2026-10-20") == date(2026, 10, 20)
    assert R.parse_day("31/02/2026") is None


# ---------------------------------------------------------------- extractor
def test_parse_response_shapes():
    good = {"candidates": [{"content": {"parts": [{"text": "thinking", "thought": True},
                                                   {"text": '```json\n{"is_statement": true, "transactions": []}\n```'}]}}]}
    assert X.parse_response(good)["is_statement"] is True
    with pytest.raises(X.ExtractionError):
        X.parse_response({"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}})
    with pytest.raises(X.ExtractionError):
        X.parse_response({"candidates": [{"content": {"parts": [{"text": "not json"}]}}]})


def test_payload_size_limit(tmp_path, monkeypatch):
    big = tmp_path / "big.png"
    big.write_bytes(b"x" * 1024)
    monkeypatch.setattr(X, "MAX_INLINE_BYTES", 1000)
    with pytest.raises(X.ExtractionError):
        X.build_payload([(str(big), "image/png")], today=date(2026, 10, 18), hint="")


def test_extract_without_key(monkeypatch, tmp_path):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with pytest.raises(X.ExtractionError, match="No Gemini key"):
        asyncio.run(X.extract([], model="m", today=date(2026, 10, 18)))


def test_key_read_from_env_file(monkeypatch, tmp_path):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("OTHER=1\nGOOGLE_API_KEY='AIzaFromFile'\n")
    assert X.api_key() == "AIzaFromFile"
