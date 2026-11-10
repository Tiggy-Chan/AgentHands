"""CLI entry point for AgentHands WebUI."""

from __future__ import annotations

import argparse
import sys
import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="AgentHands Management WebUI")
    parser.add_argument("--host", default="127.0.0.1", help="Host interface (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8990, help="Listen port (default: 8990)")
    args = parser.parse_args()

    print(f"AgentHands Console running at http://{args.host}:{args.port}", file=sys.stderr)
    uvicorn.run("webui.app:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
