PYTHON ?= .venv/bin/python

.DEFAULT_GOAL := help
.PHONY: help setup install test

help:
	@echo "Comandos disponibles:"
	@echo "  make setup                       Crea .venv e instala dependencias"
	@echo "  make install                     Instala dependencias de desarrollo"
	@echo "  make test                        Ejecuta las pruebas"

setup:
	python3 -m venv .venv
	$(MAKE) install

install:
	$(PYTHON) -m pip install -e '.[dev]'

test:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -B -m pytest -q -p no:cacheprovider
