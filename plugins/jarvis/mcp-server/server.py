#!/usr/bin/env python3
"""
Jarvis Core MCP Server

Unified content API and vault access for JARVIS protocol.

Tools - Content Lifecycle (unified API):
- jarvis_store: Write any content (vault file, memory, or content)
- jarvis_retrieve: Read/search any content (semantic, by ID, by name, list)
- jarvis_remove: Delete any content (by ID or name)

Tools - Vault Filesystem:
- jarvis_read_vault_file, jarvis_list_vault_dir, jarvis_file_exists

Tools - Memory Maintenance:
- jarvis_index_vault, jarvis_index_file, jarvis_collection_stats

Tools - Path Configuration:
- jarvis_resolve_path, jarvis_list_paths

Tools - Format Support:
- jarvis_get_format_reference

Note: Git operations (commit, status, push, etc.) have moved to jarvis-obsidian.
PKM-specific tools (index_vault, index_file, get_format_reference)
are conditionally visible based on jarvis-obsidian availability.
"""
import asyncio
import inspect
import json
import logging
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from tools.file_ops import read_vault_file, list_vault_dir, file_exists_in_vault
from tools.memory import index_vault, index_file
from tools.paths import (
    get_path,
    get_relative_path,
    list_all_paths,
    validate_paths_config,
    PathNotConfiguredError,
)
from tools.query import collection_stats
from tools.store import store
from tools.retrieve import retrieve
from tools.remove import remove

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("jarvis-core")

import system_prompt

server = Server("core", instructions=system_prompt.instructions)

