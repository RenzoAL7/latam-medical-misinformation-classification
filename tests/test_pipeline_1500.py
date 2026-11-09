from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from claim_extraction.analysis import bootstrap_uncertainty, metrics_by_topic
from claim_extraction.data import near_duplicate_groups
from claim_extraction.workflow import (
    DATASET_SPLIT_SIZES,
    assert_run_matches,
    run_name_for,
    split_fingerprints,
)


ROOT = Path(__file__).resolve().parents[1]


def test_run_names_do_not_mix_datasets_and_keep_existing_500_runs() -> None:
    assert run_name_for("claims_500", "epocas_5") == "claims_500_v1"
    assert run_name_for("claims_1500", "epocas_5") == "claims_1500_epocas_5"
    with pytest.raises(ValueError, match="Conjunto"):
        run_name_for("claims_900", "epocas_5")


def test_run_manifest_guard_rejects_other_data_but_accepts_old_manifests() -> None:
    fingerprints = {"train": "a", "validation": "b", "test": "c"}
    current = {"dataset_name": "claims_1500", "variant": "lora", "split_fingerprints": fingerprints}
    assert_run_matches(current, dataset_name="claims_1500", variant="lora", fingerprints=fingerprints)
    assert_run_matches(
        {"dataset_name": "claims_500"}, dataset_name="claims_500", variant="epocas_5", fingerprints=fingerprints
    )
    with pytest.raises(ValueError, match="dataset_name"):
        assert_run_matches(
            {"dataset_name": "claims_500"}, dataset_name="claims_1500", variant="epocas_5", fingerprints=fingerprints
        )
    with pytest.raises(ValueError, match="split_fingerprints"):
        assert_run_matches(current, dataset_name="claims_1500", variant="lora", fingerprints={"train": "z"})


def test_split_fingerprints_ignore_row_order() -> None:
    one = pd.DataFrame({"record_id": ["a", "b"]})
    other = pd.DataFrame({"record_id": ["b", "a"]})
    assert split_fingerprints({"train": one}) == split_fingerprints({"train": other})


def test_near_duplicates_group_transitively_and_ignore_unrelated_news() -> None:
    base = " ".join(f"palabra{index}" for index in range(60))
    variant = base + " final añadido"
    third = variant + " otra coda"
    other = " ".join(f"distinta{index}" for index in range(60))
    groups = near_duplicate_groups(
        [base, variant, third, other], ["uno dos tres", "cuatro cinco seis", "siete ocho nueve", "diez once doce"]
    )
    assert groups[0] == groups[1] == groups[2]
    assert groups[3] != groups[0]


def test_near_duplicates_also_link_nearly_identical_claims() -> None:
    claim = "La vacuna contra el dengue reduce las hospitalizaciones en adultos mayores"
    groups = near_duplicate_groups(["fuente uno distinta", "otro texto muy ajeno"], [claim, claim + " sanos"])
    assert groups[0] == groups[1]


def _per_prediction() -> pd.DataFrame:
    rows = []
    for index in range(40):
        tema = "Cáncer" if index % 2 else "Otra"
        rows.append({"record_id": str(index), "model_id": "alto", "model_display_name": "Alto", "meteor": 0.6, "rouge_l_f1": 0.5, "bertscore_f1": 0.9, "tema_real": tema})
        rows.append({"record_id": str(index), "model_id": "bajo", "model_display_name": "Bajo", "meteor": 0.2, "rouge_l_f1": 0.1, "bertscore_f1": 0.7, "tema_real": tema})
    return pd.DataFrame(rows)


def test_bootstrap_differences_are_paired_against_the_top_model() -> None:
    summary, differences = bootstrap_uncertainty(_per_prediction(), n_boot=200)
    assert summary.iloc[0]["model_id"] == "alto"
    assert summary.iloc[0]["ci95_low"] == pytest.approx(0.6)
    assert len(differences) == 1
    assert differences.iloc[0]["diferencia_media"] == pytest.approx(0.4)
    assert bool(differences.iloc[0]["ic_excluye_cero"]) is True


