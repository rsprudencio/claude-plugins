"""Tests for JARVIS Protocol formatting and validation."""
import pytest

from protocol import (
    ProtocolTag,
    ProtocolValidator,
    ValidationError,
    format_subject,
    format_commit_message,
    VALID_OPERATIONS,
)


class TestProtocolTag:
    """Test ProtocolTag formatting."""

    def test_to_string_create_conversational(self):
        """Create conversational tag without entry ID."""
        tag = ProtocolTag(operation="create", trigger_mode="conversational")
        assert tag.to_string() == "[JARVIS:Cc]"

    def test_to_string_create_conversational_with_entry_id(self):
        """Create conversational tag with entry ID."""
        tag = ProtocolTag(
            operation="create",
            trigger_mode="conversational",
            entry_id="20260123153045"
        )
        assert tag.to_string() == "[JARVIS:Cc:20260123153045]"

    def test_to_string_edit_agent(self):
        """Edit agent tag without entry ID."""
        tag = ProtocolTag(operation="edit", trigger_mode="agent")
        assert tag.to_string() == "[JARVIS:Ea]"

    def test_to_string_delete_conversational(self):
        """Delete conversational tag."""
        tag = ProtocolTag(operation="delete", trigger_mode="conversational")
        assert tag.to_string() == "[JARVIS:Dc]"

    def test_to_string_move_agent(self):
        """Move agent tag."""
        tag = ProtocolTag(operation="move", trigger_mode="agent")
        assert tag.to_string() == "[JARVIS:Ma]"

    def test_to_string_user_without_entry_id(self):
        """User operation without entry ID (no trigger letter)."""
        tag = ProtocolTag(operation="user", trigger_mode="conversational")
        assert tag.to_string() == "[JARVIS:U]"

    def test_to_string_user_with_entry_id(self):
        """User operation with entry ID (no trigger letter)."""
        tag = ProtocolTag(
            operation="user",
            trigger_mode="conversational",  # Ignored for user ops
            entry_id="20260123153045"
        )
        assert tag.to_string() == "[JARVIS:U:20260123153045]"

    @pytest.mark.parametrize("operation,trigger_mode,expected", [
        ("create", "conversational", "[JARVIS:Cc]"),
        ("create", "agent", "[JARVIS:Ca]"),
        ("edit", "conversational", "[JARVIS:Ec]"),
        ("edit", "agent", "[JARVIS:Ea]"),
        ("delete", "conversational", "[JARVIS:Dc]"),
        ("delete", "agent", "[JARVIS:Da]"),
        ("move", "conversational", "[JARVIS:Mc]"),
        ("move", "agent", "[JARVIS:Ma]"),
    ])
    def test_all_operation_trigger_combinations(self, operation, trigger_mode, expected):
        """Test all valid operation and trigger mode combinations."""
        tag = ProtocolTag(operation=operation, trigger_mode=trigger_mode)
        assert tag.to_string() == expected


