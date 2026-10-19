"""The two no-agent schedule scripts and setup.sh's config helper, run the way Hermes and setup.sh run them."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import date

import pytest
from conftest import FIRM, OWNER, PKG, SOURCE, write_config


@pytest.fixture
def hermes_home(tmp_path):
    """<home>/plugins/whatsapp-relay + <home>/scripts, as setup.sh lays them out in ./data."""
    (tmp_path / "plugins").mkdir()
    shutil.copytree(PKG / "plugin", tmp_path / "plugins" / "whatsapp-relay")
    shutil.copytree(PKG / "scripts", tmp_path / "scripts")
    write_config(tmp_path)
    return tmp_path


def run_script(home, name, now):
    env = {**os.environ, "RELAY_NOW": now}
    env.pop("HERMES_HOME", None)  # the scripts find the home from their own location, like under cron
    r = subprocess.run([sys.executable, str(home / "scripts" / name)], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def ledger(home, now):
    os.environ["RELAY_NOW"] = now
    sys.path.insert(0, str(home / "plugins"))
    import importlib.util
    spec = importlib.util.spec_from_file_location("L_test", home / "plugins" / "whatsapp-relay" / "ledger.py")
    L = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(L)
    return L, L.Ledger(home / "whatsapp-relay" / "ledger.db", L.load_config(home))


def test_nudge_timing(hermes_home):
    L, led = ledger(hermes_home, "2026-10-01T09:00:00")  # tracker starts 1 Oct
    assert run_script(hermes_home, "relay_nudge.py", "2026-10-07T10:15:00") == ""
    first = run_script(hermes_home, "relay_nudge.py", "2026-10-08T10:15:00")
    assert first.startswith("⏰ No statement has arrived yet (7 days")
    assert run_script(hermes_home, "relay_nudge.py", "2026-10-09T10:15:00") == "", "repeats every 2 days, not daily"
    assert "No statement" in run_script(hermes_home, "relay_nudge.py", "2026-10-10T10:15:00")
    os.environ["RELAY_NOW"] = "2026-10-11T18:00:00"
    led.add_statement(sender="the firm", files=[], covered_from=date(2026, 10, 5), covered_to=date(2026, 10, 10),
                      transactions=6, raw={})
    assert run_script(hermes_home, "relay_nudge.py", "2026-10-17T10:15:00") == ""
    later = run_script(hermes_home, "relay_nudge.py", "2026-10-19T10:15:00")
    assert "No statement for 7 days (last one came on Sun 11 Oct)" in later


def test_month_end_timing(hermes_home):
    ledger(hermes_home, "2026-10-01T09:00:00")
    assert run_script(hermes_home, "relay_month_end.py", "2026-10-30T20:45:00") == ""
    assert run_script(hermes_home, "relay_month_end.py", "2026-10-31T12:00:00") == "", "only in the evening"
    report = run_script(hermes_home, "relay_month_end.py", "2026-10-31T20:45:00")
    assert report.startswith("**Deposit report: October 2026**") and "final total follows" in report
    assert run_script(hermes_home, "relay_month_end.py", "2026-10-31T21:00:00") == "", "sent once"
    assert run_script(hermes_home, "relay_month_end.py", "2026-11-01T20:45:00") == ""


def test_month_end_catches_up_a_missed_evening(hermes_home):
    ledger(hermes_home, "2026-10-01T09:00:00")
    assert "October 2026" in run_script(hermes_home, "relay_month_end.py", "2026-11-01T20:45:00")


def test_scripts_work_without_tz_data(hermes_home):
    """Slim images may lack /usr/share/zoneinfo; the scripts must still run on India time."""
    env = {**os.environ, "RELAY_NOW": "2026-10-31T20:45:00", "PYTHONTZPATH": "/nonexistent"}
    code = ("import zoneinfo, runpy, sys; zoneinfo.reset_tzpath(['/nonexistent']);"
            "sys.modules.pop('tzdata', None); sys.modules['tzdata'] = None;"
            f"runpy.run_path({str(hermes_home / 'scripts' / 'relay_month_end.py')!r}, run_name='__main__')")
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert r.returncode in (0, None) and "October 2026" in r.stdout, r.stderr


# ---------------------------------------------------------------- setup.sh's helper
def helper(data, *args):
    r = subprocess.run([sys.executable, str(PKG / "scripts" / "setup_helper.py"), "--data", str(data), *args],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_helper_round_trip_keeps_other_settings(tmp_path):
    assert helper(tmp_path, "answers") == "OWNER=''\nSOURCE=''\nFIRM=''\nKEYWORD=''\n"
    helper(tmp_path, "save", "--owner", "9847012345", "--source", SOURCE, "--firm", "", "--keyword", " 4417 ")
    cfg_path = tmp_path / "whatsapp-relay" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    assert cfg["owner_number"] == OWNER and cfg["statement"]["sender_numbers"] == [] and cfg["statement"]["match_keywords"] == ["4417"]
    assert cfg["forwarding"]["notify_owner"] == "failures", "every default is written out"
    cfg["forwarding"]["caption_prefix"] = "From office: "
    cfg["calendar"]["holidays"] = ["2026-10-20"]
    cfg_path.write_text(json.dumps(cfg))
    helper(tmp_path, "save", "--owner", OWNER, "--source", SOURCE, "--firm", FIRM, "--keyword", "")
    cfg = json.loads(cfg_path.read_text())
    assert cfg["forwarding"]["caption_prefix"] == "From office: " and cfg["calendar"]["holidays"] == ["2026-10-20"]
    assert cfg["statement"]["sender_numbers"] == [FIRM] and cfg["statement"]["match_keywords"] == []
    assert helper(tmp_path, "answers") == f"OWNER={OWNER}\nSOURCE={SOURCE}\nFIRM={FIRM}\nKEYWORD=''\n"
    assert helper(tmp_path, "owner").strip() == OWNER


def test_helper_reads_commented_config_and_lists_groups(tmp_path):
    d = tmp_path / "whatsapp-relay"
    d.mkdir()
    (d / "config.json").write_text('{\n  // mine\n  "owner_number": "919847012345",\n'
                                   '  "forwarding": {"target_groups": ["120363000000000009@g.us", "1203630XXXX@g.us"]}\n}\n')
    (d / "groups.json").write_text(json.dumps({"groups": [{"id": "120363000000000001@g.us", "name": "Office A"}]}))
    out = helper(tmp_path, "groups").splitlines()
    assert out == ["120363000000000009@g.us  (in config.json)", "120363000000000001@g.us  Office A"]
    assert "KEYWORD=''" in helper(tmp_path, "answers")


def test_helper_reports_broken_json(tmp_path):
    d = tmp_path / "whatsapp-relay"
    d.mkdir()
    (d / "config.json").write_text("{ broken")
    r = subprocess.run([sys.executable, str(PKG / "scripts" / "setup_helper.py"), "--data", str(tmp_path), "answers"],
                       capture_output=True, text=True)
    assert r.returncode == 1 and "not valid JSON" in r.stderr


# ---------------------------------------------------------------- settings.json in a personal zip
@pytest.fixture
def personal_pkg(tmp_path):
    pkg = tmp_path / "whatsapp-relay"
    shutil.copytree(PKG / "plugin", pkg / "plugin")
    shutil.copytree(PKG / "scripts", pkg / "scripts")
    (pkg / "settings.json").write_text("""{
      // Kev's settings
      "owner_number": "9800000001",
      "forwarding": {"source_numbers": ["919800000002", "919800000003"], "forward_kinds": ["image"],
                     "target_group_names": ["AREA MANAGERS", "Asst Area Managers", "MANAGERS"]},
      "statement": {"sender_numbers": ["919800000004"], "accounts": [{"number": "1001", "name": "Asha"}],
                    "match_keywords": []},
      "calendar": {"weekly_off": ["sunday"], "holidays": ["2026-10-02"]}
    }""")
    return pkg


def phelper(pkg, data, *args):
    r = subprocess.run([sys.executable, str(pkg / "scripts" / "setup_helper.py"), "--data", str(data), *args],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_settings_json_applied_once_then_whatsapp_changes_kept(personal_pkg, tmp_path):
    data = tmp_path / "data"
    assert "Applied settings.json" in phelper(personal_pkg, data, "apply-settings")
    cfg_path = data / "whatsapp-relay" / "config.json"
    cfg = json.loads(cfg_path.read_text())
    assert cfg["owner_number"] == "9800000001" and cfg["forwarding"]["forward_kinds"] == ["image"]
    assert cfg["forwarding"]["notify_owner"] == "failures", "defaults are filled in too"
    summary = phelper(personal_pkg, data, "summary")
    assert "919800000001" in summary and "919800000002, 919800000003" in summary and "Asha (1001)" in summary
    assert "AREA MANAGERS, Asst Area Managers, MANAGERS" in summary and "2026-10-02" in summary
    assert phelper(personal_pkg, data, "answers").startswith("OWNER=919800000001\n")

    cfg["forwarding"]["source_numbers"].append("919800000005")  # as !add source would
    cfg_path.write_text(json.dumps(cfg))
    assert phelper(personal_pkg, data, "apply-settings") == "", "an unchanged settings.json is not re-applied"
    assert "919800000005" in json.loads(cfg_path.read_text())["forwarding"]["source_numbers"]

    (personal_pkg / "settings.json").write_text('{"statement": {"expected_amount": 2500}}')
    assert "Applied" in phelper(personal_pkg, data, "apply-settings"), "a new settings.json (newer zip) is applied"
    cfg = json.loads(cfg_path.read_text())
    assert cfg["statement"]["expected_amount"] == 2500 and "919800000005" in cfg["forwarding"]["source_numbers"]


def test_missing_groups_by_name(personal_pkg, tmp_path):
    data = tmp_path / "data"
    phelper(personal_pkg, data, "apply-settings")
    assert phelper(personal_pkg, data, "missing-groups").splitlines() == ["AREA MANAGERS", "Asst Area Managers", "MANAGERS"]
    (data / "whatsapp-relay" / "groups.json").write_text(json.dumps({"groups": [
        {"id": "120363000000000001@g.us", "name": "AREA MANAGERS ♻️⚧️"},
        {"id": "120363000000000003@g.us", "name": "🎯MANAGERS🎯"}]}))
    assert phelper(personal_pkg, data, "missing-groups").splitlines() == ["Asst Area Managers"]
    assert phelper(personal_pkg, data, "group-names").count("\n") == 3


def test_questions_keep_the_whole_source_list(personal_pkg, tmp_path):
    data = tmp_path / "data"
    phelper(personal_pkg, data, "apply-settings")
    phelper(personal_pkg, data, "save", "--owner", "919800000001", "--source", "919800000002", "--firm", "919800000004")
    cfg = json.loads((data / "whatsapp-relay" / "config.json").read_text())
    assert cfg["forwarding"]["source_numbers"] == ["919800000002", "919800000003"]
    phelper(personal_pkg, data, "save", "--owner", "919800000001", "--source", "919999988888")
    cfg = json.loads((data / "whatsapp-relay" / "config.json").read_text())
    assert cfg["forwarding"]["source_numbers"] == ["919999988888"] and cfg["statement"]["sender_numbers"] == []
