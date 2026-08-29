#!/usr/bin/env python3
"""Entry point for ``python -m control_bot`` or direct script execution."""
from __future__ import annotations

import sys
from pathlib import Path

if __package__:
    from .bot import run
else:
    # ``python control_bot/run.py`` puts only control_bot/ on sys.path. Add the
    # repository root so the absolute package import works in that mode too.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from control_bot.bot import run


if __name__ == "__main__":
    run()
