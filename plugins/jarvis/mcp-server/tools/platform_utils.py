"""Cross-platform utilities for system requirement detection.

Provides OS detection, command finding with PATH enrichment, semantic version
parsing, and platform-specific error messages.
"""
import os
import re
import shutil
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple


@dataclass
class Version:
    """Semantic version with prerelease and build metadata support."""
    major: int
    minor: int
    patch: int
    prerelease: str = ""
    build: str = ""

    def __str__(self) -> str:
        """Format as semantic version string."""
        version = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            version += f"-{self.prerelease}"
        if self.build:
            version += f"+{self.build}"
        return version

    def __ge__(self, other: "Version") -> bool:
        """Compare versions (prerelease/build ignored for comparison)."""
        return (self.major, self.minor, self.patch) >= (other.major, other.minor, other.patch)

    def __gt__(self, other: "Version") -> bool:
        """Compare versions (prerelease/build ignored for comparison)."""
        return (self.major, self.minor, self.patch) > (other.major, other.minor, other.patch)

    def __eq__(self, other: object) -> bool:
        """Compare versions (prerelease/build ignored for comparison)."""
        if not isinstance(other, Version):
            return NotImplemented
        return (self.major, self.minor, self.patch) == (other.major, other.minor, other.patch)


def detect_os() -> Literal["Linux", "macOS", "Windows", "WSL", "Unknown"]:
    """Detect the operating system with WSL support.

    Returns:
        One of: "Linux", "macOS", "Windows", "WSL", "Unknown"

    Detection logic:
        - platform.system() for base OS (Darwin/Linux/Windows)
        - /proc/version or WSL_DISTRO_NAME env var for WSL detection
    """
    system = platform.system()

    if system == "Darwin":
        return "macOS"
    elif system == "Windows":
        return "Windows"
    elif system == "Linux":
        # Check if running under WSL
        if _is_wsl():
            return "WSL"
        return "Linux"
    else:
        return "Unknown"


def _is_wsl() -> bool:
    """Detect if running under Windows Subsystem for Linux."""
    # Check environment variable
    if os.environ.get("WSL_DISTRO_NAME"):
        return True

    # Check /proc/version for "microsoft" or "WSL"
    try:
        with open("/proc/version", "r") as f:
            version_info = f.read().lower()
            return "microsoft" in version_info or "wsl" in version_info
    except (FileNotFoundError, PermissionError):
        return False


def which(cmd: str, enriched: bool = True) -> Optional[str]:
    """Find command in PATH with optional enrichment.

    Args:
        cmd: Command name to find (e.g., "python3", "git", "uv")
        enriched: If True, also search common install locations

    Returns:
        Absolute path to command, or None if not found

    Enriched PATH includes:
        Unix: ~/.local/bin, ~/.cargo/bin
        Windows: %LOCALAPPDATA%\\Programs\\Python, %PROGRAMFILES%\\Git\\cmd
    """
    # Try standard PATH first
    result = shutil.which(cmd)
    if result:
        return result

    if not enriched:
        return None

    # Try enriched locations
    enriched_paths = _get_enriched_paths()
    for path in enriched_paths:
        candidate = path / cmd
        # Windows needs .exe extension
        if platform.system() == "Windows" and not candidate.suffix:
            candidate = candidate.with_suffix(".exe")

        if candidate.exists() and candidate.is_file():
            return str(candidate)

    return None


def which_python() -> Optional[str]:
    """Find Python executable (prefers python3, falls back to python).

    Returns:
        Absolute path to Python, or None if not found
    """
    # Try python3 first (standard on Unix)
    result = which("python3", enriched=True)
    if result:
        return result

    # Fall back to python (standard on Windows)
    return which("python", enriched=True)


def _get_enriched_paths() -> List[Path]:
    """Get list of common tool installation paths for current platform."""
    paths = []
    system = platform.system()
    home = Path.home()

    if system in ("Linux", "Darwin"):
        # Unix-like systems
        paths.extend([
            home / ".local" / "bin",  # uv, pipx, user-installed tools
            home / ".cargo" / "bin",  # Rust tools (uv alternative install)
        ])
    elif system == "Windows":
        # Windows specific paths
        local_appdata = os.environ.get("LOCALAPPDATA")
        program_files = os.environ.get("PROGRAMFILES")

        if local_appdata:
            paths.extend([
                Path(local_appdata) / "Programs" / "Python",
                Path(local_appdata) / "Microsoft" / "WindowsApps",
            ])

        if program_files:
            paths.extend([
                Path(program_files) / "Git" / "cmd",
                Path(program_files) / "Python",
            ])

    # Filter to only existing directories
    return [p for p in paths if p.exists() and p.is_dir()]


