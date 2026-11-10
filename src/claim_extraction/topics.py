"""Clasificación secundaria del tema de una noticia, sin fine-tuning."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import numpy as np
import pandas as pd

from .text import build_source_text, clean_text


TOPIC_LABELS = (
    "Cáncer",
    "Cardiometabólicas",
    "Nutrición y suplementos",
    "Salud mental y neurología",
    "Infecciosas e inmunidad",
    "Sistema de salud y políticas",
    "Otra",
)
EMBEDDING_MODELS = {
    "minilm": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    "mpnet": "sentence-transformers/paraphrase-multilingual-mpnet-base-v2",
}
REPRESENTATIONS = ("tfidf", "minilm", "mpnet")
CLASSIFIERS = ("logistic_regression", "linear_svm")
# El protocolo pide «título y comienzo del cuerpo»; aquí se fija ese comienzo.
TOPIC_BODY_CHARS = 1000
TOPIC_CV_FOLDS = 5
_REQUIRED_COLUMNS = {"record_id", "title", "body", "tema"}

Encoder = Callable[[str, list[str]], np.ndarray]


def topic_input(title: object, body: object) -> str:
    return build_source_text(title, clean_text(body)[:TOPIC_BODY_CHARS])


def _validated(frame: pd.DataFrame, split: str) -> pd.DataFrame:
    missing = sorted(_REQUIRED_COLUMNS.difference(frame.columns))
    if missing:
        raise ValueError(f"{split} no contiene las columnas: {', '.join(missing)}.")
    unknown = sorted(set(frame["tema"]).difference(TOPIC_LABELS))
    if unknown:
        raise ValueError(f"{split} tiene temas desconocidos: {', '.join(unknown)}.")
    return frame


def _texts(frame: pd.DataFrame) -> list[str]:
    return [topic_input(title, body) for title, body in zip(frame["title"], frame["body"], strict=True)]


def sentence_transformer_encoder(
    *, cache_dir: str | None = None, batch_size: int = 32, device: str | None = None
) -> Encoder:
    """Crea un codificador que carga cada modelo de embeddings una sola vez."""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError(
            "Falta sentence-transformers. Instala: pip install -e '.[analysis,models,dev]'"
        ) from exc
    loaded: dict[str, Any] = {}

    def encode(representation: str, texts: list[str]) -> np.ndarray:
        if representation not in loaded:
            loaded[representation] = SentenceTransformer(
                EMBEDDING_MODELS[representation], cache_folder=cache_dir, device=device
            )
        return loaded[representation].encode(
            texts, batch_size=batch_size, normalize_embeddings=True, show_progress_bar=False
        )

    return encode


def _featurizer(
    representation: str, train_texts: list[str], encoder: Encoder | None
) -> Callable[[list[str]], Any]:
    """Devuelve la transformación ya ajustada solo con textos de entrenamiento."""
    if representation == "tfidf":
        from sklearn.feature_extraction.text import TfidfVectorizer

        vectorizer = TfidfVectorizer(ngram_range=(1, 2), min_df=2, sublinear_tf=True)
        vectorizer.fit(train_texts)
        return vectorizer.transform
    if representation not in EMBEDDING_MODELS:
        raise ValueError(f"Representación desconocida: {representation}.")
    if encoder is None:
        raise ValueError("Las representaciones con embeddings requieren un encoder.")
    return lambda texts: encoder(representation, texts)


def _classifier(name: str, seed: int) -> Any:
    from sklearn.linear_model import LogisticRegression
    from sklearn.svm import LinearSVC

    if name == "logistic_regression":
        return LogisticRegression(class_weight="balanced", max_iter=2000, random_state=seed)
    if name == "linear_svm":
        return LinearSVC(class_weight="balanced", random_state=seed)
    raise ValueError(f"Clasificador desconocido: {name}.")


def _scores(true: Sequence[str], predicted: Sequence[str]) -> dict[str, float]:
    from sklearn.metrics import accuracy_score, f1_score

    # Solo temas presentes en el split: uno ausente sumaría un cero artificial al promedio.
    labels = [label for label in TOPIC_LABELS if label in set(true)]
    return {
        "f1_macro": float(f1_score(true, predicted, labels=labels, average="macro", zero_division=0)),
        "accuracy": float(accuracy_score(true, predicted)),
    }


def _topic_folds(
    labels: Sequence[str], groups: Sequence[int], n_splits: int, seed: int
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Reparte por tema sin separar noticias del mismo grupo de casi-duplicados."""
    from sklearn.model_selection import StratifiedGroupKFold

    splitter = StratifiedGroupKFold(n_splits=n_splits, shuffle=True, random_state=seed)
    return list(splitter.split(np.zeros(len(labels)), labels, groups))


