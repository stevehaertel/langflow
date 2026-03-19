from __future__ import annotations

from typing import TYPE_CHECKING, Any

from lfx.components._importing import import_mod

if TYPE_CHECKING:
    from lfx.components.realtimeupdates.agent_result_extractor import AgentResultExtractor
    from lfx.components.realtimeupdates.agent_with_progress import AgentWithProgress
    from lfx.components.realtimeupdates.agent_with_progress_single_output import AgentWithProgressSingleOutput
    from lfx.components.realtimeupdates.progress_finalizer import ProgressFinalizer
    from lfx.components.realtimeupdates.progress_update_injector import ProgressUpdateInjector
    from lfx.components.realtimeupdates.shared_progress_manager import SharedProgressManager

_dynamic_imports = {
    "AgentResultExtractor": "agent_result_extractor",
    "AgentWithProgress": "agent_with_progress",
    "AgentWithProgressSingleOutput": "agent_with_progress_single_output",
    "ProgressFinalizer": "progress_finalizer",
    "ProgressUpdateInjector": "progress_update_injector",
    "SharedProgressManager": "shared_progress_manager",
}

__all__ = [
    "AgentResultExtractor",
    "AgentWithProgress",
    "AgentWithProgressSingleOutput",
    "ProgressFinalizer",
    "ProgressUpdateInjector",
    "SharedProgressManager",
]


def __getattr__(attr_name: str) -> Any:
    """Lazily import real-time updates components on attribute access."""
    if attr_name not in _dynamic_imports:
        msg = f"module '{__name__}' has no attribute '{attr_name}'"
        raise AttributeError(msg)
    try:
        result = import_mod(attr_name, _dynamic_imports[attr_name], __spec__.parent)
    except (ModuleNotFoundError, ImportError, AttributeError) as e:
        msg = f"Could not import '{attr_name}' from '{__name__}': {e}"
        raise AttributeError(msg) from e
    globals()[attr_name] = result
    return result


def __dir__() -> list[str]:
    return list(__all__)


# Made with Bob
