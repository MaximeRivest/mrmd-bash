"""Type definitions for MRP (MRMD Runtime Protocol) - Bash implementation."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExecuteError:
    """Error information from failed execution."""

    type: str
    message: str
    traceback: list[str]
    line: int | None = None
    column: int | None = None


@dataclass
class DisplayData:
    """Rich display output."""

    data: dict[str, str]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Asset:
    """File-based asset created during execution."""

    path: str
    url: str
    mimeType: str
    assetType: str
    size: int | None = None


@dataclass
class ExecuteResult:
    """Result of code execution."""

    success: bool
    stdout: str
    stderr: str
    result: str | None
    error: ExecuteError | None
    displayData: list[DisplayData]
    assets: list[Asset]
    executionCount: int
    duration: int  # milliseconds


@dataclass
class CompletionItem:
    """Single completion suggestion."""

    label: str
    insertText: str | None = None
    kind: str = "text"
    detail: str | None = None
    documentation: str | None = None
    valuePreview: str | None = None
    type: str | None = None


@dataclass
class CompleteResult:
    """Result of completion request."""

    matches: list[CompletionItem]
    cursorStart: int
    cursorEnd: int
    source: str = "runtime"


@dataclass
class InspectResult:
    """Result of inspection request."""

    found: bool
    source: str = "runtime"
    name: str | None = None
    kind: str | None = None
    type: str | None = None
    signature: str | None = None
    docstring: str | None = None
    sourceCode: str | None = None
    file: str | None = None
    line: int | None = None
    value: str | None = None
    children: list[dict] | None = None


@dataclass
class HoverResult:
    """Result of hover request."""

    found: bool
    name: str | None = None
    type: str | None = None
    value: str | None = None
    signature: str | None = None


@dataclass
class Variable:
    """Variable information."""

    name: str
    type: str
    value: str
    size: str | None = None
    expandable: bool = False
    shape: list[int] | None = None
    dtype: str | None = None
    length: int | None = None
    keys: list[str] | None = None


@dataclass
class VariablesResult:
    """Result of variables list request."""

    variables: list[Variable]
    count: int
    truncated: bool = False


@dataclass
class VariableDetail:
    """Detailed variable information with children."""

    name: str
    type: str
    value: str
    size: str | None = None
    expandable: bool = False
    length: int | None = None
    fullValue: str | None = None
    children: list[Variable] | None = None
    methods: list[str] | None = None
    attributes: list[str] | None = None
    truncated: bool = False


@dataclass
class IsCompleteResult:
    """Result of is_complete check."""

    status: str  # "complete", "incomplete", "invalid", "unknown"
    indent: str = ""


@dataclass
class SessionInfo:
    """Information about a session."""

    id: str
    language: str
    created: str  # ISO8601 timestamp
    lastActivity: str  # ISO8601 timestamp
    executionCount: int
    variableCount: int


@dataclass
class Environment:
    """Runtime environment information."""

    cwd: str
    executable: str
    shell: str | None = None


@dataclass
class Features:
    """Runtime feature flags."""

    execute: bool = True
    executeStream: bool = True
    interrupt: bool = True
    complete: bool = True
    inspect: bool = False  # Bash has limited inspection
    hover: bool = True
    variables: bool = True
    variableExpand: bool = False  # Bash variables are flat
    reset: bool = True
    isComplete: bool = True
    format: bool = False  # No formatter for bash
    assets: bool = False  # Bash doesn't generate assets


@dataclass
class Capabilities:
    """Runtime capabilities response."""

    runtime: str
    version: str
    languages: list[str]
    features: Features
    lspFallback: str | None = None
    defaultSession: str = "default"
    maxSessions: int = 10
    environment: Environment | None = None


@dataclass
class StdinRequest:
    """Request for user input."""

    prompt: str
    password: bool
    exec_id: str


class InputCancelledError(Exception):
    """Raised when input is cancelled by the user."""

    pass
