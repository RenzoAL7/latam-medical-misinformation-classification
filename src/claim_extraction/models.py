"""Carga compatible de tokenizers y modelos seq2seq desde la caché local."""

from __future__ import annotations

from typing import Any


def load_tokenizer(checkpoint: str, **kwargs: Any):
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(checkpoint, **kwargs)


def load_seq2seq_model(checkpoint: str, *, model_id: str, **kwargs: Any):
    """Carga uMT5 explícitamente porque su config publicada omite model_type."""
    if model_id == "umt5_base_es":
        from transformers import UMT5Config, UMT5ForConditionalGeneration

        config = UMT5Config.from_pretrained(checkpoint, **kwargs)
        return UMT5ForConditionalGeneration.from_pretrained(
            checkpoint, config=config, **kwargs
        )

    from transformers import AutoModelForSeq2SeqLM

    return AutoModelForSeq2SeqLM.from_pretrained(checkpoint, **kwargs)
