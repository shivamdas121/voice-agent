import os
import certifi

# Fix for macOS SSL Certificate errors - MUST be before other imports
os.environ['SSL_CERT_FILE'] = certifi.where()

import logging
import json
import aiohttp
from dotenv import load_dotenv

from livekit import agents, api
from livekit.agents import AgentSession, Agent, RoomInputOptions
from livekit.plugins import google, noise_cancellation
from livekit.agents import llm
from typing import Optional

# Load environment variables
load_dotenv(".env")

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("realestate-agent")

import config

# The Gemini Live realtime model lives under a `beta` namespace in some plugin versions
# and the top-level `realtime` namespace in newer ones. Support both so the demo just runs.
try:
    from livekit.plugins.google.beta import realtime as google_realtime  # type: ignore
except ImportError:  # pragma: no cover - depends on installed plugin version
    from livekit.plugins.google import realtime as google_realtime  # type: ignore


def _build_realtime_model():
    """Configure the Gemini Live speech-to-speech model (STT + LLM + TTS + turn detection)."""
    logger.info(f"Using Gemini Live (model={config.GEMINI_MODEL}, voice={config.GEMINI_VOICE})")
    return google_realtime.RealtimeModel(
        model=config.GEMINI_MODEL,
        voice=config.GEMINI_VOICE,
        temperature=config.GEMINI_TEMPERATURE,
    )


