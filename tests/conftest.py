import sys
from pathlib import Path

# The source modules import each other by bare name (e.g. "from agent import"),
# matching how they're run as scripts, so tests need src/ on the path too.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
