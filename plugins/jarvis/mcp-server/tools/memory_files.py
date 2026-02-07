"""File I/O for memory files.

Handles read/write for:
- Global strategic memories: <vault>/.jarvis/strategic/<name>.md
- Project-scoped memories: <vault>/.jarvis/memories/<project>/<name>.md

Files use YAML frontmatter for metadata, making them Obsidian-visible
and git-auditable. This is the Tier 1 (file SSoT) layer.
"""
import os
import re
from datetime import datetime, timezone
from typing import Optional

from .config import get_verified_vault_path

# Valid memory name: lowercase alphanumeric with hyphens, no leading/trailing hyphen
NAME_PATTERN = re.compile(r'^[a-z0-9][a-z0-9-]*[a-z0-9]$')

# Minimum name length (single chars are allowed via separate check)
MIN_NAME_LEN = 2


def validate_name(name: str) -> Optional[str]:
    """Validate memory name is a valid slug.

    Returns error message if invalid, None if valid.
    """
    if not name:
        return "Name cannot be empty"
    if len(name) < MIN_NAME_LEN:
        # Allow single alphanumeric chars
        if len(name) == 1 and re.match(r'^[a-z0-9]$', name):
            return None
        return f"Name too short: '{name}' (minimum {MIN_NAME_LEN} chars)"
    if not NAME_PATTERN.match(name):
        return (
            f"Invalid name: '{name}'. "
            "Use lowercase alphanumeric with hyphens (e.g., 'jarvis-trajectory')"
        )
    return None


def get_strategic_dir() -> tuple[str, str]:
    """Returns (<vault_path>/.jarvis/strategic/, error).

    Creates the directory if needed.
    """
    vault_path, error = get_verified_vault_path()
    if error:
        return "", error
    strategic_dir = os.path.join(vault_path, ".jarvis", "strategic")
    os.makedirs(strategic_dir, exist_ok=True)
    return strategic_dir, ""


def get_project_dir(project: str) -> tuple[str, str]:
    """Returns (<vault_path>/.jarvis/memories/<project>/, error).

    Creates the directory if needed.
    """
    vault_path, error = get_verified_vault_path()
    if error:
        return "", error
    # Sanitize project name
    safe_project = re.sub(r'[^a-z0-9-]', '', project.lower().strip())
    if not safe_project:
        return "", f"Invalid project name: '{project}'"
    project_dir = os.path.join(vault_path, ".jarvis", "memories", safe_project)
    os.makedirs(project_dir, exist_ok=True)
    return project_dir, ""


def resolve_memory_path(name: str, scope: str = "global",
                        project: Optional[str] = None) -> tuple[str, str]:
    """Resolve name + scope to full file path.

    Args:
        name: Memory name slug
        scope: "global" or "project"
        project: Required when scope="project"

    Returns:
        Tuple of (full_path, error). If error, path is empty.
    """
    name_error = validate_name(name)
    if name_error:
        return "", name_error

    if scope == "project":
        if not project:
            return "", "Project name required for scope='project'"
        base_dir, error = get_project_dir(project)
    else:
        base_dir, error = get_strategic_dir()

    if error:
        return "", error

    return os.path.join(base_dir, f"{name}.md"), ""


def _format_frontmatter(name: str, scope: str, importance: str,
                        tags: list, version: int,
                        created: str, modified: str,
                        project: Optional[str] = None) -> str:
    """Generate YAML frontmatter string."""
    lines = [
        "---",
        f"name: {name}",
        f"scope: {scope}",
    ]
    if scope == "project" and project:
        lines.append(f"project: {project}")
    lines.append(f"importance: {importance}")
    if tags:
        lines.append("tags:")
        for tag in tags:
            lines.append(f"  - {tag}")
    lines.extend([
        f"created: {created}",
        f"modified: {modified}",
        f"version: {version}",
        "---",
    ])
    return "\n".join(lines) + "\n"


def _parse_memory_frontmatter(content: str) -> dict:
    """Parse YAML frontmatter from a memory file.

    Handles the specific fields we write (name, scope, importance, etc.).
    Reuses the same regex approach as memory.py._parse_frontmatter.
    """
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
    if not match:
        return {}

    fm = {}
    for line in match.group(1).split('\n'):
        if ':' in line and not line.strip().startswith('-'):
            key, _, value = line.partition(':')
            fm[key.strip()] = value.strip().strip('"').strip("'")

    # Parse list-style tags
    tag_match = re.search(r'tags:\s*\n((?:\s+-\s+.*\n)*)', match.group(1) + '\n')
    if tag_match:
        tags = re.findall(r'-\s+(.+)', tag_match.group(1))
        fm['tags'] = [t.strip().strip('"').strip("'") for t in tags]
    elif 'tags' in fm:
        # Single-line tags: convert comma-separated to list
        fm['tags'] = [t.strip() for t in fm['tags'].split(',') if t.strip()]

    # Convert version to int if present
    if 'version' in fm:
        try:
            fm['version'] = int(fm['version'])
        except (ValueError, TypeError):
            fm['version'] = 1

    return fm


