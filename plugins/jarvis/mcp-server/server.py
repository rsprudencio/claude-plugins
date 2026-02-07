#!/usr/bin/env python3
"""
Jarvis Tools MCP Server

Git operations and vault file access for JARVIS protocol.

Tools - Git Operations:
- jarvis_commit: Create JARVIS protocol commits
- jarvis_status: Get git status
- jarvis_parse_last_commit: Parse recent commit
- jarvis_push: Push to remote
- jarvis_move_files: Move files with git mv
- jarvis_query_history: Query Jarvis operations
- jarvis_rollback: Rollback a commit
- jarvis_file_history: Get file's Jarvis history
- jarvis_rewrite_commit_messages: Remove unwanted text from commit messages

Tools - Vault File Operations (require setup confirmation):
- jarvis_write_vault_file: Write file to vault (from any directory)
- jarvis_read_vault_file: Read file from vault
- jarvis_list_vault_dir: List vault directory contents
- jarvis_file_exists: Check if file exists in vault

Tools - Memory Operations (ChromaDB semantic indexing):
- jarvis_index_vault: Bulk index all vault .md files
- jarvis_index_file: Index a single file (incremental)
- jarvis_query: Semantic search across vault memory
- jarvis_doc_read: Read specific documents by ID (any namespace)
- jarvis_collection_stats: Get collection health with breakdowns

Tools - Memory CRUD (file-backed with ChromaDB index):
- jarvis_memory_write: Write a named memory file + index
- jarvis_memory_read: Read a named memory by name
- jarvis_memory_list: List memory files with metadata
- jarvis_memory_delete: Delete memory file + index entry

Tools - Path Configuration:
- jarvis_resolve_path: Resolve a named path to absolute location
- jarvis_list_paths: List all configured paths with resolved values
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
from tools.commit import stage_files, execute_commit, get_commit_stats
from tools.config import get_debug_info
from tools.file_ops import (
    write_vault_file, read_vault_file, list_vault_dir, file_exists_in_vault
)
from tools.git_ops import (
    parse_last_commit, get_status, push_to_remote, move_files,
    query_history, rollback_commit, file_history, rewrite_commit_messages
)
from tools.memory import index_vault, index_file
from tools.paths import get_path, get_relative_path, list_all_paths, validate_paths_config, PathNotConfiguredError
from tools.query import query_vault, doc_read, collection_stats
from tools.memory_crud import (
    memory_write as mem_write,
    memory_read as mem_read,
    memory_list as mem_list,
    memory_delete as mem_delete,
)

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
    Tool(
        name="jarvis_debug_config",
        description="Debug tool: returns config loading diagnostics (config path, resolved vault path, cwd, etc.).",
        inputSchema={"type": "object", "properties": {}}
    ),
    # Vault file operations (require setup-time permission)
    Tool(
        name="jarvis_write_vault_file",
        description="Write a file within the vault directory. Requires setup confirmation. Safe from any working directory.",
        inputSchema={
            "type": "object",
            "properties": {
                "relative_path": {
                    "type": "string",
                    "description": "Path relative to vault root (e.g., 'journal/2026/01/entry.md')"
                },
                "content": {
                    "type": "string",
                    "description": "File content to write"
                }
            },
            "required": ["relative_path", "content"]
        }
    ),
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
    # Memory query operations (ChromaDB semantic search)
    Tool(
        name="jarvis_query",
        description="Semantic search across vault memory. Returns formatted results with relevance scores.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search query"
                },
                "n_results": {
                    "type": "integer",
                    "description": "Max results to return (default: 5, max: 20)",
                    "default": 5
                },
                "filter": {
                    "type": "object",
                    "description": "Optional metadata filters",
                    "properties": {
                        "directory": {"type": "string", "description": "Filter by directory (e.g., 'journal', 'notes', 'work')"},
                        "type": {"type": "string", "description": "Filter by entry type (e.g., 'journal', 'note', 'idea')"},
                        "importance": {"type": "string", "description": "Filter by importance (low, medium, high)"},
                        "tags": {"type": "string", "description": "Filter by tag (comma-separated)"}
                    }
                }
            },
            "required": ["query"]
        }
    ),
    Tool(
        name="jarvis_doc_read",
        description="Read specific documents from vault memory by ID (path).",
        inputSchema={
            "type": "object",
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Document IDs (vault-relative paths, e.g., ['notes/my-note.md'])"
                },
                "include_metadata": {
                    "type": "boolean",
                    "description": "Include parsed metadata in response (default: true)",
                    "default": True
                }
            },
            "required": ["ids"]
        }
    ),
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
    # Memory CRUD operations (file-backed with ChromaDB index)
    Tool(
        name="jarvis_memory_write",
        description="Write a named memory file (with frontmatter) and index in ChromaDB. Use for strategic memories, project context, etc.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Memory name slug (lowercase, hyphens, e.g., 'jarvis-trajectory')"
                },
                "content": {
                    "type": "string",
                    "description": "Markdown content (body only — frontmatter is auto-generated)"
                },
                "scope": {
                    "type": "string",
                    "enum": ["global", "project"],
                    "description": "Memory scope: 'global' (strategic) or 'project' (project-scoped)",
                    "default": "global"
                },
                "project": {
                    "type": "string",
                    "description": "Project name (required when scope='project')"
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags for categorization"
                },
                "importance": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                    "description": "Importance level (default: 'medium')",
                    "default": "medium"
                },
                "overwrite": {
                    "type": "boolean",
                    "description": "Allow overwriting existing memory (default: false)",
                    "default": False
                },
                "skip_secret_scan": {
                    "type": "boolean",
                    "description": "Bypass secret detection (default: false)",
                    "default": False
                }
            },
            "required": ["name", "content"]
        }
    ),
    Tool(
        name="jarvis_memory_read",
        description="Read a named memory by name. Tries ChromaDB first (fast), falls back to file.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Memory name slug (e.g., 'jarvis-trajectory')"
                },
                "scope": {
                    "type": "string",
                    "enum": ["global", "project"],
                    "description": "Memory scope (default: 'global')",
                    "default": "global"
                },
                "project": {
                    "type": "string",
                    "description": "Project name (required when scope='project')"
                }
            },
            "required": ["name"]
        }
    ),
    Tool(
        name="jarvis_memory_list",
        description="List memory files with metadata and index status.",
        inputSchema={
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["global", "project", "all"],
                    "description": "Which memories to list (default: 'all')",
                    "default": "all"
                },
                "project": {
                    "type": "string",
                    "description": "Filter by project (for scope='project')"
                },
                "tag": {
                    "type": "string",
                    "description": "Filter by tag"
                },
                "importance": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                    "description": "Filter by importance level"
                }
            }
        }
    ),
    Tool(
        name="jarvis_memory_delete",
        description="Delete a memory file and its ChromaDB index entry.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Memory name slug"
                },
                "scope": {
                    "type": "string",
                    "enum": ["global", "project"],
                    "description": "Memory scope (default: 'global')",
                    "default": "global"
                },
                "project": {
                    "type": "string",
                    "description": "Project name (required when scope='project')"
                },
                "confirm": {
                    "type": "boolean",
                    "description": "Must be true for global memory deletion (safety gate)",
                    "default": False
                }
            },
            "required": ["name"]
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
        "jarvis_debug_config": lambda args: get_debug_info(),
        # Vault file operations
        "jarvis_write_vault_file": lambda args: write_vault_file(
            args.get("relative_path", ""),
            args.get("content", "")
        ),
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
        # Memory query operations
        "jarvis_query": lambda args: query_vault(
            query=args.get("query", ""),
            n_results=args.get("n_results", 5),
            filter=args.get("filter")
        ),
        "jarvis_doc_read": lambda args: doc_read(
            ids=args.get("ids", []),
            include_metadata=args.get("include_metadata", True)
        ),
        "jarvis_collection_stats": lambda args: collection_stats(
            sample_size=args.get("sample_size", 5),
            detailed=args.get("detailed", False)
        ),
        # Memory CRUD operations
        "jarvis_memory_write": lambda args: mem_write(
            name=args.get("name", ""),
            content=args.get("content", ""),
            scope=args.get("scope", "global"),
            project=args.get("project"),
            tags=args.get("tags"),
            importance=args.get("importance", "medium"),
            overwrite=args.get("overwrite", False),
            skip_secret_scan=args.get("skip_secret_scan", False),
        ),
        "jarvis_memory_read": lambda args: mem_read(
            name=args.get("name", ""),
            scope=args.get("scope", "global"),
            project=args.get("project"),
        ),
        "jarvis_memory_list": lambda args: mem_list(
            scope=args.get("scope", "all"),
            project=args.get("project"),
            tag=args.get("tag"),
            importance=args.get("importance"),
        ),
        "jarvis_memory_delete": lambda args: mem_delete(
            name=args.get("name", ""),
            scope=args.get("scope", "global"),
            project=args.get("project"),
            confirm=args.get("confirm", False),
        ),
        # Path configuration
        "jarvis_resolve_path": lambda args: handle_resolve_path(args),
        "jarvis_list_paths": lambda args: handle_list_paths(),
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
    return {
        "success": True,
        "commit_hash": commit_result["commit_hash"],
        "protocol_tag": tag_string,
        "files_changed": stats["files_changed"],
        "insertions": stats["insertions"],
        "deletions": stats["deletions"]
    }


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


async def main():
    logger.info("Starting Jarvis Tools MCP Server")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main_sync():
    """Synchronous entry point for uvx/pip scripts."""
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
