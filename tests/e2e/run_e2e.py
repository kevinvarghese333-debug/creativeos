"""End-to-end test in the real Hermes container, with a fake WhatsApp bridge.

    python3 tests/e2e/run_e2e.py [WORKDIR]

Copies whatsapp-relay/ into WORKDIR (default: a temp folder) as if unzipped on the VPS, runs the real
`bash setup.sh` against the official nousresearch/hermes-agent image, swaps the Baileys bridge for
tests/e2e/fake_bridge.js, then drives WhatsApp-shaped messages through the real gateway, adapter and
pre_gateway_dispatch hook and checks what Hermes sent back. Needs Docker; leaves the container
running at the end so it can be inspected (`docker rm -f hermes-whatsapp` to clean up).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
C = "hermes-whatsapp"
OWNER, SOURCE, FIRM, STRANGER = "919847012345", "919847099999", "919400011111", "919811122233"
G1, G2, G3, G4 = ("120363000000000001@g.us", "120363000000000002@g.us",
                  "120363000000000003@g.us", "120363000000000004@g.us")
NAMES = {G1: "Office A", G2: "Office B", G3: "Office C", G4: "Family"}
jid = lambda n: f"{n}@s.whatsapp.net"  # noqa: E731

failures: list[str] = []


def sh(cmd, *, inp: str | None = None, check=True, cwd=None, timeout=900) -> str:
    r = subprocess.run(cmd, input=inp, text=True, capture_output=True, cwd=cwd, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{cmd} failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
    return r.stdout + r.stderr


def dx(*cmd, user=None, env=None) -> str:
    base = ["docker", "exec"] + (["-u", user] if user else [])
    for k, v in (env or {}).items():
        base += ["-e", f"{k}={v}"]
    return sh(base + [C, *cmd], check=False)


def bridge(path: str, data: dict | None = None):
    cmd = ["curl", "-sS", "--max-time", "10"]
    if data is not None:
        cmd += ["-X", "POST", "-H", "content-type: application/json", "-d", json.dumps(data)]
    out = dx(*cmd, f"http://127.0.0.1:3000{path}")
    return json.loads(out) if out.strip().startswith(("{", "[")) else out


def sent() -> list[dict]:
    out = bridge("/sent")
    return out if isinstance(out, list) else []


def inject(sender: str, body: str = "", chat: str | None = None, media: dict | None = None):
    msg = {"chatId": chat or jid(sender), "senderId": jid(sender), "senderName": f"user{sender[-4:]}", "body": body}
    if media:
        msg["mediaFile"] = media
    bridge("/inject", msg)


def wait_new(start: int, count: int, timeout: float = 30) -> list[dict]:
    """The sends after index ``start`` once at least ``count`` arrived (or whatever came by ``timeout``)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        new = sent()[start:]
        if len(new) >= count:
            time.sleep(1.5)  # let stragglers land, so "exactly N" checks are honest
            return sent()[start:]
        time.sleep(0.5)
    return sent()[start:]


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"\n      {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def texts_to(items, chat) -> list[str]:
    return [s.get("message", "") for s in items if s["kind"] == "text" and s["chatId"] == chat]


def say(sender, body, expect=1, chat=None, media=None, timeout=30):
    n = len(sent())
    inject(sender, body, chat, media)
    return wait_new(n, expect, timeout) if expect else (time.sleep(6) or sent()[n:])


def wait_bridge():
    for _ in range(90):
        out = dx("curl", "-fsS", "--max-time", "2", "http://127.0.0.1:3000/health")
        if '"connected"' in out:
            return True
        time.sleep(2)
    raise RuntimeError("fake bridge never came up:\n" + dx("sh", "-c", "tail -n 40 /opt/data/platforms/whatsapp/bridge.log /opt/data/whatsapp/bridge.log 2>/dev/null; tail -n 30 /opt/data/logs/agent.log"))


def set_chats():
    for gid, name in NAMES.items():
        bridge("/set-chat", {"id": gid, "name": name})