def select_and_evaluate_topic_classifier(
    train: pd.DataFrame,
    validation: pd.DataFrame,
    test: pd.DataFrame,
    *,
    groups: Sequence[int],
    encoder: Encoder | None = None,
    representations: Sequence[str] = REPRESENTATIONS,
    n_splits: int = TOPIC_CV_FOLDS,
    seed: int = 20261103,
) -> dict[str, Any]:
    """Elige la combinación por validación cruzada sobre train y validation, y la evalúa en test.

    ``groups`` trae un grupo de casi-duplicados por fila de train seguido de
    validation. TF-IDF se ajusta en cada fold solo con su parte de entrenamiento.
    Test solo se transforma y predice después de fijar la combinación elegida,
    reentrenada con train y validation. En un empate gana la primera combinación
    en el orden declarado.
    """
    train, validation, test = (
        _validated(train, "train"),
        _validated(validation, "validation"),
        _validated(test, "test"),
    )
    pool = pd.concat([train, validation], ignore_index=True)
    if len(groups) != len(pool):
        raise ValueError("groups debe tener un valor por cada fila de train y validation.")
    pool_texts = _texts(pool)
    pool_labels = pool["tema"].tolist()
    folds = _topic_folds(pool_labels, groups, n_splits, seed)
    # Los embeddings no dependen del fold: se calculan una sola vez para todo el conjunto.
    embeddings = {
        representation: _featurizer(representation, pool_texts, encoder)(pool_texts)
        for representation in representations
        if representation != "tfidf"
    }

    fold_rows = []
    for representation in representations:
        for fold, (fit_index, held_index) in enumerate(folds):
            fit_labels = [pool_labels[position] for position in fit_index]
            held_labels = [pool_labels[position] for position in held_index]
            if representation == "tfidf":
                featurize = _featurizer(representation, [pool_texts[position] for position in fit_index], None)
                fit_features = featurize([pool_texts[position] for position in fit_index])
                held_features = featurize([pool_texts[position] for position in held_index])
            else:
                fit_features = embeddings[representation][fit_index]
                held_features = embeddings[representation][held_index]
            for classifier_name in CLASSIFIERS:
                classifier = _classifier(classifier_name, seed).fit(fit_features, fit_labels)
                scores = _scores(held_labels, classifier.predict(held_features).tolist())
                fold_rows.append(
                    {"representation": representation, "classifier": classifier_name, "fold": fold, **scores}
                )

    fold_scores = pd.DataFrame(fold_rows)
    cv_table = (
        fold_scores.groupby(["representation", "classifier"], sort=False, as_index=False)
        .agg(
            f1_macro_media=("f1_macro", "mean"),
            f1_macro_desviacion=("f1_macro", "std"),
            accuracy_media=("accuracy", "mean"),
        )
    )
    best = int(cv_table["f1_macro_media"].to_numpy().argmax())
    cv_table["seleccionada"] = cv_table.index == best
    selected = (cv_table.at[best, "representation"], cv_table.at[best, "classifier"])

    fold_of_row = np.empty(len(pool), dtype=int)
    for fold, (_, held_index) in enumerate(folds):
        fold_of_row[held_index] = fold

    featurize = _featurizer(selected[0], pool_texts, encoder)
    pool_features = embeddings[selected[0]] if selected[0] in embeddings else featurize(pool_texts)
    classifier = _classifier(selected[1], seed).fit(pool_features, pool_labels)
    test_labels = test["tema"].tolist()
    predicted = classifier.predict(featurize(_texts(test))).tolist()

    from sklearn.metrics import confusion_matrix, precision_recall_fscore_support

    labels = [label for label in TOPIC_LABELS if label in set(test_labels) | set(predicted)]
    precision, recall, f1, support = precision_recall_fscore_support(
        test_labels, predicted, labels=labels, zero_division=0
    )
    return {
        "cv_table": cv_table,
        "fold_scores": fold_scores,
        "fold_assignments": pd.DataFrame({"record_id": pool["record_id"].tolist(), "fold": fold_of_row}),
        "selected": {"representation": selected[0], "classifier": selected[1]},
        "test_metrics": {"n": len(test_labels), **_scores(test_labels, predicted)},
        "per_class": pd.DataFrame(
            {"tema": labels, "precision": precision, "recall": recall, "f1": f1, "n": support}
        ),
        "confusion": pd.DataFrame(
            confusion_matrix(test_labels, predicted, labels=labels), index=labels, columns=labels
        ),
        "test_predictions": pd.DataFrame(
            {"record_id": test["record_id"].tolist(), "tema": test_labels, "tema_predicho": predicted}
        ),
    }
