import os
import sys
from pathlib import Path

os.environ["FITDASH_TZ"] = "America/New_York"   # fixtures are built in New York time: same results on any machine
os.environ["COLORTERM"] = "truecolor"           # color tests see 24-bit color on any machine (CI terminals report less)
os.environ["STATS_STRICT"] = "1"          # any line wider than its panel fails the test
os.environ.pop("NO_COLOR", None)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))
sys.path.insert(0, str(HERE))
