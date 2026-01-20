# mrmd-bash

MRP (MRMD Runtime Protocol) server for Bash. Enables notebook-style editors to execute Bash code with persistent sessions, completions, and variable inspection.

## Installation

```bash
pip install mrmd-bash
```

Or with uv:

```bash
uv pip install mrmd-bash
```

## Usage

Start the server:

```bash
mrmd-bash --port 8001
```

The server will be available at `http://localhost:8001/mrp/v1/`.

### Options

```
--host HOST       Host to bind to (default: 127.0.0.1)
--port PORT       Port to bind to (default: 8001)
--cwd PATH        Working directory for bash sessions
--log-level LEVEL Log level: debug, info, warning, error (default: info)
--reload          Enable auto-reload for development
```

## Features

| Feature | Support | Notes |
|---------|---------|-------|
| `execute` | ✅ | Run code and return result |
| `executeStream` | ✅ | Stream output via SSE |
| `interrupt` | ✅ | Cancel running execution (SIGINT) |
| `complete` | ✅ | Completions via compgen |
| `inspect` | ❌ | Not supported |
| `hover` | ✅ | Variable values and command types |
| `variables` | ✅ | Environment and shell variables |
| `variableExpand` | ❌ | Bash variables are flat |
| `reset` | ✅ | Restart bash session |
| `isComplete` | ✅ | Detect incomplete statements |
| `format` | ❌ | Not supported |
| `assets` | ❌ | Not supported |

## API Examples

### Check capabilities

```bash
curl http://localhost:8001/mrp/v1/capabilities
```

### Execute code

```bash
curl -X POST http://localhost:8001/mrp/v1/execute \
  -H "Content-Type: application/json" \
  -d '{"code": "echo Hello, World!"}'
```

### Get completions

```bash
curl -X POST http://localhost:8001/mrp/v1/complete \
  -H "Content-Type: application/json" \
  -d '{"code": "ec", "cursor": 2}'
```

### List variables

```bash
curl -X POST http://localhost:8001/mrp/v1/variables \
  -H "Content-Type: application/json" \
  -d '{"session": "default"}'
```

### Stream execution

```bash
curl -X POST http://localhost:8001/mrp/v1/execute/stream \
  -H "Content-Type: application/json" \
  -d '{"code": "for i in 1 2 3; do echo $i; sleep 1; done"}'
```

## Sessions

Sessions maintain persistent Bash state:
- Environment variables persist between executions
- Working directory changes persist
- Shell functions and aliases persist

```bash
# Create a session
curl -X POST http://localhost:8001/mrp/v1/sessions \
  -H "Content-Type: application/json" \
  -d '{"id": "my-session"}'

# Execute in session
curl -X POST http://localhost:8001/mrp/v1/execute \
  -H "Content-Type: application/json" \
  -d '{"code": "export MY_VAR=hello", "session": "my-session"}'

# Variable persists
curl -X POST http://localhost:8001/mrp/v1/execute \
  -H "Content-Type: application/json" \
  -d '{"code": "echo $MY_VAR", "session": "my-session"}'

# Reset session (clear state)
curl -X POST http://localhost:8001/mrp/v1/sessions/my-session/reset

# Delete session
curl -X DELETE http://localhost:8001/mrp/v1/sessions/my-session
```

## Completions

The server provides completions via bash's `compgen`:

- **Commands**: builtins, functions, aliases, executables
- **Files**: paths with `/` prefix
- **Variables**: names with `$` prefix

## Development

```bash
# Clone and install in development mode
git clone <repo>
cd mrmd-bash
pip install -e ".[dev]"

# Run with auto-reload
mrmd-bash --reload

# Run tests
pytest
```

## Protocol

This server implements the [MRMD Runtime Protocol (MRP)](../mrmd-editor/PROTOCOL.md).

## License

MIT
