PYTHON ?= python3

.PHONY: help test unittest lint typecheck run dry-run validate status report clean

help:
	@echo "Targets: test, unittest, lint, typecheck, run, dry-run, validate, status, report, clean"

test:
	pytest -v

unittest:
	$(PYTHON) -m unittest discover -s tests -v

lint:
	ruff check src tests

typecheck:
	mypy src

validate:
	$(PYTHON) main.py validate --input sample_data/clients_sample.csv

dry-run:
	$(PYTHON) main.py --dry-run --input sample_data/clients_sample.csv

run:
	$(PYTHON) main.py --input sample_data/clients_sample.csv

status:
	$(PYTHON) main.py status

report:
	$(PYTHON) main.py report

clean:
	rm -rf output/ .pytest_cache/ .ruff_cache/ .mypy_cache/ build/ dist/ *.egg-info/
	find . -type d -name "__pycache__" -exec rm -rf {} +
