from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from claim_extraction.data import validate_reference_table
from claim_extraction.metrics import operational_diagnostics, reference_metrics
from claim_extraction.models import configure_model, configure_tokenizer
from claim_extraction.registry import load_registry
from claim_extraction.text import input_for_model


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


def test_model_input_uses_the_reference_source_or_title_and_body() -> None:
    assert input_for_model({"source_text": "Texto ya preparado."}) == "Texto ya preparado."
    assert input_for_model({"title": "Título", "body": "Cuerpo"}) == "Título\n\nCuerpo"


def test_metrics_keep_reference_and_operational_results_separate() -> None:
    rows = [{"reference_claim": "La vacuna reduce el riesgo", "claim": "La vacuna reduce el riesgo"}]
    details, summary = reference_metrics(
        rows,
        bertscore_scorer=lambda references, predictions: [(1.0, 1.0, 1.0)],
        meteor_scorer=lambda reference, prediction: 1.0,
    )
    assert summary["meteor"] == 1.0
    assert summary["rouge_l_precision"] == 1.0
    assert summary["rouge_l_recall"] == 1.0
    assert summary["rouge_l_f1"] == 1.0
    assert summary["bertscore_precision"] == 1.0
    assert summary["bertscore_recall"] == 1.0
    assert details[0]["bertscore_f1"] == 1.0
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


def test_sem1_uses_a_common_optimization_and_generation_budget() -> None:
    registry = load_registry()
    training_keys = {
        "epochs",
        "learning_rate",
        "weight_decay",
        "warmup_steps",
        "effective_batch_size",
        "max_source_length",
        "max_target_length",
    }
    generation_keys = {"num_beams", "max_new_tokens"}
    first = registry["t5s_spanish"]
    expected_training = {key: first.training[key] for key in training_keys}
    expected_generation = {key: first.generation[key] for key in generation_keys}
    for spec in registry.values():
        assert {key: spec.training[key] for key in training_keys} == expected_training
        assert {key: spec.generation[key] for key in generation_keys} == expected_generation
