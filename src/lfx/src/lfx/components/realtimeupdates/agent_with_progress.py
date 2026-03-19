"""Agent with Progress Tracking - Agent with split outputs for response and progress manager.

This component extends the standard Langflow Agent to intercept tool calls
and send them to a SharedProgressManager for real-time display.

This version has split outputs (response + progress_manager) to avoid needing
the agent_result_extractor component, now that the Langflow bug is fixed.
"""

from typing import Any

from lfx.components.models_and_agents.agent import AgentComponent
from lfx.inputs.inputs import DataInput
from lfx.schema.content_types import ToolContent
from lfx.schema.data import Data
from lfx.schema.message import Message
from lfx.template.field.base import Output


class AgentWithProgress(AgentComponent):
    """Agent component that captures tool calls and sends them to SharedProgressManager.

    This version has split outputs for response and progress_manager to eliminate
    the need for agent_result_extractor component.
    """

    display_name = "Agent with Progress Tracking"
    description = "Agent that displays tool calls in a shared progress message. Split outputs version."
    icon = "bot"
    name = "AgentWithProgress"

    output_types: list[str] = ["Message", "Data"]

    def __init__(self, **kwargs):
        """Initialize the component and add custom inputs."""
        super().__init__(**kwargs)

        # Add progress manager input dynamically to avoid pickle issues with class-level input references
        if not any(inp.name == "progress_manager" for inp in self.inputs):
            self.inputs.append(
                DataInput(
                    name="progress_manager",
                    display_name="Progress Manager",
                    info="Optional: Shared Progress Manager to display tool calls.",
                    required=False,
                )
            )

    outputs = [
        Output(
            display_name="Response",
            name="response",
            method="message_response_with_progress",
        ),
        Output(
            display_name="Progress Manager",
            name="progress_manager_output",
            method="get_progress_manager",
        ),
    ]

    def get_progress_manager(self) -> Data:
        """Pass through the progress manager to downstream components.
        This allows chaining multiple components that use the same progress manager.

        Note: This is synchronous (not async) to avoid triggering agent rebuild.
        """
        if hasattr(self, "progress_manager") and self.progress_manager:
            if isinstance(self.progress_manager, Data):
                return self.progress_manager

        # Return empty Data if no manager
        return Data(data={})

    async def message_response_with_progress(self) -> Message:
        """Execute the agent with progress tracking and return the response message."""
        # Get the progress manager if provided
        progress_manager = None
        if hasattr(self, "progress_manager") and self.progress_manager:
            if isinstance(self.progress_manager, Data):
                progress_manager = self.progress_manager.data.get("manager")

        # If no progress manager, fall back to standard behavior
        if not progress_manager:
            return await super().message_response()

        # Create a callback handler to capture tool events
        from langchain_core.callbacks import AsyncCallbackHandler

        class ProgressCallbackHandler(AsyncCallbackHandler):
            """Callback handler that sends tool execution updates to SharedProgressManager."""

            def __init__(self, manager):
                super().__init__()
                self.manager = manager
                self.tool_count = 0
                self.current_tool_name = None
                self.current_tool_input = None

            async def on_chat_model_start(
                self, serialized: dict[str, Any], messages: list[list], **kwargs: Any
            ) -> None:
                """Called when chat model starts - we don't need to track this."""

            async def on_llm_start(self, serialized: dict[str, Any], prompts: list[str], **kwargs: Any) -> None:
                """Called when LLM starts - we don't need to track this."""

            async def on_llm_end(self, response, **kwargs: Any) -> None:
                """Called when LLM ends - we don't need to track this."""

            async def on_llm_error(self, error: Exception, **kwargs: Any) -> None:
                """Called when LLM encounters an error - we don't need to track this."""

            async def on_chain_start(self, serialized: dict[str, Any], inputs: dict[str, Any], **kwargs: Any) -> None:
                """Called when a chain starts - we don't need to track this."""

            async def on_chain_end(self, outputs: dict[str, Any], **kwargs: Any) -> None:
                """Called when a chain ends - we don't need to track this."""

            async def on_chain_error(self, error: Exception, **kwargs: Any) -> None:
                """Called when a chain encounters an error - we don't need to track this."""

            async def on_agent_action(self, action, **kwargs: Any) -> None:
                """Called when agent takes an action - we don't need to track this."""

            async def on_agent_finish(self, finish, **kwargs: Any) -> None:
                """Called when agent finishes - we don't need to track this."""

            async def on_tool_start(self, serialized: dict[str, Any], input_str: str, **kwargs: Any) -> None:
                """Called when a tool starts executing."""
                self.tool_count += 1
                self.current_tool_name = serialized.get("name", "Unknown Tool")
                self.current_tool_input = input_str

                # Truncate input for display text only
                truncated_input = input_str[:200] if len(input_str) > 200 else input_str
                suffix = "..." if len(input_str) > 200 else ""

                # Send update to progress manager with actual tool input
                await self.manager.add_update(
                    text=f"Calling tool: **{self.current_tool_name}**\n\nInput: {truncated_input}{suffix}",
                    title=self.current_tool_name,
                    icon="Loader",
                    error=False,
                    overwrite_previous=False,
                    tool_input=input_str,  # Pass the actual tool input
                    tool_output=None,
                )

            async def on_tool_end(self, output: str, **kwargs: Any) -> None:
                """Called when a tool finishes executing."""
                # Convert output to JSON string for proper display
                import json

                if output is not None:
                    # If output is a dict or list, convert to JSON string
                    if isinstance(output, (dict, list)):
                        output_str = json.dumps(output, indent=2)
                    else:
                        # For other types, try to convert to string
                        output_str = str(output)
                else:
                    output_str = ""

                truncated_output = output_str[:200] if len(output_str) > 200 else output_str
                suffix = "..." if len(output_str) > 200 else ""

                # Update the last message with completion, passing actual tool input and output
                await self.manager.add_update(
                    text=f"\n\n✓ Tool completed\n\nOutput: {truncated_output}{suffix}",
                    title=self.current_tool_name or f"Tool Call #{self.tool_count}",
                    icon="CheckCircle",
                    error=False,
                    overwrite_previous=True,
                    tool_input=self.current_tool_input,  # Pass the stored tool input
                    tool_output=output_str,  # Pass the actual tool output as JSON string
                )

            async def on_tool_error(self, error: Exception, **kwargs: Any) -> None:
                """Called when a tool encounters an error."""
                await self.manager.add_update(
                    text=f"✗ Tool failed: {error!s}",
                    title=self.current_tool_name or f"Tool Call #{self.tool_count}",
                    icon="AlertCircle",
                    error=True,
                    overwrite_previous=True,
                )

        # Create callback handler
        callback_handler = ProgressCallbackHandler(progress_manager)

        # Execute the agent with our callback
        try:
            # Get the session_id from the graph (same way the standard agent does)
            if hasattr(self, "graph") and hasattr(self.graph, "session_id"):
                session_id = self.graph.session_id
            elif hasattr(self, "session_id"):
                session_id = self.session_id
            else:
                session_id = None

            # Get chat history for context
            chat_history = await self.get_memory_data()
            if isinstance(chat_history, Message):
                chat_history = [chat_history]

            # Log session info for debugging
            print(f"\n{'=' * 80}")
            print(f"[Agent {self.display_name}] STARTING EXECUTION")
            print(f"[Agent] Session ID: {session_id}")
            print(f"[Agent] Chat history length: {len(chat_history)}")
            print("[Agent] Chat history messages:")
            for i, msg in enumerate(chat_history):
                if isinstance(msg, Message):
                    print(f"  {i + 1}. [{msg.sender}] {msg.text[:100]}...")
                else:
                    print(f"  {i + 1}. {type(msg).__name__}: {str(msg)[:100]}...")
            print(f"{'=' * 80}\n")

            # Set up the agent using parent class methods
            llm_model = self._get_llm()
            self.set(
                llm=llm_model,
                tools=self.tools or [],
                chat_history=chat_history,
                input_value=self.input_value,
                system_prompt=self.system_prompt,
            )

            # Create the agent runnable
            agent_runnable = self.create_agent_runnable()

            # Build AgentExecutor
            from langchain.agents import AgentExecutor

            handle_parsing_errors = hasattr(self, "handle_parsing_errors") and self.handle_parsing_errors
            verbose = hasattr(self, "verbose") and self.verbose
            max_iterations = hasattr(self, "max_iterations") and self.max_iterations

            executor = AgentExecutor.from_agent_and_tools(
                agent=agent_runnable,
                tools=self.tools or [],
                handle_parsing_errors=handle_parsing_errors,
                verbose=verbose,
                max_iterations=max_iterations,
                return_intermediate_steps=True,  # CRITICAL: Return tool call history
            )

            # Prepare input
            input_text = self.input_value.text if isinstance(self.input_value, Message) else str(self.input_value)
            agent_input = {"input": input_text}

            # Add system prompt if available
            if hasattr(self, "system_prompt") and self.system_prompt:
                agent_input["system_prompt"] = self.system_prompt

            # Add chat history in LangChain format
            if chat_history:
                from langchain_core.messages import AIMessage, HumanMessage

                lc_history = []
                for msg in chat_history:
                    if isinstance(msg, Message):
                        if msg.sender == "User":
                            lc_history.append(HumanMessage(content=msg.text))
                        else:
                            lc_history.append(AIMessage(content=msg.text))
                    else:
                        lc_history.append(msg)
                agent_input["chat_history"] = lc_history

            # Invoke with our callback
            result = await executor.ainvoke(agent_input, config={"callbacks": [callback_handler]})

            # Extract output and intermediate steps
            output_text = result.get("output", str(result)) if isinstance(result, dict) else str(result)
            intermediate_steps = result.get("intermediate_steps", []) if isinstance(result, dict) else []

            # Build a comprehensive text that includes tool calls and results
            # This will be stored in the database for downstream agents to access
            print("\n[Agent] Building message context:")
            print(f"[Agent] output_text length: {len(output_text)}")
            print(f"[Agent] intermediate_steps count: {len(intermediate_steps)}")

            # Build structured tool call history for database storage
            # Use ToolContent format to match standard agent behavior
            tool_contents = []
            if intermediate_steps:
                print(f"[Agent] Building structured ToolContent objects for {len(intermediate_steps)} tool calls")
                for i, (action, observation) in enumerate(intermediate_steps, 1):
                    tool_name = action.tool if hasattr(action, "tool") else "Unknown Tool"
                    tool_input = action.tool_input if hasattr(action, "tool_input") else {}

                    # Ensure tool_input is a dict (required by ToolContent schema)
                    if not isinstance(tool_input, dict):
                        try:
                            import json

                            tool_input = (
                                json.loads(str(tool_input))
                                if isinstance(tool_input, str)
                                else {"value": str(tool_input)}
                            )
                        except:
                            tool_input = {"value": str(tool_input)}

                    # Create structured ToolContent matching standard agent format
                    tool_content = ToolContent(
                        type="tool_use",
                        name=tool_name,
                        tool_input=tool_input,
                        output=observation,  # Store full observation
                        error=None,
                        duration=None,
                        header={"title": f"Executed **{tool_name}**", "icon": "Hammer"},
                    )
                    tool_contents.append(tool_content)
                    print(f"[Agent]   {i}. Created ToolContent for {tool_name}")

                print(f"[Agent] Built {len(tool_contents)} structured ToolContent objects")
            else:
                print("[Agent] No intermediate steps")

            # Create a message with structured tool history for database storage
            # IMPORTANT: Include tool details in BOTH text AND content_blocks
            # - content_blocks: For UI display (structured format)
            # - text: For agent consumption (LangChain agents read from text)
            if tool_contents:
                from lfx.schema.content_block import ContentBlock

                # Build text representation of tool calls for agent consumption
                tool_text = "\n\n[Previous Tool Calls]:\n"
                for i, (action, observation) in enumerate(intermediate_steps, 1):
                    tool_name = action.tool if hasattr(action, "tool") else "Unknown Tool"
                    tool_input = action.tool_input if hasattr(action, "tool_input") else {}
                    tool_text += f"\n{i}. Tool: {tool_name}\n"
                    tool_text += f"   Input: {tool_input}\n"
                    tool_text += f"   Output: {observation}\n"

                # Combine tool history with final output for agent context
                full_text = tool_text + "\n\n[Final Output]:\n" + output_text

                context_message = Message(
                    text=full_text,  # Include tool details in text for agent consumption
                    content_blocks=[
                        ContentBlock(title="Agent Steps", contents=tool_contents)
                    ],  # Structured format for UI
                    sender="Machine",
                    sender_name=self.sender_name if hasattr(self, "sender_name") else "AI",
                    session_id=session_id or "",
                    flow_id=self.graph.flow_id if hasattr(self, "graph") else None,
                )
                print("[Agent] Created context message with:")
                print(f"[Agent]   - Text length: {len(full_text)} chars (includes tool details)")
                print(f"[Agent]   - Content blocks: {len(tool_contents)} ToolContent objects")
            else:
                context_message = None

            # Create clean output message (JSON only) for downstream components
            response_message = Message(
                text=output_text,
                sender="Machine",
                sender_name=self.sender_name if hasattr(self, "sender_name") else "AI",
                session_id=session_id or "",
                flow_id=self.graph.flow_id if hasattr(self, "graph") else None,
            )
            print(f"[Agent] Created clean output message, length: {len(response_message.text)}")

            print(f"\n{'=' * 80}")
            print(f"[Agent {self.display_name}] STORING CONTEXT MESSAGE TO DATABASE (DB ONLY, NOT CHAT)")

            # Store context message with tool history (for agent context sharing)
            # We use astore_message directly to store in DB WITHOUT displaying in chat
            # The progress manager already displayed the tool calls via callbacks
            if context_message:
                # Import from langflow.memory (backend) not lfx.memory.stubs
                # The backend version properly converts Message to MessageTable for database storage
                from langflow.memory import astore_message

                print("[Agent] Storing context message with STRUCTURED tool history to database only...")
                print(f"[Agent] Session ID: {context_message.session_id}")
                print(f"[Agent] Message text length: {len(context_message.text)}")
                print(f"[Agent] Content blocks: {len(context_message.content_blocks)}")
                if context_message.content_blocks:
                    print(f"[Agent] ToolContent objects: {len(context_message.content_blocks[0].contents)}")

                # Store directly to database without sending to chat UI
                flow_id = self.graph.flow_id if hasattr(self, "graph") else None
                stored_messages = await astore_message(context_message, flow_id=flow_id)
                if stored_messages:
                    context_message = stored_messages[0]
                    print(f"[Agent] ✓ Context message stored in database with ID: {context_message.id}")
                    print("[Agent] ✓ Format: ToolContent objects in content_blocks (matches standard agent)")
                else:
                    print("[Agent] WARNING: Context message storage returned empty list")
            else:
                print("[Agent] No tool history to store")

            # Note: We don't store the clean output message to the database
            # It's only used for downstream component processing
            print(f"[Agent] Clean output message (not stored): {response_message.text[:100]}...")
            print(f"{'=' * 80}\n")

            # Return the clean response message
            return response_message

        except Exception as e:
            # Report error to progress manager
            if progress_manager:
                await progress_manager.add_update(
                    text=f"Agent execution failed: {e!s}",
                    title="Error",
                    icon="AlertCircle",
                    error=True,
                    overwrite_previous=False,
                )
            raise


# Made with Bob
