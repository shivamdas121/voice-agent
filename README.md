# Real-Estate Outbound Voice Agent (Gemini Live) 🏠📞

An AI voice agent that calls real-estate leads who filled out an enquiry form, **qualifies** them,
and **books a site visit**. Built on **LiveKit** with the **Gemini Live API** (one realtime model
does speech-to-text, reasoning, text-to-speech, and turn detection) and **Vobiz** SIP for the phone
call. Booking is handled by an **n8n** workflow.

## Flow

```
Lead submits form
   → (webhook) → trigger_server.py  → dispatches the agent with the lead's details
   → agent dials the lead via SIP   → qualifies them in conversation (Gemini Live)
   → schedule_site_visit tool       → POSTs to n8n → calendar/CRM + confirmation
   → agent reads back the confirmed slot, live on the call
```

## Components

- **`agent.py`** — the LiveKit agent worker. Runs the Gemini Live session, the qualifying
  conversation, the `schedule_site_visit` tool (calls n8n), and `transfer_call` (human handoff).
- **`config.py`** — persona, qualifying-flow prompt, Gemini model/voice, and booking config.
- **`trigger_server.py`** — FastAPI endpoint (`POST /lead-webhook`) your form's webhook calls to
  start a call. Validates an `x-api-key`, normalizes the lead data, and dispatches the agent.
- **`make_call.py`** — CLI to place a test call without a form.
- **n8n** (external) — the booking workflow: Webhook → calendar/CRM + notification → Respond to Webhook.

## Setup

### 1. Prerequisites
- Python 3.10+
- A [LiveKit Cloud](https://cloud.livekit.io/) account
- A [Gemini API key](https://aistudio.google.com/apikey)
- A SIP provider (e.g. Vobiz) with a trunk configured
- An [n8n](https://n8n.io/) instance for booking

### 2. Install
```bash
python -m venv venv
source venv/bin/activate         # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure
```bash
cp .env.example .env             # then fill in the values
```
Required: `LIVEKIT_*`, `GOOGLE_API_KEY`, `VOBIZ_SIP_*`, `BOOKING_WEBHOOK_URL`, `TRIGGER_API_KEY`.
(If you don't have a SIP trunk yet: `python list_trunks.py`, then `python create_trunk.py`.)

### 4. Build the n8n booking workflow
1. **Webhook** node (HTTP `POST`, "Respond using Respond to Webhook node") — copy its production URL
   into `BOOKING_WEBHOOK_URL`.
2. A **Google Calendar / CRM** node to create the booking from the incoming JSON
   (`lead_name`, `phone`, `preferred_date`, `preferred_time`, `intent`, `budget`, ...).
3. (Optional) a **Twilio/Gmail** node to send the lead a confirmation.
4. A **Respond to Webhook** node returning JSON like
   `{ "status": "confirmed", "confirmed_slot": "Sat 7 Jun, 4:00 PM", "message": "You're booked..." }`.
   The agent speaks `message` (or `confirmed_slot`) back on the call.

## Run

**Two processes** (or use Docker below):
```bash
# Terminal 1 — the agent worker
python agent.py start

# Terminal 2 — the form-trigger HTTP server
uvicorn trigger_server:app --host 0.0.0.0 --port 8080
```

Point your lead form's webhook at `http://<your-host>:8080/lead-webhook` (expose it publicly via a
tunnel/cloud host) with header `x-api-key: <TRIGGER_API_KEY>` and a JSON body containing at least a
phone number. Example:
```bash
curl -X POST http://localhost:8080/lead-webhook \
  -H "Content-Type: application/json" \
  -H "x-api-key: $TRIGGER_API_KEY" \
  -d '{"name":"Rahul","phone":"+919876543210","project":"Skyline Heights","budget":"1.2 Cr","intent":"buy"}'
```

**Or with Docker** (runs both the agent and the trigger server):
```bash
docker compose up --build
```

### Test a call without a form
```bash
python make_call.py --to +91XXXXXXXXXX
```

## Customize

- **Persona / brand / qualifying questions** → `config.py` (`SYSTEM_PROMPT`, `DEVELOPER_NAME`, `AGENT_NAME`).
- **Voice / model** → `GEMINI_VOICE`, `GEMINI_MODEL` in `.env` or `config.py`.
- **Language** → the agent speaks Indian English and switches to Hindi/Hinglish automatically (driven by the prompt).
- **Booking logic** → entirely in your n8n workflow; no agent code changes needed.

## Troubleshooting
- **Call connects but no agent / no audio** → make sure `python agent.py start` is running and
  `GOOGLE_API_KEY` is set. The trigger uses `CreateAgentDispatchRequest`, so the named worker must be up.
- **`404 Not Found` (SIP Trunk)** → check `VOBIZ_SIP_TRUNK_ID`; list with `python list_trunks.py`.
- **Booking not confirmed on the call** → verify `BOOKING_WEBHOOK_URL` and that the n8n workflow ends
  with a **Respond to Webhook** node returning JSON within `BOOKING_TIMEOUT` seconds.
- **Gemini import error** → ensure `livekit-plugins-google>=1.0` is installed (`pip install -r requirements.txt`).

---
_Legacy Deepgram/Sarvam/Groq pipeline and the old Next.js dashboard were moved to `_archive/`._
