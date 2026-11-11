"""Make ``src/`` importable the same way ``python src/app.py`` sees it."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
