"""Common MCP handler functions shared between mcp.py and mcp_projects.py.

This module serves as the single source of truth for MCP functionality.
"""

import asyncio
import base64
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Any, ParamSpec, TypeVar
from urllib.parse import quote, unquote, urlparse
from uuid import uuid4

from lfx.base.mcp.constants import MAX_MCP_TOOL_NAME_LENGTH
from lfx.base.mcp.util import get_flow_snake_case, get_unique_name, sanitize_mcp_name
from lfx.log.logger import logger
from lfx.utils.helpers import build_content_type_from_extension
from mcp import types
from sqlmodel import select

from langflow.api.v1.endpoints import simple_run_flow
from langflow.api.v1.schemas import SimplifiedAPIRequest
from langflow.helpers.flow import json_schema_from_flow
from langflow.schema.message import Message
from langflow.services.database.models import Flow
from langflow.services.database.models.file.model import File as UserFile
from langflow.services.database.models.user.model import User
from langflow.services.deps import get_settings_service, get_storage_service, session_scope

T = TypeVar("T")
P = ParamSpec("P")

MCP_SERVERS_FILE = "_mcp_servers"

# Create context variables
current_user_ctx: ContextVar[User] = ContextVar("current_user_ctx")
# Carries per-request variables injected via HTTP headers (e.g., X-Langflow-Global-Var-*)
current_request_variables_ctx: ContextVar[dict[str, str] | None] = ContextVar(
    "current_request_variables_ctx", default=None
)


