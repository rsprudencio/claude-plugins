# Jarvis Plugin Development Makefile
# Usage: make <target> [VERSION=x.y.z]
#
# Quick reference:
#   make version          — show current version
#   make test             — run pytest suite
#   make bump VERSION=x   — bump version files
#   make build            — Docker build + tag
#   make restart          — restart Docker container
#   make reinstall        — reinstall Claude plugins
#   make release VERSION=x — full pipeline (test→bump→build→restart→reinstall)

.PHONY: help version bump test build restart reinstall release clean

# ─── Configuration ──────────────────────────────────────────────────

# Plugin selection (PLUGIN=jarvis|todoist|strategic, default: jarvis)
PLUGIN        ?= jarvis
PLUGIN_DIR    := plugins/$(if $(filter todoist,$(PLUGIN)),jarvis-todoist,$(if $(filter strategic,$(PLUGIN)),jarvis-strategic,jarvis))
PLUGIN_JSON   := $(PLUGIN_DIR)/.claude-plugin/plugin.json
PYPROJECT     := $(PLUGIN_DIR)/mcp-server/pyproject.toml
CURRENT_VERSION := $(shell jq -r .version $(PLUGIN_JSON) 2>/dev/null || echo "unknown")

# Docker
IMAGE_NAME    := jarvis-mcp
GHCR_IMAGE    := ghcr.io/rsprudencio/jarvis
COMPOSE_FILE  := $(HOME)/.jarvis/docker-compose.yml

# Claude config directory — must be set explicitly (no auto-detection)
# Usage: make reinstall CLAUDE_DIR=~/.claude-personal
CLAUDE_DIR ?= $(CLAUDE_CONFIG_DIR)

