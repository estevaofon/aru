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

    # REPL opt-in — TUI is the default interactive mode.
    #
    # ``aru.cli`` transitively imports ``aru.completers`` which in turn
    # imports ``prompt_toolkit`` (~580 ms on cold cache) plus a handful
    # of Agno REPL utilities. None of that is needed on the TUI path,
    # so we defer the import to inside the branch that actually uses
    # it. Same story for ``aru.tui`` — keep it out of the REPL path.
    # Net effect: TUI cold-start drops by ~2.3 s, REPL cold-start
    # unchanged.
    if "--repl" in args:
        from aru.cli import run_cli
        try:
            asyncio.run(run_cli(skip_permissions=skip_permissions, resume_id=resume_id))
        except (KeyboardInterrupt, asyncio.CancelledError, SystemExit):
            pass  # Handled by cli.main() or run_cli's own exit logic
        return

    from aru.tui import run_tui
    try:
        asyncio.run(run_tui(skip_permissions=skip_permissions, resume_id=resume_id))
    except (KeyboardInterrupt, asyncio.CancelledError, SystemExit):
        pass


if __name__ == "__main__":
    main()
