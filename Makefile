VENV_PYTHON ?= $(shell if command -v python3.12 >/dev/null 2>&1; then echo python3.12; elif command -v python3.11 >/dev/null 2>&1; then echo python3.11; else echo python3; fi)
PYTHON ?= .venv/bin/python
INPUT ?=
OUT ?= artifacts
SAMPLE_SIZE ?= 100
VALIDATION_FRACTION ?= 0.2
LABEL_STUDIO_EXPORT ?=
MANIFEST ?=
GOLD ?= artifacts/gold/reference_claims.csv
GOLD_TRAIN ?= artifacts/gold/train.csv
GOLD_VALIDATION ?= artifacts/gold/validation.csv
GOLD_TEST ?= artifacts/gold/test.csv

.DEFAULT_GOAL := help
.PHONY: help setup install test download-models prepare-corpus reference-template build-gold-manifest import-labelstudio-gold split-gold split-reference train infer evaluate review-sheet

help:
	@echo "Comandos disponibles:"
	@echo "  make setup                         Crea .venv e instala el proyecto y las dependencias de modelos"
	@echo "  make test                          Ejecuta las pruebas unitarias"
	@echo "  make download-models               Descarga solamente los pesos base en la cache local de Hugging Face"
	@echo "  make prepare-corpus INPUT=/ruta.csv OUT=artifacts/candidates.jsonl"
	@echo "  make reference-template INPUT=artifacts/candidates.jsonl OUT=artifacts/references.csv SAMPLE_SIZE=100"
	@echo "  make build-gold-manifest LABEL_STUDIO_EXPORT=/ruta/export.json MANIFEST=/ruta/manifest.csv"
	@echo "  make import-labelstudio-gold LABEL_STUDIO_EXPORT=/ruta/export.json MANIFEST=/ruta/manifest.csv"
	@echo "  make split-gold GOLD=artifacts/gold/reference_claims.csv"
	@echo "  make split-reference INPUT=artifacts/references.csv TRAIN=artifacts/train.csv VALIDATION=artifacts/validation.csv"
	@echo "  Modelos: t5s_spanish, barto_spanish, mbart50_es, umt5_base_es"
	@echo "  make train MODEL=t5s_spanish TRAIN=/ruta.csv VALIDATION=/ruta.csv OUT=artifacts/checkpoints/t5s_spanish"
	@echo "  make infer MODEL=t5s_spanish CHECKPOINT=/ruta INPUT=/ruta.jsonl OUT=artifacts/predictions.jsonl"
	@echo "  make evaluate PREDICTIONS=/ruta.jsonl REFERENCES=/ruta.csv OUT=artifacts/evaluation"
	@echo "  make review-sheet ...              Utilidad futura; fuera del alcance de Sem1"

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

build-gold-manifest:
	@test -n "$(LABEL_STUDIO_EXPORT)" || (echo "Falta LABEL_STUDIO_EXPORT=/ruta/al/export.json" && exit 2)
	@test -n "$(MANIFEST)" || (echo "Falta MANIFEST=/ruta/al/manifest_final.csv" && exit 2)
	$(PYTHON) scripts/build_sem1_manifest.py --input "$(LABEL_STUDIO_EXPORT)" --output "$(MANIFEST)"

import-labelstudio-gold:
	@test -n "$(LABEL_STUDIO_EXPORT)" || (echo "Falta LABEL_STUDIO_EXPORT=/ruta/al/export.json" && exit 2)
	@test -n "$(MANIFEST)" || (echo "Falta MANIFEST=/ruta/al/manifest_final.csv" && exit 2)
	$(PYTHON) scripts/import_annotation_gold.py --input "$(LABEL_STUDIO_EXPORT)" --manifest "$(MANIFEST)" --output "$(GOLD)" --require-complete

split-gold:
	$(PYTHON) scripts/build_sem1_splits.py --input "$(GOLD)" --train-output "$(GOLD_TRAIN)" --validation-output "$(GOLD_VALIDATION)" --test-output "$(GOLD_TEST)"

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

