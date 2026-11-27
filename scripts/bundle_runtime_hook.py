"""PyInstaller runtime hook for the standalone server bundle (#841).

State must survive re-extracting a newer bundle, so the default data
directory sits beside the executable rather than inside the extracted
program files. An explicit TEXTMMO_DATA_DIR always wins.
"""

import os
import sys

if getattr(sys, "frozen", False) and not os.environ.get("TEXTMMO_DATA_DIR"):
    os.environ["TEXTMMO_DATA_DIR"] = os.path.join(
        os.path.dirname(os.path.abspath(sys.executable)), "data"
    )
