# Simplified Makefile using docker-compose
.PHONY: help dev up down build test deploy clean logs format check terraform-deploy push-pr-branch

# Load environment variables if .env exists
ifneq (,$(wildcard deployment/.env))
    include deployment/.env
    export
endif

help:
	@echo "PolicyEngine API v2 - Available commands:"
	@echo ""
	@echo "Setup (first time):"
	@echo "  make setup            - Create .env file from template"
	@echo "  make init-gcp         - Initialize GCP project (APIs, registry, bucket)"
	@echo ""
	@echo "Development:"
	@echo "  make dev              - Start all services in development mode"
	@echo "  make up [service=x]   - Start specific service or all services"
	@echo "  make down             - Stop all services"
	@echo "  make logs [service=x] - Show logs for service"
	@echo "  make test             - Run tests for all services"
	@echo ""
	@echo "Deployment:"
	@echo "  make deploy           - Full deployment (builds, pushes, deploys project + infra)"
	@echo "  make terraform-init   - Initialize terraform modules"
	@echo "  make terraform-force-init - Force reinitialize (if stuck)"
	@echo "  make terraform-plan   - Preview changes for both modules"
	@echo "  make terraform-deploy-project - Deploy project configuration only"
	@echo "  make terraform-deploy-infra   - Deploy infrastructure only"
	@echo "  make terraform-destroy        - Destroy all terraform resources"
	@echo ""
	@echo "Maintenance:"
	@echo "  make build            - Build all Docker images"
	@echo "  make clean            - Clean up containers and volumes"
	@echo "  make format           - Format Python code with ruff"
	@echo "  make check            - Run code quality checks"
	@echo "  make push-pr-branch   - Push current branch to origin for PR creation"

# Initialize GCP (enables APIs, creates bucket, etc)
init-gcp: check-deploy-env
	@bash deployment/init-gcp.sh

# Setup for first-time users
setup:
	@echo "Setting up PolicyEngine API for first time..."
	@echo "Note: Using gcloud storage commands (compatible with Python 3.13+)"
	@if [ ! -f deployment/.env ]; then \
		cp deployment/.env.example deployment/.env; \
		echo "✅ Created deployment/.env"; \
		echo ""; \
		echo "⚠️  IMPORTANT: Edit deployment/.env and set:"; \
		echo "   - PROJECT_ID to your GCP project ID"; \
		echo "   - GOOGLE_CLOUD_PROJECT to match PROJECT_ID"; \
		echo ""; \
		echo "Then run:"; \
		echo "   make dev              # For local development"; \
		echo "   make deploy           # To deploy to GCP"; \
	else \
		echo "✅ deployment/.env already exists"; \
	fi

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

build-prod:
	docker-compose -f deployment/docker-compose.prod.yml build --parallel

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
	@echo "Running tests for all services..."
	@for service in api-full simulation-executor api-tagger; do \
		echo "Testing $$service..."; \
		docker-compose -f deployment/docker-compose.yml run --rm $$service sh -c "cd /app/projects/policyengine-$$service && uv run --extra test pytest" || exit 1; \
	done

test-service:
ifndef service
	@echo "Please specify service: make test-service service=api-full"
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

# Deployment
deploy: check-deploy-env build-prod push-images terraform-ensure-init terraform-deploy-all

check-deploy-env:
	@if [ ! -f "deployment/.env" ]; then \
		echo "Error: deployment/.env not found. Run 'make setup' first"; \
		exit 1; \
	fi
	@if [ -z "$(PROJECT_ID)" ]; then \
		echo "Error: PROJECT_ID not set in deployment/.env"; \
		exit 1; \
	fi

push-images:
	@echo "Pushing images to GCP..."
	docker-compose -f deployment/docker-compose.prod.yml push

# Terraform commands
terraform-backend:
	@echo "Setting up Terraform backend..."
	@if [ -z "$(PROJECT_ID)" ]; then \
		echo "Error: PROJECT_ID not set. Please update deployment/.env"; \
		exit 1; \
	fi
	@echo "Creating GCS bucket for Terraform state: $(PROJECT_ID)-state"
	@gcloud storage buckets create gs://$(PROJECT_ID)-state \
		--project=$(PROJECT_ID) \
		--location=$(REGION) \
		--uniform-bucket-level-access 2>/dev/null || echo "Bucket already exists"
	@gcloud storage buckets update gs://$(PROJECT_ID)-state --versioning
	@echo "Backend bucket ready"

terraform-force-init:
	@echo "Force reinitializing Terraform (cleaning existing state)..."
	@rm -rf deployment/terraform/infra/.terraform deployment/terraform/project/.terraform
	@$(MAKE) terraform-init

