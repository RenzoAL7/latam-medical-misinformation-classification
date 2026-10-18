"""Small PyTorch helpers used by the sequence and transformer experiments."""

from __future__ import annotations

import copy
import random
from collections import Counter
from typing import Any, Callable


def set_seed(torch: Any, seed: int) -> None:
    random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def resolve_device(torch: Any, requested: str) -> Any:
    if requested == "auto":
        if torch.cuda.is_available():
            requested = "cuda"
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            requested = "mps"
        else:
            requested = "cpu"
    if requested == "cuda" and not torch.cuda.is_available():
        raise ValueError("CUDA was requested, but PyTorch cannot see a CUDA device")
    if requested == "mps" and not (
        hasattr(torch.backends, "mps") and torch.backends.mps.is_available()
    ):
        raise ValueError("MPS was requested, but PyTorch cannot use Apple Metal on this machine")
    return torch.device(requested)


def train_classifier(
    torch: Any,
    model: Any,
    train_loader: Any,
    validation_loader: Any,
    train_labels: list[int],
    device: Any,
    learning_rate: float,
    epochs: int,
    patience: int,
    forward_batch: Callable,
) -> list[dict[str, float]]:
    """Fit with class-weighted loss and choose the checkpoint by validation loss."""

    counts = Counter(train_labels)
    if set(counts) != {0, 1}:
        raise ValueError("The training split must contain both classes")
    total = len(train_labels)
    class_weights = torch.tensor(
        [total / (2 * counts[index]) for index in (0, 1)],
        dtype=torch.float32,
        device=device,
    )
    criterion = torch.nn.CrossEntropyLoss(weight=class_weights)
    optimizer = torch.optim.AdamW(model.parameters(), lr=learning_rate)
    best_loss = float("inf")
    best_state: dict[str, Any] | None = None
    stale_epochs = 0
    history: list[dict[str, float]] = []

    for epoch in range(1, epochs + 1):
        model.train()
        train_total = 0.0
        train_n = 0
        for batch in train_loader:
            optimizer.zero_grad(set_to_none=True)
            logits, targets = forward_batch(model, batch, device)
            loss = criterion(logits, targets)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            optimizer.step()
            train_total += float(loss.detach().cpu()) * len(targets)
            train_n += len(targets)

        model.eval()
        validation_total = 0.0
        validation_n = 0
        with torch.inference_mode():
            for batch in validation_loader:
                logits, targets = forward_batch(model, batch, device)
                loss = criterion(logits, targets)
                validation_total += float(loss.detach().cpu()) * len(targets)
                validation_n += len(targets)
        if not train_n or not validation_n:
            raise ValueError("Training and validation splits must both contain examples")
        train_loss = train_total / train_n
        validation_loss = validation_total / validation_n
        history.append({"epoch": float(epoch), "train_loss": train_loss, "validation_loss": validation_loss})

        if validation_loss < best_loss:
            best_loss = validation_loss
            best_state = {
                key: value.detach().cpu().clone()
                for key, value in model.state_dict().items()
            }
            stale_epochs = 0
        else:
            stale_epochs += 1
            if stale_epochs >= patience:
                break

    if best_state is None:
        raise RuntimeError("Training did not produce a valid checkpoint")
    model.load_state_dict(best_state)
    model.to(device)
    return history


def predict_loader(
    torch: Any,
    model: Any,
    loader: Any,
    device: Any,
    forward_batch: Callable,
) -> tuple[list[int], list[float]]:
    model.eval()
    predictions: list[int] = []
    scores: list[float] = []
    with torch.inference_mode():
        for batch in loader:
            logits, _targets = forward_batch(model, batch, device)
            probabilities = torch.softmax(logits, dim=1)
            predictions.extend(probabilities.argmax(dim=1).cpu().tolist())
            scores.extend(probabilities[:, 1].cpu().tolist())
    return predictions, scores
