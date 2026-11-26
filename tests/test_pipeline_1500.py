from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from claim_extraction.analysis import metrics_by_topic
from claim_extraction.data import near_duplicate_groups
from claim_extraction.workflow import (
    BASE_SEED,
    DATASET_SPLIT_SIZES,
    assert_run_matches,
    load_selected_evaluations,
    remove_intermediate_checkpoints,
    run_name_for,
    split_fingerprints,
    training_history,
)


ROOT = Path(__file__).resolve().parents[1]


def test_run_names_do_not_mix_datasets_and_keep_existing_500_runs() -> None:
    assert run_name_for("claims_500", "epocas_5") == "claims_500_v1"
    assert run_name_for("claims_1500", "epocas_5") == "claims_1500_epocas_5"
    with pytest.raises(ValueError, match="Conjunto"):
        run_name_for("claims_900", "epocas_5")


def test_run_manifest_guard_rejects_another_seed() -> None:
    fingerprints = {"train": "a"}
    manifest = {"dataset_name": "claims_1500", "variant": "lora", "split_fingerprints": fingerprints, "seed": 1}
    assert_run_matches(manifest, dataset_name="claims_1500", variant="lora", fingerprints=fingerprints, seed=1)
    assert_run_matches(manifest, dataset_name="claims_1500", variant="lora", fingerprints=fingerprints)
    with pytest.raises(ValueError, match="seed"):
        assert_run_matches(manifest, dataset_name="claims_1500", variant="lora", fingerprints=fingerprints, seed=2)


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


def test_metrics_by_topic_uses_the_annotated_topic() -> None:
    frame = _per_prediction()
    references = frame.drop_duplicates("record_id")[["record_id", "tema_real"]].rename(columns={"tema_real": "tema"})
    table = metrics_by_topic(frame.drop(columns="tema_real"), references)
    assert set(table["tema"]) == {"Cáncer", "Otra"}
    assert set(table["n"]) == {20}
    alto = table[table["model_id"].eq("alto")]
    assert alto["meteor"].tolist() == pytest.approx([0.6, 0.6])
    with pytest.raises(ValueError, match="tema"):
        metrics_by_topic(frame, references.rename(columns={"tema": "x"}))


def _write_evaluated_run(runs_dir: Path, name: str, model_ids: list[str], manifest: dict[str, object]) -> None:
    evaluation_dir = runs_dir / name / "evaluation"
    evaluation_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / name / "run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    pd.DataFrame(
        [{"model_id": model_id, "model_display_name": model_id.upper(), "meteor": 0.5} for model_id in model_ids]
    ).to_csv(evaluation_dir / "summary.csv", index=False)
    pd.DataFrame(
        [
            {"record_id": "001", "model_id": model_id, "model_display_name": model_id.upper(), "claim": "", "reference_claim": "r", "meteor": 0.5}
            for model_id in model_ids
        ]
    ).to_csv(evaluation_dir / "per_prediction.csv", index=False)


def _manifest(variant: str, fingerprints: dict[str, str], seed: int = BASE_SEED) -> dict[str, object]:
    return {"dataset_name": "claims_1500", "variant": variant, "split_fingerprints": fingerprints, "seed": seed}


def test_final_loader_takes_each_model_from_its_selected_variant(tmp_path: Path) -> None:
    fingerprints = {"train": "a"}
    # Las dos corridas evaluaron a ambos modelos; solo debe entrar la variante elegida de cada uno.
    for variant in ("lora", "epocas_10"):
        _write_evaluated_run(tmp_path, run_name_for("claims_1500", variant), ["m1", "m2"], _manifest(variant, fingerprints))

    summary, per_prediction = load_selected_evaluations(
        tmp_path,
        dataset_name="claims_1500",
        selected_variants={"m1": "lora", "m2": "epocas_10"},
        fingerprints=fingerprints,
    )
    assert summary[["model_id", "variante"]].values.tolist() == [["m1", "lora"], ["m2", "epocas_10"]]
    assert len(per_prediction) == 2
    assert set(per_prediction["model_id"]) == {"m1", "m2"}
    assert per_prediction["record_id"].iloc[0] == "001"
    assert per_prediction["claim"].iloc[0] == ""


