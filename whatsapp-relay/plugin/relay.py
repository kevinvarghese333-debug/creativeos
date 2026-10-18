"""WhatsApp relay: forwards files from one number to your groups, and tracks the ₹2000 credits
from statement screenshots. Runs inside the Hermes gateway via the pre_gateway_dispatch hook,
so it sees each WhatsApp message before the agent does and costs no AI tokens for forwarding.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

from . import extractor as X
from . import ledger as L

logger = logging.getLogger("whatsapp_relay")

SKIP = "skip"
PLACEHOLDER_RE = re.compile(r"^\[(?:sticker|[a-z_ ]+ received|[a-z_ ]+ could not be downloaded)\]$", re.IGNORECASE)
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
GROUP_ID_RE = re.compile(r"^\d+(?:-\d+)?@g\.us$")  # placeholders like 1203630XXXX@g.us don't match
GROUP_COMMANDS = ("!addgroup", "!removegroup", "!groupid")
FIRM = "the firm"

HELP_TEXT = """**Relay commands** (send to this number)
!status - what is set up and this month's total
!report - this month's deposit report (or !report 2026-09)
!holiday 2026-10-20 - mark a non-working day (several allowed)
!holiday remove 2026-10-20 - undo that
!holidays - list holidays
!undo - remove the entries from the last statement read
!retry - read the last failed statement again
!groups - list the forwarding groups
!removegroup 2 - stop forwarding to group 2 of that list
!testgroups - send a test line to the forwarding groups
!reload - reload config.json after editing it

Typed inside a group (only while `bash setup.sh groups on`):
!addgroup - forward files to this group
!removegroup - stop forwarding to this group
!groupid - show this group's ID

Statement screenshots or PDFs sent here with no caption are read as statements.
Add a caption to talk to Hermes about an image instead."""


def phone(value: Any) -> str:
    """Normalise to digits with country code. A bare 10-digit Indian mobile gets 91 prefixed."""
    d = L.digits(value)
    if len(d) == 10 and d[0] in "6789":
        d = "91" + d
    return d if len(d) >= 8 else ""  # placeholders like 91XXXXXXXXXX count as "not set"


def jid_for(number: str) -> str:
    n = str(number or "").strip()
    if "@" in n:
        return n
    return f"{phone(n)}@s.whatsapp.net"


def sender_aliases(user_id: str) -> set[str]:
    """All phone/LID forms Hermes knows for this sender (LID senders resolve via the bridge's mapping files)."""
    aliases: set[str] = set()
    try:
        from gateway.whatsapp_identity import expand_whatsapp_aliases, normalize_whatsapp_identifier  # type: ignore

        aliases |= set(expand_whatsapp_aliases(user_id))
        aliases.add(normalize_whatsapp_identifier(user_id))
    except Exception:
        pass
    aliases.add(L.digits(str(user_id or "").split("@", 1)[0].split(":", 1)[0]))
    return {a for a in aliases if a}


def platform_name(platform: Any) -> str:
    return str(getattr(platform, "value", platform) or "").lower()


def split_commands(text: str) -> list[str]:
    """WhatsApp batches quick messages into one, so '!status\\n!report' is two commands; a line not
    starting with '!' continues the command above it."""
    commands: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("!") or not commands:
            commands.append(line)
        else:
            commands[-1] += " " + line
    return commands


def parse_day(token: str) -> Optional[date]:
    token = token.strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", token)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", token)
        if not m:
            return None
        d, mo, y = map(int, m.groups())  # Indian day-first
    try:
        return date(y, mo, d)
    except ValueError:
        return None


@dataclass
class Batch:
    sender: str
    files: list[tuple[str, Optional[str]]] = field(default_factory=list)
    timer: Optional[asyncio.Task] = None


