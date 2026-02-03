#!/usr/bin/env python3
"""
Jarvis Tools MCP Server

Git operations and vault file access for JARVIS Protocol.

Tools - Git Operations:
- jarvis_commit: Create JARVIS Protocol commits
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

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger("jarvis-tools")

server = Server("tools")

# Tool definitions
TOOLS = [
    Tool(
        name="jarvis_commit",
        description="Create a JARVIS Protocol git commit with validation and formatting.",
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
    )
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
        )
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


async def main():
    logger.info("Starting Jarvis Tools MCP Server")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main_sync():
    """Synchronous entry point for uvx/pip scripts."""
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
