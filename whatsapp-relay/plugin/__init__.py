"""whatsapp-relay: Hermes plugin.

1. Files that arrive on the bot number from your source number are forwarded to your groups.
2. Statement screenshots/PDFs are read once by Gemini; the ₹2000 credits go into a ledger,
   you get a confirmation, a nudge after 7 days without a statement, and month totals.

All settings live in <hermes home>/whatsapp-relay/config.json.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("whatsapp_relay")

_RELAY = None


def _relay():
    global _RELAY
    if _RELAY is None:
        from .relay import Relay

        _RELAY = Relay()
    return _RELAY


async def _on_pre_gateway_dispatch(event=None, gateway=None, **kwargs):
    if event is None:
        return None
    try:
        return await _relay().handle(event, gateway)
    except Exception:
        logger.exception("[relay] message handling failed; passing message on unchanged")
        return None


def _install_adapter_intercept() -> bool:
    """Let the relay see each WhatsApp message as the adapter receives it.

    The gateway hook alone misses bursts: while Hermes is still handling one message from a chat, the
    adapter treats the next one from that chat as a follow-up, and drops it when the sender isn't an
    allowed user (the source numbers and the firm never are) before any hook runs. So 4-5 report
    screenshots sent together would lose all but the first. Wrapping the adapters' shared entry point
    hands every WhatsApp message to the relay first; whatever it doesn't handle goes on unchanged,
    and every other platform is untouched. If this Hermes version lacks that entry point, the gateway
    hook still works for single messages."""
    try:
        from gateway.platforms.base import BasePlatformAdapter  # type: ignore
    except Exception as exc:
        logger.warning("[relay] adapter entry point not found (%s); using the gateway hook only", exc)
        return False
    original = BasePlatformAdapter.handle_message
    if getattr(original, "_relay_wrapped", False):
        return True

    async def handle_message(self, event):
        if str(getattr(getattr(self, "platform", None), "value", getattr(self, "platform", ""))).lower() == "whatsapp":
            try:
                if await _relay().handle(event, None, adapter=self):
                    event._gateway_accepted = True
                    return None
            except Exception:
                logger.exception("[relay] message handling failed; passing message on unchanged")
        return await original(self, event)

    handle_message._relay_wrapped = True
    handle_message.__wrapped__ = original
    BasePlatformAdapter.handle_message = handle_message
    return True


TOOL_SCHEMA = {
    "name": "deposit_tracker",
    "description": (
        "Look up the owner's daily deposit tracker, built from savings-account statement screenshots sent on "
        "WhatsApp. Returns, for each tracked account, the month's total received, which working days were "
        "credited, which were paid late, which are missing, which are not yet covered by any statement, and when "
        "the last statement arrived. Use it whenever the "
        "owner asks about the daily ₹2000 credits, deposits received, missing days or monthly totals."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "month": {"type": "string", "description": "Month as YYYY-MM. Leave empty for the current month."}
        },
    },
}


def _tool_handler(params, **kwargs):
    del kwargs
    try:
        return json.dumps(_relay().tool_summary((params or {}).get("month")), ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def register(ctx):
    _install_adapter_intercept()
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
    try:
        ctx.register_tool(name="deposit_tracker", toolset="whatsapp_relay", schema=TOOL_SCHEMA, handler=_tool_handler)
    except Exception as exc:  # the relay still works without the tool
        logger.warning("[relay] could not register deposit_tracker tool: %s", exc)
