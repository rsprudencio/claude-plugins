"""Unit tests for platform_utils module."""
import os
import pytest
import platform
from pathlib import Path
from unittest.mock import patch, mock_open, MagicMock

from tools.platform_utils import (
    Version,
    detect_os,
    which,
    which_python,
    extract_version,
    get_install_instructions,
    format_error_message,
    check_version_requirement,
    _is_wsl,
    _get_enriched_paths,
)


class TestVersion:
    """Tests for Version dataclass."""

    def test_version_string_formatting(self):
        """Test version string representation."""
        v = Version(3, 11, 6)
        assert str(v) == "3.11.6"

    def test_version_with_prerelease(self):
        """Test version with prerelease."""
        v = Version(1, 0, 0, prerelease="beta.1")
        assert str(v) == "1.0.0-beta.1"

    def test_version_with_build_metadata(self):
        """Test version with build metadata."""
        v = Version(1, 0, 0, build="20260208")
        assert str(v) == "1.0.0+20260208"

    def test_version_with_prerelease_and_build(self):
        """Test version with both prerelease and build."""
        v = Version(1, 0, 0, prerelease="beta.1", build="20260208")
        assert str(v) == "1.0.0-beta.1+20260208"

    def test_version_comparison_equal(self):
        """Test version equality (ignores prerelease/build)."""
        v1 = Version(3, 11, 6)
        v2 = Version(3, 11, 6, prerelease="beta")
        assert v1 == v2

    def test_version_comparison_greater(self):
        """Test version greater than."""
        v1 = Version(3, 12, 0)
        v2 = Version(3, 11, 6)
        assert v1 > v2
        assert not v2 > v1

    def test_version_comparison_greater_equal(self):
        """Test version greater than or equal."""
        v1 = Version(3, 11, 6)
        v2 = Version(3, 11, 6)
        v3 = Version(3, 11, 5)
        assert v1 >= v2
        assert v1 >= v3
        assert not v3 >= v1


class TestOSDetection:
    """Tests for OS detection."""

    @patch('platform.system')
    def test_detect_macos(self, mock_system):
        """Test macOS detection."""
        mock_system.return_value = "Darwin"
        assert detect_os() == "macOS"

    @patch('platform.system')
    def test_detect_windows(self, mock_system):
        """Test Windows detection."""
        mock_system.return_value = "Windows"
        assert detect_os() == "Windows"

    @patch('platform.system')
    @patch('tools.platform_utils._is_wsl')
    def test_detect_linux(self, mock_is_wsl, mock_system):
        """Test Linux detection (not WSL)."""
        mock_system.return_value = "Linux"
        mock_is_wsl.return_value = False
        assert detect_os() == "Linux"

    @patch('platform.system')
    @patch('tools.platform_utils._is_wsl')
    def test_detect_wsl(self, mock_is_wsl, mock_system):
        """Test WSL detection."""
        mock_system.return_value = "Linux"
        mock_is_wsl.return_value = True
        assert detect_os() == "WSL"

    @patch('platform.system')
    def test_detect_unknown(self, mock_system):
        """Test unknown OS."""
        mock_system.return_value = "FreeBSD"
        assert detect_os() == "Unknown"

    @patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu"})
    def test_is_wsl_env_var(self):
        """Test WSL detection via environment variable."""
        assert _is_wsl() == True

    @patch.dict(os.environ, {}, clear=True)
    @patch('builtins.open', mock_open(read_data="Linux version 5.10.16.3-microsoft-standard-WSL2"))
    def test_is_wsl_proc_version(self):
        """Test WSL detection via /proc/version."""
        assert _is_wsl() == True

    @patch.dict(os.environ, {}, clear=True)
    @patch('builtins.open', mock_open(read_data="Linux version 5.15.0-1-amd64"))
    def test_is_not_wsl(self):
        """Test non-WSL Linux."""
        assert _is_wsl() == False


