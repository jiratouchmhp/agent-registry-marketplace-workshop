# Enterprise Agent Marketplace Workshop (Google Cloud Agent Registry v1)
#
# All Python/pytest commands default to the repository's local virtualenv so
# they run deterministically without system-package collisions.

PYTHON ?= .venv/bin/python
PYTEST ?= .venv/bin/pytest
TERRAFORM ?= terraform
HCLFMT ?= hclfmt
STRICT_HCL ?= false

# Workshop defaults aligned with WORKSHOP_SPEC.json (Bangkok asia-southeast3 primary + global Skills)
AGENT_REGISTRY_BASE_URL ?= http://127.0.0.1:8090/v1
GCP_PROJECT_ID ?= acme-agent-governance
GCP_LOCATION ?= asia-southeast3
AGENT_REGISTRY_SKILLS_LOCATION ?= global
ARTIFACT_REGISTRY_REPO ?= agent-marketplace
IMAGE_TAG ?= 1.0.0
MIDDLEWARE_PUBLIC_URL ?= http://127.0.0.1:8080
HUB_MIDDLEWARE_URL ?= http://127.0.0.1:8080
REQUIRE_KMS_SIGNATURE ?= false
KMS_PUBLIC_KEY_PEM_PATH ?=
SIGNING_KEY_PEM_PATH ?=
HUB_DEMO_HOME ?= /tmp/acme-agent-hub-demo-home
PLUGIN_DIR ?= starter-marketplace/plugins/gcp-finops-toolkit
LOCAL_SKILLS_DIR ?= local-skills
LOCAL_SKILLS_PLUGIN_NAME ?= local-ops-toolkit
SKILLS_REPO_URL ?= https://github.com/jiratouchmhp/agent-registry-marketplace-workshop.git
SKILLS_REPO_REF ?= main
SKILLS_REPO_SUBDIR ?= local-skills
SKILLS_REPO_PLUGIN_NAME ?= central-repo-skills
SKILLS_REPO_TOKEN_ENV ?=
TF_PLAN_ARGS ?=
GCP_BUILD_ARGS ?=
GCP_DEPLOY_ARGS ?=
GCP_VALIDATE_ARGS ?=
GCP_DESTROY_ARGS ?=

.PHONY: help preflight starter-check test test-e2e validate-hcl \
	run-local-stack smoke-local run-mock run-samples run-middleware \
	demo-sync demo-sync-local demo-sync-repo demo-hub-sync \
	gcp-build gcp-deploy gcp-validate gcp-destroy \
	tf-core-plan tf-sec-plan

help:
	@echo "Enterprise Agent Marketplace Workshop — Available Targets:"
	@echo ""
	@echo "Setup, Preflight & Tests:"
	@echo "  make preflight        Check Python 3.12+, .venv, git, curl, gcloud, and optional tools"
	@echo "  make starter-check    Validate starter exercises (Labs 01-04) offline"
	@echo "  make test             Run all unit and integration tests"
	@echo "  make test-e2e         Run end-to-end integration tests"
	@echo "  make validate-hcl     Verify Terraform HCL formatting (Track 3; set STRICT_HCL=true in CI)"
	@echo ""
	@echo "Zero-Docker Local Stack (Default: $(GCP_LOCATION)):"
	@echo "  make smoke-local      Run self-contained zero-Docker end-to-end smoke test (isolated ports)"
	@echo "  make run-local-stack  Launch all 4 local services in one terminal with health checks"
	@echo "  make run-mock         Start Mock Agent Registry v1 API on http://127.0.0.1:8090"
	@echo "  make run-samples      Start FinOps MCP server (:8091) and A2A agent (:8092)"
	@echo "  make run-middleware   Start Polyglot Marketplace Middleware on http://127.0.0.1:8080"
	@echo ""
	@echo "Ingestion & IDE Hydration:"
	@echo "  make demo-sync        Sync gcp-finops-toolkit into Agent Registry (mock or live)"
	@echo "  make demo-sync-local  Option 1: Ingest starter plugin + local-skills/ ($(LOCAL_SKILLS_PLUGIN_NAME))"
	@echo "  make demo-sync-repo   Option 2: Shallow-clone & ingest SKILLS_REPO_URL into Agent Registry"
	@echo "  make demo-hub-sync    Hydrate local demo HOME ($(HUB_DEMO_HOME)) for all 4 IDEs"
	@echo ""
	@echo "Zero-Docker / Zero-Terraform Live GCP ($(GCP_LOCATION) primary + $(AGENT_REGISTRY_SKILLS_LOCATION) Skills):"
	@echo "  make gcp-build        Build & push workshop images via Cloud Build in $(GCP_LOCATION) (zero local Docker)"
	@echo "  make gcp-deploy       Deploy Cloud Run + Agent Registry Services in $(GCP_LOCATION) + Skills in $(AGENT_REGISTRY_SKILLS_LOCATION)"
	@echo "  make gcp-validate     Validate live GCP deployment end-to-end as an attendee"
	@echo "  make gcp-destroy      Tear down all workshop GCP resources + zero-leftover audit"
	@echo ""
	@echo "Optional Terraform IaC Track:"
	@echo "  make tf-core-plan     Run terraform init & plan for terraform/core (Workstream A — $(GCP_LOCATION))"
	@echo "  make tf-sec-plan      Run terraform init & plan for terraform/security_governance (Workstream B — $(GCP_LOCATION), asia-southeast1 Agent Identity fallback)"

