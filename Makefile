# Developer entry points. `make help` lists them.
.DEFAULT_GOAL := help
SHELL := /bin/bash
VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: help venv install fmt lint typecheck test test-cov check run up down logs \
        migrate migration psql redis-cli docker-build clean

help: ## List available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

venv: ## Create the virtualenv
	python3 -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip

install: venv ## Install all dependencies
	$(PIP) install --quiet -r requirements-dev.txt
	@echo "Installed. Next: cp .env.example .env && make up"

fmt: ## Format and apply safe lint fixes
	$(VENV)/bin/ruff check --fix .
	$(VENV)/bin/ruff format .

lint: ## Lint and check formatting (no writes)
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .

typecheck: ## mypy --strict
	$(VENV)/bin/mypy app tests

test: ## Run the test suite
	$(PY) -m pytest

test-cov: ## Run tests with a coverage report
	$(PY) -m pytest --cov=app --cov-report=term-missing --cov-report=xml

check: lint typecheck test ## Everything CI runs, in CI's order

run: ## Run the API locally with reload (needs Postgres/Redis; see `make up`)
	$(VENV)/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8080

up: ## Start the full local stack in Docker
	docker compose up --build -d
	@echo "API: http://localhost:8080/docs"

down: ## Stop the stack and discard volumes
	docker compose down -v

logs: ## Follow the API log
	docker compose logs -f api

migrate: ## Apply migrations to the configured database
	$(VENV)/bin/alembic upgrade head

migration: ## Autogenerate a revision: make migration m="add scores"
	@test -n "$(m)" || (echo 'Usage: make migration m="description"' && exit 1)
	$(VENV)/bin/alembic revision --autogenerate -m "$(m)"

psql: ## Open a psql shell against the local stack
	docker compose exec postgres psql -U leaderboard -d leaderboard

redis-cli: ## Open a redis-cli against the local stack
	docker compose exec redis redis-cli

docker-build: ## Build the production image
	docker build -t leaderboard-service:local .

clean: ## Remove caches and the virtualenv
	rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml htmlcov
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
