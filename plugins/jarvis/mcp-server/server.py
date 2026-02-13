#!/usr/bin/env python3
"""
Jarvis Tools MCP Server

Unified content API, git operations, and vault access for JARVIS protocol.

Tools - Content Lifecycle (unified API):
- jarvis_store: Write any content (vault file, memory, or tier2)
- jarvis_retrieve: Read/search any content (semantic, by ID, by name, list)
- jarvis_remove: Delete any content (by ID or name)
- jarvis_promote: Promote tier2 content to tier1 (file-backed)

Tools - Git Operations:
- jarvis_commit, jarvis_status, jarvis_parse_last_commit, jarvis_push
- jarvis_move_files, jarvis_query_history, jarvis_rollback
- jarvis_file_history, jarvis_rewrite_commit_messages

Tools - Vault Filesystem:
- jarvis_read_vault_file, jarvis_list_vault_dir, jarvis_file_exists

Tools - Memory Maintenance:
- jarvis_index_vault, jarvis_index_file, jarvis_collection_stats

Tools - Path Configuration:
- jarvis_resolve_path, jarvis_list_paths

Tools - Format Support:
- jarvis_get_format_reference
"""
import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from protocol import (
    ProtocolTag,
    ProtocolValidator,
    format_commit_message,
    VALID_OPERATIONS
)
from tools.commit import stage_files, execute_commit, get_commit_stats, reindex_committed_files, commit_user_prologue
from tools.file_ops import read_vault_file, list_vault_dir, file_exists_in_vault
from tools.git_ops import (
    parse_last_commit, get_status, push_to_remote, move_files,
    query_history, rollback_commit, file_history, rewrite_commit_messages
)
from tools.memory import index_vault, index_file
from tools.paths import get_path, get_relative_path, list_all_paths, validate_paths_config, PathNotConfiguredError
from tools.query import collection_stats
from tools.promotion import promote, check_promotion_criteria
from tools.store import store
from tools.retrieve import retrieve
from tools.remove import remove

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger("jarvis-tools")

server = Server("core")

