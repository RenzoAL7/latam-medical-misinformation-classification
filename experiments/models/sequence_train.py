"""Word2Vec + LSTM and pretrained GloVe + BiLSTM article classifiers."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from experiments.models.common import run_cli


TOKEN_PATTERN = re.compile(r"[^\W_]+|[^\w\s]", flags=re.UNICODE)


def _tokens(text: str) -> list[str]:
    return TOKEN_PATTERN.findall(text.casefold())


def _add_sequence_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--epochs", type=int, default=15)
    parser.add_argument("--patience", type=int, default=3)
    parser.add_argument("--learning-rate", type=float, default=1e-3)
    parser.add_argument("--max-length", type=int, default=256)
    parser.add_argument("--embedding-dim", type=int, default=100)
    parser.add_argument("--hidden-size", type=int, default=64)
    parser.add_argument("--dropout", type=float, default=0.3)
    parser.add_argument("--glove-file", type=Path, help="Local GloVe text vectors; never downloaded by this script")
    parser.add_argument("--glove-dim", type=int, default=100)
    parser.add_argument("--window", type=int, default=5)
    parser.add_argument("--embedding-epochs", type=int, default=20)
    parser.add_argument("--min-count", type=int, default=1)


def _read_glove(path: Path, vocabulary: dict[str, int], dimensions: int) -> dict[str, list[float]]:
    if not path.is_file():
        raise FileNotFoundError(f"GloVe file does not exist: {path}")
    vectors: dict[str, list[float]] = {}
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            fields = line.rstrip().split()
            if len(fields) != dimensions + 1:
                # Some GloVe distributions start with a row-count/dimension header.
                if line_number == 1 and len(fields) == 2:
                    continue
                continue
            word = fields[0].casefold()
            if word not in vocabulary or word in vectors:
                continue
            try:
                vectors[word] = [float(value) for value in fields[1:]]
            except ValueError:
                continue
    if not vectors:
        raise ValueError(
            f"No GloVe vectors matched the training vocabulary in {path}; "
            "check that --glove-dim matches the file"
        )
    return vectors


def _build_vocabulary(tokenized_train: list[list[str]], min_count: int) -> dict[str, int]:
    from collections import Counter

    counts = Counter(token for row in tokenized_train for token in row)
    words = sorted(word for word, count in counts.items() if count >= min_count)
    return {"<pad>": 0, "<unk>": 1, **{word: index + 2 for index, word in enumerate(words)}}


def _make_model(torch: Any, vectors: Any, hidden_size: int, dropout: float, bidirectional: bool):
    from torch.nn.utils.rnn import pack_padded_sequence

    class ArticleLSTM(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.embedding = torch.nn.Embedding.from_pretrained(
                vectors, freeze=False, padding_idx=0
            )
            self.lstm = torch.nn.LSTM(
                input_size=int(vectors.shape[1]),
                hidden_size=hidden_size,
                batch_first=True,
                bidirectional=bidirectional,
            )
            output_size = hidden_size * (2 if bidirectional else 1)
            self.dropout = torch.nn.Dropout(dropout)
            self.classifier = torch.nn.Linear(output_size, 2)

        def forward(self, token_ids: Any, lengths: Any) -> Any:
            embedded = self.embedding(token_ids)
            packed = pack_padded_sequence(
                embedded, lengths.detach().cpu(), batch_first=True, enforce_sorted=False
            )
            _outputs, (hidden, _cell) = self.lstm(packed)
            if bidirectional:
                representation = torch.cat((hidden[-2], hidden[-1]), dim=1)
            else:
                representation = hidden[-1]
            return self.classifier(self.dropout(representation))

    return ArticleLSTM()


def _make_loader(torch: Any, sequences: list[list[int]], labels: list[int], batch_size: int, shuffle: bool, seed: int):
    from torch.nn.utils.rnn import pad_sequence
    from torch.utils.data import DataLoader, Dataset

    class ArticleDataset(Dataset):
        def __len__(self) -> int:
            return len(sequences)

        def __getitem__(self, index: int):
            sequence = sequences[index]
            return (
                torch.tensor(sequence, dtype=torch.long),
                len(sequence),
                torch.tensor(labels[index], dtype=torch.long),
            )

    def collate(batch: list[Any]):
        tokens, lengths, targets = zip(*batch)
        return pad_sequence(tokens, batch_first=True, padding_value=0), torch.tensor(lengths), torch.stack(targets)

    generator = torch.Generator().manual_seed(seed) if shuffle else None
    return DataLoader(
        ArticleDataset(), batch_size=batch_size, shuffle=shuffle, generator=generator, collate_fn=collate
    )


def _fit_sequence(mode: str, prepared: dict[str, Any], args: argparse.Namespace, run_dir: Path):
    try:
        import torch
        if mode == "word2vec_lstm":
            from gensim.models import Word2Vec
    except (ImportError, RuntimeError) as exc:
        extra = "sequence" if mode == "word2vec_lstm" else "sequence"
        raise RuntimeError(
            f"Missing or incompatible sequence dependencies. Install with: "
            f"python3 -m pip install -e '.[training,{extra}]'"
        ) from exc

    if args.batch_size < 1 or args.epochs < 1 or args.patience < 1 or args.max_length < 1:
        raise ValueError("batch size, epochs, patience, and max length must be positive")
    if (
        args.hidden_size < 1
        or args.embedding_dim < 1
        or args.learning_rate <= 0
        or args.window < 1
        or args.embedding_epochs < 1
        or args.min_count < 1
    ):
        raise ValueError("hidden size, embedding dimension, and learning rate must be positive")
    if mode == "glove_bilstm" and args.glove_file is None:
        raise ValueError("GloVe requires --glove-file pointing to a local licensed vector file")

    from experiments.models.torch_utils import predict_loader, set_seed, train_classifier

    set_seed(torch, args.seed)
    # Packed LSTM sequences are kept on CPU by default for reliable Mac support.
    if args.device == "cuda" and not torch.cuda.is_available():
        raise ValueError("CUDA was requested, but PyTorch cannot see a CUDA device")
    device = torch.device(
        "cuda" if args.device == "cuda" or (args.device == "auto" and torch.cuda.is_available()) else "cpu"
    )
    baseline = prepared["baseline"]
    text_by_split = {
        split: [baseline.build_text(row) for row in rows]
        for split, rows in prepared["by_split"].items()
    }
    train_tokens = [_tokens(text) for text in text_by_split["train"]]
    vocabulary = _build_vocabulary(train_tokens, args.min_count if mode == "word2vec_lstm" else 1)

    if mode == "word2vec_lstm":
        embedding_model = Word2Vec(
            sentences=train_tokens,
            vector_size=args.embedding_dim,
            window=args.window,
            min_count=args.min_count,
            workers=1,
            sg=1,
            seed=args.seed,
            epochs=args.embedding_epochs,
        )
        embedding_dim = args.embedding_dim
        pretrained = {word: embedding_model.wv[word] for word in vocabulary if word in embedding_model.wv}
        embedding_description = "Word2Vec skip-gram fit on train articles only"
    elif mode == "glove_bilstm":
        embedding_dim = args.glove_dim
        pretrained = _read_glove(args.glove_file, vocabulary, embedding_dim)
        embedding_description = "Pretrained local GloVe vectors; missing words use seeded random initialization"
    else:
        raise ValueError(f"Unknown sequence model: {mode}")

    set_seed(torch, args.seed)
    matrix = torch.randn(len(vocabulary), embedding_dim, dtype=torch.float32) * 0.05
    matrix[0].zero_()
    matched = 0
    for word, vector in pretrained.items():
        matrix[vocabulary[word]] = torch.as_tensor(vector, dtype=torch.float32)
        matched += 1

    def encode(texts: list[str]) -> list[list[int]]:
        result = []
        for text in texts:
            ids = [vocabulary.get(token, 1) for token in _tokens(text)[: args.max_length]]
            result.append(ids or [1])
        return result

    labels_by_split = {
        split: [baseline.LABEL_TO_ID[row["label"]] for row in rows]
        for split, rows in prepared["by_split"].items()
    }
    loaders = {
        split: _make_loader(
            torch, encode(text_by_split[split]), labels_by_split[split], args.batch_size,
            shuffle=(split == "train"), seed=args.seed,
        )
        for split in ("train", "validation")
    }
    is_bilstm = mode == "glove_bilstm"
    model = _make_model(torch, matrix, args.hidden_size, args.dropout, bidirectional=is_bilstm).to(device)

    def forward_batch(current_model: Any, batch: Any, current_device: Any):
        token_ids, lengths, targets = batch
        token_ids = token_ids.to(current_device)
        targets = targets.to(current_device)
        return current_model(token_ids, lengths), targets

    history = train_classifier(
        torch, model, loaders["train"], loaders["validation"], labels_by_split["train"],
        device, args.learning_rate, args.epochs, args.patience, forward_batch,
    )

    def predict(texts: list[str]):
        loader = _make_loader(torch, encode(texts), [0] * len(texts), args.batch_size, False, args.seed)
        return predict_loader(torch, model, loader, device, forward_batch)

    checkpoint = {
        "model_state_dict": {key: value.detach().cpu() for key, value in model.state_dict().items()},
        "vocabulary": vocabulary,
        "config": {
            "model_type": mode,
            "embedding_dim": embedding_dim,
            "hidden_size": args.hidden_size,
            "bidirectional": is_bilstm,
            "max_length": args.max_length,
            "label_to_id": baseline.LABEL_TO_ID,
        },
    }
    torch.save(checkpoint, run_dir / "model.pt")
    (run_dir / "training_history.json").write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")
    return predict, {
        "type": "GloVe + BiLSTM" if is_bilstm else "Word2Vec + LSTM",
        "embedding_source": embedding_description,
        "embedding_dim": embedding_dim,
        "embedding_vocabulary_coverage": matched / max(1, len(vocabulary) - 2),
        "vocabulary_size": len(vocabulary),
        "max_length": args.max_length,
        "hidden_size": args.hidden_size,
        "batch_size": args.batch_size,
        "device": str(device),
        "best_epoch": int(min(history, key=lambda item: item["validation_loss"])["epoch"]),
        "epochs_trained": len(history),
    }


def main(mode: str) -> int:
    descriptions = {
        "word2vec_lstm": "Entrena Word2Vec + LSTM con vocabulario y embeddings aprendidos solo en train.",
        "glove_bilstm": "Entrena GloVe + BiLSTM usando vectores locales proporcionados por el usuario.",
    }
    if mode not in descriptions:
        raise ValueError(f"Unknown sequence model: {mode}")
    return run_cli(mode, descriptions[mode], lambda prepared, args, run_dir: _fit_sequence(mode, prepared, args, run_dir), _add_sequence_arguments)