class TestCommandDetection:
    """Tests for command finding."""

    @patch('shutil.which')
    def test_which_found_in_path(self, mock_which):
        """Test command found in standard PATH."""
        mock_which.return_value = "/usr/bin/git"
        result = which("git", enriched=False)
        assert result == "/usr/bin/git"
        mock_which.assert_called_once_with("git")

    @patch('shutil.which')
    def test_which_not_found_no_enrichment(self, mock_which):
        """Test command not found without enrichment."""
        mock_which.return_value = None
        result = which("uv", enriched=False)
        assert result is None

    @patch('shutil.which')
    @patch('pathlib.Path.exists')
    @patch('pathlib.Path.is_file')
    def test_which_found_in_enriched_path(self, mock_is_file, mock_exists, mock_which):
        """Test command found in enriched paths."""
        mock_which.return_value = None
        mock_exists.return_value = True
        mock_is_file.return_value = True

        with patch('tools.platform_utils._get_enriched_paths') as mock_paths:
            mock_paths.return_value = [Path.home() / ".local" / "bin"]
            result = which("uv", enriched=True)
            assert result is not None

    @patch('tools.platform_utils.which')
    def test_which_python_prefers_python3(self, mock_which):
        """Test which_python prefers python3."""
        mock_which.side_effect = lambda cmd, enriched: "/usr/bin/python3" if cmd == "python3" else None
        result = which_python()
        assert result == "/usr/bin/python3"

    @patch('tools.platform_utils.which')
    def test_which_python_fallback_to_python(self, mock_which):
        """Test which_python falls back to python."""
        mock_which.side_effect = lambda cmd, enriched: "/usr/bin/python" if cmd == "python" else None
        result = which_python()
        assert result == "/usr/bin/python"


class TestEnrichedPaths:
    """Tests for enriched PATH generation."""

    @patch('platform.system')
    @patch('pathlib.Path.exists')
    @patch('pathlib.Path.is_dir')
    def test_get_enriched_paths_unix(self, mock_is_dir, mock_exists, mock_system):
        """Test enriched paths on Unix systems."""
        mock_system.return_value = "Linux"
        mock_exists.return_value = True
        mock_is_dir.return_value = True

        paths = _get_enriched_paths()
        path_strings = [str(p) for p in paths]

        # Should include ~/.local/bin and ~/.cargo/bin
        assert any(".local/bin" in p for p in path_strings)
        assert any(".cargo/bin" in p for p in path_strings)

    @patch('platform.system')
    @patch.dict(os.environ, {
        "LOCALAPPDATA": "C:\\Users\\Test\\AppData\\Local",
        "PROGRAMFILES": "C:\\Program Files"
    })
    @patch('pathlib.Path.exists')
    @patch('pathlib.Path.is_dir')
    def test_get_enriched_paths_windows(self, mock_is_dir, mock_exists, mock_system):
        """Test enriched paths on Windows."""
        mock_system.return_value = "Windows"
        mock_exists.return_value = True
        mock_is_dir.return_value = True

        paths = _get_enriched_paths()
        path_strings = [str(p) for p in paths]

        # Should include Windows-specific paths
        assert any("AppData" in p for p in path_strings)
        assert any("Program Files" in p for p in path_strings)