class Relay:
    def __init__(self, home: Optional[Path] = None):
        self.home = home or L.hermes_home()
        self.data = L.data_dir(self.home)
        self.config_path = self.data / "config.json"
        self.groups_path = self.data / "groups.json"
        self._config_mtime = None
        self.cfg: dict = {}
        self.ledger: Optional[L.Ledger] = None
        self.gateway = None
        self.batches: dict[str, Batch] = {}
        self.last_failed: Optional[Batch] = None
        self.tasks: set[asyncio.Task] = set()
        self.reload()

    # ------------------------------------------------------------------ config
    def reload(self) -> None:
        self.cfg = L.load_config(self.home)
        self.ledger = L.Ledger(self.data / "ledger.db", self.cfg)
        try:
            self._config_mtime = self.config_path.stat().st_mtime
        except OSError:
            self._config_mtime = None
        for g in self.cfg["forwarding"].get("target_groups", []):
            if not GROUP_ID_RE.match(str(g).strip()):
                logger.warning("[relay] target group %r in config.json is not a group ID (like 1203630...@g.us); ignoring it", g)

    def maybe_reload(self) -> None:
        try:
            mtime = self.config_path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime != self._config_mtime:
            logger.info("[relay] config.json changed, reloading")
            self.reload()

    @property
    def owner(self) -> str:
        return phone(self.cfg.get("owner_number"))

    def _matches(self, user_id: str, numbers: list) -> bool:
        wanted = {phone(n) for n in numbers if phone(n)}
        return bool(wanted and sender_aliases(user_id) & wanted)

    # ------------------------------------------------------------------ forwarding groups
    def load_groups(self) -> list[dict]:
        """Groups added with !addgroup (groups.json)."""
        try:
            data = json.loads(self.groups_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            logger.warning("[relay] could not read %s: %s", self.groups_path, exc)
            return []
        items = data.get("groups") if isinstance(data, dict) else data
        out = []
        for g in items or []:
            if isinstance(g, str):
                g = {"id": g}
            if isinstance(g, dict) and GROUP_ID_RE.match(str(g.get("id", "")).strip()):
                out.append({"id": str(g["id"]).strip(), "name": str(g.get("name") or ""),
                            "added_at": str(g.get("added_at") or "")})
        return out

    def save_groups(self, groups: list[dict]) -> None:
        tmp = self.groups_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"groups": groups}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.groups_path)

    def target_groups(self) -> list[dict]:
        """Where files are forwarded: target_groups from config.json, then the !addgroup groups; one entry per ID."""
        out, seen = [], set()
        for gid in self.cfg["forwarding"].get("target_groups", []) or []:
            gid = str(gid).strip()
            if GROUP_ID_RE.match(gid) and gid not in seen:
                seen.add(gid)
                out.append({"id": gid, "name": "", "source": "config"})
        for g in self.load_groups():
            if g["id"] not in seen:
                seen.add(g["id"])
                out.append({**g, "source": "added"})
        return out

    async def group_name(self, chat_id: str, source: Any = None) -> str:
        """The group's subject from the bridge; "" when it is not known."""
        adapter = self.adapter(source)
        if adapter is None:
            return ""
        try:
            info = await adapter.get_chat_info(chat_id)
        except Exception:
            return ""
        name = str((info or {}).get("name") or "").strip()
        return "" if name in ("", chat_id, chat_id.split("@", 1)[0], "Unknown") else name

    # ------------------------------------------------------------------ sending
    def adapter(self, source: Any = None):
        gw = self.gateway
        if gw is None:
            return None
        if source is not None:
            try:
                found = gw._intake_adapter_for(source)
                if found is not None:
                    return found
            except Exception:
                pass
        for key, adapter in (getattr(gw, "adapters", None) or {}).items():
            if platform_name(key) == "whatsapp":
                return adapter
        return None

    async def notify_owner(self, text: str, source: Any = None) -> bool:
        adapter = self.adapter(source)
        if adapter is None or not self.owner:
            logger.warning("[relay] cannot notify owner (no WhatsApp adapter or owner_number): %s", text[:120])
            return False
        try:
            result = await adapter.send(jid_for(self.owner), text)
            ok = bool(getattr(result, "success", True))
            if not ok:
                logger.warning("[relay] owner notify failed: %s", getattr(result, "error", ""))
            return ok
        except Exception as exc:
            logger.warning("[relay] owner notify raised: %s", exc)
            return False

    def spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self.tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("[relay] background task failed: %r", task.exception())

    # ------------------------------------------------------------------ entry point
    async def handle(self, event: Any, gateway: Any) -> Optional[dict]:
        source = getattr(event, "source", None)
        if source is None or platform_name(getattr(source, "platform", "")) != "whatsapp":
            return None
        self.gateway = gateway
        self.maybe_reload()
        if not self.owner:
            return None  # not configured yet

        raw = event.raw_message if isinstance(getattr(event, "raw_message", None), dict) else {}
        user_id = str(getattr(source, "user_id", None) or raw.get("senderId") or "")
        chat_id = str(getattr(source, "chat_id", "") or "")
        is_group = getattr(source, "chat_type", "") == "group" or chat_id.endswith("@g.us")
        text = str(getattr(event, "text", "") or "")
        is_owner = self._matches(user_id, [self.owner])

        if is_group:
            word = text.strip().split()[0].lower() if text.strip() else ""
            if is_owner and word in GROUP_COMMANDS:
                self.spawn(self.group_command(word, chat_id, source))
                return {"action": SKIP, "reason": f"relay: {word[1:]}"}
            # The chat= ID is what to paste into target_groups if !addgroup ever fails.
            logger.info("[relay] group message ignored: chat=%s owner=%s", chat_id, is_owner)
            if self.cfg.get("silence_groups", True):
                return {"action": SKIP, "reason": "relay: group message"}
            return None

        fwd = self.cfg["forwarding"]
        stm = self.cfg["statement"]

        if is_owner:
            if text.strip().startswith("!"):
                self.spawn(self.run_commands(split_commands(text), source))
                return {"action": SKIP, "reason": "relay: owner command"}
            if stm.get("enabled") and self._is_statement_media(event, raw):
                caption = self._caption(raw, event)
                if not caption or caption.lower().startswith(("statement", "stmt")):
                    self.queue_statement(event, raw, sender="you")
                    return {"action": SKIP, "reason": "relay: statement from owner"}
            return None  # normal Hermes conversation

        if fwd.get("enabled") and self._matches(user_id, fwd.get("source_numbers", [])):
            self.spawn(self.forward(event, raw, source))
            return {"action": SKIP, "reason": "relay: forwarded"}

        if stm.get("enabled") and self._matches(user_id, stm.get("sender_numbers", [])):
            if self._is_statement_media(event, raw):
                self.queue_statement(event, raw, sender=FIRM)
            return {"action": SKIP, "reason": "relay: statement sender"}

        if user_id.endswith("@s.whatsapp.net"):
            # Anyone else gets silence, and Hermes never sees it, so you aren't sent an "unrecognized
            # user" notice for every stranger. An unresolved @lid sender still goes to Hermes' own check.
            logger.info("[relay] message from an unknown number ignored: chat=%s", chat_id)
            return {"action": SKIP, "reason": "relay: unknown sender"}

        return None  # an unresolved @lid sender: Hermes' own allowlist decides (and stays silent)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _caption(raw: dict, event: Any) -> str:
        body = raw.get("body") if raw else None
        if body is None:
            body = "" if getattr(event, "media_urls", None) else getattr(event, "text", "")
        body = str(body or "").strip()
        return "" if PLACEHOLDER_RE.match(body) else body

    @staticmethod
    def _media(event: Any) -> list[tuple[str, Optional[str]]]:
        urls = list(getattr(event, "media_urls", None) or [])
        types = list(getattr(event, "media_types", None) or [])
        out = []
        for i, path in enumerate(urls):
            if isinstance(path, str) and Path(path).is_absolute() and Path(path).exists():
                out.append((path, types[i] if i < len(types) else None))
        return out

    def _is_statement_media(self, event: Any, raw: dict) -> bool:
        kind = str(raw.get("mediaType") or "").lower()
        for path, mime in self._media(event):
            ext = Path(path).suffix.lower()
            mime = (mime or "").lower()
            if kind == "image" or mime.startswith("image/") or ext in IMAGE_EXTS:
                return True
            if mime == "application/pdf" or ext == ".pdf":
                return True
        return False

    # ------------------------------------------------------------------ forwarding
    async def forward(self, event: Any, raw: dict, source: Any) -> None:
        fwd = self.cfg["forwarding"]
        groups = [g["id"] for g in self.target_groups()]
        sender = getattr(source, "user_name", None) or raw.get("senderName") or "source"
        media = self._media(event)
        caption = self._caption(raw, event)
        prefix = str(fwd.get("caption_prefix") or "")
        if prefix and caption:
            caption = f"{prefix}{caption}"
        elif prefix:
            caption = prefix.strip()
        kind = str(raw.get("mediaType") or "").lower()
        file_name = raw.get("fileName") or None
        adapter = self.adapter(source)

        if not groups:
            await self.notify_owner(f"A file from {sender} arrived, but no forwarding groups are set yet, so nothing was forwarded.\n"
                                    "Run `bash setup.sh groups on`, then type !addgroup inside each group.", source)
            return
        if adapter is None:
            logger.error("[relay] no WhatsApp adapter, cannot forward")
            return
        if not media:
            if raw.get("hasMedia"):
                await self.notify_owner(f"{sender} sent a {kind or 'file'} but it could not be downloaded, so it was not forwarded.", source)
                return
            if not fwd.get("forward_text_messages"):
                return
        results = []
        delay = float(fwd.get("delay_between_sends_seconds") or 0)
        for group in groups:
            if media:
                for path, mime in media:
                    ok, err = await self._send_one(adapter, group, path, kind, mime, caption, file_name)
                    results.append((group, Path(path).name, ok, err))
                    if delay:
                        await asyncio.sleep(delay)
            else:
                text = (prefix + str(getattr(event, "text", "") or "")).strip()
                try:
                    res = await adapter.send(group, text)
                    ok, err = bool(getattr(res, "success", True)), getattr(res, "error", None)
                except Exception as exc:
                    ok, err = False, str(exc)
                results.append((group, "text", ok, err))
                if delay:
                    await asyncio.sleep(delay)
        self._log_forward(sender, kind, results)
        failures = [r for r in results if not r[2]]
        notify = str(fwd.get("notify_owner") or "failures").lower()
        if failures and notify != "never":
            lines = [f"⚠️ Forwarding from {sender} failed for {len(failures)} of {len(results)} sends:"]
            lines += [f"- {g}: {e or 'unknown error'}" for g, _, _, e in failures[:6]]
            await self.notify_owner("\n".join(lines), source)
        elif notify == "always":
            what = file_name or (f"{len(media)} {kind or 'file'}" if media else "a message")
            await self.notify_owner(f"✅ Forwarded {what} from {sender} to {len(groups)} group{'s' if len(groups) != 1 else ''}.", source)

    async def _send_one(self, adapter, group: str, path: str, kind: str, mime: Optional[str],
                        caption: str, file_name: Optional[str]) -> tuple[bool, Optional[str]]:
        mime = (mime or "").lower()
        cap = caption or None
        try:
            if kind in ("image", "sticker") or (not kind and mime.startswith("image/")):
                res = await adapter.send_image_file(group, path, caption=cap)
            elif kind in ("video", "gif") or (not kind and mime.startswith("video/")):
                res = await adapter.send_video(group, path, caption=cap)
            elif kind in ("audio", "ptt") or (not kind and mime.startswith("audio/")):
                res = await adapter.send_voice(group, path)
            else:
                res = await adapter.send_document(group, path, caption=cap, file_name=file_name or _clean_name(path))
            return bool(getattr(res, "success", True)), getattr(res, "error", None)
        except Exception as exc:
            return False, str(exc)

    def _log_forward(self, sender: str, kind: str, results: list) -> None:
        try:
            entry = {"at": L.now_local(self.cfg).isoformat(), "from": sender, "kind": kind,
                     "sends": [{"group": g, "file": f, "ok": ok, "error": e} for g, f, ok, e in results]}
            with open(self.data / "forward-log.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning("[relay] could not write forward log: %s", exc)

    # ------------------------------------------------------------------ statements
    def queue_statement(self, event: Any, raw: dict, sender: str) -> None:
        files = self._archive(self._media(event))
        if not files:
            return
        key = sender
        batch = self.batches.get(key)
        if batch is None:
            batch = self.batches[key] = Batch(sender=sender)
        batch.files.extend(files)
        if batch.timer is not None and not batch.timer.done():
            batch.timer.cancel()
        wait = float(self.cfg["statement"].get("batch_seconds") or 0)
        batch.timer = asyncio.get_running_loop().create_task(self._fire(key, wait))
        self.tasks.add(batch.timer)
        batch.timer.add_done_callback(self._task_done)

    async def _fire(self, key: str, wait: float) -> None:
        await asyncio.sleep(wait)  # more screenshots arriving restart this timer
        batch = self.batches.pop(key, None)
        if batch and batch.files:
            await self.process_batch(batch)

    def _archive(self, files: list[tuple[str, Optional[str]]]) -> list[tuple[str, Optional[str]]]:
        day = L.now_local(self.cfg)
        folder = self.data / "statements" / day.strftime("%Y-%m")
        folder.mkdir(parents=True, exist_ok=True)
        out = []
        for i, (path, mime) in enumerate(files):
            dest = folder / f"{day.strftime('%Y%m%d-%H%M%S')}-{i}{Path(path).suffix.lower() or '.jpg'}"
            n = 1
            while dest.exists():
                dest = dest.with_name(f"{dest.stem}-{n}{dest.suffix}")
                n += 1
            try:
                shutil.copy2(path, dest)
                out.append((str(dest), mime))
            except OSError as exc:
                logger.warning("[relay] could not archive %s: %s", path, exc)
                out.append((path, mime))
        return out

    def hint(self) -> str:
        stm = self.cfg["statement"]
        parts = []
        if stm.get("expected_amount"):
            parts.append(f"The owner mainly tracks regular credits of about ₹{stm['expected_amount']}.")
        if stm.get("match_keywords"):
            parts.append("Those entries usually mention: " + ", ".join(map(str, stm["match_keywords"])) + ".")
        parts.append("Still list every row, not only those.")
        return "\n" + " ".join(parts)

    async def process_batch(self, batch: Batch) -> None:
        today = L.now_local(self.cfg).date()
        gem = self.cfg.get("gemini", {})
        try:
            result = await X.extract(batch.files, model=gem.get("model") or "gemini-flash-latest", today=today,
                                     hint=self.hint(), timeout=float(gem.get("timeout_seconds") or 120),
                                     base_url=str(gem.get("base_url") or ""))
        except X.ExtractionError as exc:
            self.last_failed = batch
            await self.notify_owner(f"⚠️ Couldn't read the statement from {batch.sender} ({len(batch.files)} file{'s' if len(batch.files) != 1 else ''}).\n{exc}\n"
                                    "Usually the Gemini key or the free quota. Send *!retry* to try again.")
            return
        except Exception as exc:  # unexpected
            self.last_failed = batch
            logger.exception("[relay] statement processing crashed")
            await self.notify_owner(f"⚠️ Statement processing hit an error: {exc}\nSend *!retry* to try again.")
            return
        outcome = self.apply_result(result, batch.sender, [p for p, _ in batch.files], today=today)
        await self.notify_owner(self.format_outcome(outcome, len(batch.files), batch.sender))
        for report in self.final_reports(outcome.get("months", []), today=today):
            await self.notify_owner(report)

    def matches(self, txn: dict) -> bool:
        stm = self.cfg["statement"]
        direction = str(txn.get("direction") or "").lower()
        keywords = [L.norm_text(k) for k in stm.get("match_keywords", []) if L.norm_text(k)]
        haystack = L.norm_text(f"{txn.get('description', '')} {txn.get('reference', '')}")
        keyword_ok = (not keywords) or any(k in haystack for k in keywords)
        if direction == "debit":
            return False
        if direction != "credit" and not (keywords and keyword_ok):
            return False  # unclear direction only counts when the account keywords confirm it
        expected = stm.get("expected_amount")
        if expected not in (None, "", 0):
            tol = float(stm.get("amount_tolerance") or 0)
            if abs(float(txn["amount"]) - float(expected)) > tol + 0.001:
                return False
        return keyword_ok

    def apply_result(self, result: dict, sender: str, files: list[str], *, today: date) -> dict:
        valid, rejected = [], []
        for t in result.get("transactions") or []:
            d = L.parse_iso(t.get("date"))
            try:
                amount = round(abs(float(t.get("amount"))), 2)
            except (TypeError, ValueError):
                amount = 0.0
            if not d or amount <= 0:
                rejected.append(t)
                continue
            if d > today + timedelta(days=1) or d < today - timedelta(days=400):
                rejected.append(t)  # date misread (wrong year etc.)
                continue
            valid.append({**t, "date": d, "amount": amount,
                          "description": str(t.get("description") or "").strip(),
                          "reference": str(t.get("reference") or "").strip()})
        if valid:
            # One misread year/month must not stretch the statement over months: drop rows far from the rest.
            ordered = sorted(t["date"] for t in valid)
            median = ordered[len(ordered) // 2]
            outliers = [t for t in valid if abs((t["date"] - median).days) > 45]
            rejected += outliers
            valid = [t for t in valid if t not in outliers]
        if not valid:
            return {"not_statement": not result.get("is_statement", False), "empty": True,
                    "notes": result.get("notes") or "", "rejected": len(rejected)}

        # What the statement shows = the date span visible on each screenshot/page (rows carry their
        # image number), so a page that was not sent never counts as "checked, no credit".
        by_image: dict[int, list[date]] = {}
        for t in valid:
            try:
                img = int(t.get("image") or 1)
            except (TypeError, ValueError):
                img = 1
            by_image.setdefault(img, []).append(t["date"])
        ranges = sorted((min(ds), min(max(ds), today)) for ds in by_image.values())
        merged: list[tuple[date, date]] = []
        for a, b in ranges:
            if merged and a <= merged[-1][1] + timedelta(days=1):
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        dates = [t["date"] for t in valid]
        covered_from, covered_to = merged[0][0], merged[-1][1]

        ledger = self.ledger
        sid = ledger.add_statement(sender=sender, files=files, covered_from=covered_from, covered_to=covered_to,
                                   transactions=len(valid), raw=result, ranges=merged)
        matched, seen = [], set()
        for t in valid:
            if not self.matches(t):
                continue
            k = ledger.dedupe_key(t["date"], t["amount"], t["reference"], t["description"])
            if k in seen:
                continue
            seen.add(k)
            matched.append(t)
        added = [t for t in matched if ledger.add_credit(d=t["date"], amount=t["amount"], description=t["description"],
                                                        reference=t["reference"], statement_id=sid)]
        ledger.finish_statement(sid, len(matched), len(added))

        credited = {L.parse_iso(r["entry_date"]) for r in ledger.credits_between(covered_from, covered_to)}
        shown = {d for a, b in merged for d in (a + timedelta(days=i) for i in range((b - a).days + 1))}
        workdays = ledger.working_days(covered_from, covered_to)
        missing = [d for d in workdays if d in shown and d not in credited]
        not_shown = [d for d in workdays if d not in shown and d not in credited]
        others = [t for t in valid if t not in matched and str(t.get("direction")).lower() != "debit"]
        months = sorted({(d.year, d.month) for d in dates} | {(covered_to.year, covered_to.month)})
        return {"statement_id": sid, "covered_from": covered_from, "covered_to": covered_to,
                "transactions": len(valid), "matched": matched, "added": added, "missing": missing,
                "not_shown": not_shown,
                "others": others, "rejected": len(rejected), "months": months,
                "notes": result.get("notes") or "", "account": result.get("account_label") or ""}

    def format_outcome(self, o: dict, file_count: int, sender: str) -> str:
        files = f"{file_count} file{'s' if file_count != 1 else ''}"
        if o.get("empty"):
            if o.get("not_statement"):
                return (f"That ({files} from {sender}) doesn't look like a bank statement, so nothing was recorded.\n"
                        "To ask Hermes about an image, send it with a caption.")
            note = f"\nNote: {o['notes']}" if o.get("notes") else ""
            return f"I read the statement ({files} from {sender}) but found no readable transaction rows.{note}"
        matched, added = o["matched"], o["added"]
        lines = [f"**Statement read** ({files} from {sender})"]
        if o.get("account"):
            lines.append(f"Account: {o['account']}")
        lines.append(f"Covers: {L.fmt_day(o['covered_from'])} – {L.fmt_day(o['covered_to'])} ({o['transactions']} rows)")
        if matched:
            total = sum(t["amount"] for t in matched)
            lines.append(f"Matching credits: {len(matched)} ({L.inr(total)}) · new {len(added)}, already had {len(matched) - len(added)}")
            lines.append(f"Credited on: {L.fmt_days(t['date'] for t in matched)}")
        else:
            lines.append("No matching credits found in this statement.")
        if o["missing"]:
            lines.append(f"⚠️ No credit on: {', '.join(L.fmt_day(d) for d in o['missing'][:10])}")
        if o.get("not_shown"):
            lines.append(f"⏳ Not in these screenshots: {L.fmt_days(o['not_shown'])} (send that page if you have it)")
        if matched and not o["missing"] and not o.get("not_shown"):
            lines.append("✅ Every working day in this range has its credit.")
        if o["others"]:
            shown = "; ".join(f"{t['date'].day} {t['date'].strftime('%b')} {L.inr(t['amount'])} {t['description'][:30]}"
                              for t in o["others"][:4])
            lines.append(f"Other credits seen (not counted): {shown}")
        if o.get("rejected"):
            lines.append(f"Skipped {o['rejected']} row(s) with unreadable dates or amounts.")
        for y, m in o["months"][-2:]:
            s = self.ledger.month_summary(y, m)
            lines.append(f"{s['label']} so far: **{L.inr(s['total'])}** from {s['count']} credits")
        if o.get("notes"):
            lines.append(f"Note: {o['notes'][:200]}")
        return "\n".join(lines)

    def final_reports(self, months: list, *, today: date) -> list[str]:
        out = []
        candidates = set(months) | {L.previous_month(today)}
        for y, m in sorted(candidates):
            if (y, m) > (today.year, today.month):
                continue
            key = f"final_sent:{y:04d}-{m:02d}"
            if self.ledger.get_meta(key):
                continue
            s = self.ledger.month_summary(y, m, today=today)
            if s["last_day_in"] and s["last_expected"] <= today:
                out.append(self.ledger.month_report(y, m, final=True, today=today))
                self.ledger.set_meta(key, L.now_local(self.cfg).isoformat())
        return out

    # ------------------------------------------------------------------ owner commands
    async def run_commands(self, commands: list[str], source: Any) -> None:
        for text in commands:
            reply = await self.command_reply(text)
            if reply:
                await self.notify_owner(reply, source)

    async def group_command(self, word: str, chat_id: str, source: Any) -> None:
        """!addgroup / !removegroup / !groupid typed by the owner inside a group. The answer goes to the
        owner privately; the bot never writes in the group itself."""
        name = await self.group_name(chat_id, source)
        label = name or chat_id
        if word == "!groupid":
            reply = f"Group ID for {label}:\n{chat_id}"
        elif word == "!addgroup":
            groups = self.load_groups()
            if any(g["id"] == chat_id for g in self.target_groups()):
                for g in groups:
                    if g["id"] == chat_id and name and g["name"] != name:
                        g["name"] = name
                        self.save_groups(groups)
                reply = f"✅ {label} is already a forwarding group."
            else:
                groups.append({"id": chat_id, "name": name, "added_at": L.now_local(self.cfg).isoformat(timespec="seconds")})
                self.save_groups(groups)
                count = len(self.target_groups())
                reply = f"✅ Added {label}\nFiles from the source number now go to {count} group{'s' if count != 1 else ''}."
        else:  # !removegroup
            reply = self._remove_group(chat_id, label)
        logger.info("[relay] %s in chat=%s (%s)", word, chat_id, label)
        await self.notify_owner(reply, source)

    def _remove_group(self, chat_id: str, label: str) -> str:
        groups = self.load_groups()
        kept = [g for g in groups if g["id"] != chat_id]
        if len(kept) != len(groups):
            self.save_groups(kept)
        if chat_id in [str(g).strip() for g in self.cfg["forwarding"].get("target_groups", [])]:
            return f"{label} is listed under target_groups in config.json; remove it there to stop forwarding to it."
        if len(kept) != len(groups):
            count = len(self.target_groups())
            return f"Removed {label}. Files now go to {count} group{'s' if count != 1 else ''}."
        return f"{label} was not a forwarding group."

    async def groups_text(self) -> str:
        groups = self.target_groups()
        if not groups:
            return ("No forwarding groups yet.\nRun `bash setup.sh groups on` on the server, "
                    "then type !addgroup inside each group.")
        lines = [f"**Forwarding groups** ({len(groups)})"]
        for i, g in enumerate(groups, 1):
            name = await self.group_name(g["id"]) or g.get("name") or "(name unknown)"
            where = " · in config.json" if g["source"] == "config" else ""
            lines.append(f"{i}. {name}{where}\n   {g['id']}")
        lines.append("Stop forwarding to one: !removegroup <number>")
        return "\n".join(lines)

    async def command_reply(self, text: str) -> str:
        parts = text[1:].strip().split()
        cmd = parts[0].lower() if parts else "help"
        args = parts[1:]
        today = L.now_local(self.cfg).date()
        if cmd in ("help", "commands"):
            return HELP_TEXT
        if cmd == "status":
            return self.status_text(today)
        if cmd == "report":
            y, m = today.year, today.month
            if args:
                mt = re.fullmatch(r"(\d{4})-(\d{1,2})", args[0])
                if not mt:
                    return "Use !report or !report 2026-09"
                y, m = int(mt.group(1)), int(mt.group(2))
            return self.ledger.month_report(y, m, today=today)
        if cmd == "holidays":
            hols = sorted(d for d in self.ledger.holidays() if d >= today - timedelta(days=31))
            return "Holidays: " + (", ".join(L.fmt_day(d) for d in hols) if hols else "none set")
        if cmd == "holiday":
            remove = bool(args) and args[0].lower() in ("remove", "rm", "delete", "del")
            tokens = args[1:] if remove else args
            days = [parse_day(t) for t in tokens]
            if not tokens or any(d is None for d in days):
                return "Use !holiday 2026-10-20 (or 20/10/2026). Several dates allowed."
            if remove:
                done = self.ledger.remove_holidays(days)
                return f"Removed: {', '.join(L.fmt_day(d) for d in done) or 'nothing (not set)'}"
            done = self.ledger.add_holidays(days)
            return f"Marked as holiday: {', '.join(L.fmt_day(d) for d in done) or 'already set'}"
        if cmd == "undo":
            info = self.ledger.undo_last_statement()
            if not info:
                return "No statement to undo."
            return f"Removed the last statement (received {info['received_at'][:16].replace('T', ' ')}) and its {info['removed_credits']} credit(s)."
        if cmd == "retry":
            if not self.last_failed:
                return "Nothing to retry."
            batch, self.last_failed = self.last_failed, None
            self.spawn(self.process_batch(batch))
            return f"Reading the {len(batch.files)} file(s) again…"
        if cmd == "reload":
            self.reload()
            return "Config reloaded.\n\n" + self.status_text(today)
        if cmd == "groups":
            return await self.groups_text()
        if cmd == "removegroup":
            groups = self.target_groups()
            if not args or not args[0].isdigit() or not 1 <= int(args[0]) <= len(groups):
                return "Use !removegroup <number> from the !groups list, or type !removegroup inside the group."
            g = groups[int(args[0]) - 1]
            return self._remove_group(g["id"], g.get("name") or await self.group_name(g["id"]) or g["id"])
        if cmd == "testgroups":
            groups = self.target_groups()
            adapter = self.adapter()
            if not groups:
                return "No forwarding groups yet. Type !addgroup inside each group first."
            if adapter is None:
                return "WhatsApp is not connected, so nothing was sent."
            lines = []
            for g in groups:
                label = g.get("name") or g["id"]
                try:
                    res = await adapter.send(g["id"], "Test message from the file relay. Please ignore.")
                    ok = bool(getattr(res, "success", True))
                    lines.append(f"{'✅' if ok else '❌'} {label}{'' if ok else ' - ' + str(getattr(res, 'error', ''))}")
                except Exception as exc:
                    lines.append(f"❌ {label} - {exc}")
            return "Test send:\n" + "\n".join(lines)
        if cmd in ("addgroup", "groupid"):
            return f"Type !{cmd} inside the group itself (after `bash setup.sh groups on`)."
        return f"Unknown command !{cmd}. Send !help for the list."

    def status_text(self, today: date) -> str:
        fwd, stm = self.cfg["forwarding"], self.cfg["statement"]
        last = self.ledger.last_statement_at()
        s = self.ledger.month_summary(today.year, today.month, today=today)
        groups = self.target_groups()
        if groups:
            names = ", ".join(g.get("name") or f"group {i}" for i, g in enumerate(groups, 1))
            to = f"{len(groups)} group{'s' if len(groups) != 1 else ''} ({names})"
        else:
            to = "no groups yet (type !addgroup in each group)"
        sources = [n for n in (phone(x) for x in fwd.get("source_numbers") or []) if n]
        firms = [n for n in (phone(x) for x in stm.get("sender_numbers") or []) if n]
        lines = ["**Relay status**",
                 f"Forwarding: {'on' if fwd.get('enabled') else 'off'} · from {', '.join(sources) or '(no source number set)'} → {to}",
                 f"Statements: {'on' if stm.get('enabled') else 'off'} · read from you" + (f" and {FIRM} ({', '.join(firms)})" if firms else "")
                 + f" · tracking {L.inr(float(stm['expected_amount'])) if stm.get('expected_amount') else 'any amount'} credits"
                 + (f" mentioning {', '.join(map(str, stm['match_keywords']))}" if stm.get("match_keywords") else ""),
                 f"Last statement: {last.strftime('%d %b %Y, %I:%M %p') if last else 'none yet'}",
                 f"{s['label']}: **{L.inr(s['total'])}** from {s['count']} credit{'s' if s['count'] != 1 else ''}"]
        return "\n".join(lines)

    # ------------------------------------------------------------------ agent tool
    def tool_summary(self, month: Optional[str] = None) -> dict:
        today = L.now_local(self.cfg).date()
        y, m = today.year, today.month
        if month:
            mt = re.fullmatch(r"(\d{4})-(\d{1,2})", str(month).strip())
            if mt:
                y, m = int(mt.group(1)), int(mt.group(2))
        s = self.ledger.month_summary(y, m, today=today)
        last = self.ledger.last_statement_at()
        return {
            "month": s["label"], "total_rupees": s["total"], "credits": s["count"],
            "credited_days": [d.isoformat() for d in s["credited_days"]],
            "working_days_in_month": len(s["expected_days"]),
            "working_days_so_far": len(s["expected_so_far"]),
            "missing_days": [d.isoformat() for d in s["missing"]],
            "not_yet_in_any_statement": [d.isoformat() for d in s["pending"]],
            "month_complete": s["complete"],
            "last_statement_received": last.isoformat() if last else None,
            "report_text": self.ledger.month_report(y, m, today=today),
        }


def _clean_name(path: str) -> str:
    """Bridge cache names look like doc_<hex>_<original name>; recover the original."""
    name = Path(path).name
    m = re.match(r"^(?:doc|vid|img|aud)_[0-9a-f]{6,}_(.+)$", name)
    return m.group(1) if m else name
