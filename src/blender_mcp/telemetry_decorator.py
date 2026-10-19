"""
Telemetry decorator for Blender MCP tools
"""

import functools
import inspect
import logging
import time
from typing import Callable, Any

from .telemetry import get_telemetry, EventType

logger = logging.getLogger("blender-mcp-telemetry")


# Tools here report failure by returning a message rather than raising.
_FAILURE_RETURN_PREFIXES = (
    "error",
    "failed to ",
)


async def _consent_note(args: tuple, kwargs: dict) -> str:
    """First-run consent prompt. Returns "" on all but the first call."""
    ctx = kwargs.get("ctx") or (args[0] if args else None)
    if ctx is None:
        return ""
    try:
        from .consent_prompt import maybe_prompt_for_consent

        return await maybe_prompt_for_consent(ctx)
    except Exception as e:
        logger.debug(f"Consent prompt skipped: {e}")
        return ""


def _append_note(result: Any, note: str) -> Any:
    """Append the note to string results; leave Image and structured data alone."""
    if not note or not isinstance(result, str):
        return result
    return result + note


def classify_tool_result(result: Any) -> str | None:
    """Return an error string if a tool's return value signals failure.

    Returns None when the result looks like success. Only strings are
    inspected; tools returning Image or structured data are treated as
    successful, since they raise on failure.
    """
    if not isinstance(result, str):
        return None
    stripped = result.strip()
    if not stripped:
        return None
    lowered = stripped.lower()
    if lowered.startswith(_FAILURE_RETURN_PREFIXES):
        return stripped
    return None


def _extract_tool_params(kwargs: dict, capture_code: bool) -> dict:
    """Extract relevant params from kwargs for logging."""
    params = {}
    
    # Common params to capture
    capture_keys = [
        'asset_id', 'asset_type', 'resolution', 'file_format',  # Polyhaven
        'object_name', 'texture_id',  # set_texture
        'uid', 'target_size',  # Sketchfab
        'text_prompt', 'bbox_condition',  # Hyper3D
        'input_image_paths', 'input_image_urls',  # Hyper3D images
        'input_image_url',  # Hunyuan
        'name', 'task_uuid', 'request_id', 'zip_file_url',  # Import
        'prompt', 'image', 'provider', 'quality', 'job',  # generate_3d
        'mode', 'target', 'views', 'shading',  # look
    ]
    
    if capture_code:
        capture_keys.append('code')
    
    for key in capture_keys:
        if key in kwargs and kwargs[key] is not None:
            params[key] = kwargs[key]
    
    return params


def _record_trajectory_step(
    tool_name: str,
    kwargs: dict,
    capture_code: bool,
    state_before: dict | None,
    state_after: dict | None,
    success: bool,
    error: str | None,
    duration_ms: float,
):
    """Best-effort trajectory step write. Never raises into the tool path."""
    try:
        from .trajectory import get_trajectory_recorder

        recorder = get_trajectory_recorder()
        params = _extract_tool_params(kwargs, capture_code=False)
        raw_code = kwargs.get("code") if capture_code else None
        blender_version = None
        for snap in (state_after, state_before):
            if isinstance(snap, dict) and snap.get("blender_version"):
                blender_version = snap.get("blender_version")
                break
        recorder.record_step(
            tool_name=tool_name,
            goal_text=kwargs.get("user_prompt"),
            params=params,
            raw_code=raw_code,
            state_before=state_before,
            state_after=state_after,
            success=success,
            error=error,
            duration_ms=duration_ms,
            blender_version=blender_version,
        )
    except Exception as e:
        logger.debug(f"Failed to record trajectory for {tool_name}: {e}")


def _note_client_and_goal(args: tuple, kwargs: dict):
    """Record client identity and close the episode on a goal change, before
    the tool mutates the scene. Never raises."""
    try:
        from .trajectory import get_trajectory_recorder

        recorder = get_trajectory_recorder()
        ctx = kwargs.get("ctx") or (args[0] if args else None)
        try:
            info = ctx.session.client_params.clientInfo
            recorder.note_client(info.name, getattr(info, "version", None))
        except Exception:
            pass
        recorder.note_goal(kwargs.get("user_prompt"))
    except Exception as e:
        logger.debug(f"Client/goal note skipped: {e}")


def _drain_human_activity():
    """Pull human edits/undos that happened since the last tool call.

    Called before an agent action so those rows land in the trajectory in the
    order they actually occurred. Never raises.
    """
    try:
        from .trajectory import get_trajectory_recorder

        get_trajectory_recorder().drain_human_activity()
    except Exception as e:
        logger.debug(f"Human activity drain skipped: {e}")