def test_final_loader_lists_every_missing_run_and_rejects_other_data(tmp_path: Path) -> None:
    fingerprints = {"train": "a"}
    arguments = {"dataset_name": "claims_1500", "selected_variants": {"m1": "lora", "m2": "epocas_5"}, "fingerprints": fingerprints}
    with pytest.raises(FileNotFoundError, match="claims_1500_lora/m1.*claims_1500_epocas_5/m2"):
        load_selected_evaluations(tmp_path, **arguments)

    # Una corrida que no evaluó al modelo pedido también cuenta como faltante.
    _write_evaluated_run(tmp_path, "claims_1500_lora", ["otro"], _manifest("lora", fingerprints))
    _write_evaluated_run(tmp_path, "claims_1500_epocas_5", ["m2"], _manifest("epocas_5", fingerprints))
    with pytest.raises(FileNotFoundError, match="claims_1500_lora/m1"):
        load_selected_evaluations(tmp_path, **arguments)

    _write_evaluated_run(tmp_path, "claims_1500_lora", ["m1"], _manifest("lora", {"train": "otro"}))
    with pytest.raises(ValueError, match="split_fingerprints"):
        load_selected_evaluations(tmp_path, **arguments)
    _write_evaluated_run(tmp_path, "claims_1500_lora", ["m1"], _manifest("lora", fingerprints, seed=999))
    with pytest.raises(ValueError, match="seed"):
        load_selected_evaluations(tmp_path, **arguments)


def _trainer_state(epochs: int) -> dict[str, object]:
    return {"log_history": [{"epoch": float(epoch), "eval_loss": 1.0 / epoch} for epoch in range(1, epochs + 1)]}


def test_training_history_prefers_the_saved_copy_and_falls_back_to_the_last_checkpoint(tmp_path: Path) -> None:
    for step, epochs in ((50, 1), (250, 5), (100, 2)):
        checkpoint = tmp_path / f"checkpoint-{step}"
        checkpoint.mkdir()
        (checkpoint / "trainer_state.json").write_text(json.dumps(_trainer_state(epochs)), encoding="utf-8")
    # Corridas anteriores: el historial completo está en el checkpoint de mayor paso, no en el último por orden alfabético.
    assert len(training_history(tmp_path)["log_history"]) == 5

    (tmp_path / "trainer_state.json").write_text(json.dumps(_trainer_state(7)), encoding="utf-8")
    assert len(training_history(tmp_path)["log_history"]) == 7
    with pytest.raises(FileNotFoundError):
        training_history(tmp_path / "vacio")


def test_intermediate_checkpoints_are_removed_only_after_best_and_history_exist(tmp_path: Path) -> None:
    for name in ("checkpoint-100", "checkpoint-200"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "model.safetensors").write_text("x", encoding="utf-8")
    (tmp_path / "best").mkdir()
    with pytest.raises(FileNotFoundError, match="no se borra nada"):
        remove_intermediate_checkpoints(tmp_path)
    assert (tmp_path / "checkpoint-100").is_dir()

    (tmp_path / "trainer_state.json").write_text(json.dumps(_trainer_state(2)), encoding="utf-8")
    assert remove_intermediate_checkpoints(tmp_path) == ["checkpoint-100", "checkpoint-200"]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["best", "trainer_state.json"]


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


def _own_groups(*frames: pd.DataFrame) -> list[int]:
    return list(range(sum(len(frame) for frame in frames)))


def test_topic_selection_covers_six_combinations_and_marks_one() -> None:
    pytest.importorskip("sklearn")
    from claim_extraction.topics import REPRESENTATIONS, TOPIC_CV_FOLDS, TOPIC_LABELS, select_and_evaluate_topic_classifier

    def fake_encoder(representation: str, texts: list[str]) -> np.ndarray:
        return np.array([[float(label.split()[0].lower() in text) for label in TOPIC_LABELS] for text in texts])

    train, validation = _topic_frame("train", 6), _topic_frame("validation", 2)
    results = select_and_evaluate_topic_classifier(
        train, validation, _topic_frame("test", 2), groups=_own_groups(train, validation), encoder=fake_encoder
    )
    table = results["cv_table"]
    assert len(table) == 2 * len(REPRESENTATIONS)
    assert int(table["seleccionada"].sum()) == 1
    assert len(results["fold_scores"]) == 2 * len(REPRESENTATIONS) * TOPIC_CV_FOLDS
    assert results["test_metrics"]["f1_macro"] == pytest.approx(1.0)
    assert set(results["test_predictions"].columns) == {"record_id", "tema", "tema_predicho"}


