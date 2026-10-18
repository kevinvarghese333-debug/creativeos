"""Read statement screenshots/PDFs with one Gemini call and return the transactions as JSON.

Gemini only reads. Deciding which entries count (amount, keywords, credit vs debit) is done
in plain Python afterwards, so a misread can never invent a rule.
"""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
from datetime import date
from pathlib import Path
from typing import Optional

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
MAX_INLINE_BYTES = 18 * 1024 * 1024  # Gemini inline request limit is ~20 MB

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "is_statement": {"type": "BOOLEAN"},
        "account_label": {"type": "STRING"},
        "period_start": {"type": "STRING"},
        "period_end": {"type": "STRING"},
        "transactions": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "date": {"type": "STRING"},
                    "description": {"type": "STRING"},
                    "amount": {"type": "NUMBER"},
                    "direction": {"type": "STRING", "enum": ["credit", "debit", "unknown"]},
                    "reference": {"type": "STRING"},
                    "image": {"type": "INTEGER"},
                },
                "required": ["date", "description", "amount", "direction"],
            },
        },
        "notes": {"type": "STRING"},
    },
    "required": ["is_statement", "transactions"],
}

PROMPT = """You are reading screenshots or PDF pages of an Indian savings-account statement or passbook.

Today is {today}. Extract EVERY transaction row you can see, across all the attached images, exactly as printed.

Rules:
- Dates in India are day-first (05/10/2026 is 5 October 2026). Output dates as YYYY-MM-DD.
  If a row has no year, use the year that makes the date fall on or before today.
- amount: the transaction amount as a plain positive number in rupees (no commas, no ₹). Never the running balance.
- direction: "credit" for money coming in (Cr, CR, deposit, credit column), "debit" for money going out (Dr, DR, withdrawal),
  "unknown" only if you truly cannot tell.
- description: the narration / particulars text for that row, copied as printed.
- reference: any cheque number, UTR, reference or transaction ID on that row, else "".
- image: which attached file the row appears on (1 = the first file, 2 = the second, ...).
- If the same row appears in two overlapping screenshots, list it ONCE.
- period_start / period_end: the statement period if printed, else "".
- account_label: account number (masked is fine) or account name shown, else "".
- is_statement: false if these images are not a bank/passbook/account statement at all.
- Do not guess rows that are cut off or unreadable; mention them in notes instead.
{hint}"""


class ExtractionError(RuntimeError):
    pass


def api_key() -> str:
    for name in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    # Fallback: read the Hermes .env directly (the gateway normally loads it already).
    try:
        from .ledger import hermes_home  # type: ignore
    except Exception:  # pragma: no cover - script context
        from ledger import hermes_home  # type: ignore
    env_path = hermes_home() / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            for name in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
                if line.startswith(f"{name}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _mime_for(path: str, hinted: Optional[str]) -> str:
    if hinted and "/" in hinted and hinted not in {"unknown", "application/octet-stream"}:
        return hinted
    guess = mimetypes.guess_type(path)[0]
    if guess:
        return guess
    return "image/jpeg"


def build_payload(files: list[tuple[str, Optional[str]]], *, today: date, hint: str) -> dict:
    parts: list[dict] = [{"text": PROMPT.format(today=today.isoformat(), hint=hint)}]
    total = 0
    for path, mime in files:
        data = Path(path).read_bytes()
        total += len(data)
        if total > MAX_INLINE_BYTES:
            raise ExtractionError("The statement files are too large to send in one go (over 18 MB).")
        parts.append({"inlineData": {"mimeType": _mime_for(path, mime), "data": base64.b64encode(data).decode("ascii")}})
    return {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
        },
    }


def parse_response(data: dict) -> dict:
    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason") or "no answer returned"
        raise ExtractionError(f"Gemini returned nothing ({reason}).")
    parts = ((candidates[0].get("content") or {}).get("parts")) or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if not text.strip():
        raise ExtractionError("Gemini returned an empty answer.")
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"Gemini's answer was not valid JSON: {exc}") from exc
    if not isinstance(result, dict):
        raise ExtractionError("Gemini's answer had the wrong shape.")
    result.setdefault("transactions", [])
    result.setdefault("is_statement", bool(result["transactions"]))
    return result


async def extract(files: list[tuple[str, Optional[str]]], *, model: str, today: date, hint: str = "",
                  timeout: float = 120, retries: int = 2, base_url: str = "") -> dict:
    key = api_key()
    if not key:
        raise ExtractionError("No Gemini key found. Run `bash setup.sh` again and enter the key.")
    payload = build_payload(files, today=today, hint=hint)
    url = f"{(base_url or GEMINI_BASE_URL).rstrip('/')}/models/{model}:generateContent"
    headers = {"x-goog-api-key": key, "content-type": "application/json"}

    import httpx  # shipped with Hermes

    last_error = ""
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(retries + 1):
            try:
                resp = await client.post(url, headers=headers, json=payload)
            except httpx.HTTPError as exc:
                last_error = f"network error: {exc}"
            else:
                if resp.status_code == 200:
                    return parse_response(resp.json())
                body = resp.text[:300]
                last_error = f"HTTP {resp.status_code}: {body}"
                if resp.status_code not in (408, 429, 500, 502, 503, 504):
                    break
            if attempt < retries:
                await asyncio.sleep(8 * (attempt + 1))
    raise ExtractionError(f"Gemini call failed ({last_error}).")