# Tool definitions
TOOLS = [
    # Unified content API
    Tool(
        name="jarvis_store",
        description="Store content in Jarvis. Provide ONE routing param: id (update existing from retrieve), relative_path (new vault file), or type (new memory/content). Auto-indexes .md files.",
        inputSchema={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Content to store (required for write/append modes and type-based writes)",
                },
                "id": {
                    "type": "string",
                    "description": "Document ID from jarvis_retrieve — update existing content. Routes by prefix: vault::* -> file, memory::* -> memory, obs::/pattern::/* -> content.",
                },
                "relative_path": {
                    "type": "string",
                    "description": "Vault-relative path for NEW file writes (e.g., 'journal/2026/02/entry.md'). Use when creating content with no prior ID.",
                },
                "type": {
                    "type": "string",
                    "enum": [
                        "memory",
                        "observation",
                        "pattern",
                        "learning",
                        "decision",
                        "summary",
                        "code",
                        "relationship",
                        "hint",
                        "plan",
                        "worklog",
                    ],
                    "description": "Content type for NEW content. 'memory' = strategic (file-backed). Others = ephemeral (pgvector).",
                },
                "name": {
                    "type": "string",
                    "description": "Name/slug for addressable content. Required for: memory, pattern, plan, decision.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["write", "append", "edit"],
                    "default": "write",
                    "description": "For vault file writes: 'write' (create/overwrite), 'append' (add to existing), 'edit' (find-and-replace)",
                },
                "old_string": {
                    "type": "string",
                    "description": "For edit mode: exact string to find",
                },
                "new_string": {
                    "type": "string",
                    "description": "For edit mode: replacement string",
                },
                "separator": {
                    "type": "string",
                    "default": "\n",
                    "description": "For append mode: prepended before content",
                },
                "replace_all": {
                    "type": "boolean",
                    "default": False,
                    "description": "For edit mode: replace all occurrences",
                },
                "importance": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "Importance score 0.0-1.0",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags for categorization",
                },
                "scope": {
                    "type": "string",
                    "enum": ["global", "project"],
                    "default": "global",
                    "description": "For memory type: scope",
                },
                "project": {
                    "type": "string",
                    "description": "For project-scoped memories",
                },
                "source": {
                    "type": "string",
                    "description": "Source label (default varies by route)",
                },
                "session_id": {"type": "string", "description": "Session identifier"},
                "extra_metadata": {
                    "type": "object",
                    "description": "Additional metadata key-value pairs",
                },
                "overwrite": {
                    "type": "boolean",
                    "default": False,
                    "description": "For memory type: allow overwriting (auto-set to true for id-based updates)",
                },
                "auto_index": {
                    "type": "boolean",
                    "default": True,
                    "description": "Auto-index .md files for semantic search",
                },
                "skip_secret_scan": {
                    "type": "boolean",
                    "default": False,
                    "description": "Skip secret detection",
                },
            },
            "required": ["content"],
        },
    ),
    Tool(
        name="jarvis_retrieve",
        description="Retrieve content from Jarvis. Provide ONE of: query (semantic search), id (read by ID), name (memory by name), or list_type ('content'/'memory' to browse).",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Semantic search query (searches all indexed content)",
                },
                "id": {
                    "type": "string",
                    "description": "Document ID to read (routes automatically by ID prefix)",
                },
                "name": {
                    "type": "string",
                    "description": "Strategic memory name to read",
                },
                "list_type": {
                    "type": "string",
                    "enum": ["content", "tier2", "memory"],
                    "description": "List content: 'content' (ephemeral) or 'memory' (strategic)",
                },
                "n_results": {
                    "type": "integer",
                    "default": 5,
                    "description": "Max results for query mode",
                },
                "type_filter": {
                    "type": "string",
                    "description": "Filter by content type when listing",
                },
                "min_importance": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "Min importance score for content listing",
                },
                "source": {
                    "type": "string",
                    "description": "Filter by source for content listing",
                },
                "scope": {
                    "type": "string",
                    "enum": ["global", "project", "all"],
                    "default": "global",
                    "description": "Scope for memory reads/lists",
                },
                "project": {
                    "type": "string",
                    "description": "Project name for scoped memories",
                },
                "tag": {
                    "type": "string",
                    "description": "Filter by tag for memory listing",
                },
                "importance": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "Filter by importance for memory listing",
                },
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "description": "Max results for list mode",
                },
                "filter": {
                    "type": "object",
                    "description": "Metadata filter for query mode (directory, type, importance, tags)",
                },
                "include_metadata": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include metadata in ID-based reads",
                },
                "include_content": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include document content in list results (for list_type='memory' and 'content')",
                },
                "sort_by": {
                    "type": "string",
                    "enum": [
                        "importance_desc",
                        "importance_asc",
                        "created_at_desc",
                        "created_at_asc",
                        "none",
                    ],
                    "default": "importance_desc",
                    "description": "Sort order for content list mode (default: importance_desc)",
                },
                "session_id": {
                    "type": "string",
                    "description": "Filter content results by session ID",
                },
                "user": {
                    "type": "string",
                    "description": "Filter results by user (for multi-user deployments)",
                },
            },
        },
    ),
    Tool(
        name="jarvis_remove",
        description="Delete content from Jarvis. Provide id (document ID from retrieve results) or name (strategic memory name).",
        inputSchema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Document ID to delete (from jarvis_retrieve results). Works for vault and content.",
                },
                "name": {
                    "type": "string",
                    "description": "Strategic memory name to delete",
                },
                "scope": {
                    "type": "string",
                    "enum": ["global", "project"],
                    "default": "global",
                },
                "project": {
                    "type": "string",
                    "description": "Project name for scoped memories",
                },
                "confirm": {
                    "type": "boolean",
                    "default": False,
                    "description": "Required for global memory deletion (safety gate)",
                },
            },
        },
    ),
    # Vault file operations (read-only filesystem access)
    Tool(
        name="jarvis_read_vault_file",
        description="Read a file from within the vault directory.",
        inputSchema={
            "type": "object",
            "properties": {
                "relative_path": {
                    "type": "string",
                    "description": "Path relative to vault root",
                }
            },
            "required": ["relative_path"],
        },
    ),
    Tool(
        name="jarvis_list_vault_dir",
        description="List contents of a directory within the vault.",
        inputSchema={
            "type": "object",
            "properties": {
                "relative_path": {
                    "type": "string",
                    "description": "Path relative to vault root (default: vault root)",
                }
            },
        },
    ),
    Tool(
        name="jarvis_file_exists",
        description="Check if a file or directory exists within the vault.",
        inputSchema={
            "type": "object",
            "properties": {
                "relative_path": {
                    "type": "string",
                    "description": "Path relative to vault root",
                }
            },
            "required": ["relative_path"],
        },
    ),
    # Memory operations (pgvector semantic indexing)
    Tool(
        name="jarvis_index_vault",
        description="Bulk index all .md files in the vault into PostgreSQL for semantic search.",
        inputSchema={
            "type": "object",
            "properties": {
                "force": {
                    "type": "boolean",
                    "description": "Re-index all files, even already indexed (default: false)",
                },
                "directory": {
                    "type": "string",
                    "description": "Only index files in this subdirectory (optional)",
                },
                "include_sensitive": {
                    "type": "boolean",
                    "description": "Include documents/ and people/ directories (default: false)",
                },
            },
        },
    ),
    Tool(
        name="jarvis_index_file",
        description="Index a single vault file into PostgreSQL (for incremental indexing after journal creation).",
        inputSchema={
            "type": "object",
            "properties": {
                "relative_path": {
                    "type": "string",
                    "description": "Path relative to vault root",
                }
            },
            "required": ["relative_path"],
        },
    ),
    # Memory stats
    Tool(
        name="jarvis_collection_stats",
        description="Get memory system health: document count, sample entries, and index status.",
        inputSchema={
            "type": "object",
            "properties": {
                "sample_size": {
                    "type": "integer",
                    "description": "Number of sample entries to include (default: 5)",
                    "default": 5,
                },
                "detailed": {
                    "type": "boolean",
                    "description": "Include per-type/namespace breakdowns and storage size (default: false)",
                    "default": False,
                },
            },
        },
    ),
    # Path configuration tools
    Tool(
        name="jarvis_resolve_path",
        description="Resolve a named path to its absolute filesystem location. Use for configurable vault paths.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Path identifier (e.g., 'journal_jarvis', 'inbox', 'strategic')",
                },
                "substitutions": {
                    "type": "object",
                    "description": 'Template variable replacements (e.g., {"YYYY": "2026", "MM": "02"})',
                },
                "ensure_exists": {
                    "type": "boolean",
                    "description": "Create directory if it does not exist (default: false)",
                },
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="jarvis_list_paths",
        description="List all configured paths with their resolved values. Diagnostic tool.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="jarvis_get_format_reference",
        description="Get the active file format reference (syntax guide + journal entry template). Returns the format guide content and configured extension. Call this before creating new vault files to know the correct syntax.",
        inputSchema={"type": "object", "properties": {}},
    ),
]


