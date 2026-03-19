"""Agent Result Extractor - Splits combined agent result into separate outputs.

This component extracts the response and progress_manager from the combined
Data object returned by AgentWithProgressSingleOutput.
"""

from lfx.custom.custom_component.component import Component
from lfx.inputs.inputs import HandleInput
from lfx.schema.data import Data
from lfx.schema.message import Message
from lfx.template.field.base import Output


class AgentResultExtractor(Component):
    """Extracts response and progress_manager from combined agent result.

    This component takes the single Data output from AgentWithProgressSingleOutput
    and splits it into two separate outputs for use in the flow.
    """

    display_name = "Agent Result Extractor"
    description = "Extracts response and progress manager from combined agent result."
    icon = "Split"
    name = "AgentResultExtractor"

    output_types: list[str] = ["Message", "Data"]

    inputs = [
        HandleInput(
            name="agent_result",
            display_name="Agent Result",
            info="Combined result from AgentWithProgressSingleOutput containing response and progress_manager.",
            input_types=["Data", "JSON"],
            required=True,
        ),
    ]

    outputs = [
        Output(
            display_name="Response",
            name="response",
            method="get_response",
            group_outputs=True,
        ),
        Output(
            display_name="Progress Manager",
            name="progress_manager",
            method="get_progress_manager",
            group_outputs=True,
        ),
    ]

    def get_response(self) -> Message:
        """Extract and return the response Message."""
        if not isinstance(self.agent_result, Data):
            raise TypeError(f"Expected Data object, got {type(self.agent_result)}")

        result_data = self.agent_result.data
        if not isinstance(result_data, dict):
            raise TypeError(f"Expected dict in Data.data, got {type(result_data)}")

        response = result_data.get("response")
        if response is None:
            raise ValueError("No 'response' found in agent_result")

        if not isinstance(response, Message):
            raise TypeError(f"Expected Message for response, got {type(response)}")

        self.status = "✓ Response extracted"
        return response

    def get_progress_manager(self) -> Data:
        """Extract and return the progress manager Data."""
        if not isinstance(self.agent_result, Data):
            raise TypeError(f"Expected Data object, got {type(self.agent_result)}")

        result_data = self.agent_result.data
        if not isinstance(result_data, dict):
            raise TypeError(f"Expected dict in Data.data, got {type(result_data)}")

        progress_manager = result_data.get("progress_manager")

        # Progress manager can be None if not provided to the agent
        if progress_manager is None:
            self.status = "⚠ No progress manager"
            return Data(data={})

        if not isinstance(progress_manager, Data):
            raise TypeError(f"Expected Data for progress_manager, got {type(progress_manager)}")

        self.status = "✓ Progress manager extracted"
        return progress_manager


# Made with Bob