# Tool definitions
TOOLS = [
    Tool(
        name="jarvis_commit",
        description="Create a JARVIS protocol git commit with validation and formatting.",
        inputSchema={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["create", "edit", "delete", "move", "user"],
                    "description": "Operation type"
                },
                "description": {"type": "string", "description": "Commit message"},
                "entry_id": {"type": "string", "description": "14-digit timestamp (optional)"},
                "trigger_mode": {
                    "type": "string",
                    "enum": ["conversational", "agent"],
                    "default": "conversational"
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Files to stage (optional)"
                }
            },
            "required": ["operation", "description"]
        }
    ),
    Tool(
        name="jarvis_status",
        description="Get current git status (staged, unstaged, untracked files).",
        inputSchema={"type": "object", "properties": {}}
    ),
    Tool(
        name="jarvis_parse_last_commit",
        description="Parse info about the most recent commit.",
        inputSchema={"type": "object", "properties": {}}
    ),
    Tool(
        name="jarvis_push",
        description="Push commits to remote repository.",
        inputSchema={
            "type": "object",
            "properties": {
                "branch": {"type": "string", "description": "Branch to push (optional)"}
            }
        }
    ),
    Tool(
        name="jarvis_move_files",
        description="Move/rename files using git mv (preserves history).",
        inputSchema={
            "type": "object",
            "properties": {
                "moves": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source": {"type": "string"},
                            "destination": {"type": "string"}
                        },
                        "required": ["source", "destination"]
                    }
                }
            },
            "required": ["moves"]
        }
    ),
    Tool(
        name="jarvis_query_history",
        description="Query Jarvis operations from git history.",
        inputSchema={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["create", "edit", "delete", "move", "user", "all"],
                    "description": "Filter by operation type (default: all)"
                },
                "since": {"type": "string", "description": "Time filter (e.g., 'today', '1 week ago')"},
                "limit": {"type": "integer", "description": "Max results (default: 10)"},
                "file": {"type": "string", "description": "Filter by file path (optional)"}
            }
        }
    ),
    Tool(
        name="jarvis_rollback",
        description="Rollback a specific Jarvis commit using git revert.",
        inputSchema={
            "type": "object",
            "properties": {
                "commit_hash": {"type": "string", "description": "Commit hash to revert"}
            },
            "required": ["commit_hash"]
        }
    ),
    Tool(
        name="jarvis_file_history",
        description="Get Jarvis operation history for a specific file.",
        inputSchema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the file"},
                "limit": {"type": "integer", "description": "Max results (default: 10)"}
            },
            "required": ["file_path"]
        }
    ),
    Tool(
        name="jarvis_rewrite_commit_messages",
        description="Rewrite recent commit messages to remove unwanted text (e.g., Co-Authored-By lines). WARNING: Rewrites history - only use on unpushed commits.",
        inputSchema={
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "description": "Number of recent commits to process (default: 1)"
                },
                "patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Sed regex patterns to remove. Default: ['Co-Authored-By:.*']"
                }
            }
        }
    ),
    # Unified content API
    Tool(
        name="jarvis_store",
        description="Store content in Jarvis. Provide ONE routing param: id (update existing from retrieve), relative_path (new vault file), or type (new memory/tier2). Auto-indexes .md files.",
        inputSchema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Content to store (required for write/append modes and type-based writes)"},
                "id": {"type": "string", "description": "Document ID from jarvis_retrieve — update existing content. Routes by prefix: vault::* -> file, memory::* -> memory, obs::/pattern::/* -> tier2."},
                "relative_path": {"type": "string", "description": "Vault-relative path for NEW file writes (e.g., 'journal/2026/02/entry.md'). Use when creating content with no prior ID."},
                "type": {
                    "type": "string",
                    "enum": ["memory", "observation", "pattern", "learning", "decision", "summary", "code", "relationship", "hint", "plan"],
                    "description": "Content type for NEW content. 'memory' = strategic (file-backed). Others = ephemeral (ChromaDB)."
                },
                "name": {"type": "string", "description": "Name/slug for addressable content. Required for: memory, pattern, plan, decision."},
                "mode": {
                    "type": "string", "enum": ["write", "append", "edit"], "default": "write",
                    "description": "For vault file writes: 'write' (create/overwrite), 'append' (add to existing), 'edit' (find-and-replace)"
                },
                "old_string": {"type": "string", "description": "For edit mode: exact string to find"},
                "new_string": {"type": "string", "description": "For edit mode: replacement string"},
                "separator": {"type": "string", "default": "\n", "description": "For append mode: prepended before content"},
                "replace_all": {"type": "boolean", "default": False, "description": "For edit mode: replace all occurrences"},
                "importance": {"type": "number", "minimum": 0.0, "maximum": 1.0, "description": "Importance score 0.0-1.0"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags for categorization"},
                "scope": {"type": "string", "enum": ["global", "project"], "default": "global", "description": "For memory type: scope"},
                "project": {"type": "string", "description": "For project-scoped memories"},
                "source": {"type": "string", "description": "Source label (default varies by route)"},
                "session_id": {"type": "string", "description": "Session identifier"},
                "extra_metadata": {"type": "object", "description": "Additional metadata key-value pairs"},
                "overwrite": {"type": "boolean", "default": False, "description": "For memory type: allow overwriting (auto-set to true for id-based updates)"},
                "auto_index": {"type": "boolean", "default": True, "description": "Auto-index .md files to ChromaDB"},
                "skip_secret_scan": {"type": "boolean", "default": False, "description": "Skip secret detection"}
            },
            "required": ["content"]
        }
    ),
    Tool(
        name="jarvis_retrieve",
        description="Retrieve content from Jarvis. Provide ONE of: query (semantic search), id (read by ID), name (memory by name), or list_type ('tier2'/'memory' to browse).",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Semantic search query (searches all indexed content)"},
                "id": {"type": "string", "description": "Document ID to read (routes automatically by ID prefix)"},
                "name": {"type": "string", "description": "Strategic memory name to read"},
                "list_type": {"type": "string", "enum": ["tier2", "memory"], "description": "List content: 'tier2' (ephemeral) or 'memory' (strategic)"},
                "n_results": {"type": "integer", "default": 5, "description": "Max results for query mode"},
                "type_filter": {"type": "string", "description": "Filter by content type when listing"},
                "min_importance": {"type": "number", "minimum": 0.0, "maximum": 1.0, "description": "Min importance score for tier2 listing"},
                "source": {"type": "string", "description": "Filter by source for tier2 listing"},
                "scope": {"type": "string", "enum": ["global", "project", "all"], "default": "global", "description": "Scope for memory reads/lists"},
                "project": {"type": "string", "description": "Project name for scoped memories"},
                "tag": {"type": "string", "description": "Filter by tag for memory listing"},
                "importance": {"type": "string", "enum": ["low", "medium", "high", "critical"], "description": "Filter by importance for memory listing"},
                "limit": {"type": "integer", "default": 20, "description": "Max results for list mode"},
                "filter": {"type": "object", "description": "Metadata filter for query mode (directory, type, importance, tags)"},
                "include_metadata": {"type": "boolean", "default": True, "description": "Include metadata in ID-based reads"},
                "sort_by": {
                    "type": "string",
                    "enum": ["importance_desc", "importance_asc", "created_at_desc", "created_at_asc", "none"],
                    "default": "importance_desc",
                    "description": "Sort order for tier2 list mode (default: importance_desc)"
                }
            }
        }
    ),
    Tool(
        name="jarvis_remove",
        description="Delete content from Jarvis. Provide id (document ID from retrieve results) or name (strategic memory name).",
        inputSchema={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Document ID to delete (from jarvis_retrieve results). Works for vault and tier2 content."},
                "name": {"type": "string", "description": "Strategic memory name to delete"},
                "scope": {"type": "string", "enum": ["global", "project"], "default": "global"},
                "project": {"type": "string", "description": "Project name for scoped memories"},
                "confirm": {"type": "boolean", "default": False, "description": "Required for global memory deletion (safety gate)"}
            }
        }
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
                    "description": "Path relative to vault root"
                }
            },
            "required": ["relative_path"]
        }
    ),
    Tool(
        name="jarvis_list_vault_dir",
        description="List contents of a directory within the vault.",
        inputSchema={
            "type": "object",
            "properties": {
                "relative_path": {
                    "type": "string",
                    "description": "Path relative to vault root (default: vault root)"
                }
            }
        }
    ),
    Tool(
        name="jarvis_file_exists",
        description="Check if a file or directory exists within the vault.",
        inputSchema={
            "type": "object",
            "properties": {
                "relative_path": {
                    "type": "string",
                    "description": "Path relative to vault root"
                }
            },
            "required": ["relative_path"]
        }
    ),
    # Memory operations (ChromaDB semantic indexing)
    Tool(
        name="jarvis_index_vault",
        description="Bulk index all .md files in the vault into ChromaDB for semantic search.",
        inputSchema={
            "type": "object",
            "properties": {
                "force": {
                    "type": "boolean",
                    "description": "Re-index all files, even already indexed (default: false)"
                },
                "directory": {
                    "type": "string",
                    "description": "Only index files in this subdirectory (optional)"
                },
                "include_sensitive": {
                    "type": "boolean",
                    "description": "Include documents/ and people/ directories (default: false)"
                }
            }
        }
    ),
    Tool(
        name="jarvis_index_file",
        description="Index a single vault file into ChromaDB (for incremental indexing after journal creation).",
        inputSchema={
            "type": "object",
            "properties": {
                "relative_path": {
                    "type": "string",
                    "description": "Path relative to vault root"
                }
            },
            "required": ["relative_path"]
        }
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
                    "default": 5
                },
                "detailed": {
                    "type": "boolean",
                    "description": "Include per-type/namespace breakdowns and storage size (default: false)",
                    "default": False
                }
            }
        }
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
                    "description": "Path identifier (e.g., 'journal_jarvis', 'inbox', 'db_path')"
                },
                "substitutions": {
                    "type": "object",
                    "description": "Template variable replacements (e.g., {\"YYYY\": \"2026\", \"MM\": \"02\"})"
                },
                "ensure_exists": {
                    "type": "boolean",
                    "description": "Create directory if it does not exist (default: false)"
                }
            },
            "required": ["name"]
        }
    ),
    Tool(
        name="jarvis_list_paths",
        description="List all configured paths with their resolved values. Diagnostic tool.",
        inputSchema={"type": "object", "properties": {}}
    ),
    Tool(
        name="jarvis_promote",
        description="Promote Tier 2 content to Tier 1 (file-backed) storage. Checks importance/retrieval thresholds and writes to vault.",
        inputSchema={
            "type": "object",
            "properties": {
                "doc_id": {
                    "type": "string",
                    "description": "Tier 2 document ID to promote"
                }
            },
            "required": ["doc_id"]
        }
    ),
    Tool(
        name="jarvis_get_format_reference",
        description="Get the active file format reference (syntax guide + journal entry template). Returns the format guide content and configured extension. Call this before creating new vault files to know the correct syntax.",
        inputSchema={"type": "object", "properties": {}}
    ),
]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    logger.info(f"Tool: {name}, args: {arguments}")

    handlers = {
        "jarvis_commit": handle_commit,
        "jarvis_status": lambda args: get_status(),
        "jarvis_parse_last_commit": lambda args: parse_last_commit(),
        "jarvis_push": lambda args: push_to_remote(args.get("branch")),
        "jarvis_move_files": lambda args: move_files(args.get("moves", [])),
        "jarvis_query_history": lambda args: query_history(
            operation=args.get("operation", "all"),
            since=args.get("since"),
            limit=args.get("limit", 10),
            file_path=args.get("file")
        ),
        "jarvis_rollback": lambda args: rollback_commit(args.get("commit_hash")),
        "jarvis_file_history": lambda args: file_history(
            args.get("file_path"),
            args.get("limit", 10)
        ),
        "jarvis_rewrite_commit_messages": lambda args: rewrite_commit_messages(
            count=args.get("count", 1),
            patterns=args.get("patterns")
        ),
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
            include_sensitive=args.get("include_sensitive", False)
        ),
        "jarvis_index_file": lambda args: index_file(
            args.get("relative_path", "")
        ),
        "jarvis_collection_stats": lambda args: collection_stats(
            sample_size=args.get("sample_size", 5),
            detailed=args.get("detailed", False)
        ),
        # Path configuration
        "jarvis_resolve_path": lambda args: handle_resolve_path(args),
        "jarvis_list_paths": lambda args: handle_list_paths(),
        "jarvis_promote": lambda args: promote(
            doc_id=args.get("doc_id", "")
        ),
        "jarvis_get_format_reference": lambda args: handle_get_format_reference(),
    }

    try:
        handler = handlers.get(name)
        if handler:
            if asyncio.iscoroutinefunction(handler):
                result = await handler(arguments)
            else:
                result = handler(arguments)
        else:
            result = {"success": False, "error": f"Unknown tool: {name}"}

        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        return [TextContent(type="text", text=json.dumps({"success": False, "error": str(e)}))]


