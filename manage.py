#!/usr/bin/env python3
"""Thin entrypoint so the demo commands in README.md read as `python manage.py ...`
instead of `python -m src.cli.main ...`."""
from src.cli.main import main

if __name__ == "__main__":
    main()
