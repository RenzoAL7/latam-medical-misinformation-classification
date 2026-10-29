from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from claim_extraction.data import prepare_news_candidates, validate_reference_table
from claim_extraction.metrics import operational_diagnostics, reference_metrics
from claim_extraction.registry import load_registry


ROOT = Path(__file__).resolve().parents[1]


def test_registry_contains_the_four_local_claim_extractors() -> None:
    registry = load_registry()
    assert set(registry) == {
        "umt5_base_es",
        "t5_small",
        "pegasus_large",
        "claimgen_bart_transfer",
    }
    assert registry["umt5_base_es"].base_checkpoint == "google/umt5-base"
    assert registry["t5_small"].base_checkpoint == "google-t5/t5-small"
    assert registry["pegasus_large"].base_checkpoint == "google/pegasus-large"
    assert registry["claimgen_bart_transfer"].base_checkpoint == "facebook/bart-large"
    assert registry["claimgen_bart_transfer"].input_adapter == "claimgen_transfer"


def test_scripts_cover_phase_one_flow() -> None:
    for name in [
        "prepare_claim_corpus.py",
        "create_reference_template.py",
        "split_reference.py",
        "train_seq2seq.py",
        "run_inference.py",
        "evaluate_claims.py",
        "build_blind_review_sheet.py",
    ]:
        assert (ROOT / "scripts" / name).is_file()


def test_candidate_filtering_preserves_one_news_unit() -> None:
    frame = pd.DataFrame(
        [
            {
                "record_id": "ok",
                "title": "Título",
                "body": "Una noticia médica con suficiente contenido para el filtro.",
                "body_available": "SI",
                "extraction_status": "OK",
            },
            {
                "record_id": "bad",
                "title": "Título",
                "body": "Contenido",
                "body_available": "NO",
                "extraction_status": "OK",
            },
        ]
    )
    result = prepare_news_candidates(frame, min_body_chars=10)
    assert result["record_id"].tolist() == ["ok"]
    assert result["source_text"].iloc[0].startswith("Título")


def test_metrics_require_reference_claims_and_keep_operational_diagnostics_separate() -> None:
    rows = [{"reference_claim": "La vacuna reduce el riesgo", "claim": "La vacuna reduce el riesgo"}]
    details, summary = reference_metrics(rows)
    assert summary["meteor"] > 0.99
    assert summary["rouge_l_f1"] == 1.0
    assert details[0]["exact_match"] == 1.0
    assert operational_diagnostics([{"claim": ""}])["empty_rate"] == 1.0
    with_reference = pd.DataFrame(
        [{"record_id": "1", "source_text": "fuente", "reference_claim": "claim"}]
    )
    assert validate_reference_table(with_reference).shape[0] == 1


def test_configs_are_valid_json() -> None:
    json.loads((ROOT / "configs" / "models.json").read_text(encoding="utf-8"))
