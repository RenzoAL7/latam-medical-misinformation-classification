#!/usr/bin/env python3
"""Keep the established baseline as the canonical logistic-regression run."""

from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
runpy.run_path(str(ROOT / "scripts" / "03_train_baseline.py"), run_name="__main__")