def test_bootstrap_rejects_models_evaluated_on_different_news() -> None:
    incomplete = _per_prediction().iloc[:-1]
    with pytest.raises(ValueError, match="mismas noticias"):
        bootstrap_uncertainty(incomplete, n_boot=10)


def test_metrics_by_topic_uses_the_annotated_topic() -> None:
    frame = _per_prediction()
    references = frame.drop_duplicates("record_id")[["record_id", "tema_real"]].rename(columns={"tema_real": "tema"})
    table = metrics_by_topic(frame.drop(columns="tema_real"), references, n_boot=50)
    assert set(table["tema"]) == {"Cáncer", "Otra"}
    assert set(table["n"]) == {20}
    with pytest.raises(ValueError, match="tema"):
        metrics_by_topic(frame, references.rename(columns={"tema": "x"}), n_boot=10)


def _topic_frame(prefix: str, per_topic: int) -> pd.DataFrame:
    from claim_extraction.topics import TOPIC_LABELS

    rows = []
    for label in TOPIC_LABELS:
        token = label.split()[0].lower()
        for index in range(per_topic):
            rows.append(
                {"record_id": f"{prefix}-{token}-{index}", "title": f"{token} {token}", "body": f"{token} informe {token} estudio {index}", "tema": label}
            )
    return pd.DataFrame(rows)


def test_topic_selection_covers_six_combinations_and_marks_one() -> None:
    pytest.importorskip("sklearn")
    from claim_extraction.topics import REPRESENTATIONS, TOPIC_LABELS, select_and_evaluate_topic_classifier

    def fake_encoder(representation: str, texts: list[str]) -> np.ndarray:
        return np.array([[float(label.split()[0].lower() in text) for label in TOPIC_LABELS] for text in texts])

    results = select_and_evaluate_topic_classifier(
        _topic_frame("train", 6), _topic_frame("validation", 2), _topic_frame("test", 2), encoder=fake_encoder
    )
    table = results["validation_table"]
    assert len(table) == 2 * len(REPRESENTATIONS)
    assert int(table["seleccionada"].sum()) == 1
    assert results["test_metrics"]["f1_macro"] == pytest.approx(1.0)
    assert set(results["test_predictions"].columns) == {"record_id", "tema", "tema_predicho"}


def test_topic_selection_does_not_encode_test_for_rejected_combinations() -> None:
    pytest.importorskip("sklearn")
    from claim_extraction.topics import TOPIC_LABELS, select_and_evaluate_topic_classifier

    encoded: list[tuple[str, int]] = []

    def recording_encoder(representation: str, texts: list[str]) -> np.ndarray:
        encoded.append((representation, len(texts)))
        return np.array([[float(label.split()[0].lower() in text) for label in TOPIC_LABELS] for text in texts])

    test = _topic_frame("test", 3)
    results = select_and_evaluate_topic_classifier(
        _topic_frame("train", 6),
        _topic_frame("validation", 2),
        test,
        encoder=recording_encoder,
        representations=("minilm", "mpnet"),
    )
    test_passes = [name for name, size in encoded if size == len(test)]
    assert test_passes == [results["selected"]["representation"]]


def test_topic_selection_rejects_unknown_labels() -> None:
    pytest.importorskip("sklearn")
    from claim_extraction.topics import select_and_evaluate_topic_classifier

    train = _topic_frame("train", 3)
    bad = train.assign(tema="Inventado")
    with pytest.raises(ValueError, match="desconocidos"):
        select_and_evaluate_topic_classifier(bad, train, train, representations=("tfidf",))


@pytest.mark.parametrize(
    "name",
    ["03_entrenar_modelos.ipynb", "04_evaluar_y_visualizar_modelos.ipynb", "05_clasificar_temas.ipynb"],
)
def test_downstream_notebooks_take_sizes_from_the_dataset_name(name: str) -> None:
    notebook = json.loads((ROOT / "notebooks" / name).read_text(encoding="utf-8"))
    source = "\n".join("".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "code")
    assert "DATASET_SPLIT_SIZES[DATASET_NAME]" in source
    assert "validate_sem1_splits(SPLIT_PATHS)" not in source
    assert "DATASET_NAME = 'claims_1500'" in source
    assert set(DATASET_SPLIT_SIZES) >= {"claims_500", "claims_1500"}
