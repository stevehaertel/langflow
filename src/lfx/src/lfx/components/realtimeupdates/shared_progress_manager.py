"""Shared Progress Manager - Allows multiple components to append updates to a single message block.

This component creates and manages a shared progressive message that other components
can append to, similar to how agent tool operations work.
"""

import asyncio
from time import perf_counter

from loguru import logger

from lfx.custom.custom_component.component import Component
from lfx.inputs.inputs import BoolInput, IntInput, MessageInput, MessageTextInput
from lfx.schema.content_block import ContentBlock
from lfx.schema.content_types import ToolContent
from lfx.schema.data import Data
from lfx.schema.message import Message
from lfx.template.field.base import Output
from lfx.utils.constants import MESSAGE_SENDER_AI


class SharedProgressManager(Component):
    """Manages a shared progressive message that multiple components can append to.

    Usage:
    1. Place this component early in your flow
    2. Connect it to components that need to add progress updates
    3. Each component receives the manager and can add updates
    4. Connect the final output to Chat Output
    """

    display_name = "Shared Progress Manager"
    description = "Manages a shared message for progressive updates from multiple components."
    icon = "ListTree"
    name = "SharedProgressManager"

    output_types: list[str] = ["Message", "Data"]

    inputs = [
        MessageInput(
            name="trigger",
            display_name="Trigger",
            info="Optional input to trigger initialization. Can be any message.",
            required=False,
        ),
        MessageTextInput(
            name="block_title",
            display_name="Block Title",
            info="Title for the progress message block.",
            value="Flow Progress",
            required=False,
        ),
        BoolInput(
            name="enable_debouncing",
            display_name="Enable Debouncing",
            info="Enable phased debouncing: first update immediate, subsequent updates throttled when rapid.",
            value=True,
            required=False,
        ),
        IntInput(
            name="min_update_interval_ms",
            display_name="Min Update Interval (ms)",
            info="Minimum time between updates. Updates arriving faster will be debounced (default: 100ms).",
            value=100,
            required=False,
        ),
    ]

    outputs = [
        Output(
            display_name="Progress Manager",
            name="manager",
            method="get_manager",
            types=["Data"],
            group_outputs=True,
        ),
        Output(
            display_name="Initial Message",
            name="initial_message",
            method="get_initial_message",
            types=["Message"],
            group_outputs=True,
        ),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._shared_message: Message | None = None
        self._start_time: float = perf_counter()
        self._initialized: bool = False

        # Phased debouncing state
        self._first_update_sent: bool = False
        self._last_update_time: float = 0
        self._pending_update: dict | None = None
        self._debounce_timer: asyncio.Task | None = None

    async def get_manager(self) -> Data:
        """Return a Data object containing the manager instance.
        Other components can extract this and use it to add updates.

        Note: Initialization is handled by get_initial_message() to avoid
        double initialization when both outputs are connected.
        """
        logger.info("SharedProgressManager.get_manager() called")

        # Return self wrapped in Data so it can be passed to other components
        result = Data(data={"manager": self})
        logger.info("Returning Data object with manager")
        return result

    async def _initialize_message(self) -> None:
        """Initialize the shared progressive message."""
        if self._initialized:
            logger.info("Already initialized, skipping")
            return

        # Get the custom title or use default
        block_title = getattr(self, "block_title", "Flow Progress") or "Flow Progress"

        logger.info(f"Creating initial message with title: {block_title}")
        self._shared_message = Message(
            text="",
            sender=MESSAGE_SENDER_AI,
            sender_name="System",
            session_id=getattr(self, "session_id", "") or (self.graph.session_id if hasattr(self, "graph") else ""),
            flow_id=self.graph.flow_id if hasattr(self, "graph") else None,
        )
        self._shared_message.properties.state = "partial"
        self._shared_message.properties.icon = "Bot"
        self._shared_message.content_blocks = [ContentBlock(title=block_title, contents=[])]

        # Store in DB to get an ID
        logger.info("Sending initial message to DB...")
        self._shared_message = await self.send_message(self._shared_message)
        logger.info("Message stored successfully")
        self._start_time = perf_counter()
        self._initialized = True
        logger.info("Initialization complete")

    async def add_update(
        self,
        text: str,
        title: str | None = None,
        icon: str = "CheckCircle",
        error: bool = False,
        overwrite_previous: bool = False,
        tool_input: dict | str | None = None,
        tool_output: str | None = None,
    ) -> Message:
        """Add a status update to the shared message with phased debouncing.

        Phased Debouncing Strategy:
        - First update: Sent immediately (instant feedback)
        - Subsequent updates: Debounced if arriving rapidly (<min_update_interval_ms)
        - Slow updates: Sent immediately if enough time has passed

        This method is called by other components.
        The manager must be initialized before calling this method.
        Initialization happens automatically via get_initial_message().

        Args:
            text: The message text to display
            title: Optional title for the update block
            icon: Icon name for the update
            error: Whether this is an error message
            overwrite_previous: If True, replaces the last content block instead of appending
            tool_input: Optional actual tool input to display in ToolContent
            tool_output: Optional actual tool output to display in ToolContent
        """
        logger.info(f"add_update() called with text: '{text[:50]}...', title: {title}, overwrite: {overwrite_previous}")
        if not self._initialized:
            error_msg = (
                "SharedProgressManager.add_update() called before initialization. "
                "Make sure get_initial_message() output is connected to Chat Output "
                "before any components call add_update()."
            )
            logger.error(error_msg)
            raise RuntimeError(error_msg)

        # Check if debouncing is enabled
        enable_debouncing = getattr(self, "enable_debouncing", True)

        if not enable_debouncing:
            # Debouncing disabled - send immediately (original behavior)
            return await self._send_update_now(text, title, icon, error, overwrite_previous, tool_input, tool_output)

        # PHASE 1: First update always goes immediately for instant feedback
        if not self._first_update_sent:
            logger.info("First update - sending immediately (no debouncing)")
            self._first_update_sent = True
            return await self._send_update_now(text, title, icon, error, overwrite_previous, tool_input, tool_output)

        # PHASE 2: Subsequent updates use debouncing
        min_interval = getattr(self, "min_update_interval_ms", 100)
        now = perf_counter() * 1000
        time_since_last = now - self._last_update_time

        # Store this update as pending
        self._pending_update = {
            "text": text,
            "title": title,
            "icon": icon,
            "error": error,
            "overwrite": overwrite_previous,
            "tool_input": tool_input,
            "tool_output": tool_output,
        }

        # If enough time has passed, send immediately
        if time_since_last >= min_interval:
            logger.info(f"Sufficient time passed ({time_since_last:.0f}ms >= {min_interval}ms) - sending immediately")
            return await self._send_update_now(text, title, icon, error, overwrite_previous, tool_input, tool_output)

        # Otherwise, schedule debounced send
        delay = (min_interval - time_since_last) / 1000
        logger.info(
            f"Rapid update detected ({time_since_last:.0f}ms < {min_interval}ms) - debouncing for {delay * 1000:.0f}ms"
        )

        # Cancel existing timer if present
        if self._debounce_timer and not self._debounce_timer.done():
            logger.info("Cancelling previous debounce timer")
            self._debounce_timer.cancel()

        # Schedule new debounced send
        self._debounce_timer = asyncio.create_task(self._debounced_send(delay))

        return self._shared_message

    async def _debounced_send(self, delay: float):
        """Wait for debounce period, then send pending update.

        Args:
            delay: Delay in seconds before sending
        """
        try:
            await asyncio.sleep(delay)

            if self._pending_update:
                logger.info("Debounce timer fired - sending pending update")
                update = self._pending_update
                self._pending_update = None
                await self._send_update_now(
                    update["text"],
                    update["title"],
                    update["icon"],
                    update["error"],
                    update["overwrite"],
                    update.get("tool_input"),
                    update.get("tool_output"),
                )
        except asyncio.CancelledError:
            logger.info("Debounce timer cancelled (replaced by newer update)")
            # This is expected when a new update arrives before the timer fires

    async def _send_update_now(
        self,
        text: str,
        title: str | None,
        icon: str,
        error: bool,
        overwrite_previous: bool,
        tool_input: dict | str | None = None,
        tool_output: str | None = None,
    ) -> Message:
        """Actually send the update immediately (extracted from add_update for reuse).

        Args:
            text: The message text to display
            title: Optional title for the update block
            icon: Icon name for the update
            error: Whether this is an error message
            overwrite_previous: If True, replaces the last content block instead of appending
            tool_input: Optional actual tool input to display in ToolContent
            tool_output: Optional actual tool output to display in ToolContent
        """
        duration = int((perf_counter() - self._start_time) * 1000)
        self._start_time = perf_counter()

        # Prepare tool_input for ToolContent
        # If tool_input is a string, try to parse it as JSON, otherwise wrap it
        if tool_input is not None:
            if isinstance(tool_input, str):
                import json

                try:
                    parsed_input = json.loads(tool_input)
                except (json.JSONDecodeError, ValueError):
                    # If not valid JSON, wrap the string
                    parsed_input = {"input": tool_input}
            else:
                parsed_input = tool_input
        else:
            # Default to progress update marker if no tool input provided
            parsed_input = {"_progress_update": True}

        # Prepare tool_output for ToolContent
        # If tool_output is a string, try to parse it as JSON for pretty display
        import json

        parsed_output = tool_output if tool_output is not None else text

        logger.info(f"[OUTPUT PARSING] tool_output type: {type(tool_output)}")
        logger.info(f"[OUTPUT PARSING] tool_output is None: {tool_output is None}")
        logger.info(f"[OUTPUT PARSING] parsed_output type before parsing: {type(parsed_output)}")
        if isinstance(parsed_output, str):
            logger.info(f"[OUTPUT PARSING] parsed_output is string, length: {len(parsed_output)}")
            logger.info(f"[OUTPUT PARSING] First 200 chars: {parsed_output[:200]}")

        if isinstance(parsed_output, str):
            try:
                # Try to parse as JSON - if successful, it will be displayed as pretty JSON
                parsed_output = json.loads(parsed_output)
                logger.info(f"[OUTPUT PARSING] Successfully parsed as JSON! Type: {type(parsed_output)}")
            except (json.JSONDecodeError, ValueError) as e:
                # If not valid JSON, keep as string
                logger.info(f"[OUTPUT PARSING] Failed to parse as JSON: {e}")
        else:
            logger.info(f"[OUTPUT PARSING] Not a string, keeping as-is: {type(parsed_output)}")

        logger.info(f"[OUTPUT PARSING] Final parsed_output type: {type(parsed_output)}")

        # Use ToolContent to get collapsible blocks like tool calls
        tool_content = ToolContent(
            type="tool_use",
            name=title or ("Error" if error else "Update"),
            tool_input=parsed_input,
            output=parsed_output,
            error=None,
            duration=duration,
        )

        # Create a new ContentBlock for each update
        new_block = ContentBlock(title=title or ("Error" if error else "Update"), contents=[tool_content])

        # If overwrite_previous is True, replace the last block instead of appending
        if overwrite_previous and len(self._shared_message.content_blocks) > 1:
            logger.info(f"Overwriting previous block (was: '{self._shared_message.content_blocks[-1].title}')")
            self._shared_message.content_blocks[-1] = new_block
        else:
            self._shared_message.content_blocks.append(new_block)

        # Set text to only the most recent message
        self._shared_message.text = text

        # Ensure state remains "partial" during updates for real-time display
        self._shared_message.properties.state = "partial"

        # Send the update
        logger.info("Sending message update (skip_db_update=True)...")
        self._shared_message = await self.send_message(self._shared_message, skip_db_update=True)

        # Update last send time for debouncing
        self._last_update_time = perf_counter() * 1000

        logger.info(f"Update sent. Message now has {len(self._shared_message.content_blocks)} content blocks")

        return self._shared_message

    async def finalize(self, final_text: str | None = None, block_title: str | None = None) -> Message:
        """Finalize the shared message.
        Call this at the end of your flow.

        Args:
            final_text: Optional final message to add before completing
            block_title: Optional custom title for the message block header (replaces default)
        """
        if not self._initialized:
            return Message(text=final_text or "Complete")

        if final_text:
            await self.add_update(final_text, title="Complete", icon="CheckCircle2")

        # Update block title if provided
        if block_title and self._shared_message.content_blocks:
            self._shared_message.content_blocks[0].title = block_title

        self._shared_message.properties.state = "complete"

        # Final DB update
        self._shared_message = await self.send_message(self._shared_message, skip_db_update=True)

        return self._shared_message

    async def get_initial_message(self) -> Message:
        """Get the initial message for immediate display in Chat Output."""
        logger.info("get_initial_message() called")
        if not self._initialized:
            logger.info("Not initialized, initializing now...")
            await self._initialize_message()

        if self._shared_message is None:
            logger.error("Message is None after initialization!")
            return Message(text="Error: Message not initialized")

        logger.info("Returning initial message")
        return self._shared_message

    def get_message(self) -> Message | None:
        """Get the current shared message."""
        return self._shared_message


# Made with Bob
