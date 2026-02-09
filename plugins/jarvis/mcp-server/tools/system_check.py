"""Cross-platform system requirements validation for Jarvis plugin.

Checks all prerequisites for running Jarvis on Windows, Linux, and macOS.
"""
import sys
import subprocess
import platform
from pathlib import Path
from typing import Dict, List, Tuple

from tools.platform_utils import (
    which,
    which_python,
    extract_version,
    format_error_message,
    check_version_requirement,
    Version,
)


def check_python_version() -> Tuple[bool, str, Dict]:
    """Check if Python version meets minimum requirements.

    Returns:
        (is_valid, message, details)
    """
    required = (3, 10)
    # Handle both namedtuple (normal) and tuple (when mocked in tests)
    version_info = sys.version_info
    if isinstance(version_info, tuple) and not hasattr(version_info, 'major'):
        major, minor, micro = version_info[0], version_info[1], version_info[2]
    else:
        major, minor, micro = version_info.major, version_info.minor, version_info.micro

    current_version = Version(major, minor, micro)

    details = {
        "required": f"{required[0]}.{required[1]}+",
        "current": str(current_version),
        "platform": platform.system(),
        "implementation": platform.python_implementation(),
    }

    is_valid, message = check_version_requirement(current_version, required, "Python")
    return is_valid, message, details


