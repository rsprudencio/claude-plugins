.PHONY: test chromadb help

# Colors for output
CYAN := \033[0;36m
GREEN := \033[0;32m
YELLOW := \033[0;33m
NC := \033[0m  # No Color

CHROMADB_PATH := $(HOME)/.jarvis/memory_db

help:
	@echo "$(CYAN)Jarvis Plugin Development Targets$(NC)"
	@echo ""
	@echo "$(GREEN)test$(NC)       - Run MCP server pytest suite"
	@echo "$(GREEN)chromadb$(NC)   - Explore ChromaDB database (interactive CLI)"
	@echo ""
	@echo "$(YELLOW)Database Location:$(NC)"
	@echo "  $(CHROMADB_PATH)"
	@echo ""

test:
	@echo "$(CYAN)Running MCP server tests...$(NC)"
	cd plugins/jarvis/mcp-server && uv run pytest -v
	@echo "$(GREEN)✓ Tests completed$(NC)"

chromadb:
	@echo "$(CYAN)ChromaDB Explorer$(NC)"
	@echo "$(YELLOW)Commands:$(NC)"
	@echo "  --list              List all collections"
	@echo "  --show jarvis       Show documents in jarvis collection"
	@echo "  --search 'query'    Semantic search"
	@echo "  --doc <id>          Show full document"
	@echo "  --limit N           Limit results (default: 20)"
	@echo ""
	@python3 scripts/explore-chromadb.py --db-path $(CHROMADB_PATH)
