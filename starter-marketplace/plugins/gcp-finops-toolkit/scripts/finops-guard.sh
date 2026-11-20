#!/usr/bin/env bash
set -euo pipefail

# Local FinOps policy guardrail hook.
# In --audit mode (Stop event), record completion cleanly.
if [[ "${1:-}" == "--audit" ]]; then
  exit 0
fi

# Block destructive tool calls unless explicitly approved via FINOPS_APPROVE_DESTRUCTIVE=true.
if [[ "${FINOPS_APPROVE_DESTRUCTIVE:-false}" != "true" ]]; then
  echo "Blocked destructive FinOps tool call: set FINOPS_APPROVE_DESTRUCTIVE=true after human review." >&2
  exit 2
fi

exit 0
