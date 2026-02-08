.PHONY: test help

# Colors for output
CYAN := \033[0;36m
GREEN := \033[0;32m
YELLOW := \033[0;33m
NC := \033[0m  # No Color

help:
	@echo "$(CYAN)Jarvis Plugin Development Targets$(NC)"
	@echo ""
	@echo "$(GREEN)test$(NC)       - Run MCP server pytest suite"
	@echo "$(GREEN)./chromadb$(NC)  - Explore ChromaDB database (e.g. ./chromadb --list)"
	@echo ""

test:
	@echo "$(CYAN)Running MCP server tests...$(NC)"
	cd plugins/jarvis/mcp-server && uv run pytest -v
	@echo "$(GREEN)✓ Tests completed$(NC)"

