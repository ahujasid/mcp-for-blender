"""First-run telemetry consent prompt.

Consent lives in one place: the ``telemetry_consent`` checkbox in the Blender
addon preferences, which is **off by default**. Collection of prompts, code,
screenshots and trajectory data is strictly opt-in, so this module is what gives
most users their one chance to say yes.

Only clients declaring the ``elicitation`` capability are asked, via a yes/no
dialog the client renders itself; only an explicit ``accept`` with
``consent=True`` turns collection on. Clients without it are left alone -- a
model relaying a prose answer is not the user answering, so those users opt in
via the Blender addon instead.

Asked at most once per conversation. An answer either way is remembered on disk
so it is not re-asked on every future conversation.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("BlenderMCPConsent")

# Bump to re-ask everyone (e.g. if what gets collected changes materially).
# v2: telemetry became opt-in. Answers recorded under the old on-by-default
# regime do not carry over -- an opt-out then is not an opt-in now.
CONSENT_PROMPT_VERSION = 2

_state_lock = threading.Lock()
# Keyed by client session: the server process outlives any one conversation.
_asked_sessions: set[int] = set()


# Written out rather than generated from a Pydantic model: model_json_schema()
# adds a top-level "title" and "description", which the MCP spec doesn't allow
# there and Codex rejects, so the prompt never appeared in Codex.
CONSENT_SCHEMA = {
    "type": "object",
    "properties": {
        "consent": {
            "type": "boolean",
            "title": "Contribute session data",
            "description": (
                "Yes, contribute my prompts, generated code, screenshots and scene "
                "data to help improve Blender MCP. Choosing no leaves collection off."
            ),
            "default": False,
        },
    },
    "required": ["consent"],
}


PROMPT_MESSAGE = (
    "Blender MCP can collect data from this session to help improve the tool: "
    "your prompts, the code that gets generated, viewport screenshots, and scene "
    "metadata. It is not linked to your name or account, and you can change this "
    "any time in Blender under Preferences > Add-ons > Blender MCP.\n\n"
    "This is off by default and stays off unless you turn it on. Opt in?"
)

def _state_path() -> Path:
    """Where the remembered answer lives. Next to the telemetry install ID."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "blender-mcp" / "consent_prompt.json"


def _read_state() -> dict[str, Any]:
    try:
        with open(_state_path(), "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(**fields: Any) -> None:
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        state = _read_state()
        state.update(fields)
        state["prompt_version"] = CONSENT_PROMPT_VERSION
        state["updated_at"] = time.time()
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        logger.debug(f"Could not persist consent prompt state: {e}")


def _already_answered() -> bool:
    """True if the user gave an explicit answer we should not re-ask."""
    state = _read_state()
    if state.get("prompt_version") != CONSENT_PROMPT_VERSION:
        return False
    return state.get("action") in ("accept", "decline")


def _session_key(ctx: Any) -> int | None:
    """Identity of the client session, or None if it cannot be determined."""
    try:
        return id(ctx.request_context.session)
    except Exception as e:
        logger.debug(f"Could not identify client session: {e}")
        return None


def _client_supports_elicitation(ctx: Any) -> bool:
    """Ask the client directly rather than guessing from a hardcoded list."""
    try:
        from mcp.types import ClientCapabilities, ElicitationCapability

        session = ctx.request_context.session
        return bool(
            session.check_client_capability(
                ClientCapabilities(elicitation=ElicitationCapability())
            )
        )
    except Exception as e:
        logger.debug(f"Could not check elicitation capability: {e}")
        return False


def _current_consent() -> bool | None:
    """Read consent from Blender. None if Blender could not be reached."""
    try:
        from .telemetry import get_telemetry

        return get_telemetry().check_user_consent()
    except Exception as e:
        logger.debug(f"Could not read telemetry consent: {e}")
        return None


def _apply_consent(consent: bool) -> bool:
    """Write consent to Blender and refresh the cached value."""
    try:
        from .server import get_blender_connection
        from .telemetry import telemetry

        blender = get_blender_connection()
        result = blender.send_command(
            "set_telemetry_consent", {"consent": consent}
        )
        if "error" in result:
            logger.debug(f"Addon rejected consent write: {result['error']}")
            return False
        telemetry.invalidate_consent_cache()
        return True
    except Exception as e:
        logger.debug(f"Could not set telemetry consent: {e}")
        return False


async def maybe_prompt_for_consent(ctx: Any) -> str:
    """Ask about telemetry once per conversation. Returns text to append, or "".

    Safe to call on every tool invocation: returns "" on a lock check unless
    this is the first call of a conversation that needs to ask.
    """
    session_key = _session_key(ctx)
    if session_key is None:
        # Cannot tell conversations apart, so asking could repeat every call.
        return ""

    with _state_lock:
        if session_key in _asked_sessions:
            return ""
        _asked_sessions.add(session_key)

    try:
        if _already_answered():
            return ""

        # Already opted in via the checkbox -- nothing to ask.
        consent = _current_consent()
        if consent is None or consent:
            return ""

        if not _client_supports_elicitation(ctx):
            # No dialog available. Opting in happens in the Blender addon
            # instead -- a model relaying prose is not the user answering.
            return ""

        result = await ctx.request_context.session.elicit_form(
            message=PROMPT_MESSAGE,
            requestedSchema=CONSENT_SCHEMA,
            related_request_id=ctx.request_id,
        )
        action = getattr(result, "action", None)

        if action == "accept":
            # Only a literal True is consent; anything else is "not granted".
            granted = (result.content or {}).get("consent") is True
            # Record how the answer arrived, so an opt-in can be audited later.
            _write_state(action="accept", consent=granted, via="elicitation")
            if granted and _apply_consent(True):
                return (
                    "\n\n---\n"
                    "Data collection is now ON. You can turn it off any time in "
                    "Blender under Preferences > Add-ons > Blender MCP."
                )
            return ""

        if action == "decline":
            # Explicit no. Remember it and never ask again.
            _write_state(action="decline", consent=False, via="elicitation")
            return ""

        # Cancelled/dismissed: not an answer. Stay off, ask again next session.
        return ""
    except Exception as e:
        logger.debug(f"Consent prompt failed: {e}")
        return ""


def reset_for_tests() -> None:
    """Clear the per-conversation guard. Test helper."""
    with _state_lock:
        _asked_sessions.clear()
