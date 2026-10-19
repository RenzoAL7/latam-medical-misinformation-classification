from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "experiments" / "models"
NOTEBOOK = MODELS / "01_tfidf_naive_bayes" / "baseline.ipynb"


def test_only_the_first_model_notebook_remains() -> None:
    model_directories = sorted(
        path.name
        for path in MODELS.iterdir()
        if path.is_dir() and not path.name.startswith("__")
    )

    assert model_directories == ["01_tfidf_naive_bayes"]
    assert not list(MODELS.rglob("train.py"))
    assert NOTEBOOK.is_file()


def test_baseline_notebook_contains_the_article_level_contract() -> None:
    payload = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    assert payload["nbformat"] == 4
    assert all(cell["cell_type"] in {"markdown", "code"} for cell in payload["cells"])

    source = "\n".join(
        "\n".join(cell.get("source", []))
        for cell in payload["cells"]
        if cell["cell_type"] == "code"
    )
    assert "TfidfVectorizer" in source
    assert "MultinomialNB" in source
    assert "gold/training/" in source
    assert "title" in source and "body" in source
