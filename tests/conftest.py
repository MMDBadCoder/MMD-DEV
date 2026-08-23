import sys
from pathlib import Path

# The control plane is not installed as a package; tests import it from source.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "control"))
