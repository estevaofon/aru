"""aru - A Claude Code clone built with Agno agents."""

import os

os.environ.setdefault("ORT_LOG_SEVERITY_LEVEL", "3")  # Suppress ONNX Runtime warnings (e.g. GPU detection on WSL2)

import asyncio
import sys

from dotenv import load_dotenv


def main():
    load_dotenv()
    args = sys.argv[1:]
    skip_permissions = "--dangerously-skip-permissions" in args

    resume_id = None
    if "--resume" in args:
        idx = args.index("--resume")
        if idx + 1 < len(args) and not args[idx + 1].startswith("--"):
            resume_id = args[idx + 1]
        else:
            resume_id = "last"

    # The Textual TUI is the only interactive interface. ``aru.tui`` is
    # imported lazily here so importing this module stays cheap.
    from aru.tui import run_tui
    try:
        asyncio.run(run_tui(skip_permissions=skip_permissions, resume_id=resume_id))
    except (KeyboardInterrupt, asyncio.CancelledError, SystemExit):
        pass


if __name__ == "__main__":
    main()
