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


TOOL_SCHEMA = {
    "name": "deposit_tracker",
    "description": (
        "Look up the owner's daily deposit tracker, built from savings-account statement screenshots sent on "
        "WhatsApp. Returns the month's total received, which working days were credited, which are missing, "
        "which are not yet covered by any statement, and when the last statement arrived. Use it whenever the "
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
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
    try:
        ctx.register_tool(name="deposit_tracker", toolset="whatsapp_relay", schema=TOOL_SCHEMA, handler=_tool_handler)
    except Exception as exc:  # the relay still works without the tool
        logger.warning("[relay] could not register deposit_tracker tool: %s", exc)
