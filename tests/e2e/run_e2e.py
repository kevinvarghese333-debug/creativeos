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
sys.path.insert(0, str(REPO / "tests"))
from statement_rows import ACCOUNT_A, ACCOUNT_B, ACCOUNTS, rows  # noqa: E402

C = "hermes-whatsapp"
OWNER, SOURCE, SOURCE2, FIRM, STRANGER = "919847012345", "919847099999", "919072900002", "919400011111", "919811122233"
G1, G2, G3, G4 = ("120363000000000001@g.us", "120363000000000002@g.us",
                  "120363000000000003@g.us", "120363000000000004@g.us")
NAMES = {G1: "AREA MANAGERS ♻️⚧️", G2: "Asst Area Managers", G3: "🎯MANAGERS🎯", G4: "Family"}
SETTINGS = {  # like a personal zip's settings.json, with test numbers
    "owner_number": OWNER,
    "forwarding": {"source_numbers": [SOURCE, SOURCE2], "forward_kinds": ["image"],
                   "target_group_names": ["AREA MANAGERS", "Asst Area Managers", "MANAGERS"]},
    "statement": {"sender_numbers": [FIRM], "match_keywords": [],
                  "accounts": ACCOUNTS},
    "calendar": {"weekly_off": ["sunday"], "holidays": ["2026-10-02"]},
}
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

    # ---------------------------------------------------------------- setup.sh with a personal settings.json
    (pkg / "settings.json").write_text("// test numbers\n" + json.dumps(SETTINGS, indent=2))
    out = setup(inp="AIzaFAKEKEY00000000000000000000000000\n")
    check("setup: uses settings.json and only asks for the key",
          "Applied settings.json" in out and "Asha (1001), Ravi (1002)" in out and "1. Your own" not in out
          and "Done." in out and "Next: bash setup.sh pair" in out, out)
    cfg = json.loads((data / "whatsapp-relay/config.json").read_text())
    check("setup: settings.json copied into config.json",
          cfg["owner_number"] == OWNER and cfg["forwarding"]["source_numbers"] == [SOURCE, SOURCE2]
          and cfg["forwarding"]["forward_kinds"] == ["image"] and cfg["statement"]["sender_numbers"] == [FIRM]
          and len(cfg["statement"]["accounts"]) == 2, json.dumps(cfg)[:400])
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

    out = setup(inp="\n")
    cfg2 = json.loads((data / "whatsapp-relay/config.json").read_text())
    check("setup: re-run keeps everything and doesn't re-apply settings.json",
          cfg2 == cfg and "Applied" not in out and "Done." in out, out)
    out = setup("questions", inp="\n\n\n\n\n")
    cfg3 = json.loads((data / "whatsapp-relay/config.json").read_text())
    changed = {k: (cfg.get(k), cfg3.get(k)) for k in set(cfg) | set(cfg3) if cfg.get(k) != cfg3.get(k)}
    check("setup questions: Enter keeps all answers, including both source numbers",
          not changed and "Saved" in out, f"{changed}\n{out}")

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
    check("!status answers the owner", len(t) == 1 and "Relay status" in t[0] and "no groups yet" in t[0]
          and "Groups not found yet" in t[0], str(new))
    new = say(OWNER, "!help")
    check("!help lists group and settings commands",
          any("!addgroup" in x and "!settings" in x for x in texts_to(new, jid(OWNER))), str(new))
    new = say(OWNER, "!settings")
    check("!settings shows the numbers and accounts", any(SOURCE2 in x and "Asha (1001)" in x and "Forwarded: image" in x
                                                         for x in texts_to(new, jid(OWNER))), str(new))

    # ---------------------------------------------------------------- groups found by name
    new = say(OWNER, "!addgroup", chat=G1, expect=0)
    check("groups closed: group messages are not seen", new == [], str(new))
    proc = subprocess.Popen(["bash", "setup.sh", "groups"], cwd=pkg, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines = []
    for line in proc.stdout:
        lines.append(line)
        if "Looking for these groups" in line:
            break
    wait_bridge(); set_chats(); time.sleep(3)
    n = len(sent())
    inject(STRANGER, "Thiruvankulam", chat=G1)
    inject(STRANGER, "Ashtamichira", chat=G2)
    inject(STRANGER, "hello", chat=G4)
    inject(OWNER, "good morning", chat=G3)
    try:
        rest, _ = proc.communicate(timeout=240)
    except subprocess.TimeoutExpired:
        proc.kill()
        rest = proc.communicate()[0]
    out = "".join(lines) + rest
    wait_bridge(); set_chats(); time.sleep(3)  # setup restarted the container when it stopped looking
    check("setup groups: finds all three by name, then closes group messages",
          out.count("✅ Found:") == 3 and "Stopped looking" in out and G3 in out and "Not found yet" not in out
          and proc.returncode == 0, out)
    found = texts_to(sent()[n:], jid(OWNER))
    check("the owner is told as each group is found", len(found) == 3 and found[-1].endswith("All groups found."), str(found))
    check("the bot never writes in a group", not any(texts_to(sent()[n:], g) for g in (G1, G2, G3, G4)))
    groups = json.loads((data / "whatsapp-relay/groups.json").read_text())["groups"]
    check("groups.json has the three groups (not Family)", sorted(g["id"] for g in groups) == [G1, G2, G3], str(groups))
    log = dx("tail", "-n", "400", "/opt/data/logs/agent.log")
    check("group chat IDs are logged for troubleshooting", f"chat={G4}" in log)
    new = say(OWNER, "!addgroup", chat=G4, expect=0)
    check("groups closed again afterwards", new == [], str(new))
    new = say(OWNER, "!testgroups", expect=4)
    check("!testgroups reaches all three groups", all(texts_to(new, g) for g in (G1, G2, G3))
          and "✅ AREA MANAGERS" in "".join(texts_to(new, jid(OWNER))), str(new))

    # ---------------------------------------------------------------- forwarding: pictures only
    new = say(SOURCE, "Gold Outstanding as on 17-10-2026",
              media={"kind": "image", "src": "/opt/data/fakebridge/assets/photo.jpg", "mime": "image/jpeg"}, expect=3)
    media = [s for s in new if s["kind"] == "media"]
    check("a report picture goes to all three groups with its caption",
          sorted(m["chatId"] for m in media) == sorted([G1, G2, G3])
          and all(m["caption"] == "Gold Outstanding as on 17-10-2026" and m["mediaType"] == "image" for m in media)
          and not [s for s in new if s["chatId"] == jid(SOURCE)], str(new))
    new = say(SOURCE2, "", media={"kind": "document", "src": "/opt/data/fakebridge/assets/statement.png", "mime": "image/png",
                                  "fileName": "MIS.png"}, expect=3)
    check("a picture sent as a document from another source number is forwarded too",
          len([s for s in new if s["kind"] == "media"]) == 3, str(new))
    n = len(sent())
    for i in range(5):
        inject(SOURCE, f"Zone MIS {i}", media={"kind": "image", "src": "/opt/data/fakebridge/assets/photo.jpg", "mime": "image/jpeg"})
    new = wait_new(n, 15, 90)
    check("five screenshots sent at once all reach all three groups",
          sorted(m["caption"] for m in new if m["kind"] == "media") == sorted(f"Zone MIS {i}" for i in range(5) for _ in range(3)), str(new))
    new = say(SOURCE, "", media={"kind": "document", "src": "/opt/data/fakebridge/assets/letter.pdf", "mime": "application/pdf",
                                 "fileName": "Oct Letter.pdf"}, expect=0)
    check("a PDF is not forwarded (pictures only)", new == [], str(new))
    new = say(SOURCE, "Ok Sir", expect=0)
    check("text from a source number is not forwarded", new == [], str(new))
    new = say(STRANGER, "hi", expect=0)
    check("a stranger gets no reply at all", new == [], str(new))
    fwd_log = (data / "whatsapp-relay/forward-log.jsonl").read_text().strip().splitlines()
    check("forward-log.jsonl records each forward", len(fwd_log) == 7 and all(json.loads(x)["sends"] for x in fwd_log), str(fwd_log))

    # ---------------------------------------------------------------- statements: two accounts through fake Gemini
    response = {"is_statement": True, "account_label": "", "notes": "",
                "transactions": rows(ACCOUNT_A, "1001", 1) + rows(ACCOUNT_B, "1002", 2)}
    (fb / "gemini-response.json").write_text(json.dumps(response))
    n = len(sent())
    inject(FIRM, "", media={"kind": "image", "src": "/opt/data/fakebridge/assets/statement.png", "mime": "image/png"})
    inject(FIRM, "", media={"kind": "image", "src": "/opt/data/fakebridge/assets/photo.jpg", "mime": "image/jpeg"})
    new = wait_new(n, 1, 60)
    t = texts_to(new, jid(OWNER))
    check("the firm's two ledger photos are read together, each account on its own",
          len(t) == 1 and "(2 files from the firm)" in t[0] and "*Asha* (A/C 1001)" in t[0] and "*Ravi* (A/C 1002)" in t[0]
          and "Paid late: 8 Oct (on 9 Oct)" in t[0] and "No credit on: Thu 1 Oct" in t[0]
          and "together *₹52,000*" in t[0] and not texts_to(new, jid(FIRM)), str(new))
    archived = list((data / "whatsapp-relay/statements").rglob("*.*"))
    check("both statement photos are archived", len(archived) == 2, str(archived))
    calls = (fb / "gemini-calls.log").read_text().strip().splitlines()
    check("one Gemini call for the two photos", len(calls) == 1, str(calls))

    new = say(OWNER, "!report")
    check("!report shows each account and the total", any("*Asha* (A/C 1001)" in x and "Together: *₹52,000*" in x
                                                          for x in texts_to(new, jid(OWNER))), str(new))
    new = say(OWNER, "!holiday 01/10/2026")
    check("!holiday accepts day-first dates", any("Marked as holiday: Thu 1 Oct" in x for x in texts_to(new, jid(OWNER))), str(new))
    new = say(OWNER, "!report")
    check("a holiday drops off the report", any("No credit on" not in x for x in texts_to(new, jid(OWNER))), str(new))

    (fb / "gemini-response.json").unlink()
    new = say(OWNER, "", media={"kind": "image", "src": "/opt/data/fakebridge/assets/statement.png", "mime": "image/png"}, timeout=90)
    check("a Gemini failure asks for !retry", any("Couldn't read the statement from you" in x and "!retry" in x for x in texts_to(new, jid(OWNER))), str(new))
    (fb / "gemini-response.json").write_text(json.dumps(response))
    new = say(OWNER, "!retry", expect=2, timeout=60)
    t = texts_to(new, jid(OWNER))
    check("!retry reads it again; duplicates are not counted twice",
          any("Reading the 1 file(s) again" in x for x in t) and any(x.count("new 0, already had 13") == 2 for x in t), str(new))
    new = say(OWNER, "!undo")
    check("!undo removes the last statement", any("Removed the last statement" in x for x in texts_to(new, jid(OWNER))), str(new))

    new = say(OWNER, "!holidays\n!groups", expect=2)
    t = texts_to(new, jid(OWNER))
    check("two commands in one message get two answers", len(t) == 2 and "Holidays:" in t[0] and "Forwarding groups" in t[1], str(new))
    new = say(OWNER, "!removegroup 3")
    check("!removegroup <n> removes that group", any(x.startswith("Removed ") and "2 groups" in x for x in texts_to(new, jid(OWNER))), str(new))
    new = say(OWNER, "!add source 9811122233")
    check("!add source saves a new number", any(x.startswith("✅ Saved.") and STRANGER in x for x in texts_to(new, jid(OWNER)))
          and STRANGER in json.loads((data / "whatsapp-relay/config.json").read_text())["forwarding"]["source_numbers"], str(new))
    new = say(STRANGER, "MIS", media={"kind": "image", "src": "/opt/data/fakebridge/assets/photo.jpg", "mime": "image/jpeg"}, expect=2)
    check("pictures from the newly added number are forwarded", len([s for s in new if s["kind"] == "media"]) == 2, str(new))
    say(OWNER, f"!remove source {STRANGER}")

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
