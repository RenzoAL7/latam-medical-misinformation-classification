#!/usr/bin/env python3
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from experiments.models.transformer_train import main

raise SystemExit(main("beto", "dccuchile/bert-base-spanish-wwm-cased"))
