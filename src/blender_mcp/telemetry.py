"""
Privacy-focused, anonymous telemetry for Blender MCP
Tracks tool usage, DAU/MAU, and performance metrics
"""

import contextlib
import logging
import os
import platform
import queue
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("blender-mcp-telemetry")


def get_package_version() -> str:
    """Get the installed package version"""
    try:
        return version("mcp-for-blender")
    except PackageNotFoundError:
        # Pre-rename distribution name.
        try:
            return version("blender-mcp")
        except PackageNotFoundError:
            return "unknown"


MCP_VERSION = get_package_version()

CONSENT_CACHE_TTL = 30.0


class EventType(str, Enum):
    """Types of telemetry events"""
    STARTUP = "startup"
    TOOL_EXECUTION = "tool_execution"
    PROMPT_SENT = "prompt_sent"
    CONNECTION = "connection"
    ERROR = "error"


@dataclass
class TelemetryConfig:
    """Anonymous telemetry settings.

    ``src/blender_mcp/config.py`` is gitignored (it carries the project key), so a
    source checkout has no ``blender_mcp.config`` module. This disabled default
    is what the server uses then, instead of failing the import.
    """

    enabled: bool = False
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_bucket: str = ""
    timeout: float = 5.0
    max_prompt_length: int = 1000


def _disabled_telemetry_config() -> TelemetryConfig:
    return TelemetryConfig(enabled=False)


def _load_telemetry_config() -> Any:
    """``telemetry_config`` from config.py, or a disabled default when that file is absent."""
    try:
        from .config import telemetry_config
    except ModuleNotFoundError as exc:
        # A missing dependency inside a present config.py is a real failure.
        # Only the gitignored module itself is optional.
        if exc.name not in (None, "blender_mcp.config", "config"):
            raise
        logger.warning(
            "blender_mcp.config is missing (src/blender_mcp/config.py is gitignored); "
            "telemetry is disabled"
        )
        return _disabled_telemetry_config()
    return telemetry_config


@dataclass
class TelemetryEvent:
    """Structure for telemetry events"""
    event_type: EventType
    customer_uuid: str
    session_id: str
    timestamp: float
    version: str
    platform: str

    # Optional fields
    tool_name: str | None = None
    prompt_text: str | None = None
    success: bool = True
    duration_ms: float | None = None
    error_message: str | None = None
    blender_version: str | None = None
    metadata: dict[str, Any] | None = None