# Conditional PKM tool visibility — these tools are only useful when
# jarvis-obsidian provides the git audit layer for PKM workflows.
_obsidian_cache = {"available": None, "checked_at": 0.0}
_OBSIDIAN_HEALTH_TTL = 30  # seconds

_PKM_TOOLS = {
    "jarvis_index_vault",
    "jarvis_index_file",
    "jarvis_get_format_reference",
}


def _is_obsidian_available() -> bool:
    """Check if jarvis-obsidian server is reachable (with TTL cache)."""
    now = time.time()
    if now - _obsidian_cache["checked_at"] < _OBSIDIAN_HEALTH_TTL:
        if _obsidian_cache["available"] is not None:
            return _obsidian_cache["available"]
    url = os.environ.get("JARVIS_OBSIDIAN_URL", "http://localhost:8744")
    try:
        req = urllib.request.Request(f"{url}/health", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
            available = data.get("status") == "ok"
    except Exception:
        available = False
    _obsidian_cache.update(available=available, checked_at=now)
    return available


@server.list_tools()
async def list_tools() -> list[Tool]:
    if _is_obsidian_available():
        return TOOLS
    return [t for t in TOOLS if t.name not in _PKM_TOOLS]


def handle_resolve_path(args: dict) -> dict:
    """Handle jarvis_resolve_path."""
    name = args.get("name", "")
    substitutions = args.get("substitutions")
    ensure_exists = args.get("ensure_exists", False)

    try:
        resolved = get_path(
            name, substitutions=substitutions, ensure_exists=ensure_exists
        )
        is_vault_relative = name not in {"project_memories_path"}
        result = {
            "success": True,
            "name": name,
            "resolved": resolved,
            "is_vault_relative": is_vault_relative,
            "exists": os.path.exists(resolved),
        }
        if is_vault_relative:
            result["relative"] = get_relative_path(name)
        return result
    except PathNotConfiguredError as e:
        return {"success": False, "error": str(e)}
    except ValueError as e:
        return {"success": False, "error": str(e)}


def handle_list_paths() -> dict:
    """Handle jarvis_list_paths."""
    from tools.config import get_vault_path

    result = list_all_paths()
    warnings = validate_paths_config()
    return {
        "success": True,
        "vault_path": get_vault_path(),
        "warnings": warnings,
        **result,
    }


def handle_get_format_reference() -> dict:
    """Handle jarvis_get_format_reference.

    Reads the configured file format and returns the corresponding
    syntax reference guide with extension info.
    """
    from tools.config import get_file_format
    import os

    fmt = get_file_format()
    ext = ".org" if fmt == "org" else ".md"
    ref_filename = "org.md" if fmt == "org" else "markdown.md"

    # Look for format reference in plugin defaults
    ref_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "defaults", "formats", ref_filename
    )
    ref_path = os.path.normpath(ref_path)

    if os.path.isfile(ref_path):
        with open(ref_path, "r", encoding="utf-8") as f:
            reference_content = f.read()
    else:
        reference_content = f"Format reference file not found: {ref_path}"

    return {
        "success": True,
        "format": fmt,
        "extension": ext,
        "reference": reference_content,
    }


