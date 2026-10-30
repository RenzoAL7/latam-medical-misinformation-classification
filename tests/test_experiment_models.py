from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from claim_extraction.data import prepare_news_candidates, validate_reference_table
from claim_extraction.metrics import operational_diagnostics, reference_metrics
from claim_extraction.models import configure_model, configure_tokenizer
from claim_extraction.registry import load_registry


ROOT = Path(__file__).resolve().parents[1]


def test_registry_contains_the_four_local_claim_extractors() -> None:
    registry = load_registry()
    assert set(registry) == {
        "t5s_spanish",
        "barto_spanish",
        "mbart50_es",
        "umt5_base_es",
    }
    assert registry["t5s_spanish"].base_checkpoint == "vgaraujov/t5-base-spanish"
    assert registry["barto_spanish"].base_checkpoint == "vgaraujov/bart-base-spanish"
    assert registry["mbart50_es"].base_checkpoint == "facebook/mbart-large-50"
    assert registry["umt5_base_es"].base_checkpoint == "google/umt5-base"


def test_mbart_is_configured_for_spanish_generation() -> None:
    class FakeTokenizer:
        lang_code_to_id = {"es_XX": 7}

    class FakeConfig:
        forced_bos_token_id = None

    class FakeGenerationConfig:
        forced_bos_token_id = None

    class FakeModel:
        config = FakeConfig()
        generation_config = FakeGenerationConfig()

    tokenizer = configure_tokenizer(FakeTokenizer(), model_id="mbart50_es")
    configure_model(FakeModel(), tokenizer, model_id="mbart50_es")
    assert tokenizer.src_lang == "es_XX"
    assert tokenizer.tgt_lang == "es_XX"
    assert FakeModel.config.forced_bos_token_id == 7
    assert FakeModel.generation_config.forced_bos_token_id == 7


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

    pending = pd.DataFrame(
        [{"record_id": "1", "source_text": "fuente", "reference_claim": ""}]
    )
    assert (
        validate_reference_table(
            pending, allow_missing_reference_claims=True
        ).shape[0]
        == 1
    )


def test_configs_are_valid_json() -> None:
    json.loads((ROOT / "configs" / "models.json").read_text(encoding="utf-8"))

