from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from claim_extraction.analysis import (
    average_over_seeds,
    bootstrap_uncertainty,
    holm_adjust,
    metrics_by_topic,
    model_significance_tests,
    seed_stability,
)
from claim_extraction.data import near_duplicate_groups
from claim_extraction.workflow import (
    BASE_SEED,
    DATASET_SPLIT_SIZES,
    EXPERIMENT_SEEDS,
    assert_run_matches,
    load_per_prediction_by_seed,
    run_name_for,
    split_fingerprints,
)


ROOT = Path(__file__).resolve().parents[1]


def test_run_names_do_not_mix_datasets_and_keep_existing_500_runs() -> None:
    assert run_name_for("claims_500", "epocas_5") == "claims_500_v1"
    assert run_name_for("claims_1500", "epocas_5") == "claims_1500_epocas_5"
    with pytest.raises(ValueError, match="Conjunto"):
        run_name_for("claims_900", "epocas_5")


def test_run_names_separate_seeds_but_keep_the_base_seed_folder() -> None:
    assert EXPERIMENT_SEEDS[0] == BASE_SEED
    assert len(set(EXPERIMENT_SEEDS)) == len(EXPERIMENT_SEEDS)
    assert run_name_for("claims_500", "epocas_5", BASE_SEED) == "claims_500_v1"
    assert run_name_for("claims_500", "epocas_5", 20261104) == "claims_500_v1_seed20261104"
    assert run_name_for("claims_1500", "lora", 20261105) == "claims_1500_lora_seed20261105"


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


def _scores_frame(scores: dict[str, list[float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"record_id": str(index), "model_id": model, "model_display_name": model.upper(), "meteor": value}
            for model, values in scores.items()
            for index, value in enumerate(values)
        ]
    )


def test_holm_adjusts_in_the_received_order_and_never_exceeds_one() -> None:
    assert holm_adjust([0.01, 0.04, 0.03]).tolist() == pytest.approx([0.03, 0.06, 0.06])
    assert holm_adjust([0.5, 0.9]).tolist() == pytest.approx([1.0, 1.0])


def test_pairwise_tests_match_scipy_and_read_a_clear_winner() -> None:
    stats = pytest.importorskip("scipy.stats")
    offsets = np.linspace(0.01, 0.3, 30)
    base = np.linspace(0.1, 0.5, 30)
    frame = _scores_frame({"alto": (base + offsets).tolist(), "medio": (base + offsets / 2).tolist(), "bajo": base.tolist()})

    friedman, pairs = model_significance_tests(frame, n_boot=200)

    assert friedman["hay_diferencia_global"] is True
    assert friedman["modelos"] == 3
    assert len(pairs) == 3
    first = pairs.iloc[0]
    assert (first["modelo_mayor_media"], first["modelo_comparado"]) == ("ALTO", "MEDIO")
    assert first["p_wilcoxon"] == pytest.approx(stats.wilcoxon(offsets / 2).pvalue)
    assert first["efecto_rango_biserial"] == pytest.approx(1.0)
    assert first["noticias_gana_mayor_media"] == 30
    assert first["lectura"] == "ALTO supera a MEDIO"
    assert pairs["p_holm"].ge(pairs["p_wilcoxon"]).all()


def test_pairwise_tests_never_call_identical_models_different() -> None:
    pytest.importorskip("scipy.stats")
    values = np.linspace(0.1, 0.5, 20).tolist()
    friedman, pairs = model_significance_tests(_scores_frame({"a": values, "b": values, "c": values}), n_boot=50)
    assert friedman["hay_diferencia_global"] is False
    assert pairs["p_wilcoxon"].eq(1.0).all()
    assert pairs["empates"].eq(20).all()
    assert pairs["lectura"].str.contains("no se distinguen").all()


