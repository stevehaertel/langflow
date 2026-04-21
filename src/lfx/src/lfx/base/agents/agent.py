import re
import uuid
from abc import abstractmethod
from typing import TYPE_CHECKING, cast

from langchain.agents import AgentExecutor, BaseMultiActionAgent, BaseSingleActionAgent
from langchain.agents.agent import RunnableAgent
from langchain.callbacks.base import BaseCallbackHandler
from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.runnables import Runnable

from lfx.base.agents.callback import AgentAsyncHandler
from lfx.base.agents.events import ExceptionWithMessageError, process_agent_events
from lfx.base.agents.utils import get_chat_output_sender_name
from lfx.custom.custom_component.component import Component, _get_component_toolkit
from lfx.field_typing import Tool
from lfx.inputs.inputs import InputTypes, MultilineInput
from lfx.io import BoolInput, HandleInput, IntInput, MessageInput
from lfx.log.logger import logger
from lfx.memory import delete_message
from lfx.schema.content_block import ContentBlock
from lfx.schema.data import Data
from lfx.schema.log import OnTokenFunctionType
from lfx.schema.message import Message
from lfx.template.field.base import Output
from lfx.utils.constants import MESSAGE_SENDER_AI

if TYPE_CHECKING:
    from lfx.schema.log import OnTokenFunctionType, SendMessageFunctionType


DEFAULT_TOOLS_DESCRIPTION = "A helpful assistant with access to the following tools:"
DEFAULT_AGENT_NAME = "Agent ({tools_names})"


def _detect_message_type(message_text: str) -> str:
    """Detect message type from content.

    This is Langflow-specific logic, not part of MCP spec.
    We analyze the message content that came from MCP notification params.

    Message Content Examples (From MCP params.message):
    1. "What is the metadata of my data product with id ABC?" → tool_progress
    2. "[Agent Steps]" → agent_steps
    3. "[Agent Steps]\nWhat is the metadata..." → agent_steps
    4. "AI: Here's the metadata..." → final_answer
    5. "Tool: get_metadata\nInput: {...}" → tool_invocation
    6. "[Agent Steps]\n...\nTool: get_metadata\nInput: {...}" → tool_invocation (priority)

    Args:
        message_text: The message text from MCP notification params

    Returns:
        Message type: "agent_steps" | "final_answer" | "tool_invocation" | "tool_progress"
    """
    # Check for tool invocation FIRST (higher priority than agent_steps)
    # This ensures tool invocations get their own message block with hammer emoji
    if "Tool:" in message_text and "Input:" in message_text:
        return "tool_invocation"
    elif message_text.startswith("AI:"):
        return "final_answer"
    elif "[Agent Steps]" in message_text:
        return "agent_steps"
    else:
        return "tool_progress"


def _format_progress_message(message_text: str, message_type: str) -> str:
    """Format progress message based on type.

    This is Langflow-specific formatting logic.

    Args:
        message_text: The original message text
        message_type: The detected message type

    Returns:
        Formatted message text with appropriate emoji and structure
    """
    if message_type == "agent_steps":
        # Remove redundant [Agent Steps] prefix, add emoji
        text = message_text.replace("[Agent Steps]", "").strip()
        return f"🔄 Agent Steps\n{text}" if text else "🔄 Agent Steps"
    elif message_type == "tool_invocation":
        # Remove [Agent Steps] prefix if present, add hammer emoji
        text = message_text.replace("[Agent Steps]", "").strip()
        return f"🔨 Tool Invocation\n{text}"
    elif message_type == "final_answer":
        return f"✅ {message_text}"
    else:
        return f"📊 {message_text}"