class TestProtocolValidator:
    """Test ProtocolValidator validation functions."""

    def test_validate_operation_valid(self):
        """Valid operations should pass."""
        for op in VALID_OPERATIONS:
            assert ProtocolValidator.validate_operation(op) is True

    def test_validate_operation_invalid(self):
        """Invalid operations should fail."""
        assert ProtocolValidator.validate_operation("invalid") is False
        assert ProtocolValidator.validate_operation("") is False
        assert ProtocolValidator.validate_operation("CREATE") is False  # Case sensitive
        assert ProtocolValidator.validate_operation("edit_file") is False

    def test_validate_entry_id_valid(self):
        """Valid 14-digit entry IDs should pass."""
        assert ProtocolValidator.validate_entry_id("20260123153045") is True
        assert ProtocolValidator.validate_entry_id("12345678901234") is True

    def test_validate_entry_id_invalid_length(self):
        """Entry IDs with wrong length should fail."""
        assert ProtocolValidator.validate_entry_id("123") is False
        assert ProtocolValidator.validate_entry_id("123456789012345") is False  # 15 digits
        assert ProtocolValidator.validate_entry_id("2026012315304") is False  # 13 digits

    def test_validate_entry_id_non_numeric(self):
        """Non-numeric entry IDs should fail."""
        assert ProtocolValidator.validate_entry_id("2026012315abcd") is False
        assert ProtocolValidator.validate_entry_id("20260123-15304") is False
        assert ProtocolValidator.validate_entry_id("") is False

    def test_validate_description_valid(self):
        """Non-empty descriptions should pass."""
        assert ProtocolValidator.validate_description("Normal description") is True
        assert ProtocolValidator.validate_description("A") is True
        assert ProtocolValidator.validate_description("  trimmed  ") is True

    def test_validate_description_empty(self):
        """Empty or whitespace-only descriptions should fail."""
        assert ProtocolValidator.validate_description("") is False
        assert ProtocolValidator.validate_description("   ") is False
        assert ProtocolValidator.validate_description("\n\t") is False

    def test_validate_trigger_mode_valid(self):
        """Valid trigger modes should pass."""
        assert ProtocolValidator.validate_trigger_mode("conversational") is True
        assert ProtocolValidator.validate_trigger_mode("agent") is True

    def test_validate_trigger_mode_invalid(self):
        """Invalid trigger modes should fail."""
        assert ProtocolValidator.validate_trigger_mode("invalid") is False
        assert ProtocolValidator.validate_trigger_mode("CONVERSATIONAL") is False
        assert ProtocolValidator.validate_trigger_mode("") is False

    def test_validate_all_returns_empty_dict_when_valid(self):
        """validate_all should return empty dict for valid inputs."""
        errors = ProtocolValidator.validate_all(
            operation="create",
            description="Valid description",
            entry_id="20260123153045",
            trigger_mode="conversational"
        )
        assert errors == {}

    def test_validate_all_returns_errors_dict(self):
        """validate_all should return errors for invalid inputs."""
        errors = ProtocolValidator.validate_all(
            operation="invalid_op",
            description="",
            entry_id="123",
            trigger_mode="invalid_mode"
        )

        assert "operation" in errors
        assert "description" in errors
        assert "entry_id" in errors
        assert "trigger_mode" in errors
        assert "invalid_op" in errors["operation"]
        assert "empty" in errors["description"].lower()
        assert "14 digits" in errors["entry_id"]

    def test_validate_all_without_entry_id(self):
        """validate_all should work without entry_id."""
        errors = ProtocolValidator.validate_all(
            operation="create",
            description="Valid",
            trigger_mode="conversational"
        )
        assert errors == {}


class TestFormatFunctions:
    """Test commit message formatting functions."""

    def test_format_subject_create(self):
        """Create operation formats with 'Jarvis CREATE:'."""
        subject = format_subject("create", "Add new feature")
        assert subject == "Jarvis CREATE: Add new feature"

    def test_format_subject_edit(self):
        """Edit operation formats with 'Jarvis EDIT:'."""
        subject = format_subject("edit", "Update configuration")
        assert subject == "Jarvis EDIT: Update configuration"

    def test_format_subject_delete(self):
        """Delete operation formats with 'Jarvis DELETE:'."""
        subject = format_subject("delete", "Remove old files")
        assert subject == "Jarvis DELETE: Remove old files"

    def test_format_subject_move(self):
        """Move operation formats with 'Jarvis MOVE:'."""
        subject = format_subject("move", "Reorganize structure")
        assert subject == "Jarvis MOVE: Reorganize structure"

    def test_format_subject_user(self):
        """User operation formats with 'User updates:'."""
        subject = format_subject("user", "Manual vault changes")
        assert subject == "User updates: Manual vault changes"

    def test_format_commit_message_structure(self):
        """Commit message should have correct structure."""
        message = format_commit_message("create", "Test description", "[JARVIS:Cc]")

        lines = message.split('\n')
        assert len(lines) == 3
        assert lines[0] == "Jarvis CREATE: Test description"
        assert lines[1] == ""  # Empty line
        assert lines[2] == "[JARVIS:Cc]"

    def test_format_commit_message_with_protocol_tag(self):
        """Full commit message with protocol tag."""
        message = format_commit_message(
            "create",
            "Journal entry",
            "[JARVIS:Cc:20260123153045]"
        )

        assert message.startswith("Jarvis CREATE: Journal entry")
        assert message.endswith("[JARVIS:Cc:20260123153045]")
        assert "\n\n" in message  # Double newline separator

    def test_format_commit_message_user_operation(self):
        """User operation commit message."""
        message = format_commit_message(
            "user",
            "Manual updates to notes",
            "[JARVIS:U]"
        )

        assert message.startswith("User updates: Manual updates to notes")
        assert message.endswith("[JARVIS:U]")