def check_uv() -> Tuple[bool, str, Dict]:
    """Check if uv/uvx is available.

    Returns:
        (is_valid, message, details)
    """
    uv_path = which("uv", enriched=True)
    uvx_path = which("uvx", enriched=True)

    details = {
        "uv_path": uv_path,
        "uvx_path": uvx_path,
        "required_for": "MCP server execution",
    }

    # Get version if available
    if uvx_path:
        try:
            result = subprocess.run(
                [uvx_path, "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                version = extract_version(result.stdout)
                if version:
                    details["version"] = str(version)
                else:
                    details["version"] = result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    if uvx_path:
        return True, f"✓ uvx found at {uvx_path}", details
    elif uv_path:
        return True, f"✓ uv found at {uv_path} (uvx should be available)", details
    else:
        return False, format_error_message("uv", "not found on PATH"), details


def check_git() -> Tuple[bool, str, Dict]:
    """Check if git is available.

    Returns:
        (is_valid, message, details)
    """
    git_path = which("git", enriched=True)

    details = {
        "git_path": git_path,
        "required_for": "Vault audit trail (jarvis-audit-agent)",
    }

    # Get version if available
    if git_path:
        try:
            result = subprocess.run(
                [git_path, "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                version = extract_version(result.stdout)
                if version:
                    details["version"] = str(version)
                else:
                    details["version"] = result.stdout.strip().replace("git version ", "")
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    if git_path:
        return True, f"✓ git found at {git_path}", details
    else:
        return False, format_error_message("git", "not found on PATH"), details




def check_platform_specific() -> List[Tuple[bool, str, Dict]]:
    """Check platform-specific considerations.

    Returns:
        List of (is_valid, message, details) tuples
    """
    checks = []
    system = platform.system()

    if system == "Windows":
        # Check for Git for Windows specifically
        git_for_windows = Path(r"C:\Program Files\Git\bin\git.exe")
        if git_for_windows.exists():
            checks.append((
                True,
                f"✓ Git for Windows detected",
                {"path": str(git_for_windows), "note": "Recommended for Windows"}
            ))

        # Warn about symlinks
        checks.append((
            True,
            "⚠ Symbolic links require admin privileges on Windows (not critical)",
            {"note": "Some advanced features may be limited"}
        ))

    elif system == "Darwin":
        # Check if Xcode command line tools are installed
        try:
            result = subprocess.run(
                ["xcode-select", "-p"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                checks.append((
                    True,
                    "✓ Xcode Command Line Tools installed",
                    {"path": result.stdout.strip()}
                ))
        except (subprocess.TimeoutExpired, FileNotFoundError):
            checks.append((
                True,
                "○ Xcode Command Line Tools not detected (not critical)",
                {"note": "Install with: xcode-select --install"}
            ))

    return checks


def run_system_check() -> Dict:
    """Run comprehensive system requirements check.

    Returns:
        Dict with:
        - platform: Platform name
        - healthy: Whether all critical requirements are met
        - critical_issues: List of blocking issues
        - warnings: List of non-critical warnings
        - details: Dict of detailed check results
    """
    critical_checks = [
        ("python", check_python_version()),
        ("uv", check_uv()),
        ("git", check_git()),
    ]

    optional_checks = []

    platform_checks = check_platform_specific()

    # Aggregate results
    critical_issues = []
    warnings = []
    details = {
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
        }
    }

    # Process critical checks
    for name, (is_valid, message, check_details) in critical_checks:
        details[name] = check_details
        if not is_valid:
            critical_issues.append(message)

    # Process optional checks
    for name, (is_valid, message, check_details) in optional_checks:
        details[name] = check_details
        if not is_valid and not check_details.get("optional", False):
            warnings.append(message)

    # Process platform-specific checks
    details["platform_specific"] = []
    for is_valid, message, check_details in platform_checks:
        details["platform_specific"].append(check_details)
        if not is_valid:
            warnings.append(message)

    healthy = len(critical_issues) == 0

    return {
        "platform": platform.system(),
        "healthy": healthy,
        "critical_issues": critical_issues,
        "warnings": warnings,
        "details": details,
        "summary": {
            "python": details["python"]["current"],
            "uv": bool(details["uv"]["uvx_path"] or details["uv"]["uv_path"]),
            "git": bool(details["git"]["git_path"]),
        }
    }


def format_check_result(result: Dict, verbose: bool = False) -> str:
    """Format system check result as human-readable text.

    Args:
        result: Result from run_system_check()
        verbose: Include detailed information

    Returns:
        Formatted string
    """
    lines = []

    # Header
    lines.append("=" * 60)
    lines.append(f"Jarvis System Requirements Check - {result['platform']}")
    lines.append("=" * 60)
    lines.append("")

    # Critical requirements
    lines.append("Critical Requirements:")
    for name in ["python", "uv", "git"]:
        if name in result["details"]:
            check_details = result["details"][name]
            if name == "python":
                status = "✓" if check_details["current"] >= check_details["required"] else "✗"
                lines.append(f"  {status} Python {check_details['current']}")
            elif name == "uv":
                if check_details["uvx_path"]:
                    lines.append(f"  ✓ uvx: {check_details['uvx_path']}")
                elif check_details["uv_path"]:
                    lines.append(f"  ✓ uv: {check_details['uv_path']}")
                else:
                    lines.append(f"  ✗ uv/uvx: not found")
            elif name == "git":
                if check_details["git_path"]:
                    version = check_details.get("version", "unknown")
                    lines.append(f"  ✓ git: {check_details['git_path']} ({version})")
                else:
                    lines.append(f"  ✗ git: not found")

    lines.append("")

    # Status
    if result["healthy"]:
        lines.append("Status: ✓ All critical requirements met")
    else:
        lines.append("Status: ✗ Missing critical requirements")

    lines.append("")

    # Issues
    if result["critical_issues"]:
        lines.append("Critical Issues:")
        for issue in result["critical_issues"]:
            lines.append(f"  {issue}")
        lines.append("")

    if result["warnings"]:
        lines.append("Warnings:")
        for warning in result["warnings"]:
            lines.append(f"  {warning}")
        lines.append("")

    # Platform-specific notes
    if result["details"].get("platform_specific"):
        # Only show section if there are actual notes to display
        notes_to_show = [note for note in result["details"]["platform_specific"] if "note" in note]
        if notes_to_show:
            lines.append("Platform Notes:")
            for note in notes_to_show:
                lines.append(f"  • {note['note']}")
            lines.append("")

    # Verbose details
    if verbose:
        lines.append("Detailed Information:")
        lines.append(f"  Platform: {result['details']['platform']['system']} {result['details']['platform']['release']}")
        lines.append(f"  Machine: {result['details']['platform']['machine']}")
        lines.append(f"  Python: {result['details']['python']['implementation']} {result['details']['python']['current']}")
        lines.append("")

    return "\n".join(lines)
