.DEFAULT_GOAL := help
.PHONY: help dev test lint eval ingest

help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "CodeContext targets:\n"} /^[a-zA-Z_-]+:.*?##/ { printf "  %-20s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

dev: ## Start backend + frontend + Postgres together (implemented in Checkpoint E)
	@echo "Not yet implemented. See docs/decisions/ for slice planning."
	@exit 1

test: ## Run all tests (implemented in Checkpoint D)
	@echo "Not yet implemented."
	@exit 1

lint: ## Run ruff + tsc (implemented in Checkpoint D)
	@echo "Not yet implemented."
	@exit 1

eval: ## Run the evaluation harness (later slice)
	@echo "Not yet implemented."
	@exit 1

ingest: ## Ingest a repo from CLI: make ingest REPO=owner/name (implemented in Checkpoint C)
	@echo "Not yet implemented."
	@exit 1
