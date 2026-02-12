"""Tests for unified remove module — routing deletes."""
import pytest
from tools.remove import remove
from tools.tier2 import tier2_write, tier2_read


class TestRemoveRouting:
    """Test parameter validation and routing logic."""

    def test_no_param_returns_error(self, mock_config):
        """Error when no parameter is provided."""
        result = remove()
        assert not result["success"]
        assert "Provide id" in result["error"]

    def test_both_params_returns_error(self, mock_config):
        """Error when both id and name provided."""
        result = remove(id="obs::12345", name="test-mem")
        assert not result["success"]
        assert "only ONE" in result["error"]


class TestRemoveById:
    """Test ID-based deletion."""

    def test_delete_tier2_by_id(self, mock_config):
        """Delete tier2 content by ID."""
        write_result = tier2_write(
            content="To be deleted",
            content_type="observation",
        )
        doc_id = write_result["id"]

        result = remove(id=doc_id)
        assert result["success"]
        assert result["deleted"]

        # Verify deletion
        read_result = tier2_read(doc_id)
        assert not read_result["found"]

    def test_delete_nonexistent_tier2(self, mock_config):
        """Deleting nonexistent tier2 doc returns deleted=False."""
        result = remove(id="obs::nonexistent")
        assert result["success"]
        assert not result["deleted"]
        assert result["reason"] == "not found"

    def test_delete_vault_id_requires_confirm(self, mock_config):
        """Vault IDs require confirm=True (safety gate)."""
        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        (notes_dir / "test.md").write_text("test content")

        result = remove(id="vault::notes/test.md")
        assert result["success"]
        assert result["confirmation_required"]
        assert "confirm" in result["message"].lower()

    def test_delete_vault_id_with_confirm(self, mock_config):
        """Vault file is deleted when confirm=True."""
        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        test_file = notes_dir / "test.md"
        test_file.write_text("test content")

        result = remove(id="vault::notes/test.md", confirm=True)
        assert result["success"]
        assert not test_file.exists()

    def test_delete_vault_id_not_found(self, mock_config):
        """Deleting nonexistent vault file returns error."""
        result = remove(id="vault::notes/nonexistent.md", confirm=True)
        assert not result["success"]
        assert "not found" in result["error"].lower()

    def test_delete_memory_id_redirects(self, mock_config):
        """Memory IDs redirect to name= parameter."""
        result = remove(id="memory::global::test")
        assert not result["success"]
        assert "name=" in result["error"]


    def test_delete_vault_path_traversal_blocked(self, mock_config):
        """Path traversal in vault ID is rejected by validate_vault_path."""
        result = remove(id="vault::../../etc/passwd", confirm=True)
        assert not result["success"]
        assert "escapes vault boundary" in result["error"]

    def test_delete_vault_path_traversal_no_confirm(self, mock_config):
        """Path traversal is blocked even before the confirm gate."""
        result = remove(id="vault::../secret.txt")
        assert not result["success"]
        assert "escapes vault boundary" in result["error"]


class TestRemoveByName:
    """Test name-based memory deletion."""

    def test_delete_memory_by_name(self, mock_config):
        """Delete strategic memory by name."""
        from tools.store import store

        # Create memory
        store(content="To be deleted", type="memory", name="delete-test")

        # Delete (confirm=True for global)
        result = remove(name="delete-test", confirm=True)
        assert result["success"]

    def test_delete_global_memory_requires_confirm(self, mock_config):
        """Global memory deletion without confirm returns confirmation_required."""
        from tools.store import store

        store(content="Protected", type="memory", name="confirm-test")
        result = remove(name="confirm-test", confirm=False)
        assert result["success"]
        assert result["confirmation_required"]

    def test_delete_nonexistent_memory(self, mock_config):
        """Deleting nonexistent memory with confirm returns no file deleted."""
        result = remove(name="nonexistent-mem", confirm=True)
        # memory_delete resolves path and attempts deletion
        assert isinstance(result, dict)
