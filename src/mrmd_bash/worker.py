"""Bash execution worker for MRP."""

from __future__ import annotations

print("[bash-worker] Loading worker module (stdin detection enabled)", flush=True)

import os
import pty
import re
import select
import shlex
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from .types import (
    CompletionItem,
    CompleteResult,
    ExecuteError,
    ExecuteResult,
    HoverResult,
    InputCancelledError,
    IsCompleteResult,
    StdinRequest,
    Variable,
    VariableDetail,
    VariablesResult,
)

# Marker for command completion detection
_END_MARKER = "__MRMD_END_MARKER__"
_EXIT_CODE_MARKER = "__MRMD_EXIT_CODE__"

# ANSI escape code pattern
_ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _strip_ansi(text: str) -> str:
    """Strip ANSI escape codes from text."""
    return _ANSI_ESCAPE_PATTERN.sub("", text)


class BashWorker:
    """Persistent Bash session worker.

    Maintains a long-running bash process with PTY for proper terminal handling.
    Environment variables persist between executions.
    """

    def __init__(self, cwd: str | None = None, env: dict[str, str] | None = None):
        """Initialize the Bash worker.

        Args:
            cwd: Working directory for the shell. Defaults to current directory.
            env: Additional environment variables to set.
        """
        self._cwd = cwd or os.getcwd()
        self._extra_env = env or {}
        self._execution_count = 0
        self._created = datetime.now(timezone.utc)
        self._last_activity = self._created

        # Process state
        self._process: subprocess.Popen | None = None
        self._master_fd: int | None = None
        self._lock = threading.Lock()

        # Input handling
        self._pending_input: threading.Event | None = None
        self._input_value: str | None = None
        self._input_cancelled = False

        # Find bash executable
        self._bash_path = shutil.which("bash") or "/bin/bash"

    def _build_clean_env(self) -> dict[str, str]:
        """Build a clean environment like a normal terminal session.

        Instead of inheriting everything from Electron (which sets XDG vars,
        Electron-specific vars, etc.), we build a minimal environment with
        only the essential variables a shell needs.

        This makes the shell behave like opening a fresh terminal.
        """
        # Essential variables to preserve from the current environment
        # These are what a normal terminal session would have
        essential_vars = {
            # User identity
            "HOME",
            "USER",
            "LOGNAME",
            "SHELL",
            # Locale
            "LANG",
            "LANGUAGE",
            "LC_ALL",
            "LC_CTYPE",
            "LC_MESSAGES",
            "LC_NUMERIC",
            "LC_TIME",
            "LC_COLLATE",
            # Display (needed for GUI apps launched from shell)
            "DISPLAY",
            "WAYLAND_DISPLAY",
            # Session integration
            "DBUS_SESSION_BUS_ADDRESS",
            "SSH_AUTH_SOCK",
            "SSH_AGENT_PID",
            "GPG_AGENT_INFO",
            # Editor preferences
            "EDITOR",
            "VISUAL",
            # Pager
            "PAGER",
            # Temp directory
            "TMPDIR",
            # Timezone
            "TZ",
        }

        env = {}

        # Copy only essential variables
        for var in essential_vars:
            if var in os.environ:
                env[var] = os.environ[var]

        # PATH needs special handling - use system PATH, not Electron's modified one
        # Try to get a clean PATH by checking common locations
        env["PATH"] = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")

        # TERM for proper terminal behavior
        env["TERM"] = "xterm-256color"

        return env

    def _ensure_started(self) -> None:
        """Ensure the bash process is running."""
        if self._process is not None and self._process.poll() is None:
            return

        # Create PTY for proper terminal handling
        self._master_fd, slave_fd = pty.openpty()

        # Set up a clean environment like a normal terminal
        # Don't inherit Electron's environment (XDG vars, etc.) which can confuse tools
        env = self._build_clean_env()
        env.update(self._extra_env)

        # Start bash as login interactive shell
        # -l (login): sources /etc/profile, ~/.bash_profile or ~/.profile
        # -i (interactive): enables job control, aliases, etc.
        # This matches what terminal emulators do and works on both Linux and macOS
        # (macOS users typically have config in .bash_profile, Linux in .bashrc,
        # and .bash_profile usually sources .bashrc if it exists)
        self._process = subprocess.Popen(
            [self._bash_path, "-l", "-i"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=self._cwd,
            env=env,
            start_new_session=True,
        )

        os.close(slave_fd)

        # Wait for shell to initialize and source .bashrc
        time.sleep(0.15)

        # Consume any initial output (including user's prompt)
        self._read_available(timeout=0.3)

        # Now override prompts AFTER .bashrc has been sourced
        # This ensures clean output parsing regardless of user's PS1
        self._write_command("PS1='' PS2='' PS3='' PS4=''")
        time.sleep(0.05)
        self._read_available(timeout=0.1)

        # Disable bracketed paste mode which adds escape sequences
        self._write_command("bind 'set enable-bracketed-paste off' 2>/dev/null || true")
        time.sleep(0.05)
        self._read_available(timeout=0.1)

    def _read_available(self, timeout: float = 0.1) -> str:
        """Read all available output from the process."""
        if self._master_fd is None:
            return ""

        output = []
        while True:
            ready, _, _ = select.select([self._master_fd], [], [], timeout)
            if not ready:
                break
            try:
                data = os.read(self._master_fd, 4096)
                if data:
                    output.append(data.decode("utf-8", errors="replace"))
                else:
                    break
            except OSError:
                break
            timeout = 0.05  # Shorter timeout for subsequent reads

        return "".join(output)

    def _write_command(self, cmd: str) -> None:
        """Write a command to the bash process."""
        if self._master_fd is None:
            raise RuntimeError("Bash process not started")
        os.write(self._master_fd, (cmd + "\n").encode("utf-8"))

    def _read_until_marker(
        self,
        code: str,
        end_marker: str,
        exit_marker: str,
        exec_id: str | None = None,
        on_output: Callable[[str, str, str], None] | None = None,
        on_stdin_request: Callable[[StdinRequest], str] | None = None,
    ) -> tuple[str, int]:
        """Read output until the end marker is found.

        Args:
            code: The user code being executed (used to filter echoed commands)
            end_marker: Unique marker indicating end of output
            exit_marker: Unique marker for exit code
            exec_id: Execution ID for stdin requests
            on_output: Callback for streaming output
            on_stdin_request: Callback for stdin requests - BLOCKS and returns input string

        Returns:
            Tuple of (output, exit_code)
        """
        if self._master_fd is None:
            raise RuntimeError("Bash process not started")

        output_parts: list[str] = []
        accumulated = ""

        # Stdin detection state
        idle_count = 0  # Count of consecutive idle iterations (no output)
        IDLE_THRESHOLD = 5  # After 5 iterations (0.5s) of no output, check for stdin need

        # Common prompt patterns that indicate stdin is needed
        # These patterns at end of output suggest a command is waiting for input
        PROMPT_PATTERNS = (
            ': ', '? ', '> ', '] ', ') ',  # Common prompt endings with space
            ':', '?',  # Without trailing space
            'password:', 'Password:', 'PASSWORD:',
            '[y/n]', '[Y/n]', '[y/N]', '[Y/N]',
            '(yes/no)', '(y/n)',
        )

        # Get code lines to filter echoed commands (PTY echoes back what we send)
        code_lines_set = set(line.strip() for line in code.strip().split("\n") if line.strip())

        while True:
            ready, _, _ = select.select([self._master_fd], [], [], 0.1)

            # Check if input was cancelled
            if self._input_cancelled:
                self._input_cancelled = False
                raise InputCancelledError("Input cancelled by user")

            if ready:
                # Reset idle counter when we receive data
                idle_count = 0
                try:
                    data = os.read(self._master_fd, 4096)
                    if not data:
                        break
                    chunk = data.decode("utf-8", errors="replace")
                    output_parts.append(chunk)
                    accumulated = "".join(output_parts)

                    # Call output callback with filtered chunk
                    # Filter out: markers, echo commands for markers, and echoed user commands
                    if on_output:
                        lines = chunk.split("\n")
                        filtered_lines = []
                        for line in lines:
                            stripped = line.strip()
                            # Skip marker output lines
                            if "__MRMD_EXIT_CODE__" in line or "__MRMD_END_MARKER__" in line:
                                continue
                            # Skip echo commands for markers
                            if stripped.startswith("echo __MRMD_"):
                                continue
                            # Skip our wrapper command prefix (eval wrapper)
                            if stripped.startswith("__exit_code__="):
                                continue
                            # Skip echoed user commands (PTY echo)
                            if stripped in code_lines_set:
                                continue
                            filtered_lines.append(line)
                        filtered_chunk = "\n".join(filtered_lines)
                        if filtered_chunk.strip():
                            on_output("stdout", filtered_chunk, accumulated)

                    # Check for our unique end marker - must be at START of a line
                    # (not embedded in the echoed command which also contains the marker)
                    if f"\n{end_marker}" in accumulated:
                        break

                except OSError:
                    break
            else:
                # No data available - check if we should request stdin
                idle_count += 1

                # Debug: Log idle state periodically
                if idle_count == IDLE_THRESHOLD:
                    print(f"[bash-worker] Idle threshold reached. accumulated length: {len(accumulated)}", flush=True)
                    print(f"[bash-worker] marker as output (with newline): {(chr(10) + end_marker) in accumulated}", flush=True)
                    print(f"[bash-worker] on_stdin_request is not None: {on_stdin_request is not None}", flush=True)

                # Only check for stdin need after being idle for a while
                # and if we have a callback and the marker hasn't appeared AS OUTPUT
                # (marker in echoed command doesn't count - need \n before it)
                marker_as_output = f"\n{end_marker}" in accumulated
                if (idle_count >= IDLE_THRESHOLD and
                    on_stdin_request is not None and
                    not marker_as_output and
                    accumulated):  # Must have some output

                    # Check if output ends with a prompt pattern
                    # Strip ANSI codes for pattern matching
                    clean_output = _strip_ansi(accumulated).rstrip()

                    # Debug: Show what we're checking
                    if idle_count == IDLE_THRESHOLD:
                        print(f"[bash-worker] Checking for stdin. Last 100 chars: {repr(clean_output[-100:])}", flush=True)

                    # Check for prompt patterns at end of output
                    needs_input = False
                    detected_prompt = ""

                    for pattern in PROMPT_PATTERNS:
                        if clean_output.endswith(pattern):
                            needs_input = True
                            # Extract the prompt (last line or portion)
                            lines = clean_output.split('\n')
                            detected_prompt = lines[-1] if lines else pattern
                            print(f"[bash-worker] Stdin needed! Pattern matched: {repr(pattern)}", flush=True)
                            break

                    if needs_input:
                        # Request stdin from user - this BLOCKS until input is provided
                        request = StdinRequest(
                            prompt=detected_prompt,
                            password='password' in detected_prompt.lower(),
                            exec_id=exec_id or "",
                        )

                        try:
                            # Call the callback - it should BLOCK and return the input
                            user_input = on_stdin_request(request)

                            # Write the input to PTY
                            if user_input is not None:
                                # Ensure input ends with newline
                                if not user_input.endswith('\n'):
                                    user_input += '\n'
                                os.write(self._master_fd, user_input.encode('utf-8'))

                            # Reset idle count after providing input
                            idle_count = 0

                        except InputCancelledError:
                            raise
                        except Exception as e:
                            # If stdin request fails, continue waiting
                            # (maybe user will interrupt or command will timeout)
                            pass

        # Parse output and exit code
        full_output = "".join(output_parts)
        print(f"[bash-worker] RAW full_output before filtering: {repr(full_output)}", flush=True)

        # Extract exit code using the unique marker
        exit_code = 0
        exit_match = re.search(rf"{re.escape(exit_marker)}(\d+)", full_output)
        if exit_match:
            exit_code = int(exit_match.group(1))

        # Strip ANSI escape sequences first
        output = _strip_ansi(full_output)

        # Clean up markers from output (use the unique markers)
        output = re.sub(rf"echo {re.escape(exit_marker)}\$\?.*\n", "", output)
        output = re.sub(rf"{re.escape(exit_marker)}\d+\n?", "", output)
        output = re.sub(rf"echo {re.escape(end_marker)}.*\n", "", output)
        output = re.sub(rf"{re.escape(end_marker)}\n?", "", output)

        # Remove carriage returns followed by content (bash echo)
        output = re.sub(r"\r(?!\n)", "", output)
        # Normalize line endings
        output = output.replace("\r\n", "\n")

        # Remove the command echo (bash -i echoes commands)
        # The PTY echoes back everything we send, so we need to filter
        # out lines that are just the echoed commands
        lines = output.split("\n")

        # Get the user code lines to filter them out (they get echoed)
        code_lines = code.strip().split("\n")

        filtered_lines = []
        code_line_idx = 0

        for line in lines:
            stripped = line.strip()

            # Skip empty lines at start
            if not filtered_lines and not stripped:
                continue

            # Skip if this looks like our marker commands being echoed
            # Check for base markers (the unique suffix was already stripped)
            if "__MRMD_EXIT_CODE__" in stripped or "__MRMD_END_MARKER__" in stripped:
                continue

            # Skip if this matches the next expected code line (echoed command)
            if code_line_idx < len(code_lines):
                expected = code_lines[code_line_idx].strip()
                if stripped == expected:
                    code_line_idx += 1
                    continue

            filtered_lines.append(line)

        # Remove empty trailing lines
        while filtered_lines and not filtered_lines[-1].strip():
            filtered_lines.pop()

        output = "\n".join(filtered_lines)

        return output, exit_code

    def execute(
        self,
        code: str,
        store_history: bool = True,
        exec_id: str | None = None,
    ) -> ExecuteResult:
        """Execute bash code and return the result.

        Args:
            code: Bash code to execute
            store_history: Whether to increment execution count
            exec_id: Execution identifier

        Returns:
            ExecuteResult with execution outcome
        """
        return self.execute_streaming(code, store_history, exec_id, None, None)

    def execute_streaming(
        self,
        code: str,
        store_history: bool = True,
        exec_id: str | None = None,
        on_output: Callable[[str, str, str], None] | None = None,
        on_stdin_request: Callable[[StdinRequest], None] | None = None,
    ) -> ExecuteResult:
        """Execute bash code with streaming output.

        Args:
            code: Bash code to execute
            store_history: Whether to increment execution count
            exec_id: Execution identifier
            on_output: Callback for output chunks (stream, chunk, accumulated)
            on_stdin_request: Callback when input is requested

        Returns:
            ExecuteResult with execution outcome
        """
        with self._lock:
            self._ensure_started()

            start_time = time.time()
            self._last_activity = datetime.now(timezone.utc)

            if store_history:
                self._execution_count += 1

            print(f"[bash-worker] execute_streaming called. code={repr(code[:50])}, exec_id={exec_id}", flush=True)
            print(f"[bash-worker] on_stdin_request provided: {on_stdin_request is not None}", flush=True)

            try:
                # Cancel any pending input/command by sending Ctrl+C
                # This ensures a clean state before running new commands
                if self._master_fd is not None:
                    os.write(self._master_fd, b'\x03')  # Ctrl+C
                    time.sleep(0.05)  # Brief pause for shell to process
                    # Consume any output from the interrupt
                    self._read_available(timeout=0.1)

                # Build command with UNIQUE markers for exit code and end detection
                # Use timestamp to ensure each execution has unique markers
                exec_marker = f"{_END_MARKER}_{time.time_ns()}"
                exit_marker = f"{_EXIT_CODE_MARKER}_{time.time_ns()}"

                # IMPORTANT: Send user command FIRST, then wait for completion,
                # then send markers. This prevents interactive commands (read, etc.)
                # from consuming marker commands as stdin input.
                #
                # We use a shell construct that:
                # 1. Runs the user code
                # 2. Captures the exit code
                # 3. Outputs markers AFTER the command finishes
                #
                # The eval + subshell approach ensures markers run after user code,
                # even if user code reads from stdin.
                # Strip trailing whitespace from code - newlines break single-quote escaping
                clean_code = code.rstrip()
                wrapped_code = f"__exit_code__=0; eval {shlex.quote(clean_code)} || __exit_code__=$?; echo {exit_marker}$__exit_code__; echo {exec_marker}"
                print(f"[bash-worker] Sending wrapped command: {repr(wrapped_code[:100])}", flush=True)
                self._write_command(wrapped_code)

                # Read until we see the end marker
                output, exit_code = self._read_until_marker(
                    code, exec_marker, exit_marker, exec_id, on_output, on_stdin_request
                )
                print(f"[bash-worker] Got output: {repr(output[:200] if output else 'EMPTY')}, exit_code={exit_code}", flush=True)

                duration = int((time.time() - start_time) * 1000)

                if exit_code != 0:
                    return ExecuteResult(
                        success=False,
                        stdout=output,
                        stderr="",
                        result=None,
                        error=ExecuteError(
                            type="BashError",
                            message=f"Command exited with code {exit_code}",
                            traceback=[output] if output else [],
                            line=None,
                            column=None,
                        ),
                        displayData=[],
                        assets=[],
                        executionCount=self._execution_count,
                        duration=duration,
                    )

                return ExecuteResult(
                    success=True,
                    stdout=output,
                    stderr="",
                    result=None,
                    error=None,
                    displayData=[],
                    assets=[],
                    executionCount=self._execution_count,
                    duration=duration,
                )

            except InputCancelledError:
                duration = int((time.time() - start_time) * 1000)
                return ExecuteResult(
                    success=False,
                    stdout="",
                    stderr="",
                    result=None,
                    error=ExecuteError(
                        type="InputCancelled",
                        message="Input cancelled by user",
                        traceback=[],
                    ),
                    displayData=[],
                    assets=[],
                    executionCount=self._execution_count,
                    duration=duration,
                )
            except Exception as e:
                duration = int((time.time() - start_time) * 1000)
                return ExecuteResult(
                    success=False,
                    stdout="",
                    stderr="",
                    result=None,
                    error=ExecuteError(
                        type=type(e).__name__,
                        message=str(e),
                        traceback=[],
                    ),
                    displayData=[],
                    assets=[],
                    executionCount=self._execution_count,
                    duration=duration,
                )

    def interrupt(self) -> bool:
        """Interrupt the current execution.

        Returns:
            True if interrupt was sent, False otherwise.
        """
        if self._process is None or self._process.poll() is not None:
            return False

        try:
            # Send SIGINT to the process group
            os.killpg(os.getpgid(self._process.pid), signal.SIGINT)
            return True
        except (OSError, ProcessLookupError):
            return False

    def complete(self, code: str, cursor_pos: int) -> CompleteResult:
        """Get completions for the code at cursor position.

        Uses bash's compgen for completions.

        Args:
            code: Current code
            cursor_pos: Cursor position in the code

        Returns:
            CompleteResult with completion suggestions
        """
        with self._lock:
            self._ensure_started()

            # Get the word being completed
            text_before = code[:cursor_pos]

            # Find the start of the current word
            word_start = cursor_pos
            while word_start > 0 and text_before[word_start - 1] not in " \t\n|&;(){}[]<>":
                word_start -= 1

            word = text_before[word_start:]

            # Determine completion type based on context
            matches: list[CompletionItem] = []

            # Check if it looks like a path
            if "/" in word or word.startswith("./") or word.startswith("~"):
                # File/directory completion
                completions = self._get_completions(f"compgen -f -- {word!r}")
                for c in completions:
                    kind = "file"
                    if os.path.isdir(os.path.expanduser(c)):
                        kind = "folder"
                        c = c + "/"
                    matches.append(
                        CompletionItem(
                            label=c,
                            insertText=c,
                            kind=kind,
                        )
                    )
            elif word.startswith("$"):
                # Variable completion
                var_prefix = word[1:]
                completions = self._get_completions(f"compgen -v -- {var_prefix!r}")
                for c in completions:
                    matches.append(
                        CompletionItem(
                            label="$" + c,
                            insertText="$" + c,
                            kind="variable",
                        )
                    )
            else:
                # Command completion (builtins, functions, aliases, executables)
                completions = self._get_completions(f"compgen -A function -abck -- {word!r}")
                for c in completions:
                    kind = "function"
                    # Check if it's a builtin
                    if c in {
                        "cd",
                        "echo",
                        "export",
                        "set",
                        "unset",
                        "source",
                        "alias",
                        "type",
                        "read",
                        "test",
                        "if",
                        "then",
                        "else",
                        "fi",
                        "for",
                        "while",
                        "do",
                        "done",
                        "case",
                        "esac",
                        "function",
                        "return",
                        "exit",
                        "exec",
                        "eval",
                        "trap",
                        "wait",
                        "jobs",
                        "fg",
                        "bg",
                        "kill",
                        "pwd",
                        "pushd",
                        "popd",
                        "dirs",
                        "history",
                        "declare",
                        "local",
                        "readonly",
                        "shift",
                        "getopts",
                        "true",
                        "false",
                    }:
                        kind = "keyword"
                    matches.append(
                        CompletionItem(
                            label=c,
                            insertText=c,
                            kind=kind,
                        )
                    )

            return CompleteResult(
                matches=matches[:50],  # Limit results
                cursorStart=word_start,
                cursorEnd=cursor_pos,
                source="runtime",
            )

    def _get_completions(self, compgen_cmd: str) -> list[str]:
        """Run a compgen command and return the results.

        Uses subprocess for reliable output capture.
        """
        try:
            result = subprocess.run(
                [self._bash_path, "-c", compgen_cmd],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=self._cwd,
            )
            if result.returncode != 0:
                return []

            # Parse completions
            completions = []
            for line in result.stdout.split("\n"):
                line = line.strip()
                if line:
                    completions.append(line)

            # Deduplicate while preserving order
            seen = set()
            unique = []
            for c in completions:
                if c not in seen:
                    seen.add(c)
                    unique.append(c)

            return unique
        except (subprocess.TimeoutExpired, OSError):
            return []

    def hover(self, code: str, cursor_pos: int) -> HoverResult:
        """Get hover information for symbol at cursor.

        For bash, this shows the type and value of variables or
        the type of commands.

        Args:
            code: Current code
            cursor_pos: Cursor position

        Returns:
            HoverResult with symbol information
        """
        with self._lock:
            self._ensure_started()

            # Extract the word at cursor
            text = code
            start = cursor_pos
            end = cursor_pos

            while start > 0 and text[start - 1] not in " \t\n|&;(){}[]<>$\"'":
                start -= 1
            while end < len(text) and text[end] not in " \t\n|&;(){}[]<>$\"'":
                end += 1

            word = text[start:end]

            # Check if it's a variable reference
            if start > 0 and text[start - 1] == "$":
                word = text[start:end]
                # Get variable value
                value = self._get_variable_value(word)
                if value is not None:
                    return HoverResult(
                        found=True,
                        name=f"${word}",
                        type="variable",
                        value=value,
                    )
            elif word.startswith("$"):
                var_name = word[1:]
                value = self._get_variable_value(var_name)
                if value is not None:
                    return HoverResult(
                        found=True,
                        name=word,
                        type="variable",
                        value=value,
                    )
            else:
                # Check command type
                cmd_type = self._get_command_type(word)
                if cmd_type:
                    return HoverResult(
                        found=True,
                        name=word,
                        type=cmd_type,
                        value=None,
                    )

            return HoverResult(found=False)

    def _get_variable_value(self, name: str) -> str | None:
        """Get the value of a shell variable.

        Executes a command in the session to retrieve the variable value.
        """
        if self._master_fd is None:
            return None

        # Use a wrapper that prints value with clear delimiters
        marker_start = f"__VAR_START_{time.time_ns()}__"
        marker_end = f"__VAR_END_{time.time_ns()}__"

        self._write_command(f'echo {marker_start}; echo "${{{name}}}"; echo {marker_end}')

        output = ""
        start_time = time.time()
        timeout = 2.0

        while time.time() - start_time < timeout:
            ready, _, _ = select.select([self._master_fd], [], [], 0.1)
            if ready:
                try:
                    data = os.read(self._master_fd, 4096)
                    if data:
                        output += data.decode("utf-8", errors="replace")
                        if marker_end in output:
                            break
                except OSError:
                    break

        # Strip ANSI codes
        output = _strip_ansi(output)

        # Extract value between markers
        if marker_start in output and marker_end in output:
            start_idx = output.find(marker_start) + len(marker_start)
            end_idx = output.find(marker_end)
            value = output[start_idx:end_idx].strip()
            # Remove line echoes and clean
            value = value.replace("\r", "")
            # Remove any lines that are just the echo command
            lines = [l for l in value.split("\n") if not l.strip().startswith("echo ")]
            value = "\n".join(lines).strip()
            return value if value else None

        return None

    def _get_command_type(self, cmd: str) -> str | None:
        """Get the type of a command (builtin, alias, function, file).

        Uses subprocess for reliable output capture.
        """
        if not cmd:
            return None

        try:
            result = subprocess.run(
                [self._bash_path, "-c", f"type -t {cmd!r}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return None

            cmd_type = result.stdout.strip()
            if cmd_type in ("builtin", "alias", "function", "file", "keyword"):
                return cmd_type

            return None
        except (subprocess.TimeoutExpired, OSError):
            return None

    def get_variables(self, filter_pattern: str | None = None) -> VariablesResult:
        """List environment and shell variables.

        Note: This uses subprocess and shows environment variables.
        Session-only variables (not exported) won't be visible.

        Args:
            filter_pattern: Optional regex pattern to filter variable names

        Returns:
            VariablesResult with variable list
        """
        # Use subprocess with env command for reliable output
        try:
            result = subprocess.run(
                [self._bash_path, "-c", "env"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=self._cwd,
            )
            output = result.stdout
        except (subprocess.TimeoutExpired, OSError):
            output = ""

        variables: list[Variable] = []
        pattern = re.compile(filter_pattern) if filter_pattern else None

        # Parse variables (format: NAME=value)
        for line in output.split("\n"):
            if "=" not in line:
                continue

            name, _, value = line.partition("=")
            name = name.strip()

            # Skip internal variables
            if name.startswith("_") or name.startswith("BASH"):
                continue

            # Apply filter
            if pattern and not pattern.search(name):
                continue

            # Truncate long values
            display_value = value[:100] + "..." if len(value) > 100 else value

            variables.append(
                Variable(
                    name=name,
                    type="string",
                    value=display_value,
                    size=f"{len(value)} chars" if len(value) > 100 else None,
                    expandable=False,
                )
            )

        # Sort by name
        variables.sort(key=lambda v: v.name)

        return VariablesResult(
            variables=variables[:200],  # Limit results
            count=len(variables),
            truncated=len(variables) > 200,
        )

    def get_variable_detail(self, name: str, path: list[str] | None = None) -> VariableDetail:
        """Get detailed information about a variable.

        For bash, variables are flat (no nesting), so path is ignored.

        Args:
            name: Variable name
            path: Ignored for bash

        Returns:
            VariableDetail with variable information
        """
        with self._lock:
            self._ensure_started()

            value = self._get_variable_value(name)

            if value is None:
                return VariableDetail(
                    name=name,
                    type="undefined",
                    value="",
                    expandable=False,
                )

            return VariableDetail(
                name=name,
                type="string",
                value=value[:100] + "..." if len(value) > 100 else value,
                fullValue=value,
                expandable=False,
                length=len(value),
            )

    def is_complete(self, code: str) -> IsCompleteResult:
        """Check if code is a complete statement.

        Detects unclosed quotes, brackets, and incomplete structures.

        Args:
            code: Code to check

        Returns:
            IsCompleteResult with completion status
        """
        # Simple heuristics for bash completeness

        # Count quotes
        single_quotes = code.count("'")
        double_quotes = code.count('"') - code.count('\\"')

        if single_quotes % 2 != 0:
            return IsCompleteResult(status="incomplete", indent="")
        if double_quotes % 2 != 0:
            return IsCompleteResult(status="incomplete", indent="")

        # Check brackets
        parens = code.count("(") - code.count(")")
        braces = code.count("{") - code.count("}")
        brackets = code.count("[") - code.count("]")

        if parens > 0 or braces > 0 or brackets > 0:
            return IsCompleteResult(status="incomplete", indent="  ")

        # Check for line continuation
        if code.rstrip().endswith("\\"):
            return IsCompleteResult(status="incomplete", indent="")

        # Check for incomplete control structures
        keywords_open = ["if", "then", "else", "elif", "for", "while", "until", "do", "case"]
        keywords_close = ["fi", "done", "esac"]

        # Simple counting (not perfect but reasonable)
        words = re.findall(r"\b\w+\b", code)

        depth = 0
        for word in words:
            if word in ["if", "for", "while", "until", "case"]:
                depth += 1
            elif word in ["fi", "done", "esac"]:
                depth -= 1

        if depth > 0:
            return IsCompleteResult(status="incomplete", indent="  ")

        # Check for trailing pipe or logical operators
        stripped = code.rstrip()
        if stripped.endswith("|") or stripped.endswith("&&") or stripped.endswith("||"):
            return IsCompleteResult(status="incomplete", indent="")

        return IsCompleteResult(status="complete", indent="")

    def reset(self) -> None:
        """Reset the session by restarting the bash process."""
        with self._lock:
            self._kill_process()
            self._execution_count = 0
            self._ensure_started()

    def _kill_process(self) -> None:
        """Kill the bash process."""
        if self._process is not None:
            try:
                os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
            self._process = None

        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None

    def shutdown(self) -> None:
        """Shutdown the worker and clean up resources."""
        self._kill_process()

    def provide_input(self, text: str) -> bool:
        """Provide input to a waiting read command.

        Args:
            text: Input text to provide

        Returns:
            True if input was accepted
        """
        if self._master_fd is None:
            return False

        try:
            os.write(self._master_fd, text.encode("utf-8"))
            return True
        except OSError:
            return False

    def cancel_input(self) -> bool:
        """Cancel a pending input request.

        Returns:
            True if cancellation was processed
        """
        self._input_cancelled = True
        return True

    @property
    def execution_count(self) -> int:
        """Get the current execution count."""
        return self._execution_count

    @property
    def created(self) -> datetime:
        """Get the creation timestamp."""
        return self._created

    @property
    def last_activity(self) -> datetime:
        """Get the last activity timestamp."""
        return self._last_activity

    def get_info(self) -> dict[str, Any]:
        """Get worker information."""
        return {
            "cwd": self._cwd,
            "bash_path": self._bash_path,
            "execution_count": self._execution_count,
            "created": self._created.isoformat(),
            "last_activity": self._last_activity.isoformat(),
            "running": self._process is not None and self._process.poll() is None,
        }
