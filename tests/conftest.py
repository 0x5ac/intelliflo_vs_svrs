import sys
import types
from pathlib import Path

# The tests import intelliflo.py as intelliflo.intelliflo, the one name the
# type checkers can resolve. A bare package object stands in for the real
# one so its __init__.py, which imports Home Assistant, never runs.
_pkg = types.ModuleType("intelliflo")
_pkg.__path__ = [str(Path(__file__).parent.parent / "custom_components" / "intelliflo")]
sys.modules.setdefault("intelliflo", _pkg)
