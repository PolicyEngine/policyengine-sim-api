# Simplified Makefile using docker-compose
.PHONY: help dev up down build test clean logs format check push-pr-branch

# Load environment variables if .env exists
ifneq (,$(wildcard deployment/.env))
    include deployment/.env
    export
endif

help:
	@echo "PolicyEngine Sim API - Available commands:"
	@echo ""
	@echo "Development (local simulation-executor via docker-compose, port 8082):"
	@echo "  make up [service=x]   - Start services in the background"
	@echo "  make dev              - Start services in the foreground (build + reload)"
	@echo "  make down             - Stop services"
	@echo "  make logs [service=x] - Tail service logs"
	@echo "  make ps               - Show running services"
	@echo ""
	@echo "Testing:"
	@echo "  make test             - Unit tests for all projects and libs"
	@echo "  make test-service service=x - Unit tests for one compose service"
	@echo "  make test-integration - Integration tests (services must be up)"
	@echo "  make test-complete    - Unit + integration (manages services)"
	@echo ""
	@echo "Clients:"
	@echo "  make generate-clients - Regenerate the OpenAPI Python client"
	@echo "  make publish-clients  - Publish the client to PyPI (needs PYPI_TOKEN)"
	@echo ""
	@echo "Maintenance:"
	@echo "  make build            - Build Docker images"
	@echo "  make format           - Format code with ruff"
	@echo "  make check            - ruff check + pyright"
	@echo "  make update           - uv lock --upgrade across packages"
	@echo "  make clean            - Remove caches and stop containers"
	@echo "  make push-pr-branch   - Push current branch to origin for a PR"
	@echo ""
	@echo "Deployment is automated via GitHub Actions to Modal; there is no manual deploy target."

# Development commands
dev:
	docker-compose -f deployment/docker-compose.yml up --build

up:
ifdef service
	docker-compose -f deployment/docker-compose.yml up -d $(service)
else
	docker-compose -f deployment/docker-compose.yml up -d
endif

down:
	docker-compose -f deployment/docker-compose.yml down

logs:
ifdef service
	docker-compose -f deployment/docker-compose.yml logs -f $(service)
else
	docker-compose -f deployment/docker-compose.yml logs -f
endif

# Build commands
build:
	docker-compose -f deployment/docker-compose.yml build --parallel

# Client generation
generate-clients:
	@echo "Generating API clients..."
	@./scripts/generate-clients.sh

publish-clients: generate-clients
	@echo "Publishing API clients to PyPI..."
	@if [ -z "$(PYPI_TOKEN)" ]; then \
		echo "Please set PYPI_TOKEN environment variable"; \
		exit 1; \
	fi
	@PYPI_TOKEN=$(PYPI_TOKEN) ./scripts/publish-clients.sh

# Testing
test:
	@echo "Running unit tests for all projects and libs..."
	@for proj in projects/policyengine-simulation-executor \
		projects/policyengine-simulation-gateway \
		libs/policyengine-simulation-contract \
		libs/policyengine-simulation-observability; do \
		echo "Testing $$proj..."; \
		(cd "$$proj" && uv sync --extra test && uv run pytest tests/ -v) || exit 1; \
	done
	@echo "✅ All unit tests passed"

test-service:
ifndef service
	@echo "Please specify service: make test-service service=simulation-executor"
else
	docker-compose -f deployment/docker-compose.yml run --rm $(service) sh -c "cd /app/projects/policyengine-$(service) && uv run --extra test pytest"
endif

# Integration testing
test-integration: generate-clients
	@echo "Running integration tests against local services..."
	@echo "Make sure services are running with 'make up' first!"
	@cd projects/policyengine-apis-integ && \
		uv sync --extra test && \
		uv run pytest tests/ -v -m "not requires_gcp"

test-integration-all: generate-clients
	@echo "Running all integration tests against local services..."
	@echo "Make sure services are running with 'make up' first!"
	@echo "Note: Workflow tests will be skipped without GCP credentials"
	@cd projects/policyengine-apis-integ && \
		uv sync --extra test && \
		uv run pytest tests/ -v

test-integration-with-services:
	@echo "Starting services and running integration tests..."
	@./scripts/test-integration-local.sh

# Full test suite including integration
test-all: test test-integration
	@echo "✅ All tests passed!"

# Complete test suite with services startup/shutdown
test-complete: test test-integration-with-services
	@echo "✅ All tests including integration passed!"

# Update dependencies
update:
	@echo "Updating dependencies for all packages..."
	@for pyproject in libs/*/pyproject.toml projects/*/pyproject.toml; do \
		if [ -f "$$pyproject" ]; then \
			dir=$$(dirname "$$pyproject"); \
			echo "Updating $$dir..."; \
			(cd "$$dir" && uv lock --upgrade); \
		fi \
	done
	@echo "✅ All dependencies updated"

# Code quality
format:
	@echo "Formatting code with ruff..."
	@for dir in projects/*/src libs/*/src; do \
		if [ -d "$$dir" ]; then \
			echo "Formatting $$dir..."; \
			uv run ruff format $$dir; \
		fi \
	done

check:
	@echo "Running code quality checks..."
	@for dir in projects/*/src libs/*/src; do \
		if [ -d "$$dir" ]; then \
			echo "Checking $$dir..."; \
			uv run ruff check $$dir; \
			uv run pyright $$dir; \
		fi \
	done

push-pr-branch:
	@BRANCH=$$(git branch --show-current); \
	if [ -z "$$BRANCH" ]; then \
		echo "Unable to determine current branch"; \
		exit 1; \
	fi; \
	if [ "$$BRANCH" = "main" ]; then \
		echo "Refusing to open a PR from main"; \
		exit 1; \
	fi; \
	REMOTE_URL=$$(git remote get-url origin 2>/dev/null || true); \
	case "$$REMOTE_URL" in \
		*PolicyEngine/policyengine-sim-api* ) ;; \
		* ) echo "Missing canonical origin remote PolicyEngine/policyengine-sim-api"; exit 1 ;; \
	esac; \
	git push -u origin HEAD:$$BRANCH; \
	echo "Create the PR with: gh pr create --draft --repo PolicyEngine/policyengine-sim-api --head $$BRANCH --base main"

# Integration tests
integ-test:
	cd projects/policyengine-apis-integ && uv run pytest

# Cleanup
clean:
	find . -type d -name "__pycache__" -exec rm -r {} +; \
	find . -name ".coverage" -exec rm -r {} +; \
	find . -name "artifacts" -exec rm -r {} +; \
	find . -name ".pytest_cache" -exec rm -r {} +; \
	find . -name ".venv" -exec rm -r {} +;

	docker-compose -f deployment/docker-compose.yml down -v
	docker system prune -f

# Local development helpers
shell:
ifndef service
	@echo "Please specify service: make shell service=simulation-executor"
else
	docker-compose -f deployment/docker-compose.yml exec $(service) /bin/bash
endif

ps:
	docker-compose -f deployment/docker-compose.yml ps

# Quick command for the simulation executor
dev-sim:
	docker-compose -f deployment/docker-compose.yml up simulation-executor
