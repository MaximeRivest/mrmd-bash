"""mrmd-bash: MRP (MRMD Runtime Protocol) server for Bash.

A simple HTTP server that provides runtime code execution for Bash,
implementing the MRP protocol for notebook-style editors.
"""

from .server import MRPServer, create_app
from .types import (
    Capabilities,
    CompletionItem,
    CompleteResult,
    Environment,
    ExecuteError,
    ExecuteResult,
    Features,
    HoverResult,
    InspectResult,
    IsCompleteResult,
    SessionInfo,
    Variable,
    VariableDetail,
    VariablesResult,
)
from .worker import BashWorker

__version__ = "0.1.0"

__all__ = [
    # Server
    "MRPServer",
    "create_app",
    # Worker
    "BashWorker",
    # Types
    "Capabilities",
    "CompletionItem",
    "CompleteResult",
    "Environment",
    "ExecuteError",
    "ExecuteResult",
    "Features",
    "HoverResult",
    "InspectResult",
    "IsCompleteResult",
    "SessionInfo",
    "Variable",
    "VariableDetail",
    "VariablesResult",
]