def main() -> int:
    work = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="relay-e2e-"))
    sh(["docker", "rm", "-f", C], check=False)
    if (work / "whatsapp-relay").exists():
        sh(["rm", "-rf", str(work / "whatsapp-relay")])
    work.mkdir(parents=True, exist_ok=True)
    pkg = work / "whatsapp-relay"
    shutil.copytree(REPO / "whatsapp-relay", pkg, ignore=shutil.ignore_patterns("__pycache__", "data"))
    data = pkg / "data"
    setup = lambda *a, inp=None: sh(["bash", "setup.sh", *a], inp=inp, cwd=pkg, check=False)  # noqa: E731

    # ---------------------------------------------------------------- setup.sh, first run
    out = setup(inp=f"{OWNER[2:]}\n+91 {SOURCE[2:7]} {SOURCE[7:]}\n{FIRM}\n4417\nAIzaFAKEKEY00000000000000000000000000\n")
    check("setup: saves config and finishes", "Saved " in out and "Done." in out and "Next: bash setup.sh pair" in out, out)
    cfg = json.loads((data / "whatsapp-relay/config.json").read_text())
    check("setup: answers normalised into config.json",
          cfg["owner_number"] == OWNER and cfg["forwarding"]["source_numbers"] == [SOURCE]
          and cfg["statement"]["sender_numbers"] == [FIRM] and cfg["statement"]["match_keywords"] == ["4417"], json.dumps(cfg)[:400])
    env = (data / ".env").read_text()
    check("setup: Hermes .env has bot mode, owner, home chat, Gemini key",
          "WHATSAPP_MODE=bot" in env and f"WHATSAPP_ALLOWED_USERS={OWNER}" in env and "GOOGLE_API_KEY=AIzaFAKE" in env
          and f"WHATSAPP_HOME_CHANNEL={OWNER}@s.whatsapp.net" in env)
    yaml = (data / "config.yaml").read_text()
    check("setup: config.yaml has Gemini, India time, silent strangers, plugin enabled",
          'provider: "gemini"' in yaml and "timezone: Asia/Kolkata" in yaml and "unauthorized_dm_behavior: ignore" in yaml
          and "- whatsapp-relay" in yaml and "wrap_response: false" in yaml)
    owners = sh(["stat", "-c", "%u", str(data / "plugins/whatsapp-relay/relay.py"), str(data / "whatsapp-relay/config.json"),
                 str(data / "scripts/relay_nudge.py")]).split()
    check("setup: files owned by the container user", set(owners) == {"10000"}, str(owners))

    out = setup(inp="\n\n\n\n\n")
    cfg2 = json.loads((data / "whatsapp-relay/config.json").read_text())
    check("setup: re-run with Enter keeps every answer", cfg2 == cfg and "Done." in out, out)

    # ---------------------------------------------------------------- fake bridge in place of Baileys
    fb = data / "fakebridge"
    fb.mkdir()
    shutil.copy(REPO / "tests/e2e/fake_bridge.js", fb / "bridge.js")
    (fb / "package.json").write_text('{"name": "fake-bridge", "version": "1.0.0"}\n')
    (fb / "node_modules").mkdir()
    import hashlib
    (fb / "node_modules/.hermes-pkg-hash").write_text(hashlib.sha256((fb / "package.json").read_bytes()).hexdigest()[:16])
    (fb / "assets").mkdir()
    shutil.copy(REPO / "tests/e2e/assets/statement.png", fb / "assets/statement.png")
    shutil.copy(REPO / "tests/e2e/assets/photo.jpg", fb / "assets/photo.jpg")
    shutil.copy(REPO / "tests/e2e/assets/letter.pdf", fb / "assets/letter.pdf")
    (data / "whatsapp/session").mkdir(parents=True)
    (data / "whatsapp/session/creds.json").write_text('{"fake": true}\n')
    cfg["statement"]["batch_seconds"] = 3
    cfg["gemini"]["base_url"] = "http://127.0.0.1:3999/v1beta"
    (data / "whatsapp-relay/config.json").write_text(json.dumps(cfg, indent=2))
    sh(["chown", "-R", "10000:10000", str(fb), str(data / "whatsapp"), str(data / "whatsapp-relay")])
    dx("hermes", "config", "set", "WHATSAPP_ENABLED", "true")
    print(dx("hermes", "config", "set", "platforms.whatsapp.extra.bridge_script", "/opt/data/fakebridge/bridge.js").strip())
    sh(["docker", "compose", "restart"], cwd=pkg)
    wait_bridge()
    set_chats()
    time.sleep(3)

    out = setup("status")
    check("status: WhatsApp connected, plugin enabled", "WhatsApp: connected" in out and "Plugin: whatsapp-relay enabled" in out, out)

    # ---------------------------------------------------------------- owner commands
    new = say(OWNER, "!status")
    t = texts_to(new, jid(OWNER))
    check("!status answers the owner", len(t) == 1 and "Relay status" in t[0] and "no groups yet" in t[0], str(new))
    new = say(OWNER, "!help")
    check("!help lists group commands", any("!addgroup" in x for x in texts_to(new, jid(OWNER))), str(new))

    # ---------------------------------------------------------------- groups
    new = say(OWNER, "!addgroup", chat=G1, expect=0)
    check("groups off: !addgroup in a group is not seen", new == [], str(new))

    out = setup("groups", "on")
    check("groups on: restarts and explains", "Groups on." in out, out)
    wait_bridge(); set_chats(); time.sleep(3)
    for gid in (G1, G2, G3):
        new = say(OWNER, "!addgroup", chat=gid)
        t = texts_to(new, jid(OWNER))
        check(f"!addgroup in {NAMES[gid]} confirms privately", len(t) == 1 and t[0].startswith(f"✅ Added {NAMES[gid]}")
              and not texts_to(new, gid), str(new))
    new = say(OWNER, "!addgroup", chat=G1)
    check("!addgroup twice says already added", any("already a forwarding group" in x for x in texts_to(new, jid(OWNER))), str(new))
    new = say(STRANGER, "!addgroup", chat=G4, expect=0)
    check("a stranger's !addgroup is ignored", new == [], str(new))
    new = say(OWNER, "hello everyone", chat=G4, expect=0)
    check("ordinary group chat gets no reply", new == [], str(new))
    log = dx("tail", "-n", "300", "/opt/data/logs/agent.log")
    check("group chat IDs are logged for troubleshooting", f"chat={G4}" in log)
    groups = json.loads((data / "whatsapp-relay/groups.json").read_text())["groups"]
    check("groups.json has the three groups with names", [(g["id"], g["name"]) for g in groups] == [(g, NAMES[g]) for g in (G1, G2, G3)], str(groups))

    out = setup("groups", "off")
    check("groups off: lists the groups", "Groups off." in out and G1 in out and "Office C" in out, out)
    wait_bridge(); set_chats(); time.sleep(3)
    new = say(OWNER, "!testgroups", expect=4)
    check("!testgroups reaches all three groups", all(texts_to(new, g) for g in (G1, G2, G3))
          and "✅ Office A" in "".join(texts_to(new, jid(OWNER))), str(new))

    # ---------------------------------------------------------------- forwarding
    new = say(SOURCE, "Invoice 12", media={"kind": "image", "src": "/opt/data/fakebridge/assets/photo.jpg", "mime": "image/jpeg"}, expect=3)
    media = [s for s in new if s["kind"] == "media"]
    check("photo from the source goes to all three groups with its caption",
          sorted(m["chatId"] for m in media) == sorted([G1, G2, G3]) and all(m["caption"] == "Invoice 12" and m["mediaType"] == "image" for m in media)
          and not [s for s in new if s["chatId"] == jid(SOURCE)], str(new))
    new = say(SOURCE, "", media={"kind": "document", "src": "/opt/data/fakebridge/assets/letter.pdf", "mime": "application/pdf",
                                 "fileName": "Oct Letter.pdf"}, expect=3)
    media = [s for s in new if s["kind"] == "media"]
    check("PDF from the source keeps its file name, no caption added",
          len(media) == 3 and all(m["fileName"] == "Oct Letter.pdf" and m["mediaType"] == "document" and m["caption"] == "" for m in media), str(new))
    new = say(SOURCE, "are you there?", expect=0)
    check("text from the source is ignored", new == [], str(new))
    new = say(STRANGER, "hi", expect=0)
    check("a stranger gets no reply at all", new == [], str(new))
    fwd_log = (data / "whatsapp-relay/forward-log.jsonl").read_text().strip().splitlines()
    check("forward-log.jsonl records each forward", len(fwd_log) == 2 and all(json.loads(x)["sends"] for x in fwd_log), str(fwd_log))

    # ---------------------------------------------------------------- statements through fake Gemini
    rows = [{"date": f"2026-10-{d:02d}", "description": "NEFT CR SB XXXX4417 DAILY", "amount": 2000, "direction": "credit",
             "reference": f"N2860{d:02d}", "image": 1} for d in (12, 13, 15, 16, 17)]
    rows += [{"date": "2026-10-14", "description": "ATM WDL", "amount": 500, "direction": "debit", "reference": "", "image": 1},
             {"date": "2026-10-16", "description": "SALARY OCT", "amount": 15000, "direction": "credit", "reference": "S1", "image": 1}]
    response = {"is_statement": True, "account_label": "SB XXXX4417", "transactions": rows, "notes": ""}
    (fb / "gemini-response.json").write_text(json.dumps(response))
    new = say(FIRM, "", media={"kind": "image", "src": "/opt/data/fakebridge/assets/statement.png", "mime": "image/png"}, timeout=60)
    t = texts_to(new, jid(OWNER))
    check("firm's statement is read and confirmed to the owner",
          len(t) == 1 and "Statement read" in t[0] and "from the firm" in t[0] and "Matching credits: 5 (₹10,000)" in t[0]
          and "No credit on: Wed 14 Oct" in t[0] and "SALARY" in t[0] and not texts_to(new, jid(FIRM)), str(new))
    archived = list((data / "whatsapp-relay/statements").rglob("*.png"))
    check("statement image is archived", len(archived) == 1, str(archived))

    new = say(OWNER, "!report")
    check("!report shows the missing day", any("Deposit report: October 2026" in x and "14 Oct" in x for x in texts_to(new, jid(OWNER))), str(new))
    new = say(OWNER, "!holiday 14/10/2026")
    check("!holiday accepts day-first dates", any("Marked as holiday: Wed 14 Oct" in x for x in texts_to(new, jid(OWNER))), str(new))
    new = say(OWNER, "!report")
    check("a holiday drops off the report", any("No credit on" not in x for x in texts_to(new, jid(OWNER))), str(new))

    (fb / "gemini-response.json").unlink()
    new = say(OWNER, "", media={"kind": "image", "src": "/opt/data/fakebridge/assets/statement.png", "mime": "image/png"}, timeout=90)
    check("a Gemini failure asks for !retry", any("Couldn't read the statement from you" in x and "!retry" in x for x in texts_to(new, jid(OWNER))), str(new))
    (fb / "gemini-response.json").write_text(json.dumps(response))
    new = say(OWNER, "!retry", expect=2, timeout=60)
    t = texts_to(new, jid(OWNER))
    check("!retry reads it again; duplicates are not counted twice",
          any("Reading the 1 file(s) again" in x for x in t) and any("new 0, already had 5" in x for x in t), str(new))
    new = say(OWNER, "!undo")
    check("!undo removes the last statement", any("Removed the last statement" in x for x in texts_to(new, jid(OWNER))), str(new))

    new = say(OWNER, "!holidays\n!groups", expect=2)
    t = texts_to(new, jid(OWNER))
    check("two commands in one message get two answers", len(t) == 2 and "Holidays:" in t[0] and "Forwarding groups" in t[1], str(new))
    new = say(OWNER, "!removegroup 3")
    check("!removegroup <n> removes that group", any("Removed Office C" in x for x in texts_to(new, jid(OWNER))), str(new))

    skips = lambda: dx("grep", "-c", "pre_gateway_dispatch skip", "/opt/data/logs/agent.log").strip()  # noqa: E731
    before = skips()
    inject(OWNER, "how much came in this month?")
    time.sleep(10)
    log = dx("tail", "-n", "200", "/opt/data/logs/agent.log")
    check("ordinary chat from the owner is passed to Hermes (not swallowed by the relay)",
          skips() == before and "how much came in" in log, log[-1500:])
    check("Hermes doesn't nag the owner to /sethome", not any("No home channel" in s.get("message", "") for s in sent()))

    # ---------------------------------------------------------------- schedules
    out = setup("schedules")
    check("schedules: both jobs created", "relay-nudge created" in out and "relay-month-end created" in out, out)
    out = setup("schedules")
    check("schedules: re-run updates instead of duplicating", "relay-nudge already existed" in out and out.count("Name:") in (0, 2), out)
    listing = dx("hermes", "cron", "list")
    check("schedules: next runs in India time", "10:15" in listing and "20:45" in listing, listing)
    nudge = dx("python", "/opt/data/scripts/relay_nudge.py", user="hermes", env={"RELAY_NOW": "2026-11-05T10:15:00"})
    check("nudge after 7+ days without a statement", "No statement" in nudge, nudge)
    quiet = dx("python", "/opt/data/scripts/relay_nudge.py", user="hermes")
    check("nudge stays silent while statements are recent", quiet.strip() == "", quiet)
    me = dx("python", "/opt/data/scripts/relay_month_end.py", user="hermes", env={"RELAY_NOW": "2026-10-31T20:45:00"})
    check("month-end total on the last evening", "Deposit report: October 2026" in me, me)
    me2 = dx("python", "/opt/data/scripts/relay_month_end.py", user="hermes", env={"RELAY_NOW": "2026-10-20T20:45:00"})
    check("month-end silent on other days", me2.strip() == "", me2)
    # A real scheduled delivery: make the ledger look 10 days stale, then fire the nudge job now.
    dx("python", "-c", "import sqlite3;c=sqlite3.connect('/opt/data/whatsapp-relay/ledger.db');"
       "c.execute(\"UPDATE statements SET received_at='2026-10-01T10:00:00+05:30'\");"
       "c.execute(\"UPDATE meta SET value='2026-10-01T10:00:00+05:30' WHERE key='installed_at'\");"
       "c.execute(\"DELETE FROM meta WHERE key='last_nudge_at'\");c.commit()", user="hermes")
    jobs = json.loads(dx("python", "-c", "import json;from cron.jobs import load_jobs;"
                         "print(json.dumps([{'id':j['id'],'name':j.get('name')} for j in load_jobs()]))", user="hermes"))
    nid = next(j["id"] for j in jobs if j["name"] == "relay-nudge")
    n = len(sent())
    print(dx("hermes", "cron", "run", nid).strip()[-300:])
    new = wait_new(n, 1, 120)
    check("the nudge job delivers to the owner on WhatsApp", any("No statement" in x for x in texts_to(new, jid(OWNER))), str(new) + listing[-800:])

    # ---------------------------------------------------------------- update + final state
    out = setup("update")
    check("update: copies code, restarts, reports", "Updated." in out and "Container hermes-whatsapp: running" in out, out)
    wait_bridge(); set_chats(); time.sleep(3)
    new = say(OWNER, "!status")
    check("still answering after update", any("Relay status" in x and "2 groups" in x for x in texts_to(new, jid(OWNER))), str(new))
    errors = dx("sh", "-c", "grep -iE 'relay.*(Traceback|ERROR)|whatsapp_relay.*Error' /opt/data/logs/agent.log /opt/data/logs/errors.log || true")
    check("no relay errors in the logs", errors.strip() == "", errors[-2000:])

    print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED: ' + '; '.join(failures)}")
    print(f"Workdir: {pkg}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
