# Enterprise Agent Marketplace Workshop (Google Cloud Agent Registry v1)
#
# All Python/pytest commands default to the repository's local virtualenv so
# they run deterministically without system-package collisions.

PYTHON ?= .venv/bin/python
PYTEST ?= .venv/bin/pytest
TERRAFORM ?= terraform
HCLFMT ?= hclfmt

# Workshop defaults aligned with WORKSHOP_SPEC.json
AGENT_REGISTRY_BASE_URL ?= http://127.0.0.1:8090/v1
GCP_PROJECT_ID ?= acme-agent-governance
GCP_LOCATION ?= us-central1
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

.PHONY: help test test-e2e validate-hcl run-mock run-samples run-middleware \
	demo-sync demo-sync-local demo-sync-repo demo-hub-sync tf-core-plan tf-sec-plan

help:
	@echo "Enterprise Agent Marketplace Workshop — Available Targets:"
	@echo "  make test            Run all unit and integration tests"
	@echo "  make test-e2e        Run end-to-end integration tests"
	@echo "  make validate-hcl    Verify Terraform HCL formatting across terraform/"
	@echo "  make run-mock        Start Mock Agent Registry v1 API on http://127.0.0.1:8090"
	@echo "  make run-samples     Start FinOps MCP server (:8091) and A2A agent (:8092)"
	@echo "  make run-middleware  Start Polyglot Marketplace Middleware on http://127.0.0.1:8080"
	@echo "  make demo-sync       Sync gcp-finops-toolkit into Agent Registry (mock or live)"
	@echo "  make demo-sync-local Option 1: Ingest starter plugin + local-skills/ ($(LOCAL_SKILLS_PLUGIN_NAME))"
	@echo "  make demo-sync-repo  Option 2: Shallow-clone & ingest SKILLS_REPO_URL into Agent Registry"
	@echo "  make demo-hub-sync   Hydrate local demo HOME ($(HUB_DEMO_HOME)) for all 4 IDEs"
	@echo "  make tf-core-plan    Run terraform init & plan for terraform/core (Workstream A)"
	@echo "  make tf-sec-plan     Run terraform init & plan for terraform/security_governance (Workstream B)"

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
	else \
		echo "ERROR: Neither $(TERRAFORM) nor $(HCLFMT) found on PATH." >&2; \
		exit 1; \
	fi

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
	$(PYTHON) -m tools.hub_sync sync \
		--middleware-url "$(HUB_MIDDLEWARE_URL)" \
		--home "$(HUB_DEMO_HOME)" \
		--tools all \
		--no-exec
	$(PYTHON) -m tools.hub_sync status --home "$(HUB_DEMO_HOME)"

tf-core-plan:
	@command -v $(TERRAFORM) >/dev/null 2>&1 || { echo "ERROR: terraform CLI not installed." >&2; exit 1; }
	$(TERRAFORM) -chdir=terraform/core init
	$(TERRAFORM) -chdir=terraform/core plan $(TF_PLAN_ARGS)

tf-sec-plan:
	@command -v $(TERRAFORM) >/dev/null 2>&1 || { echo "ERROR: terraform CLI not installed." >&2; exit 1; }
	$(TERRAFORM) -chdir=terraform/security_governance init
	$(TERRAFORM) -chdir=terraform/security_governance plan $(TF_PLAN_ARGS)
