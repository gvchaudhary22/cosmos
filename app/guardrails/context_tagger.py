"""
Context Tagger — MARS context tagging pattern for trust-level separation.

Wraps data in trust-level XML tags before sending to LLM, ensuring structural
separation between system instructions, verified knowledge, external data,
and untrusted user input.

Reference: mars/docs/prompt-safety.md — "Structural Separation" principle.
"""

import re
from enum import Enum
from typing import Dict, List, Optional


class TrustLevel(str, Enum):
    SYSTEM = "system"        # COSMOS internal — fully trusted
    VERIFIED = "verified"    # Knowledge base, approved docs — high trust
    EXTERNAL = "external"    # MCAPI responses, tool output — medium trust
    UNTRUSTED = "untrusted"  # User input, ICRM payloads — low trust


# Tag name mapping for each trust level
_TAG_MAP: Dict[TrustLevel, str] = {
    TrustLevel.SYSTEM: "system-context",
    TrustLevel.VERIFIED: "verified-data",
    TrustLevel.EXTERNAL: "external-data",
    TrustLevel.UNTRUSTED: "untrusted-input",
}

# Regex to strip any XML-like tags from untrusted content
_XML_TAG_RE = re.compile(r"</?[\w-]+(?:\s+[\w-]+=[\"'][^\"']*[\"'])*\s*/?>")


class ContextTagger:
    """Wraps data in trust-level XML tags before sending to LLM."""

    def tag(self, content: str, level: TrustLevel, source: str) -> str:
        """Wrap content in trust-level tags.

        Example:
            <untrusted-input source="user-chat">user message</untrusted-input>
        """
        tag_name = _TAG_MAP[level]
        # Sanitize untrusted content before wrapping
        safe_content = self.sanitize_untrusted(content) if level == TrustLevel.UNTRUSTED else content
        return f'<{tag_name} source="{source}">{safe_content}</{tag_name}>'

    def tag_tool_result(self, tool_name: str, result: dict) -> str:
        """Tag tool output as external data."""
        # Convert dict to a readable string representation
        result_str = str(result)
        return self.tag(result_str, TrustLevel.EXTERNAL, source=f"tool:{tool_name}")

    def tag_user_input(self, message: str) -> str:
        """Tag user message as untrusted."""
        return self.tag(message, TrustLevel.UNTRUSTED, source="user-chat")

    def tag_knowledge(self, content: str) -> str:
        """Tag KB content as verified."""
        return self.tag(content, TrustLevel.VERIFIED, source="knowledge-base")

    def build_tagged_prompt(
        self,
        system_instructions: str,
        tool_results: List[dict],
        user_message: str,
        knowledge_context: Optional[List[str]] = None,
    ) -> str:
        """Build a complete prompt with proper trust-level tagging.

        Structure:
            <system-context>instructions</system-context>
            <verified-data>knowledge</verified-data>
            <external-data>tool results</external-data>
            <untrusted-input>user message</untrusted-input>
        """
        parts: List[str] = []

        # System instructions (fully trusted)
        parts.append(self.tag(system_instructions, TrustLevel.SYSTEM, source="cosmos-engine"))

        # Knowledge context (verified)
        if knowledge_context:
            for kb in knowledge_context:
                parts.append(self.tag_knowledge(kb))

        # Tool results (external)
        for i, tr in enumerate(tool_results):
            tool_name = tr.get("tool_name", f"tool_{i}")
            parts.append(self.tag_tool_result(tool_name, tr))

        # User message (untrusted)
        parts.append(self.tag_user_input(user_message))

        return "\n\n".join(parts)

    def sanitize_untrusted(self, text: str) -> str:
        """Strip any XML-like tags from untrusted input to prevent tag injection."""
        return _XML_TAG_RE.sub("", text)

    def validate_output(self, response: str, untrusted_inputs: List[str]) -> dict:
        """Check that AI response doesn't echo untrusted content as instructions.

        Returns:
            {
                "safe": bool,
                "issues": List[str]  — descriptions of found issues
            }
        """
        issues: List[str] = []

        # Check if response contains trust-level tags (LLM should not emit them)
        for tag_name in _TAG_MAP.values():
            if f"<{tag_name}" in response or f"</{tag_name}>" in response:
                issues.append(
                    f"Response contains trust-level tag '<{tag_name}>' — "
                    "possible prompt injection echo"
                )

        # Check if response is echoing large chunks of untrusted input verbatim
        for inp in untrusted_inputs:
            # Only flag if the untrusted input is suspiciously long (> 50 chars)
            # and appears verbatim in the response
            if len(inp) > 50 and inp in response:
                issues.append(
                    f"Response echoes untrusted input verbatim ({len(inp)} chars)"
                )

        return {
            "safe": len(issues) == 0,
            "issues": issues,
        }
