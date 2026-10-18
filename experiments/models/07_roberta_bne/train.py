#!/usr/bin/env python3
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from experiments.models.transformer_train import main

raise SystemExit(main("roberta_bne", "PlanTL-GOB-ES/roberta-base-bne"))