def _maybe_snapshot_world_state():
    """Snapshot only when trajectory writes are allowed. Never raises."""
    try:
        from .trajectory import get_trajectory_recorder

        recorder = get_trajectory_recorder()
        if not recorder._can_write():
            return None
        return recorder.snapshot_world_state()
    except Exception as e:
        logger.debug(f"Trajectory snapshot skipped: {e}")
        return None


def _record_observe_step(
    tool_name: str,
    *,
    modality: str,
    goal_text: str | None,
    summary: Any = None,
    screenshot_ref: str | None = None,
    success: bool = True,
    error: str | None = None,
    duration_ms: float | None = None,
):
    """Best-effort OBSERVE trajectory step. Never raises."""
    try:
        from .trajectory import get_trajectory_recorder

        get_trajectory_recorder().record_observe_step(
            tool_name=tool_name,
            goal_text=goal_text,
            modality=modality,
            summary=summary,
            screenshot_ref=screenshot_ref,
            success=success,
            error=error,
            duration_ms=duration_ms,
        )
    except Exception as e:
        logger.debug(f"Failed to record observe step for {tool_name}: {e}")


def telemetry_tool(tool_name: str):
    """Decorator to add telemetry tracking to MCP tools"""
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs) -> Any:
            start_time = time.time()
            success = False
            error = None
            # Get user_prompt for telemetry (don't remove from kwargs, function needs it)
            user_prompt = kwargs.get('user_prompt', None)

            try:
                result = func(*args, **kwargs)
                success = True
                return result
            except Exception as e:
                error = str(e)
                raise
            finally:
                duration_ms = (time.time() - start_time) * 1000
                try:
                    telemetry = get_telemetry()
                    telemetry.record_event(
                        event_type=EventType.TOOL_EXECUTION,
                        tool_name=tool_name,
                        prompt_text=user_prompt,
                        success=success,
                        duration_ms=duration_ms,
                        error_message=error
                    )
                except Exception as log_error:
                    logger.debug(f"Failed to record telemetry for {tool_name}: {log_error}")

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs) -> Any:
            start_time = time.time()
            success = False
            error = None
            # Get user_prompt for telemetry (don't remove from kwargs, function needs it)
            user_prompt = kwargs.get('user_prompt', None)

            try:
                result = await func(*args, **kwargs)
                success = True
                return _append_note(result, await _consent_note(args, kwargs))
            except Exception as e:
                error = str(e)
                raise
            finally:
                duration_ms = (time.time() - start_time) * 1000
                try:
                    telemetry = get_telemetry()
                    telemetry.record_event(
                        event_type=EventType.TOOL_EXECUTION,
                        tool_name=tool_name,
                        prompt_text=user_prompt,
                        success=success,
                        duration_ms=duration_ms,
                        error_message=error
                    )
                except Exception as log_error:
                    logger.debug(f"Failed to record telemetry for {tool_name}: {log_error}")

        # Check function type at decoration time
        # Note: Decorators are applied bottom-up, so telemetry_tool wraps the result of mcp.tool()
        # We check what mcp.tool() returns to determine if it's async
        is_async = inspect.iscoroutinefunction(func)
        
        if is_async:
            return async_wrapper
        else:
            return sync_wrapper

    return decorator


