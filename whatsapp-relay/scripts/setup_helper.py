"""Reads and writes data/whatsapp-relay/config.json for setup.sh.

    setup_helper.py --data DIR apply-settings   merge ../settings.json into config.json when it is new or changed
    setup_helper.py --data DIR summary          the settings in use, one line each
    setup_helper.py --data DIR answers          previous answers as shell assignments (for the prompts)
    setup_helper.py --data DIR save --owner N --source N [--firm N] [--keyword W]
    setup_helper.py --data DIR owner            the owner number, or nothing
    setup_helper.py --data DIR groups           one line per forwarding group
    setup_helper.py --data DIR group-names      the group names to find (target_group_names)
    setup_helper.py --data DIR missing-groups   group names still to be found, one per line

Standard library only: setup.sh runs it with the server's python3, or with the Python inside the
Hermes image when the server has none.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import shlex
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent
SETTINGS = PACKAGE / "settings.json"


def load_ledger():
    spec = importlib.util.spec_from_file_location("relay_ledger", PACKAGE / "plugin" / "ledger.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


L = load_ledger()


def phone(value) -> str:
    """Same rule as the plugin: digits only, a bare 10-digit Indian mobile gets 91 in front."""
    d = L.digits(value)
    if len(d) == 10 and d[0] in "6789":
        d = "91" + d
    return d if len(d) >= 8 else ""


def config_path(data: Path) -> Path:
    return data / L.DATA_DIR_NAME / "config.json"


def merged(data: Path) -> dict:
    return L._deep_merge(L.DEFAULT_CONFIG, L.read_user_config(data))


def first(values) -> str:
    for v in values or []:
        if str(v).strip():
            return str(v).strip()
    return ""


def numbers(values) -> list[str]:
    return [n for n in (phone(v) for v in values or []) if n]


def cmd_apply_settings(data: Path) -> None:
    """settings.json ships inside a personal zip. Its values win over config.json once, when it is new
    or changed; after that, changes made from WhatsApp (!set, !add) are kept."""
    if not SETTINGS.exists():
        return
    raw = SETTINGS.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    marker = data / L.DATA_DIR_NAME / ".settings-applied"
    if marker.exists() and marker.read_text().strip() == digest:
        return
    text = re.sub(r"^\s*//.*$", "", raw.decode("utf-8"), flags=re.MULTILINE)
    settings = json.loads(text) if text.strip() else {}
    cfg = L._deep_merge(merged(data), settings)
    L.save_user_config(cfg, data)
    marker.write_text(digest + "\n")
    print(f"Applied settings.json to {config_path(data)}")


def cmd_summary(data: Path) -> None:
    cfg = merged(data)
    fwd, stm = cfg["forwarding"], cfg["statement"]
    accounts = L.tracked_accounts(cfg)
    lines = [
        f"Your number:      {phone(cfg.get('owner_number')) or '(not set)'}",
        f"Source numbers:   {', '.join(numbers(fwd.get('source_numbers'))) or 'none'}",
        f"Forwarded:        {', '.join(map(str, fwd.get('forward_kinds') or [])) or 'all files'}",
        f"Groups to find:   {', '.join(map(str, fwd.get('target_group_names') or [])) or 'none (use !addgroup)'}",
        f"Firm's number:    {', '.join(numbers(stm.get('sender_numbers'))) or 'none'}",
        f"Accounts:         {', '.join(a['name'] + ' (' + a['number'] + ')' for a in accounts) or 'one combined tracker'}",
        f"Daily amount:     ₹{stm.get('expected_amount')}"
        + (f", entry must mention {', '.join(map(str, stm['match_keywords']))}" if stm.get("match_keywords") else ""),
        f"Holidays:         {', '.join(map(str, cfg['calendar'].get('holidays') or [])) or 'none'} (plus every {', '.join(cfg['calendar'].get('weekly_off') or [])})",
    ]
    print("\n".join(lines))


def cmd_answers(data: Path) -> None:
    cfg = merged(data)
    answers = {
        "OWNER": phone(cfg.get("owner_number")),
        "SOURCE": phone(first(cfg["forwarding"].get("source_numbers"))),
        "FIRM": phone(first(cfg["statement"].get("sender_numbers"))),
        "KEYWORD": first(cfg["statement"].get("match_keywords")),
    }
    for key, value in answers.items():
        print(f"{key}={shlex.quote(value)}")


def keep_or_replace(current, answer: str) -> list[str]:
    """The questions show only the first number; keeping it keeps the whole list."""
    number = phone(answer)
    have = numbers(current)
    if not number:
        return []
    return have if number in have else [number]


def cmd_save(data: Path, args) -> None:
    cfg = merged(data)  # write every setting out, so the file shows what can be changed
    cfg["owner_number"] = phone(args.owner)
    cfg["forwarding"]["source_numbers"] = keep_or_replace(cfg["forwarding"].get("source_numbers"), args.source)
    cfg["statement"]["sender_numbers"] = keep_or_replace(cfg["statement"].get("sender_numbers"), args.firm)
    keywords = cfg["statement"].get("match_keywords") or []
    keyword = args.keyword.strip()
    cfg["statement"]["match_keywords"] = keywords if keyword and keyword == first(keywords) else ([keyword] if keyword else [])
    L.save_user_config(cfg, data)


def cmd_owner(data: Path) -> None:
    print(phone(L.read_user_config(data).get("owner_number")))


def group_list(data: Path) -> list[tuple[str, str]]:
    """(id, name) for every forwarding group: config.json target_groups, then groups.json."""
    cfg = merged(data)
    out: list[tuple[str, str]] = []
    for gid in cfg["forwarding"].get("target_groups") or []:
        gid = str(gid).strip()
        if re.fullmatch(r"\d+(?:-\d+)?@g\.us", gid) and gid not in [g for g, _ in out]:
            out.append((gid, "(in config.json)"))
    path = data / L.DATA_DIR_NAME / "groups.json"
    if path.exists():
        try:
            groups = json.loads(path.read_text(encoding="utf-8")).get("groups") or []
        except (ValueError, AttributeError):
            groups = []
        for g in groups:
            gid = str(g.get("id", "")).strip() if isinstance(g, dict) else ""
            if gid and gid not in [x for x, _ in out]:
                out.append((gid, str(g.get("name") or "")))
    return out


def cmd_groups(data: Path) -> None:
    for gid, name in group_list(data):
        print(f"{gid}  {name}".rstrip())


def cmd_group_names(data: Path) -> None:
    for name in merged(data)["forwarding"].get("target_group_names") or []:
        print(name)


def cmd_missing_groups(data: Path) -> None:
    have = {L.group_key(name) for _, name in group_list(data)}
    for name in merged(data)["forwarding"].get("target_group_names") or []:
        if L.group_key(name) not in have:
            print(name)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=Path)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("apply-settings", "summary", "answers", "owner", "groups", "group-names", "missing-groups"):
        sub.add_parser(name)
    save = sub.add_parser("save")
    save.add_argument("--owner", required=True)
    save.add_argument("--source", default="")
    save.add_argument("--firm", default="")
    save.add_argument("--keyword", default="")
    args = parser.parse_args(argv)
    commands = {"apply-settings": cmd_apply_settings, "summary": cmd_summary, "answers": cmd_answers,
                "owner": cmd_owner, "groups": cmd_groups, "group-names": cmd_group_names,
                "missing-groups": cmd_missing_groups}
    try:
        if args.cmd == "save":
            cmd_save(args.data, args)
        else:
            commands[args.cmd](args.data)
    except json.JSONDecodeError as exc:
        print(f"{config_path(args.data)} or settings.json is not valid JSON ({exc}). Fix it, then run setup again.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