def handle_mcp_errors(func: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
    """Decorator to handle MCP endpoint errors consistently."""

    @wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            msg = f"Error in {func.__name__}: {e!s}"
            await logger.aexception(msg)
            raise

    return wrapper


async def with_db_session(operation: Callable[[Any], Awaitable[T]]) -> T:
    """Execute an operation within a database session context."""
    async with session_scope() as session:
        return await operation(session)


class MCPConfig:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.enable_progress_notifications = None
            cls._instance.stream_messages_in_progress = None
        return cls._instance


def get_mcp_config():
    return MCPConfig()


class MessageStreamingEventManager:
    """Event manager that streams chat messages via MCP progress notifications.

    This event manager captures messages during flow execution and sends them
    as MCP progress notifications with the message field populated.

    Compatible with the EventManager interface expected by simple_run_flow.
    """

    def __init__(self, progress_token: str, session, request_id: str | int, queue=None):
        """Initialize the message streaming event manager.

        Args:
            progress_token: The progress token from the MCP request
            session: The MCP session to send progress notifications through
            request_id: The request ID to associate notifications with
            queue: Optional queue for standard event handling (unused for streaming)
        """
        self.progress_token = progress_token
        self.session = session
        self.request_id = request_id
        self.queue = queue
        self.message_count = 0
        self.last_progress = 0.0
        self.events: dict[str, Any] = {}

        # Register standard events
        self._register_events()

    def _register_events(self):
        """Register event handlers for message streaming."""
        # Note: Components call on_message() directly, not through send_event()
        # So we don't need to register in self.events

    def on_message(self, *, data: dict):
        """Handle message events - called directly by components."""
        print(
            f"[EVENT MANAGER DEBUG] on_message called directly with data keys: {list(data.keys()) if isinstance(data, dict) else type(data)}"
        )
        # Handle the message asynchronously using asyncio.run_coroutine_threadsafe
        import asyncio

        try:
            # Try to get the running loop in the current thread
            try:
                loop = asyncio.get_running_loop()
                # We're in an async context, create a task
                asyncio.create_task(self._handle_message(data=data))
                print("[EVENT MANAGER DEBUG] Created async task in running loop")
            except RuntimeError:
                # No running loop in this thread, need to run in a different way
                # Since we can't block here, we'll run it in a thread pool
                import threading

                def run_async():
                    try:
                        # Create a new event loop for this thread
                        new_loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(new_loop)
                        try:
                            new_loop.run_until_complete(self._handle_message(data=data))
                        finally:
                            new_loop.close()
                    except Exception as e:
                        print(f"[EVENT MANAGER DEBUG] Error in thread: {e}")
                        import traceback

                        traceback.print_exc()

                # Run in a separate thread to avoid blocking
                thread = threading.Thread(target=run_async, daemon=True)
                thread.start()
                print("[EVENT MANAGER DEBUG] Started thread for async execution")
        except Exception as e:
            print(f"[EVENT MANAGER DEBUG] Error in on_message: {e}")
            import traceback

            traceback.print_exc()

    def on_token(self, *, data: dict):
        """Handle token events - called directly by components."""
        # For now, we don't stream individual tokens

    def on_end(self, *, data: dict):
        """Handle end events - called directly by components."""
        import asyncio

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(self._handle_end(data=data))
            else:
                loop.run_until_complete(self._handle_end(data=data))
        except Exception as e:
            print(f"[EVENT MANAGER DEBUG] Error in on_end: {e}")

    def on_error(self, *, data: dict):
        """Handle error events - noop for now."""

    def on_vertices_sorted(self, *, data: dict):
        """Handle vertices sorted events - noop."""

    def on_remove_message(self, *, data: dict):
        """Handle remove message events - noop."""

    def on_end_vertex(self, *, data: dict):
        """Handle end vertex events - noop."""

    def on_build_start(self, *, data: dict):
        """Handle build start events - noop."""

    def on_log(self, *, data: dict):
        """Handle log events - noop."""

    def register_event(self, name: str, event_type: str, callback=None) -> None:
        """Register an event (compatibility method, not used for streaming)."""
        # This method exists for interface compatibility but isn't used
        # since we register events in __init__

    def send_event(self, *, event_type: str, data: Any):
        """Send an event (compatibility method, routes to appropriate handler)."""
        # Route to the appropriate handler based on event type
        handler_name = f"on_{event_type}" if not event_type.startswith("on_") else event_type
        handler = self.events.get(handler_name, self.noop)

        print(f"[EVENT MANAGER DEBUG] send_event called: event_type={event_type}, handler_name={handler_name}")
        print(f"[EVENT MANAGER DEBUG] handler={handler.__name__ if hasattr(handler, '__name__') else handler}")
        print(f"[EVENT MANAGER DEBUG] data keys={list(data.keys()) if isinstance(data, dict) else type(data)}")

        # Call the handler (note: handlers are async, but this is sync for compatibility)
        # We'll handle this by making the handlers work synchronously when needed
        try:
            import asyncio

            if asyncio.iscoroutinefunction(handler):
                # If we're in an async context, await it
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        # Create a task but don't wait for it
                        print(f"[EVENT MANAGER DEBUG] Creating async task for {handler_name}")
                        asyncio.create_task(handler(data=data))
                    else:
                        print(f"[EVENT MANAGER DEBUG] Running handler synchronously for {handler_name}")
                        loop.run_until_complete(handler(data=data))
                except RuntimeError as e:
                    # No event loop, skip
                    print(f"[EVENT MANAGER DEBUG] RuntimeError: {e}")
            else:
                print(f"[EVENT MANAGER DEBUG] Calling sync handler for {handler_name}")
                handler(data=data)
        except Exception as e:  # noqa: BLE001
            print(f"[EVENT MANAGER DEBUG] Exception in send_event: {e}")
            import traceback

            traceback.print_exc()

    def _normalize_progress_message(self, data: dict | str) -> str | None:
        """Convert child-side message/event payloads into a readable MCP progress message.

        This normalization is only used for MCP progress transport and does not modify
        the original child UI message content or storage behavior.
        """
        message_text = None
        if isinstance(data, dict):
            direct_text = data.get("text") or data.get("message")
            event_type = data.get("event")
            file_name = data.get("name")
            sender_name = data.get("sender_name") or data.get("sender")
            content_blocks = data.get("content_blocks", [])

            if isinstance(direct_text, str) and direct_text.strip():
                cleaned_text = direct_text.strip()
                if sender_name and sender_name not in {"Machine", "User"}:
                    message_text = f"{sender_name}: {cleaned_text}"
                else:
                    message_text = cleaned_text

            if not message_text and event_type in {"on_tool_start", "on_tool_end", "on_tool_error"}:
                event_data = data.get("data", {}) if isinstance(data.get("data"), dict) else {}
                tool_name = file_name or data.get("tool_name") or event_data.get("name")
                if event_type == "on_tool_start" and tool_name:
                    tool_input = event_data.get("input")
                    if tool_input not in (None, "", {}):
                        message_text = f"Tool: {tool_name}\nInput: {tool_input}"
                    else:
                        message_text = f"Tool: {tool_name}"
                elif event_type == "on_tool_end" and tool_name:
                    tool_output = event_data.get("output")
                    if tool_output not in (None, "", {}):
                        message_text = f"Tool: {tool_name}\nOutput: {tool_output}"
                    else:
                        message_text = f"Tool: {tool_name} completed"
                elif event_type == "on_tool_error" and tool_name:
                    tool_error = event_data.get("error")
                    if tool_error not in (None, ""):
                        message_text = f"Tool: {tool_name}\nError: {tool_error}"
                    else:
                        message_text = f"Tool: {tool_name} failed"
            if not message_text and content_blocks and isinstance(content_blocks, list):
                normalized_blocks: list[str] = []
                for block in content_blocks:
                    if not isinstance(block, dict):
                        continue

                    title = str(block.get("title", "")).strip()
                    contents = block.get("contents", [])
                    block_parts: list[str] = []

                    if title:
                        block_parts.append(f"[{title}]")

                    if isinstance(contents, list):
                        for content in contents:
                            if not isinstance(content, dict):
                                if content:
                                    block_parts.append(str(content))
                                continue

                            tool_name = str(content.get("name", "")).strip()
                            text_value = str(content.get("text", "")).strip()
                            tool_input = str(content.get("tool_input", "")).strip()
                            tool_output = str(content.get("tool_output", "")).strip()

                            if tool_name:
                                block_parts.append(f"Tool: {tool_name}")
                            if text_value:
                                block_parts.append(text_value)
                            if tool_input:
                                block_parts.append(f"Input: {tool_input}")
                            if tool_output:
                                block_parts.append(f"Output: {tool_output}")

                    normalized_block = "\n".join(part for part in block_parts if part).strip()
                    if normalized_block:
                        normalized_blocks.append(normalized_block)

                if normalized_blocks:
                    message_text = "\n\n".join(normalized_blocks)
                    print(f"[EVENT MANAGER DEBUG] Extracted from content_blocks: {message_text}")

        elif isinstance(data, str):
            stripped = data.strip()
            if stripped:
                message_text = stripped

        return message_text or None

    async def _handle_message(self, *, data: dict):
        """Handle message events by sending them via progress notifications.

        Args:
            data: Message data containing 'text', 'message', or 'content_blocks' field
        """
        try:
            message_text = self._normalize_progress_message(data)

            if message_text:
                self.message_count += 1
                # Increment progress slightly with each message (cap at 0.95)
                self.last_progress = min(0.95, self.last_progress + 0.05)

                print(f"[EVENT MANAGER DEBUG] Sending progress notification: {message_text}")
                await self.session.send_progress_notification(
                    progress_token=self.progress_token,
                    progress=self.last_progress,
                    total=1.0,
                    message=str(message_text),
                    related_request_id=self.request_id,
                )
                await logger.adebug(f"Sent progress notification with message: {message_text[:50]}...")
            else:
                print(
                    f"[EVENT MANAGER DEBUG] No message text found in data keys: {list(data.keys()) if isinstance(data, dict) else type(data)}"
                )
        except Exception as e:
            await logger.awarning(f"Error sending progress notification for message: {e}")
            import traceback

            traceback.print_exc()

    async def _handle_token(self, *, data: dict):
        """Handle token events (for future token-level streaming).

        Args:
            data: Token data
        """
        # For now, we don't stream individual tokens to avoid flooding
        # This could be implemented in the future with rate limiting

    async def _handle_end(self, *, data: dict):
        """Handle end event by sending final progress notification.

        Args:
            data: End event data
        """
        try:
            await self.session.send_progress_notification(
                progress_token=self.progress_token,
                progress=1.0,
                total=1.0,
                message="Flow execution complete",
                related_request_id=self.request_id,
            )
            await logger.adebug("Sent final progress notification")
        except Exception as e:
            await logger.awarning(f"Error sending final progress notification: {e}")

    def noop(self, *, data: Any) -> None:
        """No-op handler for events we don't process."""

    def __getattr__(self, name: str):
        """Get event handler by name, returning noop if not found."""
        return self.events.get(name, self.noop)


async def handle_list_resources(project_id=None):
    """Handle listing resources for MCP.

    Args:
        project_id: Optional project ID to filter resources by project
    """
    resources = []
    try:
        storage_service = get_storage_service()
        settings_service = get_settings_service()

        # Build full URL from settings
        host = getattr(settings_service.settings, "host", "localhost")
        port = getattr(settings_service.settings, "port", 3000)

        base_url = f"http://{host}:{port}".rstrip("/")
        try:
            current_user = current_user_ctx.get()
        except Exception as e:  # noqa: BLE001
            msg = f"Error getting current user: {e!s}"
            await logger.aexception(msg)
            current_user = None
        async with session_scope() as session:
            # Build query based on whether project_id is provided
            flows_query = select(Flow).where(Flow.folder_id == project_id) if project_id else select(Flow)

            flows = (await session.exec(flows_query)).all()

            for flow in flows:
                if flow.id:
                    try:
                        files = await storage_service.list_files(flow_id=str(flow.id))
                        for file_name in files:
                            # URL encode the filename
                            safe_filename = quote(file_name)
                            resource = types.Resource(
                                uri=f"{base_url}/api/v1/files/download/{flow.id}/{safe_filename}",
                                name=file_name,
                                description=f"File in flow: {flow.name}",
                                mimeType=build_content_type_from_extension(file_name),
                            )
                            resources.append(resource)
                    except FileNotFoundError as e:
                        msg = f"Error listing files for flow {flow.id}: {e}"
                        await logger.adebug(msg)
                        continue
            ####################################################
            # When a user uploads a file inside a flow
            # (e.g., via the File Read component),
            # it hits /api/v2/files (POST),
            # which saves files at the user-level.
            # So the above query for flow files is not enough.
            # So we list all user files for the current user.
            # This is not good. We need to fix this for 1.8.0.
            ###################################################
            if current_user:
                user_files_stmt = select(UserFile).where(UserFile.user_id == current_user.id)
                user_files = (await session.exec(user_files_stmt)).all()
                for user_file in user_files:
                    stored_path = getattr(user_file, "path", "") or ""
                    stored_filename = Path(stored_path).name if stored_path else user_file.name
                    safe_filename = quote(stored_filename)
                    if stored_filename.startswith(f"{MCP_SERVERS_FILE}_{current_user.id}"):
                        # reserved file name for langflow MCP server config file(s)
                        continue
                    description = getattr(user_file, "provider", None) or "User file uploaded via File Manager"
                    resource = types.Resource(
                        uri=f"{base_url}/api/v1/files/download/{current_user.id}/{safe_filename}",
                        name=stored_filename,
                        description=description,
                        mimeType=build_content_type_from_extension(stored_filename),
                    )
                    resources.append(resource)
    except Exception as e:
        msg = f"Error in listing resources: {e!s}"
        await logger.aexception(msg)
        raise
    return resources


async def handle_read_resource(uri: str) -> bytes:
    """Handle resource read requests."""
    try:
        # Parse the URI properly
        parsed_uri = urlparse(str(uri))
        # Path will be like /api/v1/files/download/{namespace}/{filename}
        path_parts = parsed_uri.path.split("/")
        # Remove empty strings from split
        path_parts = [p for p in path_parts if p]

        # The flow_id and filename should be the last two parts
        two = 2
        if len(path_parts) < two:
            msg = f"Invalid URI format: {uri}"
            raise ValueError(msg)

        flow_id = path_parts[-2]
        filename = unquote(path_parts[-1])  # URL decode the filename

        storage_service = get_storage_service()

        # Read the file content
        content = await storage_service.get_file(flow_id=flow_id, file_name=filename)
        if not content:
            msg = f"File {filename} not found in flow {flow_id}"
            raise ValueError(msg)

        # Ensure content is base64 encoded
        if isinstance(content, str):
            content = content.encode()
        return base64.b64encode(content)
    except Exception as e:
        msg = f"Error reading resource {uri}: {e!s}"
        await logger.aexception(msg)
        raise


async def handle_call_tool(
    name: str, arguments: dict, server, project_id=None, *, is_action=False, progress_token=None, request_context=None
) -> list[types.TextContent]:
    """Handle tool execution requests.

    Args:
        name: Tool name
        arguments: Tool arguments
        server: MCP server instance
        project_id: Optional project ID to filter flows by project
        is_action: Whether to use action name for flow lookup
        progress_token: Optional progress token passed from the handler
        request_context: ServerRequestContext from the new SDK (replaces server.request_context)
    """
    mcp_config = get_mcp_config()
    if mcp_config.enable_progress_notifications is None:
        settings_service = get_settings_service()
        mcp_config.enable_progress_notifications = settings_service.settings.mcp_server_enable_progress_notifications
        mcp_config.stream_messages_in_progress = settings_service.settings.mcp_server_stream_messages_in_progress

    current_user = current_user_ctx.get()
    # Build execution context with request-level variables if present
    request_variables = current_request_variables_ctx.get()
    exec_context = {"request_variables": request_variables} if request_variables else None

    async def execute_tool(session):
        # Get flow id from name
        flow = await get_flow_snake_case(name, current_user.id, session, is_action=is_action)
        if not flow:
            msg = f"Flow with name '{name}' not found"
            raise ValueError(msg)

        # If project_id is provided, verify the flow belongs to the project
        if project_id and flow.folder_id != project_id:
            msg = f"Flow '{name}' not found in project {project_id}"
            raise ValueError(msg)

        # Process inputs
        processed_inputs = dict(arguments)
        # Remove _meta from processed inputs as it's not a tool argument
        processed_inputs.pop("_meta", None)

        # Use progress_token passed from handler (extracted from request_context.meta)
        # If not provided, try to extract from arguments as fallback
        token = progress_token
        print(f"[TOKEN EXTRACTION DEBUG] Initial progress_token parameter: {token}")
        if token is None:
            if "_meta" in arguments and isinstance(arguments["_meta"], dict):
                # Try both camelCase (JSON) and snake_case (Python) keys
                token = arguments["_meta"].get("progressToken") or arguments["_meta"].get("progress_token")
                print(f"[TOKEN EXTRACTION DEBUG] Extracted from arguments._meta: {token}")
            elif request_context and request_context.meta is not None:
                # ctx.meta is a dict, not an object - use dict access
                # MCP SDK converts progressToken (JSON) to progress_token (Python dict key)
                print(f"[TOKEN EXTRACTION DEBUG] request_context.meta type: {type(request_context.meta)}")
                print(f"[TOKEN EXTRACTION DEBUG] request_context.meta: {request_context.meta}")
                if isinstance(request_context.meta, dict):
                    token = request_context.meta.get("progress_token") or request_context.meta.get("progressToken")
                    print(f"[TOKEN EXTRACTION DEBUG] Extracted from request_context.meta (dict): {token}")
                else:
                    token = getattr(request_context.meta, "progress_token", None) or getattr(request_context.meta, "progressToken", None)
                    print(f"[TOKEN EXTRACTION DEBUG] Extracted from request_context.meta (object): {token}")

        print(f"[TOKEN EXTRACTION DEBUG] Final token value: {token}")

        # Initial progress notification
        if mcp_config.enable_progress_notifications and token is not None and request_context:
            print(
                "[MCP SERVER DEBUG] About to send initial progress notification: "
                f"token={token!r}, request_id={request_context.request_id!r}, "
                f"session_type={type(request_context.session).__name__!r}"
            )
            await request_context.session.send_progress_notification(
                progress_token=token, progress=0.0, total=1.0, related_request_id=request_context.request_id
            )
            print(
                "[MCP SERVER DEBUG] Initial progress notification send completed: "
                f"token={token!r}, request_id={request_context.request_id!r}"
            )

        conversation_id = str(uuid4())
        input_request = SimplifiedAPIRequest(
            input_value=processed_inputs.get("input_value", ""), session_id=conversation_id
        )

        async def send_progress_updates(prog_token):
            try:
                progress = 0.0
                while True:
                    print(
                        "[MCP SERVER DEBUG] Background progress task sending notification: "
                        f"token={prog_token!r}, progress={min(0.9, progress)!r}, "
                        f"request_id={request_context.request_id!r}"
                    )
                    await request_context.session.send_progress_notification(
                        progress_token=prog_token,
                        progress=min(0.9, progress),
                        total=1.0,
                        related_request_id=request_context.request_id,
                    )
                    print(
                        "[MCP SERVER DEBUG] Background progress task send completed: "
                        f"token={prog_token!r}, progress={min(0.9, progress)!r}, "
                        f"request_id={request_context.request_id!r}"
                    )
                    progress += 0.1
                    await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                if mcp_config.enable_progress_notifications:
                    print(
                        "[MCP SERVER DEBUG] Background progress task sending final notification on cancel: "
                        f"token={prog_token!r}, request_id={request_context.request_id!r}"
                    )
                    await request_context.session.send_progress_notification(
                        progress_token=prog_token,
                        progress=1.0,
                        total=1.0,
                        related_request_id=request_context.request_id,
                    )
                    print(
                        "[MCP SERVER DEBUG] Background progress task final notification completed on cancel: "
                        f"token={prog_token!r}, request_id={request_context.request_id!r}"
                    )
                raise

        collected_results = []
        try:
            # Debug logging - dump everything
            import json

            print(f"[MCP SERVER DEBUG] enable_progress_notifications={mcp_config.enable_progress_notifications}")
            print(f"[MCP SERVER DEBUG] stream_messages_in_progress={mcp_config.stream_messages_in_progress}")
            print("[MCP SERVER DEBUG] Full arguments object:")
            print(json.dumps(arguments, indent=2, default=str))
            print(f"[MCP SERVER DEBUG] token={token}")
            print("[MCP SERVER DEBUG] Full request_context:")
            if request_context:
                try:
                    # Try to serialize the entire request_context
                    context_dict = (
                        vars(request_context) if hasattr(request_context, "__dict__") else str(request_context)
                    )
                    print(json.dumps(context_dict, indent=2, default=str))
                except Exception as e:
                    print(f"  Could not serialize request_context: {e}")
                    print(f"  request_context type: {type(request_context)}")
                    print(f"  request_context: {request_context}")
            else:
                print("  No request_context available")

            # Check if we should stream messages via progress notifications
            stream_messages = (
                mcp_config.enable_progress_notifications
                and mcp_config.stream_messages_in_progress
                and token is not None
            )

            print(f"[MCP SERVER DEBUG] stream_messages={stream_messages}")

            # Create event manager if streaming messages
            event_manager = None
            if stream_messages and token is not None:
                print("[MCP SERVER DEBUG] Creating MessageStreamingEventManager...")
                event_manager = MessageStreamingEventManager(
                    progress_token=token,
                    session=request_context.session,
                    request_id=request_context.request_id,
                )
                print("[MCP SERVER DEBUG] MessageStreamingEventManager created successfully")
                await logger.adebug("Created MessageStreamingEventManager for progress notifications")

            progress_task = None
            # Only start simulated progress if NOT streaming messages
            if mcp_config.enable_progress_notifications and token is not None:
                if not stream_messages:
                    progress_task = asyncio.create_task(send_progress_updates(token))

            try:
                try:
                    print(
                        "[MCP SERVER DEBUG] About to call simple_run_flow: "
                        f"flow_name={flow.name!r}, stream={stream_messages!r}, "
                        f"event_manager_type={type(event_manager).__name__ if event_manager else None}, "
                        f"conversation_id={conversation_id!r}"
                    )
                    result = await simple_run_flow(
                        flow=flow,
                        input_request=input_request,
                        stream=stream_messages,
                        api_key_user=current_user,
                        context=exec_context,
                        event_manager=event_manager,
                    )
                    print(
                        "[MCP SERVER DEBUG] simple_run_flow returned successfully: "
                        f"outputs_count={len(result.outputs)!r}"
                    )
                    # Process all outputs and messages, ensuring no duplicates
                    processed_texts = set()

                    def add_result(text: str):
                        if text not in processed_texts:
                            processed_texts.add(text)
                            print(
                                "[MCP SERVER DEBUG] Adding collected result text: "
                                f"preview={text[:200]!r}"
                            )
                            collected_results.append(types.TextContent(type="text", text=text))

                    for run_output in result.outputs:
                        for component_output in run_output.outputs:
                            # Handle messages
                            for msg in component_output.messages or []:
                                add_result(msg.message)
                            # Handle results
                            for value in (component_output.results or {}).values():
                                if isinstance(value, Message):
                                    add_result(value.get_text())
                                else:
                                    add_result(str(value))
                except Exception as e:  # noqa: BLE001
                    error_msg = f"Error Executing the {flow.name} tool. Error: {e!s}"
                    collected_results.append(types.TextContent(type="text", text=error_msg))

                return collected_results
            finally:
                if progress_task:
                    print("[MCP SERVER DEBUG] Cancelling background progress task")
                    progress_task.cancel()
                    await asyncio.gather(progress_task, return_exceptions=True)
                    print("[MCP SERVER DEBUG] Background progress task cancellation complete")

        except Exception:
            if (
                mcp_config.enable_progress_notifications
                and request_context
                and request_context.meta is not None
                and (error_token := (request_context.meta.get("progressToken") if isinstance(request_context.meta, dict) else getattr(request_context.meta, "progressToken", None)))
            ):
                await request_context.session.send_progress_notification(
                    progress_token=error_token, progress=1.0, total=1.0
                )
            raise

    try:
        return await with_db_session(execute_tool)
    except Exception as e:
        msg = f"Error executing tool {name}: {e!s}"
        await logger.aexception(msg)
        raise


async def handle_list_tools(project_id=None, *, mcp_enabled_only=False):
    """Handle listing tools for MCP.

    Args:
        project_id: Optional project ID to filter tools by project
        mcp_enabled_only: Whether to filter for MCP-enabled flows only
    """
    tools = []
    try:
        async with session_scope() as session:
            # Build query based on parameters
            if project_id:
                # Filter flows by project and optionally by MCP enabled status
                flows_query = select(Flow).where(Flow.folder_id == project_id, Flow.is_component == False)  # noqa: E712
                if mcp_enabled_only:
                    flows_query = flows_query.where(Flow.mcp_enabled == True)  # noqa: E712
            else:
                # Get all flows
                flows_query = select(Flow)

            flows = (await session.exec(flows_query)).all()

            existing_names = set()
            for flow in flows:
                if flow.user_id is None:
                    continue

                # For project-specific tools, use action names if available
                if project_id:
                    base_name = (
                        sanitize_mcp_name(flow.action_name) if flow.action_name else sanitize_mcp_name(flow.name)
                    )
                    name = get_unique_name(base_name, MAX_MCP_TOOL_NAME_LENGTH, existing_names)
                    description = flow.action_description or (
                        flow.description if flow.description else f"Tool generated from flow: {name}"
                    )
                else:
                    # For global tools, use simple sanitized names
                    base_name = sanitize_mcp_name(flow.name)
                    name = base_name[:MAX_MCP_TOOL_NAME_LENGTH]
                    if name in existing_names:
                        i = 1
                        while True:
                            suffix = f"_{i}"
                            truncated_base = base_name[: MAX_MCP_TOOL_NAME_LENGTH - len(suffix)]
                            candidate = f"{truncated_base}{suffix}"
                            if candidate not in existing_names:
                                name = candidate
                                break
                            i += 1
                    description = (
                        f"{flow.id}: {flow.description}" if flow.description else f"Tool generated from flow: {name}"
                    )

                try:
                    tool = types.Tool(
                        name=name,
                        description=description,
                        inputSchema=json_schema_from_flow(flow),
                    )
                    tools.append(tool)
                    existing_names.add(name)
                except Exception as e:  # noqa: BLE001
                    msg = f"Error in listing tools: {e!s} from flow: {base_name}"
                    await logger.awarning(msg)
                    continue
    except Exception as e:
        msg = f"Error in listing tools: {e!s}"
        await logger.aexception(msg)
        raise
    return tools
