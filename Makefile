VENV_PYTHON ?= $(shell if command -v python3.12 >/dev/null 2>&1; then echo python3.12; elif command -v python3.11 >/dev/null 2>&1; then echo python3.11; else echo python3; fi)
PYTHON ?= .venv/bin/python
INPUT ?=
OUT ?= artifacts
NEWS_INVENTORY ?= artifacts/collection/newsdata_inventory.csv
EXTRACTED_NEWS ?= artifacts/collection/newsdata_bodies.csv
COUNTRIES ?=
SAMPLE_SIZE ?= 100
VALIDATION_FRACTION ?= 0.2
LABEL_STUDIO_EXPORT ?=
MANIFEST ?=
EXCLUDE_RECORD_IDS ?=
AUDIT ?= artifacts/audit/labelstudio_claims_audit.csv
DUPLICATES ?= artifacts/audit/labelstudio_duplicate_review.csv
AUDIT_SUMMARY ?= artifacts/audit/labelstudio_audit_summary.json
REFERENCE_SET ?= artifacts/sem1_20261031/final/reference_claims.csv
REFERENCE_TRAIN ?= artifacts/sem1_20261031/final/train.csv
REFERENCE_VALIDATION ?= artifacts/sem1_20261031/final/validation.csv
REFERENCE_TEST ?= artifacts/sem1_20261031/final/test.csv
RUN_DIR ?= artifacts/runs/sem1_20261031_400_claims_v1
RUN_MODELS ?= t5s_spanish barto_spanish mbart50_es umt5_base_es

.DEFAULT_GOAL := help
.PHONY: help setup install test fetch-news extract-bodies prepare-corpus reference-template audit-labelstudio build-reference-manifest import-reference-set split-reference-set split-reference train infer evaluate run-sem1 resume-sem1 review-sheet

help:
	@echo "Comandos disponibles:"
	@echo "  make setup                         Crea .venv e instala el proyecto y las dependencias de modelos"
	@echo "  make test                          Ejecuta las pruebas unitarias"
	@echo "  make download-models               Descarga solamente los pesos base en la cache local de Hugging Face"
	@echo "  make fetch-news COUNTRIES=pe,ec   Consulta NewsData API y guarda el inventario local"
	@echo "  make extract-bodies NEWS_INVENTORY=/ruta.csv EXTRACTED_NEWS=/ruta.csv"
	@echo "  make prepare-corpus INPUT=/ruta.csv OUT=artifacts/candidates.jsonl"
	@echo "  make reference-template INPUT=artifacts/candidates.jsonl OUT=artifacts/references.csv SAMPLE_SIZE=100"
	@echo "  make audit-labelstudio LABEL_STUDIO_EXPORT=/ruta/export.json"
	@echo "  make build-reference-manifest LABEL_STUDIO_EXPORT=/ruta/export.json MANIFEST=/ruta/manifest.csv"
	@echo "  make import-reference-set LABEL_STUDIO_EXPORT=/ruta/export.json MANIFEST=/ruta/manifest.csv"
	@echo "  make split-reference-set REFERENCE_SET=artifacts/reference/reference_claims.csv"
	@echo "  make split-reference INPUT=artifacts/references.csv TRAIN=artifacts/train.csv VALIDATION=artifacts/validation.csv"
	@echo "  Modelos: t5s_spanish, barto_spanish, mbart50_es, umt5_base_es"
	@echo "  make train MODEL=t5s_spanish TRAIN=/ruta.csv VALIDATION=/ruta.csv OUT=artifacts/checkpoints/t5s_spanish"
	@echo "  make infer MODEL=t5s_spanish CHECKPOINT=/ruta INPUT=/ruta.jsonl OUT=artifacts/predictions.jsonl"
	@echo "  make evaluate PREDICTIONS=/ruta.jsonl REFERENCES=/ruta.csv OUT=artifacts/evaluation"
	@echo "  make run-sem1                     Entrena, infiere y evalúa los cuatro modelos sobre 400 referencias"
	@echo "  make resume-sem1                  Continúa una corrida Sem1 ya completada por modelo"
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