class TelemetryCollector:
    """Main telemetry collection class"""

    def __init__(self):
        """Initialize telemetry collector"""
        # Check the disable flags before importing config. A source checkout
        # has no config.py, and that import used to raise before the flags
        # were read, which took down tools such as get_addon_status.
        if self._is_disabled():
            self.config = _disabled_telemetry_config()
            logger.warning("Telemetry disabled via environment variable")
        else:
            self.config = _load_telemetry_config()

        # Generate or load customer UUID
        self._customer_uuid: str = self._get_or_create_uuid()
        self._session_id: str = str(uuid.uuid4())

        # Rate limiting tracking
        self._event_timestamps: list[float] = []
        self._rate_limit_lock = threading.Lock()

        self._consent_cache: bool | None = None
        self._consent_cached_at: float = 0.0
        self._consent_lock = threading.Lock()

        # Background queue and worker
        self._queue: "queue.Queue[TelemetryEvent]" = queue.Queue(maxsize=1000)
        self._worker: threading.Thread = threading.Thread(
            target=self._worker_loop, daemon=True
        )
        self._worker.start()

        logger.debug(f"Telemetry initialized (enabled={self.config.enabled})")

    def _is_disabled(self) -> bool:
        """Check if telemetry is disabled via environment variables"""
        disable_vars = [
            "DISABLE_TELEMETRY",
            "BLENDER_MCP_DISABLE_TELEMETRY",
            "MCP_DISABLE_TELEMETRY"
        ]

        for var in disable_vars:
            if os.environ.get(var, "").lower() in ("true", "1", "yes", "on"):
                return True
        return False

    def _get_data_directory(self) -> Path:
        """Get directory for storing telemetry data"""
        if sys.platform == "win32":
            base_dir = Path(os.environ.get('APPDATA', Path.home() / 'AppData' / 'Roaming'))
        elif sys.platform == "darwin":
            base_dir = Path.home() / 'Library' / 'Application Support'
        else:  # Linux
            base_dir = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local' / 'share'))

        data_dir = base_dir / 'BlenderMCP'
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir

    def _get_or_create_uuid(self) -> str:
        """Get or create anonymous customer UUID"""
        try:
            data_dir = self._get_data_directory()
            uuid_file = data_dir / "customer_uuid.txt"

            if uuid_file.exists():
                customer_uuid = uuid_file.read_text(encoding="utf-8").strip()
                if customer_uuid:
                    return customer_uuid

            # Create new UUID
            customer_uuid = str(uuid.uuid4())
            uuid_file.write_text(customer_uuid, encoding="utf-8")

            # Set restrictive permissions on Unix
            if sys.platform != "win32":
                os.chmod(uuid_file, 0o600)

            return customer_uuid
        except Exception as e:
            logger.debug(f"Failed to persist UUID: {e}")
            return str(uuid.uuid4())

    def _check_user_consent(self) -> bool:
        """Whether collection is permitted. Unreachable Blender means no."""
        return self.check_user_consent() is True

    def check_user_consent(self) -> bool | None:
        """Consent from Blender: True, False, or None if unreachable.

        Cached for CONSENT_CACHE_TTL seconds. A mutating tool call would
        otherwise trigger several consent round-trips (telemetry event, plus a
        before/after trajectory snapshot each), all serialized on the single
        Blender socket.
        """
        with self._consent_lock:
            if (
                self._consent_cache is not None
                and (time.time() - self._consent_cached_at) < CONSENT_CACHE_TTL
            ):
                return self._consent_cache

        try:
            from .server import get_blender_connection
            blender = get_blender_connection()
            result = blender.send_command("get_telemetry_consent")
            consent = bool(result.get("consent", False))
        except Exception as e:
            logger.debug(f"Could not check telemetry consent: {e}")
            return None

        with self._consent_lock:
            self._consent_cache = consent
            self._consent_cached_at = time.time()
        return consent

    def invalidate_consent_cache(self) -> None:
        """Force the next consent check to re-query Blender."""
        with self._consent_lock:
            self._consent_cache = None
            self._consent_cached_at = 0.0

    def record_event(
        self,
        event_type: EventType,
        tool_name: str | None = None,
        prompt_text: str | None = None,
        success: bool = True,
        duration_ms: float | None = None,
        error_message: str | None = None,
        blender_version: str | None = None,
        metadata: dict[str, Any] | None = None
    ):
        """Record a telemetry event (non-blocking)"""
        if not self.config.enabled:
            return

        # Check user consent for private data collection
        user_consent = self._check_user_consent()
        
        if not user_consent:
            # Without consent, only collect minimal anonymous usage data:
            # - Session startup events
            # - Tool execution events (tool name, success, duration)
            # - Basic session info for DAU/MAU calculation
            # Remove all private information:
            prompt_text = None  # No user prompts
            metadata = None  # No code snippets, params, screenshots, scene info
            # Keep error_message for debugging, but sanitize it
            if error_message:
                # Only keep generic error type, not specific details
                error_message = "Error occurred (details withheld without consent)"

        # Truncate prompt if needed (only if consent was given)
        if prompt_text and len(prompt_text) > self.config.max_prompt_length:
            prompt_text = prompt_text[:self.config.max_prompt_length] + "..."

        # Truncate error messages (only if consent was given and not already sanitized)
        if error_message and user_consent and len(error_message) > 200:
            error_message = error_message[:200] + "..."

        event = TelemetryEvent(
            event_type=event_type,
            customer_uuid=self._customer_uuid,
            session_id=self._session_id,
            timestamp=time.time(),
            version=MCP_VERSION,
            platform=platform.system().lower(),
            tool_name=tool_name,
            prompt_text=prompt_text,
            success=success,
            duration_ms=duration_ms,
            error_message=error_message,
            blender_version=blender_version,
            metadata=metadata
        )

        # Enqueue for background worker
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            logger.debug("Telemetry queue full, dropping event")

    def _worker_loop(self):
        """Background worker that sends telemetry"""
        while True:
            event = self._queue.get()
            try:
                self._send_event(event)
            except Exception as e:
                logger.debug(f"Telemetry send failed: {e}")
            finally:
                with contextlib.suppress(Exception):
                    self._queue.task_done()

    def _auth_headers(self) -> dict[str, str]:
        """Headers for Supabase REST/Storage API requests"""
        return {
            "apikey": self.config.supabase_anon_key,
            "Authorization": f"Bearer {self.config.supabase_anon_key}",
        }

    def _send_event(self, event: TelemetryEvent):
        """Send event to Supabase via the PostgREST API"""
        try:
            data = {
                "customer_uuid": event.customer_uuid,
                "session_id": event.session_id,
                "event_type": event.event_type.value,
                "tool_name": event.tool_name,
                "prompt_text": event.prompt_text,
                "success": event.success,
                "duration_ms": event.duration_ms,
                "error_message": event.error_message,
                "version": event.version,
                "platform": event.platform,
                "blender_version": event.blender_version,
                "metadata": event.metadata or {},
                "event_timestamp": int(event.timestamp),
            }

            response = httpx.post(
                f"{self.config.supabase_url}/rest/v1/telemetry_events",
                json=data,
                headers={**self._auth_headers(), "Prefer": "return=minimal"},
                timeout=self.config.timeout,
            )
            response.raise_for_status()
            logger.debug(f"Telemetry sent: {event.event_type}")

        except Exception as e:
            logger.debug(f"Failed to send telemetry: {e}")

    def upload_screenshot(self, image_bytes: bytes, prefix: str) -> str:
        """Upload screenshot to Supabase Storage.
        
        Args:
            image_bytes: PNG image data
            prefix: Filename prefix (e.g., 'screenshot')
            
        Returns:
            Storage path reference (storage:bucket/filename) or empty string on failure
        """
        if not self.config.enabled:
            return ""

        # Only upload screenshots with user consent
        if not self._check_user_consent():
            logger.debug("User has not consented to telemetry, skipping screenshot upload")
            return ""

        try:
            # Generate unique filename
            timestamp = int(time.time() * 1000)
            filename = f"{prefix}_{self._session_id}_{timestamp}.png"

            bucket = self.config.supabase_bucket
            response = httpx.post(
                f"{self.config.supabase_url}/storage/v1/object/{bucket}/{filename}",
                content=image_bytes,
                headers={**self._auth_headers(), "Content-Type": "image/png"},
                # Image uploads need more headroom than the event timeout
                timeout=10.0,
            )
            response.raise_for_status()

            return f"storage:{bucket}/{filename}"
        except Exception:
            return ""

# Global telemetry instance
_telemetry_collector: TelemetryCollector | None = None


def get_telemetry() -> TelemetryCollector:
    """Get the global telemetry collector instance"""
    global _telemetry_collector
    if _telemetry_collector is None:
        _telemetry_collector = TelemetryCollector()
    return _telemetry_collector


def record_tool_usage(
    tool_name: str,
    success: bool,
    duration_ms: float,
    error: str | None = None
):
    """Convenience function to record tool usage"""
    get_telemetry().record_event(
        event_type=EventType.TOOL_EXECUTION,
        tool_name=tool_name,
        success=success,
        duration_ms=duration_ms,
        error_message=error
    )


def record_startup(blender_version: str | None = None):
    """Record server startup event"""
    get_telemetry().record_event(
        event_type=EventType.STARTUP,
        blender_version=blender_version
    )


def is_telemetry_enabled() -> bool:
    """Check if telemetry is enabled"""
    try:
        return get_telemetry().config.enabled
    except Exception:
        return False
