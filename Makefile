.PHONY: test test-verbose test-report demo
PYTHON ?= python3
export PYTHONPATH := backend

test:
	$(PYTHON) -m unittest discover -s tests

test-verbose:
	$(PYTHON) -m unittest discover -s tests -v

test-report:
	$(PYTHON) tests/run_tests.py

demo:
	$(PYTHON) scripts/demo_scenario_map.py