class LCAgentComponent(Component):
    trace_type = "agent"

    def __init__(self, *args, **kwargs):
        """Initialize the agent component with progress message tracking."""
        super().__init__(*args, **kwargs)
        # Track message IDs by type for smart updates (replace mode)
        self._progress_message_ids: dict[str, str] = {}
        # Track active tool calls for nested notifications (Phase 2)
        self._active_tool_calls: dict[str, dict] = {}
        # Store reference to current parent message for nesting
        self._current_parent_message: Message | None = None
        # Feature flag for nested notifications - reuse existing MCP streaming flag
        import os
        self._enable_nested_notifications = os.getenv("LANGFLOW_MCP_SERVER_STREAM_MESSAGES_IN_PROGRESS", "false").lower() == "true"

    _base_inputs: list[InputTypes] = [
        MessageInput(
            name="input_value",
            display_name="Input",
            info="The input provided by the user for the agent to process.",
            tool_mode=True,
        ),
        BoolInput(
            name="handle_parsing_errors",
            display_name="Handle Parse Errors",
            value=True,
            advanced=True,
            info="Should the Agent fix errors when reading user input for better processing?",
        ),
        BoolInput(name="verbose", display_name="Verbose", value=True, advanced=True),
        IntInput(
            name="max_iterations",
            display_name="Max Iterations",
            value=15,
            advanced=True,
            info="The maximum number of attempts the agent can make to complete its task before it stops.",
        ),
        MultilineInput(
            name="agent_description",
            display_name="Agent Description [Deprecated]",
            info=(
                "The description of the agent. This is only used when in Tool Mode. "
                f"Defaults to '{DEFAULT_TOOLS_DESCRIPTION}' and tools are added dynamically. "
                "This feature is deprecated and will be removed in future versions."
            ),
            advanced=True,
            value=DEFAULT_TOOLS_DESCRIPTION,
        ),
    ]

    outputs = [
        Output(display_name="Response", name="response", method="message_response"),
        Output(display_name="Agent", name="agent", method="build_agent", tool_mode=False),
    ]

    # Get shared callbacks for tracing and save them to self.shared_callbacks
    def _get_shared_callbacks(self) -> list[BaseCallbackHandler]:
        if not hasattr(self, "shared_callbacks"):
            self.shared_callbacks = self.get_langchain_callbacks()
        return self.shared_callbacks

    async def _find_parent_tool_call_message(self, tool_call_id: str) -> Message | None:
        """Find the parent message that contains the specified tool call.

        Phase 2.2: Parent Message Lookup

        Args:
            tool_call_id: The unique ID of the tool call to find

        Returns:
            The parent Message containing the tool call, or None if not found
        """
        if not tool_call_id or tool_call_id not in self._active_tool_calls:
            await logger.adebug(f"[NESTED NOTIFICATIONS] Tool call ID {tool_call_id} not found in active calls")
            return None

        tool_call_info = self._active_tool_calls[tool_call_id]
        parent_message_id = tool_call_info.get("parent_message_id")

        if not parent_message_id:
            await logger.adebug(f"[NESTED NOTIFICATIONS] No parent message ID for tool call {tool_call_id}")
            return None

        # Return the stored parent message reference
        if self._current_parent_message and str(self._current_parent_message.get_id()) == str(parent_message_id):
            await logger.adebug(f"[NESTED NOTIFICATIONS] Found parent message {parent_message_id} for tool call {tool_call_id}")
            return self._current_parent_message

        await logger.adebug(f"[NESTED NOTIFICATIONS] Parent message {parent_message_id} not in current reference")
        return None

    async def _add_child_notification_to_parent(
        self,
        parent_message: Message,
        notification_type: str,
        notification_text: str,
        tool_call_id: str
    ) -> Message:
        """Add child notification as nested ContentBlock in parent message.

        Phase 2.3: Nested Block Creation Logic

        Args:
            parent_message: The parent message to add the notification to
            notification_type: Type of notification (agent_steps, tool_invocation, final_answer)
            notification_text: The notification text content
            tool_call_id: The tool call this notification relates to

        Returns:
            The updated parent message
        """
        from lfx.schema.content_block import ContentBlock, TextContent

        # Find the "Agent Steps" content block (should be the first one)
        if not parent_message.content_blocks:
            await logger.awarning("[NESTED NOTIFICATIONS] Parent message has no content blocks")
            return parent_message

        agent_steps_block = parent_message.content_blocks[0]

        # Initialize nested_blocks if needed
        if not hasattr(agent_steps_block, 'nested_blocks') or agent_steps_block.nested_blocks is None:
            agent_steps_block.nested_blocks = []

        # Check if a block with this notification_type already exists
        block_type = f"child_{notification_type}"
        existing_block = None
        existing_index = None

        for i, block in enumerate(agent_steps_block.nested_blocks):
            if hasattr(block, 'block_type') and block.block_type == block_type:
                existing_block = block
                existing_index = i
                break

        if existing_block:
            # Update existing block instead of creating duplicate
            existing_block.contents = [TextContent(type="text", text=notification_text)]
            await logger.adebug(
                f"[NESTED NOTIFICATIONS] Updated existing nested block: type={notification_type}, "
                f"title={existing_block.title}, index={existing_index}"
            )
        else:
            # Create new nested block for this notification
            nested_block = ContentBlock(
                title=self._get_notification_title(notification_type),
                contents=[TextContent(type="text", text=notification_text)],
                block_type=block_type,
                is_expandable=True,
                is_expanded=False,
                nesting_depth=1
            )
            agent_steps_block.nested_blocks.append(nested_block)
            await logger.adebug(
                f"[NESTED NOTIFICATIONS] Created new nested block: type={notification_type}, "
                f"title={nested_block.title}"
            )

        # Mark nested_blocks as explicitly set so it gets serialized
        if hasattr(agent_steps_block, 'model_fields_set'):
            agent_steps_block.model_fields_set.add('nested_blocks')

        await logger.adebug(
            f"[NESTED NOTIFICATIONS] Processed nested block: type={notification_type}, "
            f"parent_msg_id={parent_message.get_id()}, "
            f"total_nested_blocks_now={len(agent_steps_block.nested_blocks)}"
        )

        # Log the content blocks structure before saving
        await logger.adebug(
            f"[NESTED NOTIFICATIONS] Before save - content_blocks count: {len(parent_message.content_blocks)}, "
            f"first_block_nested_count: {len(parent_message.content_blocks[0].nested_blocks) if parent_message.content_blocks else 0}"
        )

        # CRITICAL FIX: Save to database AND send SSE update
        # Use the send_message_callback if available (saves to DB), otherwise fall back to send_message (SSE only)
        try:
            if hasattr(self, '_send_message_callback') and self._send_message_callback:
                # This saves to database AND sends SSE
                saved_message = await self._send_message_callback(parent_message)
                await logger.adebug(
                    f"[NESTED NOTIFICATIONS] Saved to DB and sent SSE: "
                    f"parent_id={saved_message.get_id()}, total_nested={len(agent_steps_block.nested_blocks)}"
                )
                return saved_message
            elif hasattr(self, 'send_message'):
                # Fallback: SSE only (won't persist to DB)
                await self.send_message(parent_message)
                await logger.awarning(
                    f"[NESTED NOTIFICATIONS] Only sent SSE (no DB save): "
                    f"parent_id={parent_message.get_id()}, total_nested={len(agent_steps_block.nested_blocks)}"
                )
        except Exception as e:
            await logger.awarning(
                f"[NESTED NOTIFICATIONS] Failed to save/send updated parent message: {e}"
            )

        return parent_message

    def _get_notification_title(self, notification_type: str) -> str:
        """Get display title for notification type."""
        titles = {
            "agent_steps": "🔄 Agent Steps",
            "tool_invocation": "🔨 Tool Invocation",
            "final_answer": "✅ Final Answer",
            "tool_progress": "📊 Tool Progress"
        }
        return titles.get(notification_type, f"📋 {notification_type}")

    @abstractmethod
    def build_agent(self) -> AgentExecutor:
        """Create the agent."""

    async def message_response(self) -> Message:
        """Run the agent and return the response."""
        agent = self.build_agent()
        message = await self.run_agent(agent=agent)

        self.status = message
        return message

    def _validate_outputs(self) -> None:
        required_output_methods = ["build_agent"]
        output_names = [output.name for output in self.outputs]
        for method_name in required_output_methods:
            if method_name not in output_names:
                msg = f"Output with name '{method_name}' must be defined."
                raise ValueError(msg)
            if not hasattr(self, method_name):
                msg = f"Method '{method_name}' must be defined."
                raise ValueError(msg)

    def get_agent_kwargs(self, *, flatten: bool = False) -> dict:
        base = {
            "handle_parsing_errors": self.handle_parsing_errors,
            "verbose": self.verbose,
            "allow_dangerous_code": True,
        }
        agent_kwargs = {
            "handle_parsing_errors": self.handle_parsing_errors,
            "max_iterations": self.max_iterations,
        }
        if flatten:
            return {
                **base,
                **agent_kwargs,
            }
        return {**base, "agent_executor_kwargs": agent_kwargs}

    def get_chat_history_data(self) -> list[Data] | None:
        # might be overridden in subclasses
        return None

    def _data_to_messages_skip_empty(self, data: list[Data]) -> list[BaseMessage]:
        """Convert data to messages, filtering only empty text while preserving non-text content.

        Note: added to fix issue with certain providers failing when given empty text as input.
        """
        messages = []
        for value in data:
            # Only skip if the message has a text attribute that is empty/whitespace
            text = getattr(value, "text", None)
            if isinstance(text, str) and not text.strip():
                # Skip only messages with empty/whitespace-only text strings
                continue

            lc_message = value.to_lc_message()
            messages.append(lc_message)

        return messages

    async def run_agent(
        self,
        agent: Runnable | BaseSingleActionAgent | BaseMultiActionAgent | AgentExecutor,
    ) -> Message:
        # Inject progress callback into MCP tools before running the agent
        await self._inject_progress_callback_into_tools()

        # Check if this agent is being called as a tool by a parent agent
        # If so, we'll forward our steps as progress notifications
        parent_progress_callback = getattr(self, '_agent_progress_callback', None)

        if isinstance(agent, AgentExecutor):
            runnable = agent
        else:
            # note the tools are not required to run the agent, hence the validation removed.
            handle_parsing_errors = hasattr(self, "handle_parsing_errors") and self.handle_parsing_errors
            verbose = hasattr(self, "verbose") and self.verbose
            max_iterations = hasattr(self, "max_iterations") and self.max_iterations
            await logger.adebug(
                "[AGENT MCP DEBUG] constructing AgentExecutor.from_agent_and_tools: "
                f"tool_names={[getattr(tool, 'name', type(tool).__name__) for tool in (self.tools or [])]!r}, "
                f"tool_callbacks={[getattr(tool, 'callbacks', None) for tool in (self.tools or [])]!r}, "
                f"handle_parsing_errors={handle_parsing_errors!r}, verbose={verbose!r}, "
                f"max_iterations={max_iterations!r}"
            )
            try:
                runnable = AgentExecutor.from_agent_and_tools(
                    agent=agent,
                    tools=self.tools or [],
                    handle_parsing_errors=handle_parsing_errors,
                    verbose=verbose,
                    max_iterations=max_iterations,
                )
            except Exception as e:
                import traceback
                await logger.aerror(f"[AGENT EXECUTOR ERROR] Exception during AgentExecutor.from_agent_and_tools: {e}")
                await logger.aerror(f"[AGENT EXECUTOR ERROR] Full traceback:\n{traceback.format_exc()}")
                raise
        # Convert input_value to proper format for agent
        lc_message = None
        if isinstance(self.input_value, Message):
            lc_message = self.input_value.to_lc_message()
            # Extract text content from the LangChain message for agent input
            # Agents expect a string input, not a Message object
            if hasattr(lc_message, "content"):
                if isinstance(lc_message.content, str):
                    input_dict: dict[str, str | list[BaseMessage] | BaseMessage] = {"input": lc_message.content}
                elif isinstance(lc_message.content, list):
                    # For multimodal content, extract text parts
                    text_parts = [item.get("text", "") for item in lc_message.content if item.get("type") == "text"]
                    input_dict = {"input": " ".join(text_parts) if text_parts else ""}
                else:
                    input_dict = {"input": str(lc_message.content)}
            else:
                input_dict = {"input": str(lc_message)}
        else:
            input_dict = {"input": self.input_value}

        # Ensure input_dict is initialized
        if "input" not in input_dict:
            input_dict = {"input": self.input_value}

        # Use enhanced prompt if available (set by IBM Granite handler), otherwise use original
        system_prompt_to_use = getattr(self, "_effective_system_prompt", None) or getattr(self, "system_prompt", None)
        if system_prompt_to_use and system_prompt_to_use.strip():
            input_dict["system_prompt"] = system_prompt_to_use

        if hasattr(self, "chat_history") and self.chat_history:
            if isinstance(self.chat_history, Data):
                input_dict["chat_history"] = self._data_to_messages_skip_empty([self.chat_history])
            elif all(hasattr(m, "to_data") and callable(m.to_data) and "text" in m.data for m in self.chat_history):
                input_dict["chat_history"] = self._data_to_messages_skip_empty(self.chat_history)
            elif all(isinstance(m, Message) for m in self.chat_history):
                input_dict["chat_history"] = self._data_to_messages_skip_empty([m.to_data() for m in self.chat_history])

        # Handle multimodal input (images + text)
        # Note: Agent input must be a string, so we extract text and move images to chat_history
        if lc_message is not None and hasattr(lc_message, "content") and isinstance(lc_message.content, list):
            # Extract images and text from the text content items
            # Support both "image" (legacy) and "image_url" (standard) types
            image_dicts = [item for item in lc_message.content if item.get("type") in ("image", "image_url")]
            text_content = [item for item in lc_message.content if item.get("type") not in ("image", "image_url")]

            text_strings = [
                item.get("text", "")
                for item in text_content
                if item.get("type") == "text" and item.get("text", "").strip()
            ]

            # Set input to concatenated text or empty string
            input_dict["input"] = " ".join(text_strings) if text_strings else ""

            # If input is still a list or empty, provide a default
            if isinstance(input_dict["input"], list) or not input_dict["input"]:
                input_dict["input"] = "Process the provided images."

            if "chat_history" not in input_dict:
                input_dict["chat_history"] = []

            if isinstance(input_dict["chat_history"], list):
                input_dict["chat_history"].extend(HumanMessage(content=[image_dict]) for image_dict in image_dicts)
            else:
                input_dict["chat_history"] = [HumanMessage(content=[image_dict]) for image_dict in image_dicts]

        # Final safety check: ensure input is never empty (prevents Anthropic API errors)
        current_input = input_dict.get("input", "")
        if isinstance(current_input, list):
            current_input = " ".join(map(str, current_input))
        elif not isinstance(current_input, str):
            current_input = str(current_input)

        if not current_input.strip():
            input_dict["input"] = "Continue the conversation."
        else:
            input_dict["input"] = current_input

        if hasattr(self, "graph"):
            session_id = self.graph.session_id
        elif hasattr(self, "_session_id"):
            session_id = self._session_id
        else:
            session_id = None

        sender_name = get_chat_output_sender_name(self) or self.display_name or "AI"
        agent_message = Message(
            sender=MESSAGE_SENDER_AI,
            sender_name=sender_name,
            properties={"icon": "Bot", "state": "partial"},
            content_blocks=[ContentBlock(title="Agent Steps", contents=[])],
            session_id=session_id or uuid.uuid4(),
        )

        # Phase 2.1: Store parent message reference for nested notifications
        # This will be used to route child notifications to nested blocks
        self._current_parent_message = agent_message

        # Phase 2.1: Generate tool call ID for tracking
        # This will be used to associate child notifications with this tool invocation
        current_tool_call_id = str(uuid.uuid4())
        self._active_tool_calls[current_tool_call_id] = {
            "parent_message_id": None,  # Will be set after message is saved
            "tool_name": "agent_execution",
            "started_at": None
        }
        # Store for use in progress callback
        self._current_tool_call_id = current_tool_call_id

        # Create token callback if event_manager is available
        # This wraps the event_manager's on_token method to match OnTokenFunctionType Protocol
        on_token_callback: OnTokenFunctionType | None = None
        if self._event_manager:
            on_token_callback = cast("OnTokenFunctionType", self._event_manager.on_token)

        # Create a wrapper for send_message that also forwards agent steps as progress notifications
        # if this agent is being called as a tool by a parent agent
        send_message_callback = cast("SendMessageFunctionType", self.send_message)

        # Store send_message_callback so _add_child_notification_to_parent can use it to save to DB
        self._send_message_callback = send_message_callback

        if parent_progress_callback:
            await logger.adebug(
                "[AGENT MCP DEBUG] Child agent detected parent callback - will forward agent steps as progress"
            )

            async def send_message_with_progress(message: Message, skip_db_update: bool = False) -> Message:
                """Wrapper that sends message locally AND forwards as progress notification to parent."""
                # First, send the message locally (to child flow's UI)
                result_message = await self.send_message(message=message, skip_db_update=skip_db_update)

                # Then, forward agent steps as progress notifications to parent
                # Extract the latest content from the message to send as progress
                if message.content_blocks and message.content_blocks[0].contents:
                    latest_content = message.content_blocks[0].contents[-1]

                    # Format the progress message based on content type
                    if hasattr(latest_content, 'type'):
                        if latest_content.type == "tool_use":
                            # Tool invocation
                            header = getattr(latest_content, 'header', None)
                            if header and isinstance(header, dict):
                                progress_msg = f"🔨 {header.get('title', 'Tool use')}"
                            else:
                                progress_msg = "🔨 Tool use"
                            if hasattr(latest_content, 'output') and latest_content.output:
                                name = getattr(latest_content, 'name', 'tool')
                                progress_msg = f"✅ Executed **{name}**"
                        elif latest_content.type == "text":
                            # Text content (input/output)
                            header = getattr(latest_content, 'header', None)
                            if header and isinstance(header, dict):
                                title = header.get('title', 'Text')
                            else:
                                title = 'Text'
                            progress_msg = f"📝 {title}"
                        else:
                            progress_msg = f"Agent step: {latest_content.type}"

                        # Send progress notification to parent
                        try:
                            await parent_progress_callback(progress_msg)
                            await logger.adebug(
                                f"[AGENT MCP DEBUG] Forwarded agent step to parent: {progress_msg!r}"
                            )
                        except Exception as e:
                            await logger.awarning(
                                f"[AGENT MCP DEBUG] Failed to forward agent step to parent: {e}"
                            )

                return result_message

            send_message_callback = cast("SendMessageFunctionType", send_message_with_progress)

            # Update stored callback to use the wrapped version
            self._send_message_callback = send_message_callback

        try:
            # Phase 2.1: Save agent message to database BEFORE starting execution
            # This ensures it has an ID when progress notifications arrive
            if self._enable_nested_notifications:
                agent_message = await send_message_callback(agent_message)
                # CRITICAL: Update the reference so progress callback sees the message with ID
                self._current_parent_message = agent_message
                await logger.adebug(
                    f"[NESTED NOTIFICATIONS] Pre-saved agent message with ID: {agent_message.get_id()}"
                )

            shared_callbacks = self._get_shared_callbacks()
            runtime_callbacks = [AgentAsyncHandler(self.log), *shared_callbacks]
            await logger.adebug(
                "[AGENT MCP DEBUG] about to call runnable.astream_events: "
                f"input_keys={list(input_dict.keys())!r}, "
                f"tool_names={[getattr(tool, 'name', type(tool).__name__) for tool in (self.tools or [])]!r}, "
                f"tool_callbacks={[getattr(tool, 'callbacks', None) for tool in (self.tools or [])]!r}, "
                f"runtime_callbacks={runtime_callbacks!r}, "
                f"send_message_repr={self.send_message!r}, "
                f"has_event_manager={self._event_manager is not None!r}, "
                f"has_parent_callback={parent_progress_callback is not None!r}"
            )
            result = await process_agent_events(
                runnable.astream_events(
                    input_dict,
                    # here we use the shared callbacks because the AgentExecutor uses the tools
                    config={"callbacks": runtime_callbacks},
                    version="v2",
                ),
                agent_message,
                send_message_callback,
                on_token_callback,
            )
        except ExceptionWithMessageError as e:
            # Only delete message from database if it has an ID (was stored)
            if hasattr(e, "agent_message"):
                msg_id = e.agent_message.get_id()
                if msg_id:
                    await delete_message(id_=msg_id)
            await self._send_message_event(e.agent_message, category="remove_message")
            logger.error(f"ExceptionWithMessageError: {e}")
            raise
        except Exception as e:
            # Log or handle any other exceptions
            logger.error(f"Error: {e}")
            raise

        # Phase 2.1: Update parent_message_id in tracking after message is saved
        if hasattr(self, '_current_tool_call_id') and self._current_tool_call_id in self._active_tool_calls:
            if agent_message.has_id():
                self._active_tool_calls[self._current_tool_call_id]["parent_message_id"] = str(agent_message.get_id())
                await logger.adebug(
                    f"[NESTED NOTIFICATIONS] Updated parent_message_id for tool_call_id={self._current_tool_call_id}: "
                    f"parent_message_id={agent_message.get_id()}"
                )

        # Phase 2.2: Save the final message with all nested blocks to the database
        if self._enable_nested_notifications and self._current_parent_message:
            # Debug: Check the message object before sending
            first_block = self._current_parent_message.content_blocks[0] if self._current_parent_message.content_blocks else None
            nested_count = len(first_block.nested_blocks) if first_block and hasattr(first_block, 'nested_blocks') else 0

            await logger.adebug(
                f"[NESTED NOTIFICATIONS] Saving final message with nested blocks: "
                f"message_id={self._current_parent_message.get_id()}, "
                f"content_blocks_count={len(self._current_parent_message.content_blocks)}, "
                f"first_block_nested_count={nested_count}, "
                f"first_block_has_nested_blocks_attr={hasattr(first_block, 'nested_blocks') if first_block else False}, "
                f"first_block_type={type(first_block).__name__ if first_block else None}"
            )

            # Debug: Try to access nested_blocks directly
            if first_block:
                try:
                    nested_blocks_value = first_block.nested_blocks
                    model_fields_set_value = first_block.model_fields_set if hasattr(first_block, 'model_fields_set') else set()
                    nested_in_fields_set = 'nested_blocks' in model_fields_set_value
                    await logger.adebug(
                        f"[NESTED NOTIFICATIONS] first_block.nested_blocks type: {type(nested_blocks_value)}, "
                        f"len: {len(nested_blocks_value) if nested_blocks_value else 0}, "
                        f"model_fields_set: {model_fields_set_value}, "
                        f"nested_blocks_in_model_fields_set: {nested_in_fields_set}"
                    )
                except Exception as e:
                    await logger.awarning(f"[NESTED NOTIFICATIONS] Error accessing nested_blocks: {e}")

            # Save the message with all accumulated nested blocks
            final_message = await send_message_callback(self._current_parent_message)
            await logger.adebug(f"[NESTED NOTIFICATIONS] Final message saved with ID: {final_message.get_id()}")

        self.status = result
        return result

    @abstractmethod
    def create_agent_runnable(self) -> Runnable:
        """Create the agent."""

    def validate_tool_names(self) -> None:
        """Validate tool names to ensure they match the required pattern."""
        pattern = re.compile(r"^[a-zA-Z0-9_-]+$")
        if hasattr(self, "tools") and self.tools:
            for tool in self.tools:
                if not pattern.match(tool.name):
                    msg = (
                        f"Invalid tool name '{tool.name}': must only contain letters, numbers, underscores, dashes,"
                        " and cannot contain spaces."
                    )
                    raise ValueError(msg)


