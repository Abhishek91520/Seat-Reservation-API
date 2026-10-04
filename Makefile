.PHONY: up down test-db-up test test-unit test-conc test-chaos lint burst fuzz reconcile smoke help

PYTHON ?= python
PYTEST ?= pytest
RUFF ?= ruff
BASE_URL ?= http://localhost:8000
TEST_DATABASE_URL ?= postgresql://postgres:postgres@localhost:5433/testdb

help:
	@echo "Available commands:"
	@echo "  make up               - Start the full docker stack (app + postgres)"
	@echo "  make down             - Stop all running docker stacks"
	@echo "  make test-db-up       - Start the fast test postgres container on port 5433"
	@echo "  make test             - Run all unit, integration, and concurrency tests"
	@echo "  make test-unit        - Run unit tests only"
	@echo "  make test-conc        - Run high-concurrency test suite"
	@echo "  make test-chaos       - Run chaos and failure injection tests"
	@echo "  make lint             - Run ruff linter and formatting checks"
	@echo "  make burst            - Run burst benchmark against BASE_URL (default: http://localhost:8000)"
	@echo "  make fuzz             - Run 60s random operations fuzzer"
	@echo "  make reconcile        - Reconcile database and metrics consistency"
	@echo "  make smoke            - Run end-to-end smoke tests against BASE_URL"

up:
	docker compose up -d --build

down:
	docker compose down -v
	docker compose -f docker-compose.test.yml down -v 2>/dev/null || true
	docker compose -f docker-compose.prodlike.yml down -v 2>/dev/null || true
	docker compose -f docker-compose.pgbouncer.yml down -v 2>/dev/null || true

test-db-up:
	docker compose -f docker-compose.test.yml up -d

test:
	$(PYTEST) -m "not chaos and not canary" -v

test-unit:
	$(PYTEST) tests/unit -v

test-conc:
	$(PYTEST) tests/concurrency -v

test-chaos:
	$(PYTEST) tests/chaos -v -m chaos

lint:
	$(RUFF) check .
	$(RUFF) format --check .

burst:
	$(PYTHON) scripts/burst.py $(BASE_URL)

fuzz:
	$(PYTHON) scripts/fuzz.py $(BASE_URL)

reconcile:
	$(PYTHON) scripts/reconcile.py $(BASE_URL)

smoke:
	$(PYTHON) scripts/smoke.py $(BASE_URL)