def _strip_frontmatter(content: str) -> str:
    """Remove YAML frontmatter from content."""
    return re.sub(r'^---\s*\n.*?\n---\s*\n', '', content, count=1, flags=re.DOTALL)


def write_memory_file(path: str, name: str, content: str, scope: str,
                      project: Optional[str], importance: str,
                      tags: list, overwrite: bool) -> dict:
    """Write markdown file with YAML frontmatter.

    Args:
        path: Full file path
        name: Memory name slug
        content: Memory content (markdown body, no frontmatter)
        scope: "global" or "project"
        project: Project name (for project scope)
        importance: "low", "medium", "high", "critical"
        tags: List of tag strings
        overwrite: Whether to overwrite existing file

    Returns:
        {success, path, created, version}
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    existing_version = 0
    created_at = now_iso

    if os.path.isfile(path):
        if not overwrite:
            return {
                "success": False,
                "error": f"Memory '{name}' already exists. Use overwrite=true to update.",
                "exists": True,
            }
        # Read existing to preserve created_at and bump version
        try:
            with open(path, 'r', encoding='utf-8') as f:
                existing = f.read()
            fm = _parse_memory_frontmatter(existing)
            created_at = fm.get("created", now_iso)
            existing_version = fm.get("version", 1)
            if isinstance(existing_version, str):
                existing_version = int(existing_version)
        except Exception:
            pass

    version = existing_version + 1 if os.path.isfile(path) else 1
    frontmatter = _format_frontmatter(
        name=name, scope=scope, importance=importance,
        tags=tags, version=version, created=created_at,
        modified=now_iso, project=project,
    )

    full_content = frontmatter + content

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(full_content)
        return {
            "success": True,
            "path": path,
            "created": not os.path.isfile(path) or version == 1,
            "version": version,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def read_memory_file(path: str) -> dict:
    """Read memory file, parse frontmatter, return content + metadata.

    Args:
        path: Full file path

    Returns:
        {success, content, body, metadata} or {success: false, error}
    """
    if not os.path.isfile(path):
        return {"success": False, "error": f"File not found: {path}"}

    try:
        with open(path, 'r', encoding='utf-8') as f:
            full_content = f.read()

        metadata = _parse_memory_frontmatter(full_content)
        body = _strip_frontmatter(full_content)

        return {
            "success": True,
            "content": full_content,
            "body": body.strip(),
            "metadata": metadata,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def list_memory_files(scope: str = "global", project: Optional[str] = None,
                      tag: Optional[str] = None,
                      importance: Optional[str] = None) -> list:
    """Scan filesystem for memory files, parse frontmatter, apply filters.

    Args:
        scope: "global", "project", or "all"
        project: Project name (for scope="project")
        tag: Filter by tag
        importance: Filter by importance level

    Returns:
        List of {name, scope, importance, tags, modified, path}
    """
    dirs_to_scan = []

    if scope in ("global", "all"):
        strategic_dir, error = get_strategic_dir()
        if not error:
            dirs_to_scan.append(("global", strategic_dir, None))

    if scope in ("project", "all"):
        if scope == "project" and project:
            project_dir, error = get_project_dir(project)
            if not error:
                dirs_to_scan.append(("project", project_dir, project))
        elif scope == "all":
            # Scan all project directories
            vault_path, error = get_verified_vault_path()
            if not error:
                memories_base = os.path.join(vault_path, ".jarvis", "memories")
                if os.path.isdir(memories_base):
                    for proj_name in os.listdir(memories_base):
                        proj_dir = os.path.join(memories_base, proj_name)
                        if os.path.isdir(proj_dir):
                            dirs_to_scan.append(("project", proj_dir, proj_name))

    results = []
    for entry_scope, directory, proj_name in dirs_to_scan:
        if not os.path.isdir(directory):
            continue
        for filename in os.listdir(directory):
            if not filename.endswith(".md"):
                continue
            filepath = os.path.join(directory, filename)
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
                fm = _parse_memory_frontmatter(content)
            except Exception:
                fm = {}

            entry_importance = fm.get("importance", "medium")
            entry_tags = fm.get("tags", [])
            if isinstance(entry_tags, str):
                entry_tags = [t.strip() for t in entry_tags.split(",")]

            # Apply filters
            if importance and entry_importance != importance:
                continue
            if tag and tag not in entry_tags:
                continue

            name = filename[:-3]  # strip .md
            results.append({
                "name": name,
                "scope": entry_scope,
                "project": proj_name,
                "importance": entry_importance,
                "tags": entry_tags,
                "modified": fm.get("modified", ""),
                "version": fm.get("version", 1),
                "path": filepath,
            })

    return results


def delete_memory_file(path: str) -> dict:
    """Delete a memory file.

    Args:
        path: Full file path

    Returns:
        {success} or {success: false, error}
    """
    if not os.path.isfile(path):
        return {"success": False, "error": f"File not found: {path}"}

    try:
        os.remove(path)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}
