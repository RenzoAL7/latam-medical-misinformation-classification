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


def select_and_evaluate_topic_classifier(
    train: pd.DataFrame,
    validation: pd.DataFrame,
    test: pd.DataFrame,
    *,
    encoder: Encoder | None = None,
    representations: Sequence[str] = REPRESENTATIONS,
    seed: int = 20261103,
) -> dict[str, Any]:
    """Elige la mejor combinación por F1 macro en validation y la evalúa en test.

    Test solo se transforma y predice después de fijar la combinación elegida.
    En un empate gana la primera combinación en el orden declarado.
    """
    train, validation, test = (
        _validated(train, "train"),
        _validated(validation, "validation"),
        _validated(test, "test"),
    )
    train_texts = _texts(train)
    validation_texts = _texts(validation)
    train_labels = train["tema"].tolist()
    validation_labels = validation["tema"].tolist()

    candidates = []
    fitted: dict[tuple[str, str], tuple[Any, Any]] = {}
    for representation in representations:
        featurize = _featurizer(representation, train_texts, encoder)
        train_features = featurize(train_texts)
        validation_features = featurize(validation_texts)
        for classifier_name in CLASSIFIERS:
            classifier = _classifier(classifier_name, seed).fit(train_features, train_labels)
            scores = _scores(validation_labels, classifier.predict(validation_features).tolist())
            candidates.append({"representation": representation, "classifier": classifier_name, **scores})
            fitted[(representation, classifier_name)] = (featurize, classifier)

    validation_table = pd.DataFrame(candidates)
    best = int(validation_table["f1_macro"].to_numpy().argmax())
    validation_table["seleccionada"] = validation_table.index == best
    selected = (validation_table.at[best, "representation"], validation_table.at[best, "classifier"])

    featurize, classifier = fitted[selected]
    test_labels = test["tema"].tolist()
    predicted = classifier.predict(featurize(_texts(test))).tolist()

    from sklearn.metrics import confusion_matrix, precision_recall_fscore_support

    labels = [label for label in TOPIC_LABELS if label in set(test_labels) | set(predicted)]
    precision, recall, f1, support = precision_recall_fscore_support(
        test_labels, predicted, labels=labels, zero_division=0
    )
    return {
        "validation_table": validation_table,
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
