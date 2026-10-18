"""Shared fakes for the relay unit tests: a WhatsApp adapter that records sends, a gateway that holds
it, and helpers to build WhatsApp-shaped MessageEvents like Hermes' adapter does."""

from __future__ import annotations

import asyncio
import enum
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PKG = Path(__file__).resolve().parents[1] / "whatsapp-relay"
sys.path.insert(0, str(PKG))

OWNER, SOURCE, FIRM, STRANGER = "919847012345", "919847099999", "919400011111", "919811122233"
G1, G2, G3 = "120363000000000001@g.us", "120363000000000002@g.us", "120363000000000003@g.us"


class FakeAdapter:
    def __init__(self, names=None):
        self.sent: list[dict] = []
        self.names = names or {}
        self.fail_for: set[str] = set()

    def _result(self, chat_id):
        ok = chat_id not in self.fail_for
        return SimpleNamespace(success=ok, error=None if ok else "bridge said no")

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append({"kind": "text", "chat": chat_id, "text": content})
        return self._result(chat_id)

    async def _media(self, kind, chat_id, path, caption=None, file_name=None):
        self.sent.append({"kind": kind, "chat": chat_id, "path": path, "caption": caption, "file_name": file_name})
        return self._result(chat_id)

    async def send_image_file(self, chat_id, image_path, caption=None, **kw):
        return await self._media("image", chat_id, image_path, caption)

    async def send_video(self, chat_id, video_path, caption=None, **kw):
        return await self._media("video", chat_id, video_path, caption)

    async def send_voice(self, chat_id, audio_path, caption=None, **kw):
        return await self._media("audio", chat_id, audio_path, caption)

    async def send_document(self, chat_id, file_path, caption=None, file_name=None, **kw):
        return await self._media("document", chat_id, file_path, caption, file_name)

    async def get_chat_info(self, chat_id):
        return {"name": self.names.get(chat_id, chat_id.split("@")[0]), "type": "group"}

    def texts_to(self, chat):
        return [s["text"] for s in self.sent if s["kind"] == "text" and s["chat"] == chat]


class Platform(enum.Enum):  # like gateway.config.Platform
    WHATSAPP = "whatsapp"


class FakeGateway:
    def __init__(self, adapter):
        self.adapters = {Platform.WHATSAPP: adapter}


def jid(number):
    return f"{number}@s.whatsapp.net"


def event(sender, text="", *, group=None, media=None, media_type="", file_name="", has_media=None):
    """A MessageEvent as Hermes' WhatsApp adapter builds it. ``media`` = [(path, mime)]."""
    media = media or []
    raw = {"senderId": jid(sender), "chatId": group or jid(sender), "isGroup": bool(group), "body": text,
           "hasMedia": bool(media) if has_media is None else has_media, "mediaType": media_type, "fileName": file_name}
    if raw["hasMedia"] and not text:
        raw["body"] = f"[{media_type or 'document'} received]"
    source = SimpleNamespace(platform=Platform.WHATSAPP, user_id=jid(sender), chat_id=group or jid(sender),
                             chat_type="group" if group else "dm", user_name=f"user{sender[-4:]}", chat_name=None)
    return SimpleNamespace(source=source, text=raw["body"], raw_message=raw,
                           media_urls=[p for p, _ in media], media_types=[m for _, m in media])


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("RELAY_NOW", "2026-10-18T12:00:00")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    write_config(tmp_path)
    return tmp_path


def write_config(home: Path, **overrides):
    cfg = {"owner_number": OWNER,
           "forwarding": {"source_numbers": [SOURCE], "target_groups": [], "delay_between_sends_seconds": 0},
           "statement": {"sender_numbers": [FIRM], "match_keywords": ["4417"], "batch_seconds": 0}}
    for key, value in overrides.items():
        section, _, name = key.partition("__")
        if name:
            cfg.setdefault(section, {})[name] = value
        else:
            cfg[section] = value
    d = home / "whatsapp-relay"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text(json.dumps(cfg))


def media_file(home: Path, name: str, data: bytes = b"x") -> str:
    path = home / "image_cache" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return str(path)


@pytest.fixture
def relay(home):
    from plugin.relay import Relay

    r = Relay(home)
    r.adapter_obj = FakeAdapter({G1: "Office A", G2: "Office B", G3: "Office C"})
    r.gateway = FakeGateway(r.adapter_obj)
    return r


def drive(relay, ev):
    """Run one message through the hook, then let the relay's background work finish."""
    async def go():
        result = await relay.handle(ev, relay.gateway)
        while relay.tasks:
            await asyncio.gather(*list(relay.tasks), return_exceptions=True)
        return result
    return asyncio.run(go())
