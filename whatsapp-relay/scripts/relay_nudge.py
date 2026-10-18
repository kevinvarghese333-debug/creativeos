"""Hermes no-agent cron script: nudge the owner when no statement has arrived for a week.

Runs daily. Prints nothing (silent tick) unless a nudge is due; Hermes delivers whatever
this prints to WhatsApp. Uses no AI tokens.
"""

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent  # <hermes home>/scripts/ -> <hermes home>


def load_ledger():
    path = HOME / "plugins" / "whatsapp-relay" / "ledger.py"
    spec = importlib.util.spec_from_file_location("relay_ledger", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    L = load_ledger()
    cfg = L.load_config(HOME)
    stm = cfg.get("statement", {})
    if not stm.get("enabled", True):
        return 0
    ledger = L.Ledger(L.data_dir(HOME) / "ledger.db", cfg)
    now = L.now_local(cfg)

    last = ledger.last_statement_at()
    since = last or datetime.fromisoformat(ledger.get_meta("installed_at") or now.isoformat())
    if since.tzinfo is None:
        since = since.replace(tzinfo=now.tzinfo)
    age_days = (now - since).total_seconds() / 86400
    after = float(stm.get("nudge_after_days") or 7)
    if age_days < after:
        return 0

    repeat = float(stm.get("nudge_repeat_days") or 2)
    last_nudge = ledger.get_meta("last_nudge_at")
    if last_nudge:
        prev = datetime.fromisoformat(last_nudge)
        if prev.tzinfo is None:
            prev = prev.replace(tzinfo=now.tzinfo)
        if now - prev < timedelta(days=repeat) - timedelta(hours=2):
            return 0

    days = int(age_days)
    if last:
        msg = (f"⏰ No statement for {days} days (last one came on {last.strftime('%a %d %b')}).\n"
               "Please forward this week's statement screenshots to this number.")
    else:
        msg = (f"⏰ No statement has arrived yet ({days} days since the tracker started).\n"
               "Forward the statement screenshots to this number when you get them.")
    s = ledger.month_summary(now.year, now.month, today=now.date())
    if s["pending"]:
        msg += f"\nDays this month not yet checked: {L.fmt_days(s['pending'])}"
    ledger.set_meta("last_nudge_at", now.isoformat())
    print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