fetch-news:
	@test -n "$$NEWSDATA_API_KEY" || (echo "Define NEWSDATA_API_KEY en tu entorno" && exit 2)
	@test -n "$(COUNTRIES)" || (echo "Falta COUNTRIES=pe,ec,..." && exit 2)
	$(PYTHON) scripts/fetch_newsdata.py --output "$(NEWS_INVENTORY)" --countries "$(COUNTRIES)"

extract-bodies:
	@test -f "$(NEWS_INVENTORY)" || (echo "No existe NEWS_INVENTORY=$(NEWS_INVENTORY)" && exit 2)
	$(PYTHON) scripts/extract_news_bodies.py --input "$(NEWS_INVENTORY)" --output "$(EXTRACTED_NEWS)"

prepare-corpus:
	@test -n "$(INPUT)" || (echo "Falta INPUT=/ruta/al/export.csv" && exit 2)
	$(PYTHON) scripts/prepare_claim_corpus.py --input "$(INPUT)" --output "$(OUT)"

reference-template:
	@test -n "$(INPUT)" || (echo "Falta INPUT=/ruta/candidates.jsonl" && exit 2)
	$(PYTHON) scripts/create_reference_template.py --input "$(INPUT)" --output "$(OUT)" --sample-size "$(SAMPLE_SIZE)"

audit-labelstudio:
	@test -n "$(LABEL_STUDIO_EXPORT)" || (echo "Falta LABEL_STUDIO_EXPORT=/ruta/al/export.json" && exit 2)
	$(PYTHON) scripts/audit_labelstudio_export.py --input "$(LABEL_STUDIO_EXPORT)" --audit-output "$(AUDIT)" --duplicates-output "$(DUPLICATES)" --summary-output "$(AUDIT_SUMMARY)"

build-reference-manifest:
	@test -n "$(LABEL_STUDIO_EXPORT)" || (echo "Falta LABEL_STUDIO_EXPORT=/ruta/al/export.json" && exit 2)
	@test -n "$(MANIFEST)" || (echo "Falta MANIFEST=/ruta/al/manifest_final.csv" && exit 2)
	$(PYTHON) scripts/build_sem1_manifest.py --input "$(LABEL_STUDIO_EXPORT)" --output "$(MANIFEST)" $(if $(EXCLUDE_RECORD_IDS),--exclude-record-ids "$(EXCLUDE_RECORD_IDS)")

import-reference-set:
	@test -n "$(LABEL_STUDIO_EXPORT)" || (echo "Falta LABEL_STUDIO_EXPORT=/ruta/al/export.json" && exit 2)
	@test -n "$(MANIFEST)" || (echo "Falta MANIFEST=/ruta/al/manifest_final.csv" && exit 2)
	$(PYTHON) scripts/import_reference_set.py --input "$(LABEL_STUDIO_EXPORT)" --manifest "$(MANIFEST)" --output "$(REFERENCE_SET)" --require-complete

split-reference-set:
	$(PYTHON) scripts/build_sem1_splits.py --input "$(REFERENCE_SET)" --train-output "$(REFERENCE_TRAIN)" --validation-output "$(REFERENCE_VALIDATION)" --test-output "$(REFERENCE_TEST)"

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

run-sem1:
	$(PYTHON) scripts/run_sem1_experiment.py --train "$(REFERENCE_TRAIN)" --validation "$(REFERENCE_VALIDATION)" --test "$(REFERENCE_TEST)" --run-dir "$(RUN_DIR)" --models $(RUN_MODELS)

resume-sem1:
	$(PYTHON) scripts/run_sem1_experiment.py --train "$(REFERENCE_TRAIN)" --validation "$(REFERENCE_VALIDATION)" --test "$(REFERENCE_TEST)" --run-dir "$(RUN_DIR)" --models $(RUN_MODELS) --resume

review-sheet:
	@test -n "$(INPUT)" || (echo "Falta INPUT=/ruta/candidates.jsonl" && exit 2)
	@test -n "$(PREDICTIONS)" || (echo "Falta PREDICTIONS=/ruta/predictions.jsonl" && exit 2)
	$(PYTHON) scripts/build_blind_review_sheet.py --input "$(INPUT)" --predictions "$(PREDICTIONS)" --output-dir "$(OUT)" --sample-size "$(SAMPLE_SIZE)"

