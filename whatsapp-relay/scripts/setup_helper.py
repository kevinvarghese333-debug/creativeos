"""Reads and writes data/whatsapp-relay/config.json for setup.sh.

    setup_helper.py --data DIR answers      previous answers as shell assignments (for the prompts)
    setup_helper.py --data DIR save --owner N --source N [--firm N] [--keyword W]
    setup_helper.py --data DIR owner        the owner number, or nothing
    setup_helper.py --data DIR groups       one line per forwarding group

Standard library only: setup.sh runs it with the server's python3, or with the Python inside the
Hermes image when the server has none.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shlex
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugin"


def load_ledger():
    spec = importlib.util.spec_from_file_location("relay_ledger", PLUGIN_DIR / "ledger.py")
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


def read_user_config(data: Path) -> dict:
    """The user's own config.json (no defaults merged in); {} when it does not exist yet."""
    path = config_path(data)
    if not path.exists():
        return {}
    text = re.sub(r"^\s*//.*$", "", path.read_text(encoding="utf-8"), flags=re.MULTILINE)
    return json.loads(text) if text.strip() else {}


def first(values) -> str:
    for v in values or []:
        if str(v).strip():
            return str(v).strip()
    return ""


def cmd_answers(data: Path) -> None:
    cfg = L._deep_merge(L.DEFAULT_CONFIG, read_user_config(data))
    answers = {
        "OWNER": phone(cfg.get("owner_number")),
        "SOURCE": phone(first(cfg["forwarding"].get("source_numbers"))),
        "FIRM": phone(first(cfg["statement"].get("sender_numbers"))),
        "KEYWORD": first(cfg["statement"].get("match_keywords")),
    }
    for key, value in answers.items():
        print(f"{key}={shlex.quote(value)}")


def cmd_save(data: Path, args) -> None:
    user = read_user_config(data)
    cfg = L._deep_merge(L.DEFAULT_CONFIG, user)  # write every setting out, so the file shows what can be changed
    cfg["owner_number"] = phone(args.owner)
    cfg["forwarding"]["source_numbers"] = [phone(args.source)] if phone(args.source) else []
    cfg["statement"]["sender_numbers"] = [phone(args.firm)] if phone(args.firm) else []
    cfg["statement"]["match_keywords"] = [args.keyword.strip()] if args.keyword.strip() else []
    path = config_path(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def cmd_owner(data: Path) -> None:
    print(phone(read_user_config(data).get("owner_number")))


def cmd_groups(data: Path) -> None:
    cfg = L._deep_merge(L.DEFAULT_CONFIG, read_user_config(data))
    seen = []
    for gid in cfg["forwarding"].get("target_groups") or []:
        gid = str(gid).strip()
        if re.fullmatch(r"\d+(?:-\d+)?@g\.us", gid) and gid not in seen:
            seen.append(gid)
            print(f"{gid}  (in config.json)")
    path = data / L.DATA_DIR_NAME / "groups.json"
    if path.exists():
        try:
            groups = json.loads(path.read_text(encoding="utf-8")).get("groups") or []
        except (ValueError, AttributeError):
            groups = []
        for g in groups:
            gid = str(g.get("id", "")).strip() if isinstance(g, dict) else ""
            if gid and gid not in seen:
                seen.append(gid)
                print(f"{gid}  {g.get('name') or ''}".rstrip())


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True, type=Path)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("answers")
    save = sub.add_parser("save")
    save.add_argument("--owner", required=True)
    save.add_argument("--source", default="")
    save.add_argument("--firm", default="")
    save.add_argument("--keyword", default="")
    sub.add_parser("owner")
    sub.add_parser("groups")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "answers":
            cmd_answers(args.data)
        elif args.cmd == "save":
            cmd_save(args.data, args)
        elif args.cmd == "owner":
            cmd_owner(args.data)
        else:
            cmd_groups(args.data)
    except json.JSONDecodeError as exc:
        print(f"{config_path(args.data)} is not valid JSON ({exc}). Fix or delete it, then run setup again.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
