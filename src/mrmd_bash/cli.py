"""Command-line interface for mrmd-bash."""

from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    """Main entry point for mrmd-bash CLI."""
    parser = argparse.ArgumentParser(
        prog="mrmd-bash",
        description="MRP (MRMD Runtime Protocol) server for Bash",
    )

    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8001,
        help="Port to bind to (default: 8001)",
    )
    parser.add_argument(
        "--cwd",
        default=None,
        help="Working directory for bash sessions (default: current directory)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload for development",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error"],
        help="Log level (default: info)",
    )

    args = parser.parse_args()

    # Resolve working directory
    cwd = os.path.abspath(args.cwd) if args.cwd else os.getcwd()

    print(f"Starting mrmd-bash server...")
    print(f"  Host: {args.host}")
    print(f"  Port: {args.port}")
    print(f"  Working directory: {cwd}")
    print(f"  URL: http://{args.host}:{args.port}/mrp/v1/capabilities")
    print()

    try:
        import uvicorn

        # Set CWD environment variable for the app factory
        os.environ["MRMD_BASH_CWD"] = cwd

        uvicorn.run(
            "mrmd_bash.server:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            reload=args.reload,
            log_level=args.log_level,
        )
    except KeyboardInterrupt:
        print("\nShutting down...")
        sys.exit(0)
    except ImportError as e:
        print(f"Error: {e}")
        print("Make sure uvicorn is installed: pip install uvicorn")
        sys.exit(1)


if __name__ == "__main__":
    main()