def test_topic_folds_keep_near_duplicates_together_and_exclude_test() -> None:
    pytest.importorskip("sklearn")
    from claim_extraction.topics import select_and_evaluate_topic_classifier

    train, validation, test = _topic_frame("train", 6), _topic_frame("validation", 2), _topic_frame("test", 2)
    # Cada tres filas forman un grupo de casi-duplicados.
    groups = [position // 3 for position in range(len(train) + len(validation))]
    results = select_and_evaluate_topic_classifier(
        train, validation, test, groups=groups, representations=("tfidf",)
    )
    assignments = results["fold_assignments"]
    assert not set(assignments["record_id"]).intersection(test["record_id"])
    assert len(assignments) == len(train) + len(validation)
    assert assignments.assign(group=groups).groupby("group")["fold"].nunique().eq(1).all()


def test_topic_selection_requires_one_group_per_row() -> None:
    pytest.importorskip("sklearn")
    from claim_extraction.topics import select_and_evaluate_topic_classifier

    train = _topic_frame("train", 6)
    with pytest.raises(ValueError, match="groups"):
        select_and_evaluate_topic_classifier(train, train, train, groups=[0, 1], representations=("tfidf",))


def test_topic_selection_does_not_encode_test_for_rejected_combinations() -> None:
    pytest.importorskip("sklearn")
    from claim_extraction.topics import TOPIC_LABELS, select_and_evaluate_topic_classifier

    encoded: list[tuple[str, int]] = []

    def recording_encoder(representation: str, texts: list[str]) -> np.ndarray:
        encoded.append((representation, len(texts)))
        return np.array([[float(label.split()[0].lower() in text) for label in TOPIC_LABELS] for text in texts])

    train, validation, test = _topic_frame("train", 6), _topic_frame("validation", 2), _topic_frame("test", 3)
    results = select_and_evaluate_topic_classifier(
        train,
        validation,
        test,
        groups=_own_groups(train, validation),
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
        select_and_evaluate_topic_classifier(bad, train, train, groups=_own_groups(bad, train), representations=("tfidf",))


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


def _code(name: str) -> str:
    notebook = json.loads((ROOT / "notebooks" / name).read_text(encoding="utf-8"))
    return "\n".join("".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "code")


@pytest.mark.parametrize("name", ["03_entrenar_modelos.ipynb", "04_evaluar_y_visualizar_modelos.ipynb"])
def test_training_and_evaluation_notebooks_use_one_run_per_variant(name: str) -> None:
    source = _code(name)
    assert "run_name_for(DATASET_NAME, variant)" in source
    assert "BASE_SEED" in source
    assert "_seed" not in source
    # Run All recorre las cuatro variantes; ya no hay que cambiar VARIANT a mano.
    assert "VARIANT =" not in source


def test_training_notebook_runs_every_variant_and_survives_a_failed_model() -> None:
    source = _code("03_entrenar_modelos.ipynb")
    assert "VARIANTS_TO_RUN = list(EXPERIMENT_VARIANTS)" in source
    assert "release_accelerator_memory()" in source
    assert "except Exception as exc:" in source and "failures.append(" in source


def test_evaluation_notebook_evaluates_every_ready_variant() -> None:
    source = _code("04_evaluar_y_visualizar_modelos.ipynb")
    assert "map(load_run, EXPERIMENT_VARIANTS)" in source
    assert "for run in READY_RUNS:" in source


def test_evaluation_notebook_writes_the_final_results_folder() -> None:
    source = _code("04_evaluar_y_visualizar_modelos.ipynb")
    assert "'05_resultados_finales'" in source
    for name in ("load_selected_evaluations", "metrics_by_topic", "training_history"):
        assert name in source
    # Sem1 termina en las métricas de los cuatro modelos: sin pruebas de hipótesis.
    for removed in ("model_significance_tests", "bootstrap_uncertainty", "with_spanish_meteor", "friedman", "wilcoxon"):
        assert removed not in source.lower()
    # Los gráficos del conjunto salen una sola vez, en la carpeta final.
    assert "FINAL_FIGURES_DIR / '03_distribucion_longitudes.png'" in source
    assert "FINAL_FIGURES_DIR / filename" in source
    assert "'04_tokens_noticias.png'" in source and "'05_tokens_afirmaciones.png'" in source
    assert " FIGURES_DIR / filename" not in source
    # Ya no se copian resultados por modelo ni se guardan CSV intermedios.
    for removed in ("EVALUATION_DIR / model_id", "token_counts_by_model.csv", "article_and_claim_lengths.csv", "training_curves.csv", "checkpoint-*"):
        assert removed not in source


def test_evaluation_tables_have_unique_numbers() -> None:
    import re

    source = _code("04_evaluar_y_visualizar_modelos.ipynb")
    for folder in ("TABLES_DIR", "FIGURES_DIR", "FINAL_TABLES_DIR", "FINAL_FIGURES_DIR"):
        numbers = re.findall(rf"(?<![A-Z_]){folder} / '(\d\d)_", source)
        assert numbers and len(numbers) == len(set(numbers)), folder


def test_topic_notebook_passes_near_duplicate_groups_to_cross_validation() -> None:
    notebook = json.loads((ROOT / "notebooks" / "05_clasificar_temas.ipynb").read_text(encoding="utf-8"))
    source = "\n".join("".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "code")
    assert "near_duplicate_groups(" in source
    assert "groups=groups" in source
    assert "validation_table" not in source

