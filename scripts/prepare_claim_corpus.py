#!/usr/bin/env python3
"""Prepara candidatos de noticias sin alterar ni versionar el CSV de origen."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claim_extraction.data import prepare_news_candidates, read_table, write_jsonl


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="CSV o JSONL de noticias.")
    parser.add_argument("--output", required=True, help="JSONL local de candidatos.")
    parser.add_argument("--min-body-chars", type=int, default=400)
    parser.add_argument("--sample-size", type=int, default=None)
    parser.add_argument("--seed", type=int, default=20261029)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_path = Path(args.input).expanduser()
    output_path = Path(args.output).expanduser()
    frame = read_table(input_path)
    candidates = prepare_news_candidates(
        frame,
        min_body_chars=args.min_body_chars,
        sample_size=args.sample_size,
        seed=args.seed,
    )
    write_jsonl(candidates.to_dict(orient="records"), output_path)
    summary = {
        "input_rows": int(len(frame)),
        "candidate_rows": int(len(candidates)),
        "sample_size": args.sample_size,
        "min_body_chars": args.min_body_chars,
        "seed": args.seed,
        "output": str(output_path),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
