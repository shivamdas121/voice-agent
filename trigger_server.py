"""
Form -> Call trigger server.

A tiny HTTP endpoint that your lead form (Typeform / Google Forms / website / Meta lead ad)
POSTs to on submit. It validates a shared secret, normalizes the lead's data, and dispatches the
`outbound-caller` agent into a fresh room with that data as metadata. The running agent worker
(agent.py) then joins the room and dials the lead.

Run it with:   uvicorn trigger_server:app --host 0.0.0.0 --port 8080
Expose it publicly (e.g. via a tunnel/cloud) so your form provider's webhook can reach it.
"""

import os
import json
import random
import logging

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Header, HTTPException
from livekit import api

load_dotenv(".env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("trigger-server")

LIVEKIT_URL = os.getenv("LIVEKIT_URL")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")
TRIGGER_API_KEY = os.getenv("TRIGGER_API_KEY")
DEFAULT_COUNTRY_CODE = os.getenv("DEFAULT_COUNTRY_CODE", "+91")
AGENT_NAME = "outbound-caller"  # must match agent.py WorkerOptions

app = FastAPI(title="Real-estate lead -> call trigger")

# Common field aliases coming from different form providers -> our canonical keys.
_PHONE_KEYS = ["phone", "phone_number", "mobile", "phoneNumber", "contact", "number"]
_NAME_KEYS = ["lead_name", "name", "full_name", "fullName"]
_FIELD_ALIASES = {
    "project": ["project", "property", "interested_project"],
    "intent": ["intent", "purpose"],
    "budget": ["budget", "budget_range"],
    "location": ["location", "preferred_location", "city"],
    "configuration": ["configuration", "config", "bhk", "type"],
    "timeline": ["timeline", "timeframe"],
    "source": ["source", "lead_source", "utm_source"],
}


def _first(body: dict, keys: list):
    for k in keys:
        v = body.get(k)
        if v not in (None, ""):
            return v
    return None


def _normalize_phone(raw: str) -> str:
    """Strip formatting and ensure E.164 (prepend default country code if missing)."""
    cleaned = "".join(ch for ch in str(raw) if ch.isdigit() or ch == "+")
    if not cleaned:
        return cleaned
    if not cleaned.startswith("+"):
        # 10-digit local number -> prepend default country code.
        cleaned = f"{DEFAULT_COUNTRY_CODE}{cleaned.lstrip('0')}"
    return cleaned


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/lead-webhook")
async def lead_webhook(request: Request, x_api_key: str = Header(default=None)):
    if TRIGGER_API_KEY and x_api_key != TRIGGER_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing x-api-key")

    if not (LIVEKIT_URL and LIVEKIT_API_KEY and LIVEKIT_API_SECRET):
        raise HTTPException(status_code=500, detail="LiveKit credentials not configured")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Body must be JSON")

    phone = _normalize_phone(_first(body, _PHONE_KEYS) or "")
    if not phone or len(phone) < 8:
        raise HTTPException(status_code=400, detail="A valid phone number is required")

    # Build metadata the agent reads (agent.py: _build_instructions).
    metadata = {"phone_number": phone}
    name = _first(body, _NAME_KEYS)
    if name:
        metadata["lead_name"] = name
    for canonical, aliases in _FIELD_ALIASES.items():
        value = _first(body, aliases)
        if value:
            metadata[canonical] = value
    if body.get("user_prompt"):
        metadata["user_prompt"] = body["user_prompt"]

    room_name = f"call-{phone.replace('+', '')}-{random.randint(1000, 9999)}"

    lk_api = api.LiveKitAPI(
        url=LIVEKIT_URL, api_key=LIVEKIT_API_KEY, api_secret=LIVEKIT_API_SECRET
    )
    try:
        dispatch = await lk_api.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=AGENT_NAME,
                room=room_name,
                metadata=json.dumps(metadata),
            )
        )
    except Exception as e:
        logger.error(f"Dispatch failed: {e}")
        raise HTTPException(status_code=502, detail=f"Failed to dispatch call: {e}")
    finally:
        await lk_api.aclose()

    logger.info(f"Dispatched call to {phone} in room {room_name}")
    return {"success": True, "room": room_name, "dispatch_id": dispatch.id, "phone": phone}
