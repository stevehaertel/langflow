"""
Progress Finalizer - Completes a shared progressive message and outputs it to Chat.

This component should be the last in your chain when using SharedProgressManager.
"""

from lfx.custom.custom_component.component import Component
from lfx.inputs.inputs import BoolInput, DataInput, MessageTextInput
from lfx.schema.data import Data
from lfx.schema.message import Message
from lfx.template.field.base import Output


class ProgressFinalizer(Component):
    """
    Finalizes a shared progressive message and outputs it for display.
    
    Usage:
    Place this at the end of your flow chain after all components that
    add updates to the shared progress manager.
    """
    
    display_name = "Progress Finalizer"
    description = "Finalizes and outputs a shared progressive message."
    icon = "CheckCircle2"
    name = "ProgressFinalizer"
    
    output_types: list[str] = ["Message"]
    
    inputs = [
        DataInput(
            name="progress_manager",
            display_name="Progress Manager",
            info="Shared Progress Manager from upstream components.",
            required=True,
        ),
        MessageTextInput(
            name="final_message",
            display_name="Final Message",
            info="Optional final message to add before completing.",
            value="All operations completed successfully",
            required=False,
        ),
        MessageTextInput(
            name="custom_title",
            display_name="Custom Title",
            info="Optional custom title for the final update. Defaults to 'Complete'.",
            value="",
            required=False,
        ),
        MessageTextInput(
            name="block_title",
            display_name="Block Title",
            info="Optional custom title for the message block header (replaces 'Finished'). If not provided, uses 'Finished'.",
            value="",
            required=False,
        ),
        BoolInput(
            name="overwrite_previous",
            display_name="Overwrite Previous",
            info="If True, replaces the previous message block instead of appending a new one.",
            value=False,
            required=False,
        ),
    ]
    
    outputs = [
        Output(
            display_name="Final Message",
            name="message",
            method="finalize_and_output",
        ),
    ]
    
    async def finalize_and_output(self) -> Message:
        """Finalize the shared message and return it for display."""
        # Get the manager from the input Data
        manager_data = self.progress_manager
        if not isinstance(manager_data, Data):
            raise TypeError(f"Expected Data object with manager, got {type(manager_data)}")
        
        manager = manager_data.data.get("manager")
        if manager is None:
            raise ValueError("No manager found in progress_manager input")
        
        # Get final message, custom title, block title, and overwrite flag
        final_text = self.final_message if hasattr(self, 'final_message') else "Complete"
        custom_title = self.custom_title if hasattr(self, 'custom_title') and self.custom_title else None
        block_title = self.block_title if hasattr(self, 'block_title') and self.block_title else None
        overwrite = self.overwrite_previous if hasattr(self, 'overwrite_previous') else False
        
        # Add final update with custom title if provided
        try:
            if custom_title:
                await manager.add_update(
                    final_text,
                    title=custom_title,
                    icon="CheckCircle2",
                    overwrite_previous=overwrite
                )
                final_message = await manager.finalize(final_text=None, block_title=block_title)
            else:
                final_message = await manager.finalize(final_text, block_title=block_title)
            
            # Persist the final message state to database
            # This ensures all updates (including those from progress_update_injector) are saved
            await manager.persist_to_database()
            
            self.status = "✓ Progress finalized and persisted"
            return final_message
            
        except Exception as e:
            raise

# Made with Bob