class TestVersionExtraction:
    """Tests for version extraction from strings."""

    def test_extract_python_version(self):
        """Test extracting Python version."""
        result = extract_version("Python 3.11.6")
        assert result == Version(3, 11, 6)

    def test_extract_uv_version_with_metadata(self):
        """Test extracting uv version with build metadata."""
        result = extract_version("uv 0.9.30 (Homebrew 2026-02-04)")
        assert result.major == 0
        assert result.minor == 9
        assert result.patch == 30

    def test_extract_git_version(self):
        """Test extracting git version."""
        result = extract_version("git version 2.39.3 (Apple Git-145)")
        assert result == Version(2, 39, 3)

    def test_extract_version_with_prerelease(self):
        """Test extracting version with prerelease."""
        result = extract_version("1.0.0-beta.1")
        assert result.major == 1
        assert result.minor == 0
        assert result.patch == 0
        assert result.prerelease == "beta.1"

    def test_extract_version_with_build(self):
        """Test extracting version with build metadata."""
        result = extract_version("2.1.0+build.456")
        assert result.major == 2
        assert result.minor == 1
        assert result.patch == 0
        assert result.build == "build.456"

    def test_extract_version_with_prerelease_and_build(self):
        """Test extracting version with both prerelease and build."""
        result = extract_version("1.0.0-rc.1+20260208")
        assert result == Version(1, 0, 0, prerelease="rc.1", build="20260208")

    def test_extract_version_no_patch(self):
        """Test extracting version without patch number."""
        result = extract_version("Python 3.11")
        assert result.major == 3
        assert result.minor == 11
        assert result.patch == 0

    def test_extract_version_not_found(self):
        """Test when no version is found."""
        result = extract_version("No version here")
        assert result is None


class TestInstallInstructions:
    """Tests for installation instructions."""

    def test_get_install_instructions_python(self):
        """Test getting Python install instructions."""
        instructions = get_install_instructions("python")
        assert "macOS" in instructions
        assert "Linux" in instructions
        assert "Windows" in instructions
        assert "WSL" in instructions
        assert "brew install" in instructions["macOS"]
        assert "python.org" in instructions["Windows"]

    def test_get_install_instructions_uv(self):
        """Test getting uv install instructions."""
        instructions = get_install_instructions("uv")
        assert "astral.sh/uv/install.sh" in instructions["Linux"]
        assert "docs.astral.sh" in instructions["Windows"]

    def test_get_install_instructions_git(self):
        """Test getting git install instructions."""
        instructions = get_install_instructions("git")
        assert "xcode-select" in instructions["macOS"]
        assert "apt install git" in instructions["Linux"]
        assert "git-scm.com" in instructions["Windows"]

    def test_get_install_instructions_unknown_tool(self):
        """Test getting instructions for unknown tool."""
        instructions = get_install_instructions("unknown-tool")
        assert "macOS" in instructions
        assert "Install unknown-tool" in instructions["macOS"]

    @patch('tools.platform_utils.detect_os')
    def test_format_error_message_macos(self, mock_detect_os):
        """Test error message formatting on macOS."""
        mock_detect_os.return_value = "macOS"
        message = format_error_message("python", "not found")
        assert "✗ python: not found" in message
        assert "brew install" in message or "python.org" in message

    @patch('tools.platform_utils.detect_os')
    def test_format_error_message_windows(self, mock_detect_os):
        """Test error message formatting on Windows."""
        mock_detect_os.return_value = "Windows"
        message = format_error_message("git", "not found")
        assert "✗ git: not found" in message
        assert "git-scm.com" in message


class TestVersionRequirementCheck:
    """Tests for version requirement checking."""

    def test_check_version_requirement_passes(self):
        """Test version requirement check passes."""
        actual = Version(3, 11, 6)
        required = (3, 10)
        is_valid, message = check_version_requirement(actual, required, "Python")
        assert is_valid == True
        assert "✓ Python 3.11.6" in message

    def test_check_version_requirement_fails(self):
        """Test version requirement check fails."""
        actual = Version(3, 9, 5)
        required = (3, 10)
        is_valid, message = check_version_requirement(actual, required, "Python")
        assert is_valid == False
        assert "✗ Python" in message
        assert "3.9.5" in message
        assert "3.10" in message

    def test_check_version_requirement_exact_match(self):
        """Test version requirement check with exact match."""
        actual = Version(3, 10, 0)
        required = (3, 10)
        is_valid, message = check_version_requirement(actual, required, "Python")
        assert is_valid == True
        assert "✓ Python 3.10.0" in message
