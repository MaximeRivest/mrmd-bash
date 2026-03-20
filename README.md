# mrmd-bash

MRP (MRMD Runtime Protocol) server for Bash, implemented in Go.

One `mrmd-bash` server process = one persistent Bash runtime namespace.
If you need another isolated namespace, start another server process on another port.

## Build

```bash
go build -o bin/mrmd-bash ./cmd/mrmd-bash
```

## Usage

```bash
./bin/mrmd-bash --port 8001
```

Server URL:

```text
http://localhost:8001/mrp/v1/
```

### Options

```text
--host HOST        Host to bind to (default: 127.0.0.1)
--port PORT        Port to bind to (default: 8001)
--cwd PATH         Working directory for the bash runtime
--log-level LEVEL  Log level: debug, info, warning, error (default: info)
--reload           Accepted for CLI compatibility; no-op in Go
```

## Features

| Feature | Support | Notes |
|---------|---------|-------|
| `execute` | ✅ | Run code and return result |
| `executeStream` | ✅ | Stream output via SSE |
| `interrupt` | ✅ | Cancel running execution (SIGINT) |
| `complete` | ✅ | Completions via `compgen` |
| `inspect` | ❌ | Not supported |
| `hover` | ✅ | Variable values and command types |
| `variables` | ✅ | Shell and environment variables from the live session |
| `variableExpand` | ❌ | Bash variables are flat |
| `reset` | ✅ | Restart bash runtime namespace |
| `isComplete` | ✅ | Detect incomplete statements |
| `format` | ❌ | Not supported |
| `history` | ✅ | Browse execution input history for up-arrow / Ctrl-R, persisted via `$HISTFILE` / `~/.bash_history` |
| `assets` | ❌ | Not supported |

## API Examples

### Capabilities

```bash
curl http://localhost:8001/mrp/v1/capabilities
```

### Execute

```bash
curl -X POST http://localhost:8001/mrp/v1/execute \
  -H "Content-Type: application/json" \
  -d '{"code": "echo Hello, World!"}'
```

### Variables

```bash
curl -X POST http://localhost:8001/mrp/v1/variables \
  -H "Content-Type: application/json" \
  -d '{}'
```

### Reset runtime

```bash
curl -X POST http://localhost:8001/mrp/v1/reset
```

### History

```bash
curl -X POST http://localhost:8001/mrp/v1/history \
  -H "Content-Type: application/json" \
  -d '{"n": 20}'
```

### Stream execution

```bash
curl -X POST http://localhost:8001/mrp/v1/execute/stream \
  -H "Content-Type: application/json" \
  -d '{"code": "for i in 1 2 3; do echo $i; sleep 1; done"}'
```

## Runtime Model

Runtime state persists between executions:
- environment variables persist
- working directory changes persist
- shell functions and aliases persist

To get a fresh namespace, either:
- call `POST /mrp/v1/reset`, or
- start a new `mrmd-bash` process

History is persisted separately via Bash's native history file (`$HISTFILE`, defaulting to `~/.bash_history`), so `reset` clears the runtime namespace but does not erase command history.

## Protocol

This server implements the shared MRP spec at:
- `../spec/mrp-protocol.md`

## Notes

- `stdout` / `stderr` are delivered from a PTY-backed interactive bash session.
- ANSI escape sequences are preserved in streamed output.
- Interactive input is supported through `/mrp/v1/input` and `/mrp/v1/input/cancel`.

## License

MIT