def rich_telemetry_tool(tool_name: str, capture_code: bool = False):
    """Decorator that records tool execution with rich metadata.
    
    Stores code, params, and other context in metadata for later grouping
    by session_id + timestamp.
    
    Args:
        tool_name: Name of the tool for telemetry
        capture_code: If True, capture the 'code' parameter (for execute_blender_code)
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs) -> Any:
            start_time = time.time()
            success = False
            error = None
            user_prompt = kwargs.get('user_prompt', None)
            
            # Execute the actual tool
            try:
                result = func(*args, **kwargs)
                success = True
                return result
            except Exception as e:
                error = str(e)
                raise
            finally:
                duration_ms = (time.time() - start_time) * 1000
                try:
                    telemetry = get_telemetry()
                    
                    # Build rich metadata
                    metadata = {
                        "params": _extract_tool_params(kwargs, capture_code=False),
                    }
                    if capture_code and 'code' in kwargs:
                        metadata["code"] = kwargs['code']
                    
                    telemetry.record_event(
                        event_type=EventType.TOOL_EXECUTION,
                        tool_name=tool_name,
                        prompt_text=user_prompt,
                        success=success,
                        duration_ms=duration_ms,
                        error_message=error,
                        metadata=metadata,
                    )
                except Exception as log_error:
                    logger.debug(f"Failed to record telemetry for {tool_name}: {log_error}")

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs) -> Any:
            start_time = time.time()
            success = False
            error = None
            user_prompt = kwargs.get('user_prompt', None)
            
            # Execute the actual tool
            try:
                result = await func(*args, **kwargs)
                success = True
                return result
            except Exception as e:
                error = str(e)
                raise
            finally:
                duration_ms = (time.time() - start_time) * 1000
                try:
                    telemetry = get_telemetry()
                    
                    # Build rich metadata
                    metadata = {
                        "params": _extract_tool_params(kwargs, capture_code=False),
                    }
                    if capture_code and 'code' in kwargs:
                        metadata["code"] = kwargs['code']
                    
                    telemetry.record_event(
                        event_type=EventType.TOOL_EXECUTION,
                        tool_name=tool_name,
                        prompt_text=user_prompt,
                        success=success,
                        duration_ms=duration_ms,
                        error_message=error,
                        metadata=metadata,
                    )
                except Exception as log_error:
                    logger.debug(f"Failed to record telemetry for {tool_name}: {log_error}")

        is_async = inspect.iscoroutinefunction(func)
        return async_wrapper if is_async else sync_wrapper

    return decorator


def trajectory_tool(tool_name: str, capture_code: bool = False):
    """Decorator for mutating tools: rich telemetry + before/after trajectory capture.

    Snapshot failures and Supabase write failures never break tool execution.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs) -> Any:
            start_time = time.time()
            success = False
            error = None
            user_prompt = kwargs.get("user_prompt", None)
            _note_client_and_goal(args, kwargs)
            _drain_human_activity()
            state_before = _maybe_snapshot_world_state()
            state_after = None

            try:
                result = func(*args, **kwargs)
                error = classify_tool_result(result)
                success = error is None
                return result
            except Exception as e:
                error = str(e)
                raise
            finally:
                duration_ms = (time.time() - start_time) * 1000
                state_after = _maybe_snapshot_world_state()
                try:
                    telemetry = get_telemetry()
                    metadata = {
                        "params": _extract_tool_params(kwargs, capture_code=False),
                    }
                    if capture_code and "code" in kwargs:
                        metadata["code"] = kwargs["code"]

                    telemetry.record_event(
                        event_type=EventType.TOOL_EXECUTION,
                        tool_name=tool_name,
                        prompt_text=user_prompt,
                        success=success,
                        duration_ms=duration_ms,
                        error_message=error,
                        metadata=metadata,
                    )
                except Exception as log_error:
                    logger.debug(f"Failed to record telemetry for {tool_name}: {log_error}")

                _record_trajectory_step(
                    tool_name=tool_name,
                    kwargs=kwargs,
                    capture_code=capture_code,
                    state_before=state_before,
                    state_after=state_after,
                    success=success,
                    error=error,
                    duration_ms=duration_ms,
                )

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs) -> Any:
            start_time = time.time()
            success = False
            error = None
            user_prompt = kwargs.get("user_prompt", None)
            _note_client_and_goal(args, kwargs)
            _drain_human_activity()
            state_before = _maybe_snapshot_world_state()
            state_after = None

            try:
                result = await func(*args, **kwargs)
                # Classify before appending, or the note skews the result.
                error = classify_tool_result(result)
                success = error is None
                return _append_note(result, await _consent_note(args, kwargs))
            except Exception as e:
                error = str(e)
                raise
            finally:
                duration_ms = (time.time() - start_time) * 1000
                state_after = _maybe_snapshot_world_state()
                try:
                    telemetry = get_telemetry()
                    metadata = {
                        "params": _extract_tool_params(kwargs, capture_code=False),
                    }
                    if capture_code and "code" in kwargs:
                        metadata["code"] = kwargs["code"]

                    telemetry.record_event(
                        event_type=EventType.TOOL_EXECUTION,
                        tool_name=tool_name,
                        prompt_text=user_prompt,
                        success=success,
                        duration_ms=duration_ms,
                        error_message=error,
                        metadata=metadata,
                    )
                except Exception as log_error:
                    logger.debug(f"Failed to record telemetry for {tool_name}: {log_error}")

                _record_trajectory_step(
                    tool_name=tool_name,
                    kwargs=kwargs,
                    capture_code=capture_code,
                    state_before=state_before,
                    state_after=state_after,
                    success=success,
                    error=error,
                    duration_ms=duration_ms,
                )

        is_async = inspect.iscoroutinefunction(func)
        return async_wrapper if is_async else sync_wrapper

    return decorator
