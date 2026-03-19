"""Progress Update Injector - Adds a message to the shared progress manager.

This component allows you to inject updates into a shared progress message
from parallel paths in your flow.
"""

from lfx.custom.custom_component.component import Component
from lfx.inputs.inputs import BoolInput, DataInput, HandleInput, MessageTextInput
from lfx.schema.data import Data
from lfx.schema.message import Message
from lfx.template.field.base import Output


class ProgressUpdateInjector(Component):
    """Injects a status update into a shared progress manager.

    This component takes a message and adds it to the shared progress,
    allowing parallel flow paths to contribute to the same message block.
    """

    display_name = "Progress Update Injector"
    description = "Adds a status update to a shared progress message."
    icon = "Plus"
    name = "ProgressUpdateInjector"

    inputs = [
        DataInput(
            name="progress_manager",
            display_name="Progress Manager",
            info="Shared Progress Manager from SharedProgressManager component.",
            required=True,
        ),
        HandleInput(
            name="message_input",
            display_name="Message to Add",
            info="Message or text to add to the progress. Can be output from any component.",
            input_types=["Message", "Data", "str"],
            required=True,
        ),
        MessageTextInput(
            name="update_title",
            display_name="Update Title",
            info="Title for this update (e.g., 'Data Extraction', 'Validation')",
            value="Update",
            required=False,
        ),
        MessageTextInput(
            name="update_icon",
            display_name="Update Icon",
            info="Icon name for this update (e.g., 'CheckCircle', 'AlertCircle', 'Loader')",
            value="CheckCircle",
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
            display_name="Progress Manager",
            name="progress_manager_output",
            method="inject_update",
        ),
    ]

    async def inject_update(self) -> Data:
        """Inject the message into the shared progress and pass the manager along."""
        # Get the manager from the input Data
        manager_data = self.progress_manager
        if not isinstance(manager_data, Data):
            raise TypeError(f"Expected Data object with manager, got {type(manager_data)}")

        manager = manager_data.data.get("manager")
        if manager is None:
            raise ValueError("No manager found in progress_manager input")

        # Extract text from the message input
        message_text = ""
        if isinstance(self.message_input, Message):
            message_text = self.message_input.text or str(self.message_input)
        elif isinstance(self.message_input, Data):
            # Try to extract text from Data
            data_dict = self.message_input.data
            if isinstance(data_dict, dict):
                message_text = str(data_dict.get("text", data_dict))
            else:
                message_text = str(data_dict)
        elif isinstance(self.message_input, str):
            message_text = self.message_input
        else:
            message_text = str(self.message_input)

        # Get title, icon, and overwrite flag
        title = self.update_title if hasattr(self, "update_title") else "Update"
        icon = self.update_icon if hasattr(self, "update_icon") else "CheckCircle"
        overwrite = self.overwrite_previous if hasattr(self, "overwrite_previous") else False

        # Add the update to the shared progress
        await manager.add_update(text=message_text, title=title, icon=icon, error=False, overwrite_previous=overwrite)

        self.status = f"✓ Added update: {title}"

        # Pass the manager to the next component
        return manager_data


# Made with Bob
