"""
Agent with Progress Tracking (Single Output Version) - Workaround for Langflow validation bug.

This version outputs a single Data object containing both the response and progress manager,
avoiding the "2 tools" validation error in Langflow's component_tool.py.
"""

from typing import Any

from lfx.components.models_and_agents.agent import AgentComponent
from lfx.inputs.inputs import DataInput
from lfx.schema.content_types import ToolContent
from lfx.schema.data import Data
from lfx.schema.message import Message
from lfx.template.field.base import Output


class AgentWithProgressSingleOutput(AgentComponent):
    """
    Agent component that captures tool calls and sends them to SharedProgressManager.
    
    This version uses a single output to avoid Langflow's validation bug that occurs
    when an agent has multiple outputs and is converted to a tool.
    """
    
    display_name = "Agent with Progress (Single Output)"
    description = "Agent that displays tool calls in a shared progress message. Single output version."
    icon = "bot"
    name = "AgentWithProgressSingleOutput"
    
    output_types: list[str] = ["Data"]
    
    outputs = [
        Output(
            display_name="Agent Result",
            name="agent_result",
            method="get_agent_result",
        ),
    ]
    
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
    
    async def get_agent_result(self) -> Data:
        """
        Execute the agent with progress tracking and return combined result.
        
        Returns a Data object containing:
        - response: The agent's Message response
        - progress_manager: The SharedProgressManager for downstream components
        """
        # Get the progress manager if provided
        progress_manager = None
        if hasattr(self, 'progress_manager') and self.progress_manager:
            if isinstance(self.progress_manager, Data):
                progress_manager = self.progress_manager.data.get("manager")
        
        # If no progress manager, fall back to standard behavior
        if not progress_manager:
            response = await super().message_response()
            return Data(data={
                "response": response,
                "progress_manager": None
            })
        
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
                pass
            
            async def on_llm_start(
                self, serialized: dict[str, Any], prompts: list[str], **kwargs: Any
            ) -> None:
                """Called when LLM starts - we don't need to track this."""
                pass
            
            async def on_llm_end(
                self, response, **kwargs: Any
            ) -> None:
                """Called when LLM ends - we don't need to track this."""
                pass
            
            async def on_llm_error(
                self, error: Exception, **kwargs: Any
            ) -> None:
                """Called when LLM encounters an error - we don't need to track this."""
                pass
            
            async def on_chain_start(
                self, serialized: dict[str, Any], inputs: dict[str, Any], **kwargs: Any
            ) -> None:
                """Called when a chain starts - we don't need to track this."""
                pass
            
            async def on_chain_end(
                self, outputs: dict[str, Any], **kwargs: Any
            ) -> None:
                """Called when a chain ends - we don't need to track this."""
                pass
            
            async def on_chain_error(
                self, error: Exception, **kwargs: Any
            ) -> None:
                """Called when a chain encounters an error - we don't need to track this."""
                pass
            
            async def on_agent_action(
                self, action, **kwargs: Any
            ) -> None:
                """Called when agent takes an action - we don't need to track this."""
                pass
            
            async def on_agent_finish(
                self, finish, **kwargs: Any
            ) -> None:
                """Called when agent finishes - we don't need to track this."""
                pass
            
            async def on_tool_start(
                self, serialized: dict[str, Any], input_str: str, **kwargs: Any
            ) -> None:
                """Called when a tool starts executing."""
                self.tool_count += 1
                self.current_tool_name = serialized.get("name", "Unknown Tool")
                self.current_tool_input = input_str
                
                # Truncate input for display text only
                truncated_input = input_str[:200] if len(input_str) > 200 else input_str
                suffix = "..." if len(input_str) > 200 else ""
                
                # Send update to progress manager with actual tool input
                # Use overwrite_previous=False to keep all tool blocks visible
                await self.manager.add_update(
                    text=f"Calling tool: **{self.current_tool_name}**\n\nInput: {truncated_input}{suffix}",
                    title=self.current_tool_name,
                    icon="Loader",
                    error=False,
                    overwrite_previous=False,  # Keep all tool blocks
                    tool_input=input_str,  # Pass the actual tool input
                    tool_output=None
                )
            
            async def on_tool_end(
                self, output: str, **kwargs: Any
            ) -> None:
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
                    tool_output=output_str  # Pass the actual tool output as JSON string
                )
            
            async def on_tool_error(
                self, error: Exception, **kwargs: Any
            ) -> None:
                """Called when a tool encounters an error."""
                await self.manager.add_update(
                    text=f"✗ Tool failed: {str(error)}",
                    title=self.current_tool_name or f"Tool Call #{self.tool_count}",
                    icon="AlertCircle",
                    error=True,
                    overwrite_previous=True
                )
        
        # Create callback handler
        callback_handler = ProgressCallbackHandler(progress_manager)
        
        # Execute the agent with our callback
        try:
            # Get the session_id from the graph (same way the standard agent does)
            if hasattr(self, 'graph') and hasattr(self.graph, 'session_id'):
                session_id = self.graph.session_id
            elif hasattr(self, 'session_id'):
                session_id = self.session_id
            else:
                session_id = None
            
            # Get chat history for context
            chat_history = await self.get_memory_data()
            if isinstance(chat_history, Message):
                chat_history = [chat_history]
            
            # Log session info for debugging
            print(f"\n{'='*80}")
            print(f"[Agent {self.display_name}] STARTING EXECUTION")
            print(f"[Agent] Session ID: {session_id}")
            print(f"[Agent] Chat history length: {len(chat_history)}")
            print(f"[Agent] Chat history messages:")
            for i, msg in enumerate(chat_history):
                if isinstance(msg, Message):
                    print(f"  {i+1}. [{msg.sender}] {msg.text[:50] if msg.text else '(empty)'}... | Timestamp: {msg.timestamp} | Content blocks: {len(msg.content_blocks)} | ID: {msg.id}")
                    if msg.content_blocks:
                        for j, block in enumerate(msg.content_blocks):
                            print(f"      Block {j+1}: {block.title if hasattr(block, 'title') else 'No title'} | Contents: {len(block.contents) if hasattr(block, 'contents') else 0}")
                else:
                    print(f"  {i+1}. {type(msg).__name__}: {str(msg)[:100]}...")
            print(f"{'='*80}\n")
            
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
            if hasattr(self, 'system_prompt') and self.system_prompt:
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
            result = await executor.ainvoke(
                agent_input,
                config={"callbacks": [callback_handler]}
            )
            
            # Extract output and intermediate steps
            output_text = result.get("output", str(result)) if isinstance(result, dict) else str(result)
            intermediate_steps = result.get("intermediate_steps", []) if isinstance(result, dict) else []
            
            print(f"\n[Agent] Agent execution complete:")
            print(f"[Agent] output_text length: {len(output_text)}")
            print(f"[Agent] intermediate_steps count: {len(intermediate_steps)}")
            
            # Persist the SharedProgressManager's message to database
            # This saves the tool calls that were added during execution
            # Note: If progress_finalizer.py is used, it will persist again with additional updates
            print(f"\n{'='*80}")
            print(f"[Agent {self.display_name}] PERSISTING PROGRESS MESSAGE TO DATABASE")
            
            if progress_manager:
                await progress_manager.persist_to_database()
                print(f"[Agent] ✓ SharedProgressManager message persisted with tool calls")
                print(f"[Agent] Note: If using progress_finalizer, it will persist again with final updates")
            else:
                print(f"[Agent] WARNING: No progress manager available")
            
            print(f"{'='*80}\n")
            
            # Create clean output message (JSON only) for downstream components
            response_message = Message(
                text=output_text,
                sender="Machine",
                sender_name=self.sender_name if hasattr(self, 'sender_name') else "AI",
                session_id=session_id or "",
                flow_id=self.graph.flow_id if hasattr(self, 'graph') else None,
            )
            print(f"[Agent] Created clean output message, length: {len(response_message.text)}")
            print(f"[Agent] Clean output message (for downstream): {response_message.text[:100]}...")
            print(f"{'='*80}\n")
            
            # Return combined result as Data
            return Data(data={
                "response": response_message,
                "progress_manager": self.progress_manager
            })
            
        except Exception as e:
            # Report error to progress manager
            if progress_manager:
                await progress_manager.add_update(
                    text=f"Agent execution failed: {str(e)}",
                    title="Error",
                    icon="AlertCircle",
                    error=True,
                    overwrite_previous=False
                )
            raise


# Made with Bob