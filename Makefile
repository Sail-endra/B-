PY      := ./.venv/bin/python
export PYTHONPATH := .

# Eval runs offline by default so the numbers never depend on network weather or
# on whose key happened to be set. Drop OFFLINE=0 on the command line to measure
# the real stack: `make eval OFFLINE=0`.
OFFLINE ?= 1
export COPILOT_OFFLINE = $(OFFLINE)

.PHONY: help venv benchmark setup serve eval eval-fast eval-ablate calibrate grounding baseline test lint clean

help:
	@echo "make venv        create .venv and install dependencies"
	@echo "make benchmark   build the labelled benchmark corpus from materials/"
	@echo "make setup       alias for benchmark"
	@echo "make serve       run the API and frontend on :8471"
	@echo "make eval        full suite, writes eval/RESULTS.md"
	@echo "make eval-fast   retrieval + refusal only (~seconds)"
	@echo "make eval-ablate the ablation table"
	@echo "make calibrate   sweep and report the refusal threshold"
	@echo "make test        unit tests"
	@echo ""
	@echo "Add OFFLINE=0 to any eval target to use configured API keys."

venv:
	python3.11 -m venv .venv || python3 -m venv .venv
	$(PY) -m pip install --quiet --upgrade pip
	$(PY) -m pip install --quiet -r requirements.txt
	@echo "done. Copy .env.example to .env to add keys (all optional)."

benchmark:
	$(PY) -m eval.benchmark.build

setup: benchmark


serve:
	COPILOT_OFFLINE=0 $(PY) -m uvicorn api.main:app --reload --port 8471

eval:
	$(PY) -m eval.report

eval-fast:
	$(PY) -m eval.report --fast

eval-ablate:
	$(PY) -m eval.run_retrieval --ablate

calibrate:
	$(PY) -m eval.run_refusal --calibrate

grounding:
	$(PY) -m eval.run_grounding

baseline:
	COPILOT_OFFLINE=0 $(PY) -m eval.run_baseline --json eval/baseline.json

test:
	COPILOT_EMBEDDER=tfidf $(PY) -m pytest tests -q

clean:
	# The -wal and -shm sidecars must go with the database. Deleting copilot.db
	# alone leaves a write-ahead log referring to a file that no longer exists,
	# and the next open fails with "disk I/O error".
	rm -f data/copilot.db data/copilot.db-wal data/copilot.db-shm data/embedder*.pkl
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
