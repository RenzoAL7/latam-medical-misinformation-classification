"""Carga compatible de tokenizers y modelos seq2seq desde la caché local."""

from __future__ import annotations

from typing import Any


def configure_tokenizer(tokenizer: Any, *, model_id: str | None) -> Any:
    """Configura los códigos de idioma necesarios para modelos multilingües."""
    if model_id == "mbart50_es":
        tokenizer.src_lang = "es_XX"
        tokenizer.tgt_lang = "es_XX"
    return tokenizer


def configure_model(model: Any, tokenizer: Any, *, model_id: str) -> Any:
    """Fija el idioma de generación cuando el modelo lo necesita."""
    if model_id == "mbart50_es":
        language_id = tokenizer.lang_code_to_id["es_XX"]
        model.config.forced_bos_token_id = language_id
        if getattr(model, "generation_config", None) is not None:
            model.generation_config.forced_bos_token_id = language_id
    return model


def load_tokenizer(checkpoint: str, *, model_id: str | None = None, **kwargs: Any):
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(checkpoint, **kwargs)
    return configure_tokenizer(tokenizer, model_id=model_id)


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

