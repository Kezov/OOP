"""Entry point for the bundled app (PyInstaller)."""

import sys

from ghostvoice.gui import main

if __name__ == "__main__":
    sys.exit(main())