terraform-init: terraform-backend
	@echo "Initializing Terraform..."
	@# Remove example backend files if they exist
	@rm -f deployment/terraform/infra/backend.example.tf deployment/terraform/infra/backend.example.tfvars
	@rm -f deployment/terraform/project/backend.example.tf deployment/terraform/project/backend.example_tf
	@# Create or update backend.tf files
	@echo "Configuring backend for $(PROJECT_ID)-state..."
	@echo "terraform {" > deployment/terraform/infra/backend.tf.tmp
	@echo "  backend \"gcs\" {" >> deployment/terraform/infra/backend.tf.tmp
	@echo "    bucket = \"$(PROJECT_ID)-state\"" >> deployment/terraform/infra/backend.tf.tmp
	@echo "    prefix = \"infra\"" >> deployment/terraform/infra/backend.tf.tmp
	@echo "  }" >> deployment/terraform/infra/backend.tf.tmp
	@echo "}" >> deployment/terraform/infra/backend.tf.tmp
	@mv deployment/terraform/infra/backend.tf.tmp deployment/terraform/infra/backend.tf
	@echo "terraform {" > deployment/terraform/project/backend.tf.tmp
	@echo "  backend \"gcs\" {" >> deployment/terraform/project/backend.tf.tmp
	@echo "    bucket = \"$(PROJECT_ID)-state\"" >> deployment/terraform/project/backend.tf.tmp
	@echo "    prefix = \"project\"" >> deployment/terraform/project/backend.tf.tmp
	@echo "  }" >> deployment/terraform/project/backend.tf.tmp
	@echo "}" >> deployment/terraform/project/backend.tf.tmp
	@mv deployment/terraform/project/backend.tf.tmp deployment/terraform/project/backend.tf
	@echo "Initializing project module..."
	-cd deployment/terraform/project && terraform init -reconfigure 2>/dev/null || true
	@echo "Initializing infra module..."
	cd deployment/terraform/infra && terraform init -reconfigure

terraform-ensure-init:
	@echo "Checking Terraform initialization..."
	@# Always run init if backend.tf was recently modified or .terraform doesn't exist
	@if [ ! -d "deployment/terraform/infra/.terraform" ] || \
	   [ "deployment/terraform/infra/backend.tf" -nt "deployment/terraform/infra/.terraform" ] || \
	   [ ! -f "deployment/terraform/infra/.terraform/terraform.tfstate" ]; then \
		echo "Running terraform init..."; \
		$(MAKE) terraform-init; \
	else \
		echo "Terraform already initialized"; \
	fi

terraform-plan: terraform-ensure-init
	@echo "Planning Terraform changes..."
	@if [ -z "$(TF_VAR_org_id)" ] || [ "$(TF_VAR_org_id)" = "your-org-id" ]; then \
		echo "\n=== Skipping PROJECT module (using existing project) ==="; \
	else \
		echo "\n=== Planning PROJECT module ==="; \
		cd deployment/terraform/project && terraform plan; \
	fi
	@echo "\n=== Planning INFRA module ==="
	@# Auto-populate all required variables
	@US_VERSION=$$(grep -A1 'name = "policyengine-us"' projects/policyengine-simulation-executor/uv.lock | grep version | head -1 | sed 's/.*"\(.*\)".*/\1/') && \
	UK_VERSION=$$(grep -A1 'name = "policyengine-uk"' projects/policyengine-simulation-executor/uv.lock | grep version | head -1 | sed 's/.*"\(.*\)".*/\1/') && \
	COMMIT_URL="https://github.com/PolicyEngine/policyengine-api-v2/commit/$$(git rev-parse HEAD)" && \
	echo "project_id = \"$${TF_VAR_project_id}\"" > deployment/terraform/infra/auto.tfvars && \
	echo "commit_url = \"$$COMMIT_URL\"" >> deployment/terraform/infra/auto.tfvars && \
	echo "policyengine-us-package-version = \"$$US_VERSION\"" >> deployment/terraform/infra/auto.tfvars && \
	echo "policyengine-uk-package-version = \"$$UK_VERSION\"" >> deployment/terraform/infra/auto.tfvars && \
	echo "is_prod = $${TF_VAR_is_prod:-false}" >> deployment/terraform/infra/auto.tfvars && \
	echo "full_container_tag = \"$${TF_VAR_full_container_tag:-latest}\"" >> deployment/terraform/infra/auto.tfvars && \
	echo "simulation_container_tag = \"$${TF_VAR_simulation_container_tag:-latest}\"" >> deployment/terraform/infra/auto.tfvars && \
	echo "tagger_container_tag = \"$${TF_VAR_tagger_container_tag:-latest}\"" >> deployment/terraform/infra/auto.tfvars && \
	echo "region = \"$${TF_VAR_region:-us-central1}\"" >> deployment/terraform/infra/auto.tfvars && \
	echo "stage = \"$${TF_VAR_stage:-dev}\"" >> deployment/terraform/infra/auto.tfvars && \
	cd deployment/terraform/infra && terraform plan -var-file=auto.tfvars