class RealEstateFunctions(llm.ToolContext):
    """Tools the agent can call: book a site visit (via n8n) and transfer to a human."""

    def __init__(self, ctx: agents.JobContext, phone_number: str = None):
        super().__init__(tools=[])
        self.ctx = ctx
        self.phone_number = phone_number

    @llm.function_tool(
        description=(
            "Book a property site visit for the qualified lead. Call this ONLY after the lead has "
            "agreed to a specific day and time. Pass everything you learned during the call."
        )
    )
    async def schedule_site_visit(
        self,
        preferred_date: str,
        preferred_time: str,
        lead_name: Optional[str] = None,
        intent: Optional[str] = None,
        budget: Optional[str] = None,
        location: Optional[str] = None,
        configuration: Optional[str] = None,
        timeline: Optional[str] = None,
        financing: Optional[str] = None,
        notes: Optional[str] = None,
    ):
        """Send the qualified lead + chosen slot to the n8n booking workflow and return the result."""
        payload = {
            "phone": self.phone_number,
            "lead_name": lead_name,
            "preferred_date": preferred_date,
            "preferred_time": preferred_time,
            "intent": intent,
            "budget": budget,
            "location": location,
            "configuration": configuration,
            "timeline": timeline,
            "financing": financing,
            "notes": notes,
            "room": self.ctx.room.name,
        }
        logger.info(f"Scheduling site visit: {payload}")

        if not config.BOOKING_WEBHOOK_URL:
            logger.warning("BOOKING_WEBHOOK_URL is not set; cannot reach n8n.")
            return (
                "I've noted your preferred slot. Our team will text you a confirmation shortly."
            )

        try:
            timeout = aiohttp.ClientTimeout(total=config.BOOKING_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(config.BOOKING_WEBHOOK_URL, json=payload) as resp:
                    resp.raise_for_status()
                    # n8n's "Respond to Webhook" node should return JSON; tolerate plain text too.
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        data = {"message": (await resp.text())}

            confirmed = data.get("confirmed_slot") or f"{preferred_date} at {preferred_time}"
            message = data.get("message")
            if message:
                return message
            return (
                f"Your site visit is confirmed for {confirmed}. "
                "You'll receive a confirmation message with the details shortly."
            )
        except Exception as e:
            logger.error(f"Booking webhook failed: {e}")
            return (
                "I've captured your preferred slot, but I couldn't confirm it instantly. "
                "Our team will reach out to confirm the exact time shortly."
            )

    @llm.function_tool(
        description="Transfer the call to a human sales agent or another phone number."
    )
    async def transfer_call(self, destination: Optional[str] = None):
        """Transfer the live call via SIP REFER."""
        if destination is None:
            destination = config.DEFAULT_TRANSFER_NUMBER
            if not destination:
                return "Error: No default transfer number configured."
        if "@" not in destination:
            if config.SIP_DOMAIN:
                clean_dest = destination.replace("tel:", "").replace("sip:", "")
                destination = f"sip:{clean_dest}@{config.SIP_DOMAIN}"
            else:
                if not destination.startswith("tel:") and not destination.startswith("sip:"):
                    destination = f"tel:{destination}"
        elif not destination.startswith("sip:"):
            destination = f"sip:{destination}"

        logger.info(f"Transferring call to {destination}")

        participant_identity = None
        if self.phone_number:
            participant_identity = f"sip_{self.phone_number}"
        else:
            for p in self.ctx.room.remote_participants.values():
                participant_identity = p.identity
                break

        if not participant_identity:
            logger.error("Could not determine participant identity for transfer")
            return "Failed to transfer: could not identify the caller."

        try:
            logger.info(f"Transferring participant {participant_identity} to {destination}")
            await self.ctx.api.sip.transfer_sip_participant(
                api.TransferSIPParticipantRequest(
                    room_name=self.ctx.room.name,
                    participant_identity=participant_identity,
                    transfer_to=destination,
                    play_dialtone=False,
                )
            )
            return "Transfer initiated successfully."
        except Exception as e:
            logger.error(f"Transfer failed: {e}")
            return f"Error executing transfer: {e}"


class OutboundAssistant(Agent):
    """AI agent for outbound real-estate lead-qualification calls."""

    def __init__(self, instructions: str, tools: list) -> None:
        super().__init__(instructions=instructions, tools=tools)


# Fields we surface from the form (via call metadata) into the system prompt as lead context.
_LEAD_FIELDS = [
    ("lead_name", "Name"),
    ("project", "Interested project"),
    ("intent", "Intent"),
    ("budget", "Budget"),
    ("location", "Preferred location"),
    ("configuration", "Configuration"),
    ("timeline", "Timeline"),
    ("source", "Lead source"),
]


def _build_instructions(config_dict: dict, phone_number: str = None) -> str:
    """Compose the system prompt with whatever the form already told us about the lead."""
    lines = []
    for key, label in _LEAD_FIELDS:
        value = config_dict.get(key)
        if value:
            lines.append(f"- {label}: {value}")
    if phone_number:
        lines.append(f"- Phone: {phone_number}")

    # Free-text context passed through from the form / dispatcher.
    extra = config_dict.get("user_prompt")

    instructions = config.SYSTEM_PROMPT
    if lines:
        instructions += "\n\n## Lead context (from the form they submitted):\n" + "\n".join(lines)
        instructions += (
            "\n\nUse this context to personalize the call. Don't re-ask what you already know; "
            "confirm it instead, and focus on the gaps."
        )
    if extra:
        instructions += f"\n\n## Additional context:\n{extra}"
    return instructions


async def entrypoint(ctx: agents.JobContext):
    """
    Main entrypoint for the agent.

    For outbound calls:
    1. Reads 'phone_number' and lead fields from job/room metadata.
    2. Connects to the room and starts a Gemini Live session.
    3. Dials the lead via SIP and greets them once they pick up.
    """
    logger.info(f"Connecting to room: {ctx.room.name}")

    phone_number = None
    config_dict = {}

    # Job metadata (dispatch / trigger server)
    try:
        if ctx.job.metadata:
            data = json.loads(ctx.job.metadata)
            phone_number = data.get("phone_number")
            config_dict = data
    except Exception:
        pass

    # Room metadata overrides job metadata if present
    try:
        if ctx.room.metadata:
            data = json.loads(ctx.room.metadata)
            if data.get("phone_number"):
                phone_number = data.get("phone_number")
            config_dict.update(data)
    except Exception:
        logger.warning("No valid JSON metadata found in Room.")

    # Tools
    fnc_ctx = RealEstateFunctions(ctx, phone_number)

    # Gemini Live session: one realtime model does STT + LLM + TTS + turn detection.
    session = AgentSession(
        llm=_build_realtime_model(),
    )

    instructions = _build_instructions(config_dict, phone_number)

    await session.start(
        room=ctx.room,
        agent=OutboundAssistant(
            instructions=instructions,
            tools=list(fnc_ctx.function_tools.values()),
        ),
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVCTelephony(),
            close_on_disconnect=True,
        ),
    )

    # Decide whether we need to dial out (vs. the lead already being in the room).
    should_dial = False
    if phone_number:
        user_already_here = any(
            ("sip_" in p.identity) for p in ctx.room.remote_participants.values()
        )
        if not user_already_here:
            should_dial = True
            logger.info("User not in room. Agent will initiate dial-out.")
        else:
            logger.info("User already in room. Only generating greeting.")

    if should_dial:
        logger.info(f"Initiating outbound SIP call to {phone_number}...")
        try:
            await ctx.api.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    room_name=ctx.room.name,
                    sip_trunk_id=config.SIP_TRUNK_ID,
                    sip_call_to=phone_number,
                    participant_identity=f"sip_{phone_number}",
                    wait_until_answered=True,
                )
            )
            logger.info("Call answered! Agent is now listening.")
            await session.generate_reply(instructions=config.INITIAL_GREETING)
        except Exception as e:
            logger.error(f"Failed to place outbound call: {e}")
            ctx.shutdown()
    else:
        logger.info("No dial-out needed; greeting whoever is in the room.")
        await session.generate_reply(instructions=config.fallback_greeting)


if __name__ == "__main__":
    # The agent name "outbound-caller" is used by the trigger/dispatch to find this worker.
    agents.cli.run_app(
        agents.WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name="outbound-caller",
        )
    )