def test_a_pair_is_not_read_as_a_difference_when_friedman_finds_none(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("scipy.stats")
    import claim_extraction.analysis as analysis

    monkeypatch.setattr(analysis, "_friedman", lambda scores: (0.1, 0.9))
    base = np.linspace(0.1, 0.5, 30)
    frame = _scores_frame({"a": (base + 0.2).tolist(), "b": base.tolist(), "c": (base + 0.1).tolist()})
    _, pairs = analysis.model_significance_tests(frame, n_boot=50)
    assert pairs["p_holm"].lt(0.05).all()
    assert pairs["lectura"].str.contains("no se distinguen").all()


def test_two_models_skip_friedman_but_keep_the_paired_test() -> None:
    pytest.importorskip("scipy.stats")
    base = np.linspace(0.1, 0.5, 30)
    friedman, pairs = model_significance_tests(_scores_frame({"a": (base + 0.2).tolist(), "b": base.tolist()}), n_boot=50)
    assert friedman["p"] is None
    assert pairs.iloc[0]["lectura"] == "A supera a B"


def test_significance_tests_require_the_same_news_and_two_models() -> None:
    pytest.importorskip("scipy.stats")
    with pytest.raises(ValueError, match="mismas noticias"):
        model_significance_tests(_per_prediction().iloc[:-1], n_boot=10)
    with pytest.raises(ValueError, match="dos modelos"):
        model_significance_tests(_scores_frame({"solo": [0.1, 0.2]}), n_boot=10)


def test_average_over_seeds_keeps_one_score_per_news_and_model() -> None:
    first = _scores_frame({"a": [0.2, 0.4], "b": [0.1, 0.1]})
    second = _scores_frame({"a": [0.4, 0.6], "b": [0.3, 0.1]})
    averaged = average_over_seeds({1: first, 2: second}, metrics=("meteor",))
    assert len(averaged) == 4
    assert averaged["n_semillas"].eq(2).all()
    value = averaged.loc[averaged["model_id"].eq("a") & averaged["record_id"].eq("0"), "meteor"].iloc[0]
    assert value == pytest.approx(0.3)


def test_seed_analyses_reject_seeds_with_different_news() -> None:
    complete = _scores_frame({"a": [0.2, 0.4]})
    with pytest.raises(ValueError, match="mismos modelos"):
        average_over_seeds({1: complete, 2: complete.iloc[:-1]}, metrics=("meteor",))
    with pytest.raises(ValueError, match="duplicadas"):
        seed_stability({1: pd.concat([complete, complete])})


def test_seed_stability_counts_how_often_each_model_comes_first() -> None:
    frames = {
        1: _scores_frame({"a": [0.6, 0.6], "b": [0.4, 0.4]}),
        2: _scores_frame({"a": [0.5, 0.5], "b": [0.4, 0.4]}),
        3: _scores_frame({"a": [0.3, 0.3], "b": [0.4, 0.4]}),
    }
    per_seed, summary = seed_stability(frames)
    assert len(per_seed) == 6
    top = summary.iloc[0]
    assert top["model_id"] == "a"
    assert top["n_semillas"] == 3
    assert top["veces_primero"] == 2
    assert top["media"] == pytest.approx(1.4 / 3)
    assert top["desviacion"] == pytest.approx(np.std([0.6, 0.5, 0.3], ddof=1))


def _write_evaluated_run(runs_dir: Path, name: str, model_id: str, manifest: dict[str, object]) -> None:
    model_dir = runs_dir / name / "evaluation" / model_id
    model_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / name / "run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    pd.DataFrame(
        [{"record_id": "001", "model_id": model_id, "model_display_name": model_id, "claim": "", "reference_claim": "r", "meteor": 0.5}]
    ).to_csv(model_dir / "per_prediction.csv", index=False)


def test_seed_loader_combines_the_variant_selected_for_each_model(tmp_path: Path) -> None:
    fingerprints = {"train": "a"}
    for seed in (BASE_SEED, 20261104):
        for model_id, variant in (("m1", "lora"), ("m2", "epocas_10")):
            manifest = {"dataset_name": "claims_1500", "variant": variant, "split_fingerprints": fingerprints, "seed": seed}
            _write_evaluated_run(tmp_path, run_name_for("claims_1500", variant, seed), model_id, manifest)

    loaded = load_per_prediction_by_seed(
        tmp_path,
        dataset_name="claims_1500",
        selected_variants={"m1": "lora", "m2": "epocas_10"},
        seeds=(BASE_SEED, 20261104),
        fingerprints=fingerprints,
    )
    assert set(loaded) == {BASE_SEED, 20261104}
    assert set(loaded[BASE_SEED]["model_id"]) == {"m1", "m2"}
    assert loaded[BASE_SEED]["record_id"].iloc[0] == "001"
    assert loaded[BASE_SEED]["claim"].iloc[0] == ""


def test_seed_loader_lists_every_missing_run_and_rejects_wrong_seed(tmp_path: Path) -> None:
    fingerprints = {"train": "a"}
    arguments = {"dataset_name": "claims_1500", "selected_variants": {"m1": "lora"}, "fingerprints": fingerprints}
    with pytest.raises(FileNotFoundError, match="claims_1500_lora/m1.*claims_1500_lora_seed20261104/m1"):
        load_per_prediction_by_seed(tmp_path, seeds=(BASE_SEED, 20261104), **arguments)

    wrong = {"dataset_name": "claims_1500", "variant": "lora", "split_fingerprints": fingerprints, "seed": 999}
    _write_evaluated_run(tmp_path, "claims_1500_lora", "m1", wrong)
    with pytest.raises(ValueError, match="seed"):
        load_per_prediction_by_seed(tmp_path, seeds=(BASE_SEED,), **arguments)


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


@pytest.mark.parametrize("name", ["03_entrenar_modelos.ipynb", "04_evaluar_y_visualizar_modelos.ipynb"])
def test_training_and_evaluation_notebooks_separate_runs_by_seed(name: str) -> None:
    notebook = json.loads((ROOT / "notebooks" / name).read_text(encoding="utf-8"))
    source = "\n".join("".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "code")
    assert "run_name_for(DATASET_NAME, VARIANT, SEED)" in source
    assert "seed=SEED)" in source
    assert "SEED = EXPERIMENT_SEEDS[0]" in source


def test_evaluation_notebook_runs_the_fixed_statistical_analysis() -> None:
    notebook = json.loads((ROOT / "notebooks" / "04_evaluar_y_visualizar_modelos.ipynb").read_text(encoding="utf-8"))
    source = "\n".join("".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "code")
    for name in ("load_per_prediction_by_seed", "average_over_seeds", "seed_stability", "model_significance_tests"):
        assert name in source


def test_topic_notebook_passes_near_duplicate_groups_to_cross_validation() -> None:
    notebook = json.loads((ROOT / "notebooks" / "05_clasificar_temas.ipynb").read_text(encoding="utf-8"))
    source = "\n".join("".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "code")
    assert "near_duplicate_groups(" in source
    assert "groups=groups" in source
    assert "validation_table" not in source