# Colors
CYAN    := \033[0;36m
GREEN   := \033[0;32m
YELLOW  := \033[0;33m
RED     := \033[0;31m
NC      := \033[0m

# ─── Targets ────────────────────────────────────────────────────────

help: ## Show available targets
	@echo "$(CYAN)Jarvis Plugin Development$(NC)  ($(PLUGIN): v$(CURRENT_VERSION))"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  $(GREEN)%-15s$(NC) %s\n", $$1, $$2}'
	@echo ""
	@echo "$(YELLOW)Full release:$(NC)  make release VERSION=x.y.z [PLUGIN=jarvis|todoist|strategic]"

version: ## Show current plugin version
	@echo "$(CURRENT_VERSION)"

test: ## Run MCP server pytest suite
	@echo "$(CYAN)Running tests...$(NC)"
	cd plugins/jarvis/mcp-server && uv run pytest tests/ -x -q
	@echo "$(GREEN)✓ Tests passed$(NC)"

bump: ## Bump version (VERSION=x.y.z [PLUGIN=jarvis|todoist|strategic])
	@if [ -z "$(VERSION)" ]; then \
		echo "$(RED)Usage: make bump VERSION=x.y.z [PLUGIN=jarvis|todoist|strategic]$(NC)"; \
		echo "Current $(PLUGIN) version: $(CURRENT_VERSION)"; \
		exit 1; \
	fi
	@echo "$(CYAN)Bumping $(PLUGIN): $(CURRENT_VERSION) → $(VERSION)$(NC)"
	@jq --arg v "$(VERSION)" '.version = $$v' $(PLUGIN_JSON) > $(PLUGIN_JSON).tmp && \
		mv $(PLUGIN_JSON).tmp $(PLUGIN_JSON)
	@echo "  $(PLUGIN_JSON)"
	@if [ -f "$(PYPROJECT)" ]; then \
		sed -i '' 's/^version = ".*"/version = "$(VERSION)"/' $(PYPROJECT); \
		echo "  $(PYPROJECT)"; \
	fi
	@echo "$(GREEN)✓ $(PLUGIN) bumped to $(VERSION)$(NC)"

build: ## Build Docker image (tags local + GHCR)
	$(eval BUILD_VERSION := $(or $(VERSION),$(CURRENT_VERSION)))
	@echo "$(CYAN)Building Docker image v$(BUILD_VERSION)...$(NC)"
	docker build -f docker/Dockerfile \
		--build-arg JARVIS_VERSION=$(BUILD_VERSION) \
		-t $(IMAGE_NAME):$(BUILD_VERSION) \
		-t $(IMAGE_NAME):latest \
		.
	@docker tag $(IMAGE_NAME):$(BUILD_VERSION) $(GHCR_IMAGE):latest
	@echo "$(GREEN)✓ Built $(IMAGE_NAME):$(BUILD_VERSION)$(NC)"
	@echo "  Also tagged: $(GHCR_IMAGE):latest (local override)"

restart: ## Restart Docker container via compose
	@echo "$(CYAN)Restarting container...$(NC)"
	@docker compose -f $(COMPOSE_FILE) down 2>/dev/null || \
		(docker ps --filter "name=jarvis" -q | xargs -r docker stop 2>/dev/null; \
		 docker ps -a --filter "name=jarvis" -q | xargs -r docker rm 2>/dev/null) || true
	@docker compose -f $(COMPOSE_FILE) up -d
	@echo "Waiting for health check..."
	@sleep 4
	@curl -sf http://localhost:8741/health > /dev/null && \
		echo "$(GREEN)✓ Container healthy$(NC)" || \
		echo "$(RED)✗ Health check failed$(NC)"

reinstall: ## Reinstall all 3 Claude plugins (CLAUDE_DIR= required)
	@if [ -z "$(CLAUDE_DIR)" ]; then \
		echo "$(RED)Error: CLAUDE_DIR is required$(NC)"; \
		echo "  make reinstall CLAUDE_DIR=~/.claude"; \
		echo "  make reinstall CLAUDE_DIR=~/.claude-personal"; \
		exit 1; \
	fi
	@echo "$(CYAN)Reinstalling plugins...$(NC)"
	@_dir=$$(eval echo "$(CLAUDE_DIR)"); \
	echo "  Config dir: $$_dir"; \
	unset CLAUDECODE; \
	export CLAUDE_CONFIG_DIR="$$_dir"; \
	claude plugin marketplace update && \
	claude plugin uninstall jarvis@jarvis-plugins 2>/dev/null; \
	claude plugin uninstall jarvis-todoist@jarvis-plugins 2>/dev/null; \
	claude plugin uninstall jarvis-strategic@jarvis-plugins 2>/dev/null; \
	claude plugin install jarvis@jarvis-plugins && \
	claude plugin install jarvis-todoist@jarvis-plugins && \
	claude plugin install jarvis-strategic@jarvis-plugins
	@echo ""
	@echo "$(GREEN)✓ All plugins reinstalled$(NC)"
	@echo "$(YELLOW)⚠ RESTART CLAUDE CODE to apply changes$(NC)"

release: ## Full pipeline: test → bump → build → restart → reinstall (requires VERSION=x.y.z)
	@if [ -z "$(VERSION)" ]; then \
		echo "$(RED)Usage: make release VERSION=x.y.z$(NC)"; \
		echo "Current version: $(CURRENT_VERSION)"; \
		exit 1; \
	fi
	@echo "$(CYAN)═══ Release pipeline: $(CURRENT_VERSION) → $(VERSION) ═══$(NC)"
	@echo ""
	$(MAKE) test
	@echo ""
	$(MAKE) bump VERSION=$(VERSION)
	@echo ""
	$(MAKE) build VERSION=$(VERSION)
	@echo ""
	$(MAKE) restart
	@echo ""
	$(MAKE) reinstall
	@echo ""
	@echo "$(GREEN)═══ Release $(VERSION) complete ═══$(NC)"
	@echo ""
	@echo "Remaining manual steps:"
	@echo "  1. git add + git commit"
	@echo "  2. git tag -a v$(VERSION) -m 'Version $(VERSION): ...'"
	@echo "  3. git push && git push --tags  (when ready)"
	@echo "  4. $(YELLOW)Restart Claude Code$(NC)"

clean: ## Remove local Docker images
	@echo "$(CYAN)Cleaning Docker images...$(NC)"
	@docker rmi $(IMAGE_NAME):latest $(GHCR_IMAGE):latest 2>/dev/null || true
	@docker images $(IMAGE_NAME) --format '{{.ID}}' | xargs docker rmi 2>/dev/null || true
	@echo "$(GREEN)✓ Docker images cleaned$(NC)"
