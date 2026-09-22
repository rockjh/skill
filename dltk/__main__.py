"""Allow `python -m dltk` to behave like the console script."""

from .cli import console_main

raise SystemExit(console_main())
