"""TF-IDF + Naive Bayes and TF-IDF + linear SVM implementations."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from experiments.models.common import run_cli


def _fit_sparse(mode: str, prepared: dict[str, Any], args: Any, run_dir: Path):
    try:
        import joblib
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.pipeline import Pipeline
        if mode == "naive_bayes":
            from sklearn.naive_bayes import MultinomialNB
        elif mode == "linear_svm":
            from sklearn.svm import LinearSVC
        else:
            raise ValueError(f"Unknown sparse model: {mode}")
    except ImportError as exc:
        raise RuntimeError(
            "Missing scikit-learn/joblib. Install with: "
            "python3 -m pip install -e '.[training]'"
        ) from exc

    baseline = prepared["baseline"]
    train_rows = prepared["by_split"]["train"]
    train_texts = [baseline.build_text(row) for row in train_rows]
    train_labels = [baseline.LABEL_TO_ID[row["label"]] for row in train_rows]
    if mode == "naive_bayes":
        classifier = MultinomialNB(alpha=1.0, fit_prior=False)
        model_type = "TfidfVectorizer + MultinomialNB"
    else:
        classifier = LinearSVC(class_weight="balanced", random_state=args.seed)
        model_type = "TfidfVectorizer + LinearSVC"
    pipeline = Pipeline(
        [
            (
                "tfidf",
                TfidfVectorizer(
                    lowercase=True,
                    strip_accents="unicode",
                    ngram_range=(1, 2),
                    min_df=1,
                    sublinear_tf=True,
                    max_features=30_000,
                ),
            ),
            ("classifier", classifier),
        ]
    )
    pipeline.fit(train_texts, train_labels)
    joblib.dump(pipeline, run_dir / "model.joblib")

    def predict(texts: list[str]):
        predictions = pipeline.predict(texts)
        estimator = pipeline.named_steps["classifier"]
        if hasattr(estimator, "predict_proba"):
            scores = pipeline.predict_proba(texts)[:, 1]
        elif hasattr(estimator, "decision_function"):
            scores = pipeline.decision_function(texts)
        else:  # pragma: no cover - both configured estimators expose a score
            scores = None
        return predictions, scores

    return predict, {
        "type": model_type,
        "class_weighting": "balanced prior" if mode == "naive_bayes" else "balanced",
        "ngram_range": [1, 2],
        "max_features": 30_000,
        "max_iter": None,
    }


def main(mode: str) -> int:
    descriptions = {
        "naive_bayes": "Entrena TF-IDF + Multinomial Naive Bayes con la partición de artículos del baseline.",
        "linear_svm": "Entrena TF-IDF + SVM lineal con la partición de artículos del baseline.",
    }
    if mode not in descriptions:
        print(f"Unknown sparse model: {mode}", file=sys.stderr)
        return 2
    return run_cli(mode, descriptions[mode], lambda prepared, args, run_dir: _fit_sparse(mode, prepared, args, run_dir))