# Tool name -> handler mapping (module-level to avoid per-call allocation)
_HANDLERS = {
    # Unified content API
    "jarvis_store": lambda args: store(**args),
    "jarvis_retrieve": lambda args: retrieve(**args),
    "jarvis_remove": lambda args: remove(**args),
    # Vault file operations (read-only)
    "jarvis_read_vault_file": lambda args: read_vault_file(
        args.get("relative_path", "")
    ),
    "jarvis_list_vault_dir": lambda args: list_vault_dir(
        args.get("relative_path", ".")
    ),
    "jarvis_file_exists": lambda args: file_exists_in_vault(
        args.get("relative_path", "")
    ),
    # Memory indexing operations
    "jarvis_index_vault": lambda args: index_vault(
        force=args.get("force", False),
        directory=args.get("directory"),
        include_sensitive=args.get("include_sensitive", False),
    ),
    "jarvis_index_file": lambda args: index_file(args.get("relative_path", "")),
    "jarvis_collection_stats": lambda args: collection_stats(
        sample_size=args.get("sample_size", 5),
        detailed=args.get("detailed", False),
    ),
    # Path configuration
    "jarvis_resolve_path": lambda args: handle_resolve_path(args),
    "jarvis_list_paths": lambda args: handle_list_paths(),
    "jarvis_get_format_reference": lambda args: handle_get_format_reference(),
}


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    logger.info(f"Tool: {name}, args: {arguments}")

    try:
        handler = _HANDLERS.get(name)
        if handler:
            result = handler(arguments or {})
            if inspect.isawaitable(result):
                result = await result
        else:
            result = {"success": False, "error": f"Unknown tool: {name}"}

        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        return [
            TextContent(
                type="text", text=json.dumps({"success": False, "error": str(e)})
            )
        ]


def get_background_tasks():
    """Registry of background async tasks to run alongside the MCP server.

    Both stdio (main) and HTTP (http_app lifespan) transports consume this,
    ensuring no drift between transport modes.
    """
    from tools.patterns import pattern_detection_loop
    from tools.todoist_sync import todoist_sync_loop
    from tools.sync_worker import sync_worker_loop

    return [pattern_detection_loop(), todoist_sync_loop(), sync_worker_loop()]


async def main():
    logger.info("Starting Jarvis Core MCP Server")

    # Initialize pgvector schema (idempotent — safe to call every startup)
    try:
        from tools.schema import ensure_schema, check_model_consistency
        ensure_schema()
        check_model_consistency()
    except Exception as e:
        logger.warning("Schema initialization deferred (database may not be ready): %s", e)

    async with stdio_server() as (read_stream, write_stream):
        server_task = server.run(
            read_stream, write_stream, server.create_initialization_options()
        )
        await asyncio.gather(
            server_task, *get_background_tasks(), return_exceptions=True
        )


def main_sync():
    """Synchronous entry point for uvx/pip scripts."""
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
