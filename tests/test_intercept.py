"""The adapter intercept: every WhatsApp message reaches the relay before the adapter's busy-session
check, so a burst of screenshots from one number isn't cut down to the first."""

from __future__ import annotations

import asyncio
import sys
import types

import pytest
from conftest import G1, G2, OWNER, SOURCE, FakeAdapter, Platform, event, jid, media_file


@pytest.fixture
def base_module(monkeypatch):
    """A stand-in for gateway.platforms.base whose handle_message drops every message after the first
    from the same chat, like Hermes does for senders who aren't allowed users."""
    class BasePlatformAdapter:
        def __init__(self, platform):
            self.platform = platform
            self.passed_on = []
            self.active = set()

        async def handle_message(self, event):
            chat = event.source.chat_id
            if chat in self.active:
                return None  # "Dropping message from unauthorized user in active session"
            self.active.add(chat)
            self.passed_on.append(event)

    mod = types.ModuleType("gateway.platforms.base")
    mod.BasePlatformAdapter = BasePlatformAdapter
    for name in ("gateway", "gateway.platforms"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", mod)
    yield mod
    # undo the class patch so other tests see the plain stand-in
    wrapped = getattr(BasePlatformAdapter.handle_message, "__wrapped__", None)
    if wrapped:
        BasePlatformAdapter.handle_message = wrapped


class WhatsAppStandIn(FakeAdapter):
    pass


def test_burst_from_a_source_number_is_all_forwarded(relay, home, base_module, monkeypatch):
    import plugin as P

    monkeypatch.setattr(P, "_RELAY", relay)
    assert P._install_adapter_intercept() is True
    assert P._install_adapter_intercept() is True, "installing twice is harmless"

    class Adapter(base_module.BasePlatformAdapter, WhatsAppStandIn):
        def __init__(self):
            base_module.BasePlatformAdapter.__init__(self, Platform.WHATSAPP)
            WhatsAppStandIn.__init__(self)

    adapter = Adapter()
    relay.gateway = None
    relay.adapter_obj = adapter
    relay.save_groups([{"id": G1, "name": "A"}, {"id": G2, "name": "B"}])

    async def burst():
        for i in range(5):
            await adapter.handle_message(event(SOURCE, f"MIS {i}", media=[(media_file(home, f"img_{i}.jpg"), "image/jpeg")],
                                               media_type="image"))
        await adapter.handle_message(event(OWNER, "how much came in?"))
        while relay.tasks:
            await asyncio.gather(*list(relay.tasks), return_exceptions=True)
    asyncio.run(burst())

    images = [s for s in adapter.sent if s["kind"] == "image"]
    assert len(images) == 10 and {s["caption"] for s in images} == {f"MIS {i}" for i in range(5)}
    assert [e.source.chat_id for e in adapter.passed_on] == [jid(OWNER)], "only the owner's chat goes on to Hermes"


def test_other_platforms_untouched(relay, base_module, monkeypatch):
    import plugin as P

    monkeypatch.setattr(P, "_RELAY", relay)
    P._install_adapter_intercept()
    telegram = base_module.BasePlatformAdapter("telegram")
    ev = event(SOURCE, "hello")
    asyncio.run(telegram.handle_message(ev))
    assert telegram.passed_on == [ev]


def test_without_hermes_the_hook_still_works(monkeypatch):
    import plugin as P

    monkeypatch.setitem(sys.modules, "gateway.platforms.base", None)
    assert P._install_adapter_intercept() is False