terraform-deploy-project: terraform-ensure-init
	@echo "Deploying GCP project configuration..."
	@echo "Note: This creates a NEW GCP project. Skip if using existing project."
	@if [ -z "$(TF_VAR_org_id)" ] || [ "$(TF_VAR_org_id)" = "your-org-id" ]; then \
		echo "⚠️  Skipping project creation - TF_VAR_org_id not set"; \
		echo "   Using existing project: $(PROJECT_ID)"; \
	else \
		cd deployment/terraform/project && terraform apply -auto-approve; \
	fi

terraform-deploy-infra: terraform-ensure-init
	@echo "Deploying infrastructure (Cloud Run, etc)..."
	@# Auto-populate all required variables
	@US_VERSION=$$(grep -A1 'name = "policyengine-us"' projects/policyengine-simulation-executor/uv.lock | grep version | head -1 | sed 's/.*"\(.*\)".*/\1/') && \
	UK_VERSION=$$(grep -A1 'name = "policyengine-uk"' projects/policyengine-simulation-executor/uv.lock | grep version | head -1 | sed 's/.*"\(.*\)".*/\1/') && \
	COMMIT_URL="https://github.com/PolicyEngine/policyengine-api-v2/commit/$$(git rev-parse HEAD)" && \
	echo "project_id = \"$${TF_VAR_project_id}\"" > deployment/terraform/infra/auto.tfvars && \
	echo "commit_url = \"$$COMMIT_URL\"" >> deployment/terraform/infra/auto.tfvars && \
	echo "policyengine-us-package-version = \"$$US_VERSION\"" >> deployment/terraform/infra/auto.tfvars && \
	echo "policyengine-uk-package-version = \"$$UK_VERSION\"" >> deployment/terraform/infra/auto.tfvars && \
	echo "is_prod = $${TF_VAR_is_prod:-false}" >> deployment/terraform/infra/auto.tfvars && \
	echo "full_container_tag = \"$${TF_VAR_full_container_tag:-latest}\"" >> deployment/terraform/infra/auto.tfvars && \
	echo "simulation_container_tag = \"$${TF_VAR_simulation_container_tag:-latest}\"" >> deployment/terraform/infra/auto.tfvars && \
	echo "tagger_container_tag = \"$${TF_VAR_tagger_container_tag:-latest}\"" >> deployment/terraform/infra/auto.tfvars && \
	echo "region = \"$${TF_VAR_region:-us-central1}\"" >> deployment/terraform/infra/auto.tfvars && \
	echo "stage = \"$${TF_VAR_stage:-dev}\"" >> deployment/terraform/infra/auto.tfvars && \
	cd deployment/terraform/infra && terraform apply -auto-approve -var-file=auto.tfvars

terraform-deploy-all: terraform-ensure-init
	@echo "Starting full deployment..."
	@# Try project deployment but don't fail if skipped
	@$(MAKE) terraform-deploy-project || true
	@# Always deploy infrastructure
	@$(MAKE) terraform-deploy-infra
	@echo "✅ Deployment completed successfully!"

terraform-deploy: terraform-deploy-all  # Alias for backward compatibility

terraform-import:
	@echo "Importing existing resources into terraform state..."
	@cd deployment/terraform && ./import-existing.sh

terraform-handle-workflows:
	@echo "Checking and handling existing workflows..."
	@cd deployment/terraform && ./handle-existing-workflows.sh $(PROJECT_ID) $(TF_VAR_region)

terraform-destroy:
	@echo "⚠️  WARNING: This will destroy all terraform-managed resources!"
	@echo "Press Ctrl+C to cancel, or Enter to continue..."
	@read confirm
	@echo "Destroying infrastructure first..."
	cd deployment/terraform/infra && terraform destroy -auto-approve
	@echo "Destroying project configuration..."
	cd deployment/terraform/project && terraform destroy -auto-approve
	@echo "✅ All terraform resources destroyed"

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
	@echo "Re-exporting Modal image requirements from uv.lock..."
	@./scripts/export-modal-image-requirements.sh
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
		*PolicyEngine/policyengine-api-v2* ) ;; \
		* ) echo "Missing canonical origin remote PolicyEngine/policyengine-api-v2"; exit 1 ;; \
	esac; \
	git push -u origin HEAD:$$BRANCH; \
	echo "Create the PR with: gh pr create --draft --repo PolicyEngine/policyengine-api-v2 --head $$BRANCH --base main"

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
	@echo "Please specify service: make shell service=api-full"
else
	docker-compose -f deployment/docker-compose.yml exec $(service) /bin/bash
endif

ps:
	docker-compose -f deployment/docker-compose.yml ps

# Production helpers
prod-build:
	docker-compose -f deployment/docker-compose.prod.yml build

prod-push:
	docker-compose -f deployment/docker-compose.prod.yml push

# Quick commands for specific services
dev-full:
	docker-compose -f deployment/docker-compose.yml up api-full

dev-sim:
	docker-compose -f deployment/docker-compose.yml up simulation-executor

dev-tagger:
	docker-compose -f deployment/docker-compose.yml up api-tagger
