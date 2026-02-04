"""JARVIS protocol implementation.

Handles validation, formatting, and construction of JARVIS protocol
commit messages and tags.
"""
import re
from typing import Literal, Optional
from dataclasses import dataclass

OperationType = Literal["create", "edit", "delete", "move", "user"]
TriggerMode = Literal["conversational", "agent"]

# Valid operations
VALID_OPERATIONS = {"create", "edit", "delete", "move", "user"}

# Entry ID pattern: 14 digits (YYYYMMDDHHMMSS)
ENTRY_ID_PATTERN = re.compile(r'^[0-9]{14}$')

# Operation to letter mapping
OPERATION_LETTERS = {
    "create": "C",
    "edit": "E",
    "delete": "D",
    "move": "M",
    "user": "U"
}

# Trigger mode to letter mapping
TRIGGER_LETTERS = {
    "conversational": "c",
    "agent": "a"
}


@dataclass
class ProtocolTag:
    """Represents a JARVIS protocol tag."""
    operation: OperationType
    trigger_mode: TriggerMode
    entry_id: Optional[str] = None

    def to_string(self) -> str:
        """Convert to protocol tag string.
        
        Examples:
            [JARVIS:Cc] - Conversational create
            [JARVIS:Da] - Agent delete
            [JARVIS:Cc:20260123153045] - Conversational create with entry ID
        """
        op_letter = OPERATION_LETTERS[self.operation]
        
        # User operations don't have trigger mode
        if self.operation == "user":
            if self.entry_id:
                return f"[JARVIS:{op_letter}:{self.entry_id}]"
            return f"[JARVIS:{op_letter}]"
        
        mode_letter = TRIGGER_LETTERS[self.trigger_mode]

        if self.entry_id:
            return f"[JARVIS:{op_letter}{mode_letter}:{self.entry_id}]"
        return f"[JARVIS:{op_letter}{mode_letter}]"


class ValidationError(Exception):
    """Raised when protocol validation fails."""
    pass


class ProtocolValidator:
    """Validates JARVIS protocol inputs."""

    @classmethod
    def validate_operation(cls, operation: str) -> bool:
        """Validate operation type."""
        return operation in VALID_OPERATIONS

    @classmethod
    def validate_entry_id(cls, entry_id: str) -> bool:
        """Validate entry ID format (14 digits)."""
        return bool(ENTRY_ID_PATTERN.match(entry_id))

    @classmethod
    def validate_description(cls, description: str) -> bool:
        """Validate description is non-empty."""
        return bool(description and description.strip())

    @classmethod
    def validate_trigger_mode(cls, mode: str) -> bool:
        """Validate trigger mode."""
        return mode in ("conversational", "agent")

    @classmethod
    def validate_all(
        cls,
        operation: str,
        description: str,
        entry_id: Optional[str] = None,
        trigger_mode: str = "conversational"
    ) -> dict:
        """Validate all inputs and return errors dict.
        
        Returns:
            Empty dict if valid, otherwise dict with error messages.
        """
        errors = {}
        
        if not cls.validate_operation(operation):
            errors["operation"] = f"Invalid operation '{operation}'. Must be one of: {', '.join(VALID_OPERATIONS)}"
        
        if not cls.validate_description(description):
            errors["description"] = "Description cannot be empty"
        
        if entry_id and not cls.validate_entry_id(entry_id):
            errors["entry_id"] = f"Invalid entry_id format '{entry_id}'. Must be 14 digits (YYYYMMDDHHMMSS)"
        
        if not cls.validate_trigger_mode(trigger_mode):
            errors["trigger_mode"] = f"Invalid trigger_mode '{trigger_mode}'. Must be 'conversational' or 'agent'"
        
        return errors


def format_subject(operation: OperationType, description: str) -> str:
    """Format the commit subject line.
    
    Examples:
        Jarvis CREATE: Add new journal entry
        Jarvis EDIT: Update protocol docs
        User updates: Manual vault reorganization
    """
    if operation == "user":
        return f"User updates: {description}"
    return f"Jarvis {operation.upper()}: {description}"


def format_commit_message(
    operation: OperationType,
    description: str,
    protocol_tag: str
) -> str:
    """Format complete commit message.
    
    Format:
        Line 1: Subject
        Line 2: (empty)
        Line 3: Protocol tag
    """
    subject = format_subject(operation, description)
    return f"{subject}\n\n{protocol_tag}"
