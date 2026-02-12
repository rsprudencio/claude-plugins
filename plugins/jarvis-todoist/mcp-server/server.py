#!/usr/bin/env python3
"""
Jarvis Todoist API MCP Server

Local stdio MCP server wrapping the Todoist API via the official
todoist-api-python SDK. Eliminates dependency on external HTTP MCP
(ai.todoist.net/mcp) which drops sessions during idle periods.

Tools:
- find_tasks, find_tasks_by_date
- add_tasks, complete_tasks, update_tasks
- delete_object
- user_info
- find_projects, add_projects
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

import todoist_api

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("jarvis-todoist-api")

server = Server("api")

TOOLS = [
    Tool(
        name="find_tasks",
        description="Find tasks by project, labels, or search text.",
        inputSchema={
            "type": "object",
            "properties": {
                "projectId": {
                    "type": "string",
                    "description": 'Project ID or "inbox" for inbox tasks.',
                },
                "sectionId": {
                    "type": "string",
                    "description": "Section ID to filter by.",
                },
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter by labels (all must match).",
                },
                "searchText": {
                    "type": "string",
                    "description": "Text search in task content and description.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 50).",
                    "default": 50,
                },
            },
        },
    ),
    Tool(
        name="find_tasks_by_date",
        description="Get tasks by date range. Use startDate 'today' for today's tasks including overdue.",
        inputSchema={
            "type": "object",
            "properties": {
                "startDate": {
                    "type": "string",
                    "description": "Start date: YYYY-MM-DD or 'today'.",
                    "default": "today",
                },
                "daysCount": {
                    "type": "integer",
                    "description": "Number of days from start (default 1).",
                    "default": 1,
                    "minimum": 1,
                    "maximum": 30,
                },
                "overdueOption": {
                    "type": "string",
                    "enum": ["overdue-only", "include-overdue", "exclude-overdue"],
                    "description": "How to handle overdue tasks (default: include-overdue).",
                    "default": "include-overdue",
                },
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Filter by labels.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 50).",
                    "default": 50,
                },
            },
        },
    ),
    Tool(
        name="add_tasks",
        description="Create one or more tasks in Todoist.",
        inputSchema={
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "Array of tasks to create.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "Task title."},
                            "description": {"type": "string"},
                            "dueString": {"type": "string", "description": "Due date in natural language."},
                            "priority": {
                                "type": "string",
                                "enum": ["p1", "p2", "p3", "p4"],
                                "description": "p1=highest, p4=lowest.",
                            },
                            "labels": {"type": "array", "items": {"type": "string"}},
                            "projectId": {"type": "string", "description": 'Project ID or "inbox".'},
                            "sectionId": {"type": "string"},
                            "parentId": {"type": "string"},
                            "deadlineDate": {"type": "string", "description": "Deadline in YYYY-MM-DD."},
                            "duration": {"type": "string", "description": 'Duration: "2h", "90m", "2h30m".'},
                            "order": {"type": "number"},
                        },
                        "required": ["content"],
                    },
                },
            },
            "required": ["tasks"],
        },
    ),
    Tool(
        name="complete_tasks",
        description="Complete (close) one or more tasks by ID.",
        inputSchema={
            "type": "object",
            "properties": {
                "ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Task IDs to complete.",
                },
            },
            "required": ["ids"],
        },
    ),
    Tool(
        name="update_tasks",
        description="Update one or more existing tasks.",
        inputSchema={
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "Tasks to update (each must have 'id').",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "Task ID to update."},
                            "content": {"type": "string"},
                            "description": {"type": "string"},
                            "dueString": {"type": "string"},
                            "priority": {"type": "string", "enum": ["p1", "p2", "p3", "p4"]},
                            "labels": {"type": "array", "items": {"type": "string"}},
                            "projectId": {"type": "string"},
                            "sectionId": {"type": "string"},
                            "parentId": {"type": "string"},
                            "deadlineDate": {"type": "string"},
                            "duration": {"type": "string"},
                            "order": {"type": "number"},
                        },
                        "required": ["id"],
                    },
                },
            },
            "required": ["tasks"],
        },
    ),
    Tool(
        name="delete_object",
        description="Delete a task, project, section, or comment by ID.",
        inputSchema={
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["task", "project", "section", "comment"],
                    "description": "Type of object to delete.",
                },
                "id": {
                    "type": "string",
                    "description": "ID of the object to delete.",
                },
            },
            "required": ["type", "id"],
        },
    ),
    Tool(
        name="user_info",
        description="Get Todoist user information (name, email, timezone, plan).",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    Tool(
        name="find_projects",
        description="List all projects, optionally filter by name.",
        inputSchema={
            "type": "object",
            "properties": {
                "search": {
                    "type": "string",
                    "description": "Search by project name (case-insensitive substring).",
                },
            },
        },
    ),
    Tool(
        name="add_projects",
        description="Create one or more Todoist projects.",
        inputSchema={
            "type": "object",
            "properties": {
                "projects": {
                    "type": "array",
                    "description": "Array of projects to create.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Project name."},
                            "parentId": {"type": "string", "description": "Parent project ID for sub-projects."},
                            "viewStyle": {
                                "type": "string",
                                "enum": ["list", "board", "calendar"],
                            },
                            "isFavorite": {"type": "boolean"},
                        },
                        "required": ["name"],
                    },
                },
            },
            "required": ["projects"],
        },
    ),
]

# Map tool names to API functions with argument translation
HANDLERS = {
    "find_tasks": lambda args: todoist_api.find_tasks(
        project_id=args.get("projectId"),
        section_id=args.get("sectionId"),
        labels=args.get("labels"),
        search_text=args.get("searchText"),
        limit=args.get("limit", 50),
    ),
    "find_tasks_by_date": lambda args: todoist_api.find_tasks_by_date(
        start_date=args.get("startDate", "today"),
        days_count=args.get("daysCount", 1),
        overdue_option=args.get("overdueOption", "include-overdue"),
        labels=args.get("labels"),
        limit=args.get("limit", 50),
    ),
    "add_tasks": lambda args: todoist_api.add_tasks(args["tasks"]),
    "complete_tasks": lambda args: todoist_api.complete_tasks(args["ids"]),
    "update_tasks": lambda args: todoist_api.update_tasks(args["tasks"]),
    "delete_object": lambda args: todoist_api.delete_object(args["type"], args["id"]),
    "user_info": lambda args: todoist_api.user_info(),
    "find_projects": lambda args: todoist_api.find_projects(search=args.get("search")),
    "add_projects": lambda args: todoist_api.add_projects(args["projects"]),
}


@server.list_tools()
async def list_tools():
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    handler = HANDLERS.get(name)
    if not handler:
        result = {"success": False, "error": f"Unknown tool: {name}"}
    else:
        try:
            result = handler(arguments)
        except Exception as e:
            result = {"success": False, "error": f"Tool execution error: {str(e)}"}

    return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


async def main():
    logger.info("Starting Jarvis Todoist API MCP Server")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main_sync():
    """Synchronous entry point for uvx/pip scripts."""
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
