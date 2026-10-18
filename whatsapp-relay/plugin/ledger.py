"""Deposit ledger for the WhatsApp relay plugin.

Standard library only, so the cron scripts in ~/.hermes/scripts/ can import it
with whatever Python Hermes runs them under.

Stores:
  credits      - each matched credit (one row per real entry, de-duplicated)
  statements   - each statement batch received (what dates it covered)
  holidays     - non-working days added from WhatsApp (!holiday)
  meta         - small key/value state (last nudge, reports already sent)
"""

from __future__ import annotations

import calendar
import difflib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

DATA_DIR_NAME = "whatsapp-relay"
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

DEFAULT_CONFIG: dict[str, Any] = {
    "owner_number": "",
    "timezone": "Asia/Kolkata",
    "forwarding": {
        "enabled": True,
        "source_numbers": [],
        "target_groups": [],
        "forward_text_messages": False,
        "caption_prefix": "",
        "notify_owner": "failures",
        "delay_between_sends_seconds": 1.5,
    },
    "statement": {
        "enabled": True,
        "sender_numbers": [],
        "expected_amount": 2000,
        "amount_tolerance": 0,
        "match_keywords": [],
        "batch_seconds": 45,
        "nudge_after_days": 7,
        "nudge_repeat_days": 2,
    },
    "calendar": {
        "weekly_off": ["sunday"],
        "holidays": [],
    },
    "gemini": {
        "model": "gemini-flash-latest",
        "timeout_seconds": 120,
    },
}


# --------------------------------------------------------------------------- paths + config

def hermes_home() -> Path:
    """Hermes home folder: HERMES_HOME, else Hermes' own resolver, else ~/.hermes."""
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    try:
        from hermes_constants import get_hermes_home  # type: ignore

        return Path(get_hermes_home())
    except Exception:
        return Path.home() / ".hermes"


def data_dir(home: Optional[Path] = None) -> Path:
    d = (home or hermes_home()) / DATA_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(home: Optional[Path] = None) -> dict:
    path = data_dir(home) / "config.json"
    user: dict = {}
    if path.exists():
        text = path.read_text(encoding="utf-8")
        # Allow // comments so the example file can explain itself.
        text = re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)
        user = json.loads(text) if text.strip() else {}
    return _deep_merge(DEFAULT_CONFIG, user)


def digits(value: Any) -> str:
    """'+91 98470-12345' -> '919847012345'."""
    return re.sub(r"\D+", "", str(value or ""))


def tz(cfg: dict) -> ZoneInfo:
    try:
        return ZoneInfo(cfg.get("timezone") or "Asia/Kolkata")
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def now_local(cfg: dict) -> datetime:
    fake = os.environ.get("RELAY_NOW", "").strip()  # test hook: pretend it is this ISO time
    if fake:
        d = datetime.fromisoformat(fake)
        return d if d.tzinfo else d.replace(tzinfo=tz(cfg))
    return datetime.now(tz(cfg))


# --------------------------------------------------------------------------- formatting helpers

def inr(amount: float) -> str:
    """Indian digit grouping: 120000 -> '₹1,20,000'."""
    negative = amount < 0
    amount = abs(amount)
    whole = int(round(amount)) if abs(amount - round(amount)) < 0.005 else None
    if whole is None:
        rupees, paise = divmod(round(amount * 100), 100)
        tail = f".{int(paise):02d}"
    else:
        rupees, tail = whole, ""
    s = str(int(rupees))
    if len(s) > 3:
        head, last3 = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups + [last3])
    return f"{'-' if negative else ''}₹{s}{tail}"


def fmt_day(d: date) -> str:
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def fmt_days(days: Iterable[date], limit: int = 12) -> str:
    days = sorted(days)
    if not days:
        return "none"
    shown = [str(d.day) for d in days[:limit]]
    months = {d.strftime("%b") for d in days}
    text = ", ".join(shown)
    if len(months) == 1:
        text += f" {days[0].strftime('%b')}"
    else:
        text = ", ".join(f"{d.day} {d.strftime('%b')}" for d in days[:limit])
    if len(days) > limit:
        text += f" (+{len(days) - limit} more)"
    return text


def month_name(year: int, month: int) -> str:
    return f"{calendar.month_name[month]} {year}"