async def handle_commit(args: dict) -> dict:
    """Handle jarvis_commit."""
    operation = args.get("operation")
    description = args.get("description")
    entry_id = args.get("entry_id")
    trigger_mode = args.get("trigger_mode", "conversational")
    files = args.get("files")

    # Validate
    errors = ProtocolValidator.validate_all(
        operation=operation,
        description=description,
        entry_id=entry_id,
        trigger_mode=trigger_mode
    )
    if errors:
        return {"success": False, "validation_errors": errors}

    # Auto user prologue: when explicit files are provided and this isn't a
    # user operation, automatically commit any other dirty vault files as
    # [JARVIS:U] first.  This keeps the audit trail clean without relying on
    # the LLM agent to order operations correctly.
    prologue_result = None
    if operation != "user" and files:
        prologue_result = commit_user_prologue(set(files))
        if prologue_result and not prologue_result.get("success", True):
            return prologue_result

    # Stage
    stage_result = stage_files(files)
    if not stage_result["success"]:
        return stage_result

    # Build protocol tag
    tag = ProtocolTag(operation=operation, trigger_mode=trigger_mode, entry_id=entry_id)
    tag_string = tag.to_string()

    # Commit
    commit_msg = format_commit_message(operation, description, tag_string)
    commit_result = execute_commit(commit_msg)
    if not commit_result["success"]:
        return commit_result

    stats = get_commit_stats()
    index_sync = reindex_committed_files()

    response = {
        "success": True,
        "commit_hash": commit_result["commit_hash"],
        "protocol_tag": tag_string,
        "files_changed": stats["files_changed"],
        "insertions": stats["insertions"],
        "deletions": stats["deletions"],
    }
    if prologue_result and prologue_result.get("commit_hash"):
        response["user_prologue"] = prologue_result
    if index_sync["reindexed"]:
        response["reindexed"] = index_sync["reindexed"]
    if index_sync["unindexed"]:
        response["unindexed"] = index_sync["unindexed"]
    return response


def handle_resolve_path(args: dict) -> dict:
    """Handle jarvis_resolve_path."""
    name = args.get("name", "")
    substitutions = args.get("substitutions")
    ensure_exists = args.get("ensure_exists", False)

    try:
        resolved = get_path(name, substitutions=substitutions, ensure_exists=ensure_exists)
        is_vault_relative = name not in {"db_path", "project_memories_path"}
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
        os.path.dirname(os.path.abspath(__file__)),
        "defaults", "formats", ref_filename
    )
    ref_path = os.path.normpath(ref_path)

    if os.path.isfile(ref_path):
        with open(ref_path, 'r', encoding='utf-8') as f:
            reference_content = f.read()
    else:
        reference_content = f"Format reference file not found: {ref_path}"

    return {
        "success": True,
        "format": fmt,
        "extension": ext,
        "reference": reference_content,
    }


async def main():
    logger.info("Starting Jarvis Tools MCP Server")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main_sync():
    """Synchronous entry point for uvx/pip scripts."""
    from tools.config import get_mcp_transport
    transport = get_mcp_transport()
    if transport != "local":
        logger.info(f"MCP transport is '{transport}', skipping stdio server (use HTTP instead)")
        sys.exit(0)
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
