"""PyInstaller entry point — launches the GUI (like ``python -m tcginventory``)."""

import sys

from tcginventory.app import main

sys.exit(main())
