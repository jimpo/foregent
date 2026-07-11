"""Enable ``python -m foregent`` as an alias for the ``foregent`` CLI."""

from foregent.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