def parse_iso(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except Exception:
        return None


def norm_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


# --------------------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS statements (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at   TEXT NOT NULL,
    sender        TEXT,
    file_count    INTEGER,
    files         TEXT,
    covered_from  TEXT,
    covered_to    TEXT,
    transactions  INTEGER,
    matched       INTEGER,
    added         INTEGER,
    raw_json      TEXT
);
CREATE TABLE IF NOT EXISTS credits (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_date    TEXT NOT NULL,
    amount        REAL NOT NULL,
    description   TEXT,
    reference     TEXT,
    dedupe_key    TEXT UNIQUE,
    statement_id  INTEGER,
    added_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS credits_date ON credits(entry_date);
CREATE TABLE IF NOT EXISTS coverage (
    statement_id  INTEGER NOT NULL,
    day_from      TEXT NOT NULL,
    day_to        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS coverage_range ON coverage(day_from, day_to);
CREATE TABLE IF NOT EXISTS holidays (
    day           TEXT PRIMARY KEY,
    added_at      TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key           TEXT PRIMARY KEY,
    value         TEXT
);
"""


class Ledger:
    def __init__(self, db_path: Path, cfg: dict):
        self.db_path = Path(db_path)
        self.cfg = cfg
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            if self.get_meta("installed_at", conn=c) is None:
                self.set_meta("installed_at", now_local(cfg).isoformat(), conn=c)

    @classmethod
    def open_default(cls, cfg: Optional[dict] = None, home: Optional[Path] = None) -> "Ledger":
        cfg = cfg or load_config(home)
        return cls(data_dir(home) / "ledger.db", cfg)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------------------------------------------------------------- meta
    def get_meta(self, key: str, default: Optional[str] = None, conn=None) -> Optional[str]:
        if conn is None:
            with self._conn() as c:
                return self.get_meta(key, default, conn=c)
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str, conn=None) -> None:
        if conn is None:
            with self._conn() as c:
                return self.set_meta(key, value, conn=c)
        conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # ---------------------------------------------------------------- calendar
    def holidays(self) -> set[date]:
        days = {parse_iso(d) for d in self.cfg.get("calendar", {}).get("holidays", [])}
        with self._conn() as c:
            days |= {parse_iso(r["day"]) for r in c.execute("SELECT day FROM holidays")}
        return {d for d in days if d}

    def add_holidays(self, days: Iterable[date]) -> list[date]:
        added = []
        with self._conn() as c:
            for d in days:
                cur = c.execute("INSERT OR IGNORE INTO holidays(day, added_at) VALUES(?, ?)",
                                (d.isoformat(), now_local(self.cfg).isoformat()))
                if cur.rowcount:
                    added.append(d)
        return added

    def remove_holidays(self, days: Iterable[date]) -> list[date]:
        removed = []
        with self._conn() as c:
            for d in days:
                cur = c.execute("DELETE FROM holidays WHERE day=?", (d.isoformat(),))
                if cur.rowcount:
                    removed.append(d)
        return removed

    def is_working_day(self, d: date, holidays: Optional[set[date]] = None) -> bool:
        off = {str(w).strip().lower() for w in self.cfg.get("calendar", {}).get("weekly_off", ["sunday"])}
        if WEEKDAYS[d.weekday()] in off:
            return False
        hols = self.holidays() if holidays is None else holidays
        return d not in hols

    def working_days(self, start: date, end: date) -> list[date]:
        hols = self.holidays()
        out, d = [], start
        while d <= end:
            if self.is_working_day(d, hols):
                out.append(d)
            d += timedelta(days=1)
        return out

    # ---------------------------------------------------------------- statements + credits
    def add_statement(self, *, sender: str, files: list[str], covered_from: Optional[date],
                      covered_to: Optional[date], transactions: int, raw: Any,
                      ranges: Optional[list[tuple[date, date]]] = None) -> int:
        """``ranges`` = the date spans actually visible (one per screenshot); defaults to from..to."""
        if ranges is None and covered_from and covered_to:
            ranges = [(covered_from, covered_to)]
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO statements(received_at, sender, file_count, files, covered_from, covered_to,"
                " transactions, matched, added, raw_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (now_local(self.cfg).isoformat(), sender, len(files), json.dumps(files),
                 covered_from.isoformat() if covered_from else None,
                 covered_to.isoformat() if covered_to else None,
                 transactions, 0, 0, json.dumps(raw, ensure_ascii=False, default=str)[:200000]),
            )
            sid = int(cur.lastrowid)
            for a, b in ranges or []:
                c.execute("INSERT INTO coverage(statement_id, day_from, day_to) VALUES(?,?,?)",
                          (sid, a.isoformat(), b.isoformat()))
            return sid

    def finish_statement(self, statement_id: int, matched: int, added: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE statements SET matched=?, added=? WHERE id=?", (matched, added, statement_id))

    @staticmethod
    def dedupe_key(d: date, amount: float, reference: str, description: str) -> str:
        ref = norm_text(reference)
        basis = f"ref:{ref}" if len(ref) >= 4 else f"desc:{norm_text(description)}"
        return f"{d.isoformat()}|{amount:.2f}|{basis}"

    def add_credit(self, *, d: date, amount: float, description: str, reference: str,
                   statement_id: Optional[int]) -> bool:
        """Insert a matched credit. Returns False when it is already recorded."""
        key = self.dedupe_key(d, amount, reference, description)
        with self._conn() as c:
            if c.execute("SELECT 1 FROM credits WHERE dedupe_key=?", (key,)).fetchone():
                return False
            # Same day + same amount with near-identical text = the same entry read twice
            # (overlapping screenshots, slightly different OCR).
            for row in c.execute("SELECT description, reference FROM credits WHERE entry_date=? AND ABS(amount-?)<0.01",
                                 (d.isoformat(), amount)):
                ref_a, ref_b = norm_text(row["reference"]), norm_text(reference)
                if ref_a and ref_b and len(ref_a) >= 4 and len(ref_b) >= 4:
                    if ref_a == ref_b:
                        return False
                    continue  # different references = genuinely different entries
                ratio = difflib.SequenceMatcher(None, norm_text(row["description"]), norm_text(description)).ratio()
                if ratio >= 0.8:
                    return False
            c.execute(
                "INSERT INTO credits(entry_date, amount, description, reference, dedupe_key, statement_id, added_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (d.isoformat(), amount, description, reference, key, statement_id, now_local(self.cfg).isoformat()),
            )
            return True

    def undo_last_statement(self) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM statements ORDER BY id DESC LIMIT 1").fetchone()
            if not row:
                return None
            removed = c.execute("DELETE FROM credits WHERE statement_id=?", (row["id"],)).rowcount
            c.execute("DELETE FROM coverage WHERE statement_id=?", (row["id"],))
            c.execute("DELETE FROM statements WHERE id=?", (row["id"],))
            return {"id": row["id"], "received_at": row["received_at"], "removed_credits": removed}

    def last_statement_at(self) -> Optional[datetime]:
        with self._conn() as c:
            row = c.execute("SELECT received_at FROM statements ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return None
        try:
            return datetime.fromisoformat(row["received_at"])
        except ValueError:
            return None

    def credits_between(self, start: date, end: date) -> list[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute(
                "SELECT * FROM credits WHERE entry_date BETWEEN ? AND ? ORDER BY entry_date, id",
                (start.isoformat(), end.isoformat())))

    def covered_days(self, start: date, end: date) -> set[date]:
        covered: set[date] = set()
        with self._conn() as c:
            rows = list(c.execute(
                "SELECT day_from, day_to FROM coverage WHERE day_to >= ? AND day_from <= ?",
                (start.isoformat(), end.isoformat())))
        for r in rows:
            a, b = parse_iso(r["day_from"]), parse_iso(r["day_to"])
            if not a or not b:
                continue
            d = max(a, start)
            while d <= min(b, end):
                covered.add(d)
                d += timedelta(days=1)
        return covered

    # ---------------------------------------------------------------- reports
    def month_summary(self, year: int, month: int, today: Optional[date] = None) -> dict:
        today = today or now_local(self.cfg).date()
        first = date(year, month, 1)
        last = date(year, month, calendar.monthrange(year, month)[1])
        rows = self.credits_between(first, last)
        credited_days = {parse_iso(r["entry_date"]) for r in rows}
        expected = self.working_days(first, last)
        covered = self.covered_days(first, last)
        upto = min(last, today)
        missing = [d for d in expected if d <= upto and d in covered and d not in credited_days]
        pending = [d for d in expected if d <= upto and d not in covered and d not in credited_days]
        last_expected = expected[-1] if expected else None
        complete = bool(last_expected) and all(d in covered or d in credited_days for d in expected)
        return {
            "year": year, "month": month, "label": month_name(year, month),
            "total": round(sum(float(r["amount"]) for r in rows), 2),
            "count": len(rows),
            "credited_days": sorted(d for d in credited_days if d),
            "expected_days": expected,
            "expected_so_far": [d for d in expected if d <= upto],
            "missing": missing,
            "pending": pending,
            "complete": complete,
            "last_expected": last_expected,
        }

    def month_report(self, year: int, month: int, *, final: bool = False, today: Optional[date] = None) -> str:
        s = self.month_summary(year, month, today=today)
        exp = self.cfg.get("statement", {}).get("expected_amount")
        title = f"**{'Final total' if final else 'Deposit report'}: {s['label']}**"
        lines = [title, f"Received: **{inr(s['total'])}** from {s['count']} credit{'s' if s['count'] != 1 else ''}"]
        so_far = s["expected_so_far"]
        if so_far:
            whole_month = len(so_far) == len(s["expected_days"])
            lines.append(f"Working days: {len(so_far)}" if whole_month
                         else f"Working days so far: {len(so_far)} of {len(s['expected_days'])}")
            if exp:
                label = "Expected" if whole_month else "Expected so far"
                lines.append(f"{label} at {inr(float(exp))}/day: {inr(float(exp) * len(so_far))}")
                short = float(exp) * len(so_far) - s["total"]
                if whole_month and s["complete"] and abs(short) >= 0.01:
                    lines.append(f"{'Short by' if short > 0 else 'Extra'}: {inr(abs(short))}")
        if s["missing"]:
            lines.append(f"⚠️ No credit on: {fmt_days(s['missing'])}")
        if s["pending"]:
            lines.append(f"⏳ Not in any statement yet: {fmt_days(s['pending'])}")
        if not s["missing"] and not s["pending"] and so_far:
            lines.append("✅ Every working day so far is accounted for.")
        if s["missing"]:
            lines.append("If any of those were holidays, send *!holiday YYYY-MM-DD* and they drop off.")
        return "\n".join(lines)


def last_day_of_month(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def previous_month(d: date) -> tuple[int, int]:
    first = d.replace(day=1) - timedelta(days=1)
    return first.year, first.month
