"""MRP HTTP server for Bash runtime."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from sse_starlette.sse import EventSourceResponse
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .types import (
    Capabilities,
    Environment,
    Features,
    InputCancelledError,
    SessionInfo,
)
from .worker import BashWorker


def _json_response(data: Any, status_code: int = 200) -> JSONResponse:
    """Create a JSON response, converting dataclasses as needed."""
    if hasattr(data, "__dataclass_fields__"):
        data = asdict(data)
    return JSONResponse(data, status_code=status_code)


def _dataclass_to_dict(obj: Any) -> Any:
    """Recursively convert dataclasses to dicts."""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _dataclass_to_dict(v) for k, v in asdict(obj).items()}
    elif isinstance(obj, list):
        return [_dataclass_to_dict(item) for item in obj]
    elif isinstance(obj, dict):
        return {k: _dataclass_to_dict(v) for k, v in obj.items()}
    return obj


class SessionManager:
    """Manages bash sessions."""

    def __init__(self, cwd: str | None = None):
        self._sessions: dict[str, tuple[BashWorker, SessionInfo]] = {}
        self._default_cwd = cwd or os.getcwd()
        self._lock = threading.Lock()
        self._pending_inputs: dict[str, asyncio.Event] = {}
        self._input_values: dict[str, str] = {}

    def get_or_create_session(self, session_id: str = "default") -> tuple[BashWorker, SessionInfo]:
        """Get or create a session."""
        with self._lock:
            if session_id in self._sessions:
                worker, info = self._sessions[session_id]
                # Update last activity
                info.lastActivity = datetime.now(timezone.utc).isoformat()
                info.executionCount = worker.execution_count
                return worker, info

            # Create new session
            worker = BashWorker(cwd=self._default_cwd)
            info = SessionInfo(
                id=session_id,
                language="bash",
                created=datetime.now(timezone.utc).isoformat(),
                lastActivity=datetime.now(timezone.utc).isoformat(),
                executionCount=0,
                variableCount=0,
            )
            self._sessions[session_id] = (worker, info)
            return worker, info

    def get_session(self, session_id: str) -> tuple[BashWorker, SessionInfo] | None:
        """Get a session by ID."""
        return self._sessions.get(session_id)

    def list_sessions(self) -> list[SessionInfo]:
        """List all sessions."""
        sessions = []
        for worker, info in self._sessions.values():
            info.executionCount = worker.execution_count
            info.lastActivity = worker.last_activity.isoformat()
            sessions.append(info)
        return sessions

    def destroy_session(self, session_id: str) -> bool:
        """Destroy a session."""
        with self._lock:
            if session_id not in self._sessions:
                return False
            worker, _ = self._sessions.pop(session_id)
            worker.shutdown()
            return True

    def reset_session(self, session_id: str) -> bool:
        """Reset a session (clear state but keep session)."""
        with self._lock:
            if session_id not in self._sessions:
                return False
            worker, info = self._sessions[session_id]
            worker.reset()
            info.executionCount = 0
            info.variableCount = 0
            return True

    def register_pending_input(self, exec_id: str) -> asyncio.Event:
        """Register a pending input request."""
        event = asyncio.Event()
        self._pending_inputs[exec_id] = event
        return event

    def provide_input(self, exec_id: str, text: str) -> bool:
        """Provide input for a pending request."""
        if exec_id not in self._pending_inputs:
            return False
        self._input_values[exec_id] = text
        self._pending_inputs[exec_id].set()
        return True

    def get_input_value(self, exec_id: str) -> str | None:
        """Get and clear the input value for an exec_id."""
        return self._input_values.pop(exec_id, None)

    def cancel_input(self, exec_id: str) -> bool:
        """Cancel a pending input request."""
        if exec_id not in self._pending_inputs:
            return False
        # Signal cancellation by setting without value
        self._pending_inputs[exec_id].set()
        return True

    def clear_pending_input(self, exec_id: str) -> None:
        """Clear a pending input registration."""
        self._pending_inputs.pop(exec_id, None)
        self._input_values.pop(exec_id, None)

    def shutdown_all(self) -> None:
        """Shutdown all sessions."""
        with self._lock:
            for worker, _ in self._sessions.values():
                worker.shutdown()
            self._sessions.clear()


class MRPServer:
    """MRP HTTP server for Bash."""

    def __init__(self, cwd: str | None = None):
        self._cwd = cwd or os.getcwd()
        self._sessions = SessionManager(cwd=self._cwd)
        self._bash_version = self._get_bash_version()
        self._bash_path = shutil.which("bash") or "/bin/bash"

    def _get_bash_version(self) -> str:
        """Get the bash version."""
        try:
            result = subprocess.run(
                ["bash", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            # Parse version from "GNU bash, version X.Y.Z..."
            for line in result.stdout.split("\n"):
                if "version" in line.lower():
                    parts = line.split("version")
                    if len(parts) > 1:
                        version = parts[1].strip().split()[0]
                        return version
            return "unknown"
        except Exception:
            return "unknown"

    async def handle_capabilities(self, request: Request) -> JSONResponse:
        """GET /capabilities - Return runtime capabilities."""
        caps = Capabilities(
            runtime="bash",
            version=self._bash_version,
            languages=["bash", "sh", "shell"],
            features=Features(
                execute=True,
                executeStream=True,
                interrupt=True,
                complete=True,
                inspect=False,  # Bash has very limited inspection
                hover=True,
                variables=True,
                variableExpand=False,
                reset=True,
                isComplete=True,
                format=False,
                assets=False,
            ),
            lspFallback=None,
            defaultSession="default",
            maxSessions=10,
            environment=Environment(
                cwd=self._cwd,
                executable=self._bash_path,
                shell=self._bash_path,
            ),
        )
        return _json_response(caps)

    async def handle_list_sessions(self, request: Request) -> JSONResponse:
        """GET /sessions - List all sessions."""
        sessions = self._sessions.list_sessions()
        return _json_response({"sessions": [_dataclass_to_dict(s) for s in sessions]})

    async def handle_create_session(self, request: Request) -> JSONResponse:
        """POST /sessions - Create a new session."""
        try:
            body = await request.json()
        except Exception:
            body = {}

        session_id = body.get("id", f"session-{datetime.now().timestamp()}")

        # Check if session already exists
        existing = self._sessions.get_session(session_id)
        if existing:
            return _json_response(
                {"error": f"Session {session_id} already exists"},
                status_code=409,
            )

        _, info = self._sessions.get_or_create_session(session_id)
        return _json_response(info, status_code=201)

    async def handle_get_session(self, request: Request) -> JSONResponse:
        """GET /sessions/{id} - Get session details."""
        session_id = request.path_params["id"]
        session = self._sessions.get_session(session_id)

        if not session:
            return _json_response(
                {"error": f"Session {session_id} not found"},
                status_code=404,
            )

        worker, info = session
        info.executionCount = worker.execution_count
        return _json_response(info)

    async def handle_delete_session(self, request: Request) -> JSONResponse:
        """DELETE /sessions/{id} - Delete a session."""
        session_id = request.path_params["id"]

        if self._sessions.destroy_session(session_id):
            return _json_response({"deleted": True})
        else:
            return _json_response(
                {"error": f"Session {session_id} not found"},
                status_code=404,
            )

    async def handle_reset_session(self, request: Request) -> JSONResponse:
        """POST /sessions/{id}/reset - Reset a session."""
        session_id = request.path_params["id"]

        if self._sessions.reset_session(session_id):
            return _json_response({"reset": True})
        else:
            return _json_response(
                {"error": f"Session {session_id} not found"},
                status_code=404,
            )

    async def handle_execute(self, request: Request) -> JSONResponse:
        """POST /execute - Execute code and return result."""
        body = await request.json()

        code = body.get("code", "")
        session_id = body.get("session", "default")
        store_history = body.get("storeHistory", True)
        exec_id = body.get("execId")

        worker, _ = self._sessions.get_or_create_session(session_id)

        # Run execution in thread pool to not block
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: worker.execute(code, store_history, exec_id),
        )

        return _json_response(result)

    async def handle_execute_stream(self, request: Request) -> EventSourceResponse:
        """POST /execute/stream - Execute code with SSE streaming."""
        body = await request.json()

        code = body.get("code", "")
        session_id = body.get("session", "default")
        store_history = body.get("storeHistory", True)
        exec_id = body.get("execId", f"exec-{datetime.now().timestamp()}")

        worker, _ = self._sessions.get_or_create_session(session_id)

        async def event_generator():
            loop = asyncio.get_running_loop()
            output_queue: asyncio.Queue = asyncio.Queue()
            execution_done = asyncio.Event()

            def on_output(stream: str, chunk: str, accumulated: str):
                """Callback for output chunks."""
                asyncio.run_coroutine_threadsafe(
                    output_queue.put(
                        {
                            "type": stream,
                            "data": {
                                "content": chunk,
                                "accumulated": accumulated,
                            },
                        }
                    ),
                    loop,
                )

            def on_stdin_request(req):
                """Callback for stdin requests - BLOCKS until input is provided.

                This mirrors the Python server's behavior:
                1. Send stdin_request event to client via SSE
                2. Register pending input with session manager
                3. Block waiting for POST /input to provide the text
                4. Return the input text to the worker
                """
                # Send stdin_request event to client
                asyncio.run_coroutine_threadsafe(
                    output_queue.put(
                        {
                            "type": "stdin_request",
                            "data": {
                                "prompt": req.prompt,
                                "password": req.password,
                                "execId": req.exec_id,
                            },
                        }
                    ),
                    loop,
                )

                # Register that we're waiting for input
                input_event = self._sessions.register_pending_input(exec_id)

                # Wait for the input (blocking - we're in a worker thread)
                async def wait_for_input():
                    await input_event.wait()
                    # Check if input was provided or cancelled
                    value = self._sessions.get_input_value(exec_id)
                    if value is None:
                        # Input was cancelled
                        raise InputCancelledError("Input cancelled by user")
                    return value

                try:
                    # Wait up to 5 minutes for input
                    future = asyncio.run_coroutine_threadsafe(wait_for_input(), loop)
                    response = future.result(timeout=300)
                    return response
                except InputCancelledError:
                    raise
                except Exception as e:
                    raise RuntimeError(f"Failed to get input: {e}")
                finally:
                    # Clean up
                    self._sessions.clear_pending_input(exec_id)

            def run_execution():
                """Run execution in background thread."""
                try:
                    result = worker.execute_streaming(
                        code,
                        store_history,
                        exec_id,
                        on_output,
                        on_stdin_request,
                    )
                    asyncio.run_coroutine_threadsafe(
                        output_queue.put({"type": "result", "data": result}),
                        loop,
                    )
                except Exception as e:
                    asyncio.run_coroutine_threadsafe(
                        output_queue.put(
                            {
                                "type": "error",
                                "data": {
                                    "type": type(e).__name__,
                                    "message": str(e),
                                },
                            }
                        ),
                        loop,
                    )
                finally:
                    asyncio.run_coroutine_threadsafe(
                        output_queue.put({"type": "done", "data": {}}),
                        loop,
                    )
                    execution_done.set()

            # Start execution in background thread
            exec_thread = threading.Thread(target=run_execution, daemon=True)
            exec_thread.start()

            # Yield start event
            yield {
                "event": "start",
                "data": json.dumps(
                    {
                        "execId": exec_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                ),
            }

            # Stream events
            while True:
                try:
                    item = await asyncio.wait_for(output_queue.get(), timeout=60.0)

                    event_type = item["type"]
                    event_data = item["data"]

                    if event_type == "done":
                        yield {"event": "done", "data": "{}"}
                        break
                    elif event_type == "result":
                        yield {
                            "event": "result",
                            "data": json.dumps(_dataclass_to_dict(event_data)),
                        }
                    elif event_type == "error":
                        yield {"event": "error", "data": json.dumps(event_data)}
                    elif event_type == "stdin_request":
                        yield {"event": "stdin_request", "data": json.dumps(event_data)}
                    else:
                        yield {"event": event_type, "data": json.dumps(event_data)}

                except asyncio.TimeoutError:
                    # Send ping to keep connection alive
                    yield {"event": "ping", "data": "{}"}

        return EventSourceResponse(event_generator())

    async def handle_input(self, request: Request) -> JSONResponse:
        """POST /input - Send user input to waiting execution."""
        body = await request.json()

        session_id = body.get("session", "default")
        exec_id = body.get("exec_id")
        text = body.get("text", "")

        if not exec_id:
            return _json_response(
                {"accepted": False, "error": "exec_id is required"},
                status_code=400,
            )

        # Provide input to the pending request - this signals the blocked callback
        accepted = self._sessions.provide_input(exec_id, text)

        return _json_response({"accepted": accepted})

    async def handle_input_cancel(self, request: Request) -> JSONResponse:
        """POST /input/cancel - Cancel pending input request."""
        body = await request.json()

        session_id = body.get("session", "default")
        exec_id = body.get("exec_id")

        if not exec_id:
            return _json_response(
                {"cancelled": False, "error": "exec_id is required"},
                status_code=400,
            )

        # Cancel the pending input - this signals the blocked callback with no value
        cancelled = self._sessions.cancel_input(exec_id)

        return _json_response({"cancelled": cancelled})

    async def handle_interrupt(self, request: Request) -> JSONResponse:
        """POST /interrupt - Interrupt running execution."""
        body = await request.json()
        session_id = body.get("session", "default")

        session = self._sessions.get_session(session_id)
        if not session:
            return _json_response(
                {"interrupted": False, "error": "Session not found"},
                status_code=404,
            )

        worker, _ = session
        interrupted = worker.interrupt()

        return _json_response({"interrupted": interrupted})

    async def handle_complete(self, request: Request) -> JSONResponse:
        """POST /complete - Get completions at cursor position."""
        body = await request.json()

        code = body.get("code", "")
        cursor = body.get("cursor", len(code))
        session_id = body.get("session", "default")

        worker, _ = self._sessions.get_or_create_session(session_id)

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: worker.complete(code, cursor),
        )

        return _json_response(result)

    async def handle_inspect(self, request: Request) -> JSONResponse:
        """POST /inspect - Get detailed info about symbol."""
        # Bash has limited inspection capabilities
        return _json_response(
            {
                "found": False,
                "source": "runtime",
                "error": "Inspection not supported for bash",
            }
        )

    async def handle_hover(self, request: Request) -> JSONResponse:
        """POST /hover - Get quick tooltip for symbol."""
        body = await request.json()

        code = body.get("code", "")
        cursor = body.get("cursor", len(code))
        session_id = body.get("session", "default")

        worker, _ = self._sessions.get_or_create_session(session_id)

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: worker.hover(code, cursor),
        )

        return _json_response(result)

    async def handle_variables(self, request: Request) -> JSONResponse:
        """POST /variables - List session variables."""
        body = await request.json()

        session_id = body.get("session", "default")
        filter_config = body.get("filter", {})
        name_pattern = filter_config.get("namePattern")

        worker, _ = self._sessions.get_or_create_session(session_id)

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: worker.get_variables(name_pattern),
        )

        return _json_response(result)

    async def handle_variable_detail(self, request: Request) -> JSONResponse:
        """POST /variables/{name} - Get variable details."""
        name = request.path_params["name"]
        body = await request.json()

        session_id = body.get("session", "default")
        path = body.get("path", [])

        worker, _ = self._sessions.get_or_create_session(session_id)

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: worker.get_variable_detail(name, path),
        )

        return _json_response(result)

    async def handle_is_complete(self, request: Request) -> JSONResponse:
        """POST /is_complete - Check if code is complete."""
        body = await request.json()

        code = body.get("code", "")
        session_id = body.get("session", "default")

        worker, _ = self._sessions.get_or_create_session(session_id)
        result = worker.is_complete(code)

        return _json_response(result)

    async def handle_format(self, request: Request) -> JSONResponse:
        """POST /format - Format code (not supported for bash)."""
        body = await request.json()
        code = body.get("code", "")

        return _json_response(
            {
                "formatted": code,
                "changed": False,
                "error": "Formatting not supported for bash",
            }
        )

    async def handle_assets(self, request: Request) -> Response:
        """GET /assets/{path} - Serve saved assets (not supported for bash)."""
        return Response(
            content="Assets not supported for bash runtime",
            status_code=404,
            media_type="text/plain",
        )

    def create_app(self) -> Starlette:
        """Create the Starlette application."""
        routes = [
            # Capabilities
            Route("/mrp/v1/capabilities", self.handle_capabilities, methods=["GET"]),
            # Sessions
            Route("/mrp/v1/sessions", self.handle_list_sessions, methods=["GET"]),
            Route("/mrp/v1/sessions", self.handle_create_session, methods=["POST"]),
            Route("/mrp/v1/sessions/{id}", self.handle_get_session, methods=["GET"]),
            Route("/mrp/v1/sessions/{id}", self.handle_delete_session, methods=["DELETE"]),
            Route("/mrp/v1/sessions/{id}/reset", self.handle_reset_session, methods=["POST"]),
            # Execution
            Route("/mrp/v1/execute", self.handle_execute, methods=["POST"]),
            Route("/mrp/v1/execute/stream", self.handle_execute_stream, methods=["POST"]),
            # Input
            Route("/mrp/v1/input", self.handle_input, methods=["POST"]),
            Route("/mrp/v1/input/cancel", self.handle_input_cancel, methods=["POST"]),
            # Interrupt
            Route("/mrp/v1/interrupt", self.handle_interrupt, methods=["POST"]),
            # Completion & Introspection
            Route("/mrp/v1/complete", self.handle_complete, methods=["POST"]),
            Route("/mrp/v1/inspect", self.handle_inspect, methods=["POST"]),
            Route("/mrp/v1/hover", self.handle_hover, methods=["POST"]),
            # Variables
            Route("/mrp/v1/variables", self.handle_variables, methods=["POST"]),
            Route("/mrp/v1/variables/{name}", self.handle_variable_detail, methods=["POST"]),
            # Code Analysis
            Route("/mrp/v1/is_complete", self.handle_is_complete, methods=["POST"]),
            Route("/mrp/v1/format", self.handle_format, methods=["POST"]),
            # Assets
            Route("/mrp/v1/assets/{path:path}", self.handle_assets, methods=["GET"]),
        ]

        app = Starlette(routes=routes, on_shutdown=[self._on_shutdown])

        # Add CORS middleware
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        return app

    async def _on_shutdown(self) -> None:
        """Cleanup on shutdown."""
        self._sessions.shutdown_all()


def create_app(cwd: str | None = None) -> Starlette:
    """Create the MRP server application.

    Args:
        cwd: Working directory for bash sessions. If not provided,
             reads from MRMD_BASH_CWD environment variable.

    Returns:
        Starlette application
    """
    # Allow CLI to pass CWD via environment variable
    if cwd is None:
        cwd = os.environ.get("MRMD_BASH_CWD")

    server = MRPServer(cwd=cwd)
    return server.create_app()
