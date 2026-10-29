VENV_PYTHON ?= $(shell if command -v python3.12 >/dev/null 2>&1; then echo python3.12; elif test -x /Users/renzo/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3.12; then echo /Users/renzo/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3.12; else echo python3; fi)
PYTHON ?= .venv/bin/python
INPUT ?=
OUT ?= artifacts
SAMPLE_SIZE ?= 100
VALIDATION_FRACTION ?= 0.2

.DEFAULT_GOAL := help
.PHONY: help setup install test download-models prepare-corpus reference-template split-reference train infer evaluate review-sheet

help:
	@echo "Comandos disponibles:"
	@echo "  make setup                         Crea .venv e instala el proyecto y las dependencias de modelos"
	@echo "  make test                          Ejecuta las pruebas unitarias"
	@echo "  make download-models               Descarga solamente los pesos base en la cache local de Hugging Face"
	@echo "  make prepare-corpus INPUT=/ruta.csv OUT=artifacts/candidates.jsonl"
	@echo "  make reference-template INPUT=artifacts/candidates.jsonl OUT=artifacts/references.csv SAMPLE_SIZE=100"
	@echo "  make split-reference INPUT=artifacts/references.csv TRAIN=artifacts/train.csv VALIDATION=artifacts/validation.csv"
	@echo "  make train MODEL=umt5_base_es TRAIN=/ruta.csv VALIDATION=/ruta.csv OUT=artifacts/checkpoints/umt5"
	@echo "  make infer MODEL=umt5_base_es CHECKPOINT=/ruta INPUT=/ruta.jsonl OUT=artifacts/predictions.jsonl"
	@echo "  make evaluate PREDICTIONS=/ruta.jsonl REFERENCES=/ruta.csv OUT=artifacts/evaluation"
	@echo "  make review-sheet INPUT=/ruta.jsonl PREDICTIONS=/ruta.jsonl OUT=artifacts/review SAMPLE_SIZE=100"

setup:
	$(VENV_PYTHON) -m venv .venv
	$(MAKE) install

install:
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -e '.[dev,models]'
	$(PYTHON) -m nltk.downloader wordnet omw-1.4

test:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -B -m pytest -q -p no:cacheprovider

download-models:
	$(PYTHON) scripts/download_models.py --all

prepare-corpus:
	@test -n "$(INPUT)" || (echo "Falta INPUT=/ruta/al/export.csv" && exit 2)
	$(PYTHON) scripts/prepare_claim_corpus.py --input "$(INPUT)" --output "$(OUT)"

reference-template:
	@test -n "$(INPUT)" || (echo "Falta INPUT=/ruta/candidates.jsonl" && exit 2)
	$(PYTHON) scripts/create_reference_template.py --input "$(INPUT)" --output "$(OUT)" --sample-size "$(SAMPLE_SIZE)"

split-reference:
	@test -n "$(INPUT)" || (echo "Falta INPUT=/ruta/references.csv" && exit 2)
	@test -n "$(TRAIN)" || (echo "Falta TRAIN=/ruta/train.csv" && exit 2)
	@test -n "$(VALIDATION)" || (echo "Falta VALIDATION=/ruta/validation.csv" && exit 2)
	$(PYTHON) scripts/split_reference.py --input "$(INPUT)" --train-output "$(TRAIN)" --validation-output "$(VALIDATION)" --validation-fraction "$(VALIDATION_FRACTION)"

train:
	@test -n "$(MODEL)" || (echo "Falta MODEL=<id>" && exit 2)
	@test -n "$(TRAIN)" || (echo "Falta TRAIN=/ruta/train.csv" && exit 2)
	@test -n "$(VALIDATION)" || (echo "Falta VALIDATION=/ruta/dev.csv" && exit 2)
	$(PYTHON) scripts/train_seq2seq.py --model "$(MODEL)" --train "$(TRAIN)" --validation "$(VALIDATION)" --output-dir "$(OUT)"

infer:
	@test -n "$(MODEL)" || (echo "Falta MODEL=<id>" && exit 2)
	@test -n "$(CHECKPOINT)" || (echo "Falta CHECKPOINT=/ruta/al/checkpoint" && exit 2)
	@test -n "$(INPUT)" || (echo "Falta INPUT=/ruta/candidates.jsonl" && exit 2)
	$(PYTHON) scripts/run_inference.py --model "$(MODEL)" --checkpoint "$(CHECKPOINT)" --input "$(INPUT)" --output "$(OUT)"

evaluate:
	@test -n "$(PREDICTIONS)" || (echo "Falta PREDICTIONS=/ruta/predictions.jsonl" && exit 2)
	@test -n "$(REFERENCES)" || (echo "Falta REFERENCES=/ruta/references.csv" && exit 2)
	$(PYTHON) scripts/evaluate_claims.py --predictions "$(PREDICTIONS)" --references "$(REFERENCES)" --output-dir "$(OUT)"

review-sheet:
	@test -n "$(INPUT)" || (echo "Falta INPUT=/ruta/candidates.jsonl" && exit 2)
	@test -n "$(PREDICTIONS)" || (echo "Falta PREDICTIONS=/ruta/predictions.jsonl" && exit 2)
	$(PYTHON) scripts/build_blind_review_sheet.py --input "$(INPUT)" --predictions "$(PREDICTIONS)" --output-dir "$(OUT)" --sample-size "$(SAMPLE_SIZE)"