preflight:
	./scripts/preflight_check.sh

starter-check:
	$(PYTHON) scripts/check_starter.py

test:
	$(PYTEST) -v

test-e2e:
	$(PYTEST) -v -k "e2e or end_to_end or live_asgi or real_middleware"

validate-hcl:
	@if command -v $(TERRAFORM) >/dev/null 2>&1; then \
		$(TERRAFORM) fmt -check -recursive terraform/; \
	elif command -v $(HCLFMT) >/dev/null 2>&1; then \
		$(HCLFMT) -check -require-no-change \
			terraform/core/*.tf \
			terraform/core/terraform.tfvars.example \
			terraform/security_governance/*.tf \
			terraform/security_governance/terraform.tfvars.example > /dev/null; \
		echo "hclfmt check passed for terraform/core and terraform/security_governance."; \
	elif [ "$(STRICT_HCL)" = "true" ]; then \
		echo "ERROR: Neither $(TERRAFORM) nor $(HCLFMT) found on PATH." >&2; \
		exit 1; \
	else \
		echo "[SKIP] Neither $(TERRAFORM) nor $(HCLFMT) found on PATH — skipping HCL check (Track 3 Terraform IaC only; set STRICT_HCL=true to fail in CI)."; \
	fi

run-local-stack:
	GCP_PROJECT_ID="$(GCP_PROJECT_ID)" \
	GCP_LOCATION="$(GCP_LOCATION)" \
	REQUIRE_KMS_SIGNATURE="$(REQUIRE_KMS_SIGNATURE)" \
	KMS_PUBLIC_KEY_PEM_PATH="$(KMS_PUBLIC_KEY_PEM_PATH)" \
	./scripts/run_local_stack.sh

smoke-local:
	GCP_PROJECT_ID="$(GCP_PROJECT_ID)" \
	GCP_LOCATION="$(GCP_LOCATION)" \
	./scripts/local_smoke_test.sh

run-mock:
	$(PYTHON) -m uvicorn mock_registry.app:app --host 127.0.0.1 --port 8090

run-samples:
	@bash -euo pipefail -c '\
		$(PYTHON) -m uvicorn --app-dir samples/mcp-server app:app --host 127.0.0.1 --port 8091 & \
		PID_MCP=$$!; \
		$(PYTHON) -m uvicorn --app-dir samples/a2a-agent app:app --host 127.0.0.1 --port 8092 & \
		PID_A2A=$$!; \
		trap "kill $$PID_MCP $$PID_A2A 2>/dev/null || true; wait $$PID_MCP $$PID_A2A 2>/dev/null || true" INT TERM EXIT; \
		echo "Sample FinOps MCP Server listening on http://127.0.0.1:8091/mcp (PID $$PID_MCP)"; \
		echo "Sample FinOps A2A Agent listening on http://127.0.0.1:8092/a2a (PID $$PID_A2A)"; \
		wait $$PID_MCP $$PID_A2A'

run-middleware:
	AGENT_REGISTRY_BASE_URL="$(AGENT_REGISTRY_BASE_URL)" \
	GCP_PROJECT_ID="$(GCP_PROJECT_ID)" \
	GCP_LOCATION="$(GCP_LOCATION)" \
	MIDDLEWARE_PUBLIC_URL="$(MIDDLEWARE_PUBLIC_URL)" \
	REQUIRE_KMS_SIGNATURE="$(REQUIRE_KMS_SIGNATURE)" \
	KMS_PUBLIC_KEY_PEM_PATH="$(KMS_PUBLIC_KEY_PEM_PATH)" \
	$(PYTHON) -m uvicorn middleware.app.main:app --host 127.0.0.1 --port 8080

demo-sync:
	@SIGN_FLAG=""; \
	if [ -n "$(SIGNING_KEY_PEM_PATH)" ]; then \
		SIGN_FLAG="--sign-pem $(SIGNING_KEY_PEM_PATH)"; \
	fi; \
	$(PYTHON) -m tools.plugin_sync sync \
		--plugin-dir "$(PLUGIN_DIR)" \
		--registry-url "$(AGENT_REGISTRY_BASE_URL)" \
		--project "$(GCP_PROJECT_ID)" \
		--location "$(GCP_LOCATION)" \
		--target-state TARGET_STATE_ACTIVE \
		--scanning-policy ENABLED_BLOCKING \
		$$SIGN_FLAG

demo-sync-local:
	@set -eu; \
	set -- \
		--registry-url "$(AGENT_REGISTRY_BASE_URL)" \
		--project "$(GCP_PROJECT_ID)" \
		--location "$(GCP_LOCATION)" \
		--target-state TARGET_STATE_ACTIVE \
		--scanning-policy ENABLED_BLOCKING; \
	if [ -n "$(SIGNING_KEY_PEM_PATH)" ]; then \
		set -- "$$@" --sign-pem "$(SIGNING_KEY_PEM_PATH)"; \
	fi; \
	$(PYTHON) -m tools.plugin_sync ingest \
		--source-type local \
		--path "$(PLUGIN_DIR)" \
		"$$@"; \
	$(PYTHON) -m tools.plugin_sync ingest \
		--source-type local \
		--path "$(LOCAL_SKILLS_DIR)" \
		--plugin-name "$(LOCAL_SKILLS_PLUGIN_NAME)" \
		"$$@"

demo-sync-repo:
	@set -eu; \
	set -- \
		--source-type git \
		--repo-url "$(SKILLS_REPO_URL)" \
		--plugin-name "$(SKILLS_REPO_PLUGIN_NAME)" \
		--registry-url "$(AGENT_REGISTRY_BASE_URL)" \
		--project "$(GCP_PROJECT_ID)" \
		--location "$(GCP_LOCATION)" \
		--target-state TARGET_STATE_ACTIVE \
		--scanning-policy ENABLED_BLOCKING; \
	if [ -n "$(SKILLS_REPO_REF)" ]; then \
		set -- "$$@" --ref "$(SKILLS_REPO_REF)"; \
	fi; \
	if [ -n "$(SKILLS_REPO_SUBDIR)" ]; then \
		set -- "$$@" --subdir "$(SKILLS_REPO_SUBDIR)"; \
	fi; \
	if [ -n "$(SKILLS_REPO_TOKEN_ENV)" ]; then \
		set -- "$$@" --git-token-env "$(SKILLS_REPO_TOKEN_ENV)"; \
	fi; \
	if [ -n "$(SIGNING_KEY_PEM_PATH)" ]; then \
		set -- "$$@" --sign-pem "$(SIGNING_KEY_PEM_PATH)"; \
	fi; \
	$(PYTHON) -m tools.plugin_sync ingest "$$@"

demo-hub-sync:
	@set -eu; \
	$(PYTHON) -m tools.hub_sync sync \
		--middleware-url "$(HUB_MIDDLEWARE_URL)" \
		--home "$(HUB_DEMO_HOME)" \
		--tools all \
		--no-exec; \
	$(PYTHON) -m tools.hub_sync status --home "$(HUB_DEMO_HOME)"

gcp-build:
	@set -eu; \
	./scripts/build_images_cloudbuild.sh \
		--project "$(GCP_PROJECT_ID)" \
		--region "$(GCP_LOCATION)" \
		--repo "$(ARTIFACT_REGISTRY_REPO)" \
		--tag "$(IMAGE_TAG)" \
		$(GCP_BUILD_ARGS)

gcp-deploy:
	@set -eu; \
	./scripts/deploy_gcp.sh \
		--project "$(GCP_PROJECT_ID)" \
		--region "$(GCP_LOCATION)" \
		--skills-location "$(AGENT_REGISTRY_SKILLS_LOCATION)" \
		--repo "$(ARTIFACT_REGISTRY_REPO)" \
		--tag "$(IMAGE_TAG)" \
		$(GCP_DEPLOY_ARGS)

gcp-validate:
	@set -eu; \
	./scripts/validate_live_gcp.sh \
		--project "$(GCP_PROJECT_ID)" \
		--region "$(GCP_LOCATION)" \
		--skills-location "$(AGENT_REGISTRY_SKILLS_LOCATION)" \
		$(GCP_VALIDATE_ARGS)

gcp-destroy:
	@set -eu; \
	./scripts/destroy_gcp.sh \
		--project "$(GCP_PROJECT_ID)" \
		--region "$(GCP_LOCATION)" \
		--skills-location "$(AGENT_REGISTRY_SKILLS_LOCATION)" \
		--repo "$(ARTIFACT_REGISTRY_REPO)" \
		$(GCP_DESTROY_ARGS)

tf-core-plan:
	@command -v $(TERRAFORM) >/dev/null 2>&1 || { echo "ERROR: terraform CLI not installed." >&2; exit 1; }
	$(TERRAFORM) -chdir=terraform/core init
	$(TERRAFORM) -chdir=terraform/core plan $(TF_PLAN_ARGS)

tf-sec-plan:
	@command -v $(TERRAFORM) >/dev/null 2>&1 || { echo "ERROR: terraform CLI not installed." >&2; exit 1; }
	$(TERRAFORM) -chdir=terraform/security_governance init
	$(TERRAFORM) -chdir=terraform/security_governance plan $(TF_PLAN_ARGS)
