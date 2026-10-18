"""Hermes no-agent cron script: month-end deposit total.

Runs every evening. On the last day of the month (after 6 pm) it prints that month's report;
if that evening was missed (gateway down, timezone set differently) it sends it the next day.
The final total, once the last week's statement arrives, is sent by the plugin itself.
Prints nothing on other days, so Hermes stays silent. Uses no AI tokens.
"""

import importlib.util
import sys
from datetime import date, datetime
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
    if not cfg.get("statement", {}).get("enabled", True):
        return 0
    ledger = L.Ledger(L.data_dir(HOME) / "ledger.db", cfg)
    now = L.now_local(cfg)
    today = now.date()

    targets = []
    if today == L.last_day_of_month(today) and now.hour >= 18:
        targets.append((today.year, today.month))
    py, pm = L.previous_month(today)
    installed = ledger.get_meta("installed_at")
    installed_day = datetime.fromisoformat(installed).date() if installed else today
    if installed_day <= L.last_day_of_month(date(py, pm, 1)):
        targets.append((py, pm))

    out = []
    for y, m in targets:
        tag = f"{y:04d}-{m:02d}"
        if ledger.get_meta(f"month_end_sent:{tag}") or ledger.get_meta(f"final_sent:{tag}"):
            continue
        report = ledger.month_report(y, m, today=today)
        s = ledger.month_summary(y, m, today=today)
        if not s["complete"]:
            report += "\nThe final total follows automatically once the last week's statement is in."
        ledger.set_meta(f"month_end_sent:{tag}", now.isoformat())
        out.append(report)
    if out:
        print("\n\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