def extract_version(version_string: str) -> Optional[Version]:
    """Extract semantic version from arbitrary output.

    Handles various formats:
        - "Python 3.11.6"
        - "uv 0.9.30 (Homebrew 2026-02-04)"
        - "git version 2.39.3 (Apple Git-145)"
        - "1.0.0-beta.1+build.123"

    Args:
        version_string: Output containing version number

    Returns:
        Version object, or None if no version found
    """
    # Regex for semantic version with optional prerelease and build metadata
    # Matches: MAJOR.MINOR.PATCH[-PRERELEASE][+BUILD]
    pattern = r"(\d+)\.(\d+)(?:\.(\d+))?(?:-([a-zA-Z0-9.-]+))?(?:\+([a-zA-Z0-9.-]+))?"

    match = re.search(pattern, version_string)
    if not match:
        return None

    major = int(match.group(1))
    minor = int(match.group(2))
    patch = int(match.group(3)) if match.group(3) else 0
    prerelease = match.group(4) or ""
    build = match.group(5) or ""

    return Version(
        major=major,
        minor=minor,
        patch=patch,
        prerelease=prerelease,
        build=build
    )


def get_install_instructions(tool: str) -> Dict[str, str]:
    """Get platform-specific installation instructions for a tool.

    Args:
        tool: Tool name ("python", "uv", "git", "claude")

    Returns:
        Dict mapping platform names to installation commands
    """
    instructions = {
        "python": {
            "macOS": "brew install python@3.12 (Homebrew) or download from python.org",
            "Linux": "sudo apt install python3 (Debian/Ubuntu) or sudo yum install python3 (RedHat/CentOS)",
            "Windows": "Download from python.org or install from Microsoft Store (search 'Python 3.11')",
            "WSL": "sudo apt install python3 (inside WSL)",
        },
        "uv": {
            "macOS": "curl -LsSf https://astral.sh/uv/install.sh | sh",
            "Linux": "curl -LsSf https://astral.sh/uv/install.sh | sh",
            "Windows": "Download installer from https://docs.astral.sh/uv/getting-started/installation/",
            "WSL": "curl -LsSf https://astral.sh/uv/install.sh | sh (inside WSL)",
        },
        "git": {
            "macOS": "xcode-select --install (Command Line Tools) or brew install git",
            "Linux": "sudo apt install git (Debian/Ubuntu) or sudo yum install git (RedHat/CentOS)",
            "Windows": "Download Git for Windows from https://git-scm.com/download/win",
            "WSL": "sudo apt install git (inside WSL)",
        },
        "claude": {
            "macOS": "npm install -g @anthropic-ai/claude-cli or download from claude.ai",
            "Linux": "npm install -g @anthropic-ai/claude-cli",
            "Windows": "npm install -g @anthropic-ai/claude-cli (requires Node.js)",
            "WSL": "npm install -g @anthropic-ai/claude-cli (inside WSL)",
        },
    }

    return instructions.get(tool, {
        "macOS": f"Install {tool} for macOS",
        "Linux": f"Install {tool} for Linux",
        "Windows": f"Install {tool} for Windows",
        "WSL": f"Install {tool} for WSL",
    })


def format_error_message(tool: str, issue: str) -> str:
    """Format error message with platform-specific install instructions.

    Args:
        tool: Tool name ("python", "uv", "git", "claude")
        issue: Specific issue (e.g., "not found", "version too old")

    Returns:
        Formatted error message with installation instructions
    """
    current_os = detect_os()
    instructions = get_install_instructions(tool)

    install_msg = instructions.get(current_os, instructions.get("Linux", "See installation docs"))

    return f"✗ {tool}: {issue}\n   Install: {install_msg}"


def check_version_requirement(
    actual: Version,
    required: Tuple[int, int],
    tool_name: str
) -> Tuple[bool, str]:
    """Check if version meets minimum requirement.

    Args:
        actual: Actual version detected
        required: Minimum required version as (major, minor) tuple
        tool_name: Tool name for error messages

    Returns:
        (is_valid, message) tuple
    """
    required_version = Version(required[0], required[1], 0)

    if actual >= required_version:
        return True, f"✓ {tool_name} {actual}"
    else:
        return False, format_error_message(
            tool_name,
            f"version {actual} found (requires {required[0]}.{required[1]}+)"
        )