class LCToolsAgentComponent(LCAgentComponent):
    _base_inputs = [
        HandleInput(
            name="tools",
            display_name="Tools",
            input_types=["Tool"],
            is_list=True,
            required=False,
            info="These are the tools that the agent can use to help with tasks.",
        ),
        *LCAgentComponent.get_base_inputs(),
    ]

    def build_agent(self) -> AgentExecutor:
        self.validate_tool_names()
        agent = self.create_agent_runnable()
        return AgentExecutor.from_agent_and_tools(
            agent=RunnableAgent(runnable=agent, input_keys_arg=["input"], return_keys_arg=["output"]),
            tools=self.tools,
            **self.get_agent_kwargs(flatten=True),
        )

    @abstractmethod
    def create_agent_runnable(self) -> Runnable:
        """Create the agent."""

    def get_tool_name(self) -> str:
        return self.display_name or "Agent"

    def get_tool_description(self) -> str:
        return self.agent_description or DEFAULT_TOOLS_DESCRIPTION

    def _build_tools_names(self):
        tools_names = ""
        if self.tools:
            tools_names = ", ".join([tool.name for tool in self.tools])
        return tools_names

    # Set shared callbacks for tracing
    def set_tools_callbacks(self, tools_list: list[Tool], callbacks_list: list[BaseCallbackHandler]):
        """Set shared callbacks for tracing to the tools.

        If we do not pass down the same callbacks to each tool
        used by the agent, then each tool will instantiate a new callback.
        For some tracing services, this will cause
        the callback handler to lose the id of its parent run (Agent)
        and thus throw an error in the tracing service client.

        Args:
            tools_list: list of tools to set the callbacks for
            callbacks_list: list of callbacks to set for the tools
        Returns:
            None
        """
        for tool in tools_list or []:
            if hasattr(tool, "callbacks"):
                tool.callbacks = callbacks_list

    async def _inject_progress_callback_into_tools(self) -> None:
        """Inject progress callback into MCP tools so they can send progress updates to the UI.

        This enhanced version includes:
        - Message type detection (agent_steps, final_answer, tool_invocation, tool_progress)
        - Message ID tracking for smart updates (replace mode)
        - Improved message formatting with emojis
        - Langflow-specific metadata (not sent over MCP)
        """
        if not self.tools:
            return

        # Create a progress callback that sends messages to the UI
        async def agent_progress_callback(progress_data: dict) -> None:
            """Forward progress notifications from MCP tools to the agent's UI.

            Phase 2.4: Modified to route notifications to nested blocks when feature is enabled.

            Note: progress_data comes from MCP notification params (spec-compliant).
            It contains: {"progress": 0.05, "total": 1.0, "message": "..."}
            The message field is a custom extension compatible with the spec.

            This function adds Langflow-specific logic on top of MCP notifications:
            - Detects message type from content
            - Routes to nested blocks (if enabled) or creates top-level messages
            - Tracks message IDs for smart updates
            - Adds metadata for UI rendering
            - Formats messages with emojis
            """
            # Extract message from MCP params (spec-compliant)
            message_text = progress_data.get("message", "")
            if not message_text or not hasattr(self, "send_message"):
                return

            # OUR LOGIC: Detect message type from content (Langflow-specific)
            message_type = _detect_message_type(message_text)

            # Phase 2.4: Check if nested notifications are enabled
            if self._enable_nested_notifications and hasattr(self, '_current_parent_message'):
                # NEW BEHAVIOR: Route to nested block in parent message
                parent_message = self._current_parent_message

                if parent_message and parent_message.has_id():
                    # Format message for nested display
                    formatted_text = _format_progress_message(message_text, message_type)

                    # Add as nested block instead of creating new message
                    await self._add_child_notification_to_parent(
                        parent_message=parent_message,
                        notification_type=message_type,
                        notification_text=formatted_text,
                        tool_call_id=str(parent_message.get_id())  # Use parent message ID as tool_call_id
                    )

                    await logger.adebug(
                        f"[NESTED NOTIFICATIONS] Routed to nested block: type={message_type}, "
                        f"parent_id={parent_message.get_id()}, text={message_text[:100]}"
                    )
                    return  # Don't create top-level message
                else:
                    await logger.adebug(
                        f"[NESTED NOTIFICATIONS] Parent message not ready (no ID yet), "
                        f"falling back to top-level message"
                    )

            # FALLBACK: Original behavior - create top-level message
            # Use replace mode for all message types to avoid duplicates
            update_mode = "replace"

            # Get or create message ID for this type
            if message_type in ["tool_progress", "agent_steps"]:
                tracking_key = "agent_progress"
            else:
                tracking_key = message_type

            if tracking_key not in self._progress_message_ids:
                self._progress_message_ids[tracking_key] = str(uuid.uuid4())

            message_id = self._progress_message_ids[tracking_key]

            # Format message based on type
            formatted_text = _format_progress_message(message_text, message_type)

            # Create Langflow Message
            from lfx.schema.message import Message
            progress_message = Message(
                text=formatted_text,
                sender="Machine",
                sender_name="Agent Tool",
                session_id=getattr(self, "session_id", ""),
            )

            # Store metadata as temporary attributes
            progress_message._message_type = message_type  # type: ignore
            progress_message._update_mode = update_mode  # type: ignore
            progress_message._tracking_id = message_id  # type: ignore
            progress_message._source = "mcp_tool"  # type: ignore

            try:
                # Send to Langflow UI
                await self.send_message(progress_message)
                await logger.adebug(
                    f"[AGENT PROGRESS] Sent progress message to UI: type={message_type}, "
                    f"mode={update_mode}, id={message_id}, text={message_text[:100]}"
                )
            except Exception as e:
                await logger.awarning(f"[AGENT PROGRESS] Failed to send progress message: {e}")

        # Inject the callback into MCP tools
        for tool in self.tools:
            # Check if this is an MCP tool by looking for the coroutine attribute
            if hasattr(tool, "coroutine") and hasattr(tool.coroutine, "__name__"):
                # Store the callback in the tool's metadata so the MCP tool_coroutine can access it
                if not hasattr(tool, "metadata"):
                    tool.metadata = {}
                tool.metadata["_agent_progress_callback"] = agent_progress_callback
                await logger.adebug(
                    f"[AGENT PROGRESS] Injected progress callback into tool: {tool.name}"
                )

    async def _get_tools(self) -> list[Tool]:
        component_toolkit = _get_component_toolkit()
        tools_names = self._build_tools_names()
        agent_description = self.get_tool_description()
        # TODO: Agent Description Depreciated Feature to be removed
        description = f"{agent_description}{tools_names}"

        tools = component_toolkit(component=self).get_tools(
            tool_name=self.get_tool_name(),
            tool_description=description,
            # here we do not use the shared callbacks as we are exposing the agent as a tool
            callbacks=self.get_langchain_callbacks(),
        )
        if hasattr(self, "tools_metadata"):
            tools = component_toolkit(component=self, metadata=self.tools_metadata).update_tools_metadata(tools=tools)

        return tools
