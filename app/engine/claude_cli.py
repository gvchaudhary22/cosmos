"""
Claude CLI Client — Calls Claude via the CLI binary instead of Anthropic SDK.

Uses the same `claude` binary that MARS TaskService uses. No ANTHROPIC_API_KEY needed —
the CLI uses its own authentication (OAuth/API key configured during `claude login`).

This enables COSMOS to call Opus 4.6 for enrichment tasks (contextual headers,
synthetic Q&A, business rules, negatives, grounding, spec execution) without
requiring a separate API key.

Usage:
    client = ClaudeCLI()
    response = await client.prompt("Summarize this document", model="claude-opus-4-6")
    print(response)  # "The document describes..."
"""

import asyncio
import json
import os
import shutil
from typing import Optional

import structlog

logger = structlog.get_logger()


class ClaudeCLI:
    """Calls Claude via CLI subprocess — uses CLI auth, no API key needed."""

    def __init__(self, model: str = "claude-opus-4-6", timeout_seconds: int = 120):
        self.model = model
        self.timeout = timeout_seconds
        self._cli_path = self._find_cli()

    def _find_cli(self) -> str:
        """Find the claude CLI binary."""
        # Check env var first
        env_path = os.environ.get("CLAUDE_CLI_PATH")
        if env_path and os.path.exists(env_path):
            return env_path

        # Check PATH
        found = shutil.which("claude")
        if found:
            return found

        # Check common locations
        home = os.path.expanduser("~")
        common = [
            f"{home}/.local/bin/claude",
            "/usr/local/bin/claude",
            f"{home}/.npm-global/bin/claude",
            f"{home}/.claude/local/claude",
        ]
        for p in common:
            if os.path.exists(p):
                return p

        return "claude"  # fallback, hope it's in PATH

    async def prompt(
        self,
        text: str,
        model: Optional[str] = None,
        max_tokens: int = 1000,
        cwd: Optional[str] = None,
    ) -> str:
        """Send a prompt to Claude CLI and return the text response.

        Args:
            text: The prompt text
            model: Model to use (default: claude-opus-4-6)
            max_tokens: Max tokens (not directly supported by CLI, but we set budget)
            cwd: Working directory for the CLI process

        Returns:
            The text response from Claude
        """
        use_model = model or self.model

        args = [
            self._cli_path,
            "-p", text,
            "--model", use_model,
            "--dangerously-skip-permissions",
            "--no-session-persistence",
            "--output-format", "json",
        ]

        # Clean env to avoid nested CLI detection
        env = self._clean_env()

        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd or os.getcwd(),
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.timeout,
            )

            if process.returncode != 0:
                err_msg = stderr.decode("utf-8", errors="replace").strip()
                logger.warning("claude_cli.error",
                               returncode=process.returncode,
                               stderr=err_msg[:200])
                return ""

            # Parse JSON output
            output = stdout.decode("utf-8", errors="replace").strip()
            try:
                result = json.loads(output)
                return result.get("result", output)
            except json.JSONDecodeError:
                # CLI returned plain text
                return output

        except asyncio.TimeoutError:
            logger.warning("claude_cli.timeout", timeout=self.timeout)
            return ""
        except FileNotFoundError:
            logger.error("claude_cli.not_found", path=self._cli_path)
            return ""
        except Exception as e:
            logger.warning("claude_cli.failed", error=str(e))
            return ""

    async def prompt_json(
        self,
        text: str,
        model: Optional[str] = None,
    ) -> Optional[dict]:
        """Send a prompt and parse the response as JSON.

        Returns None if parsing fails.
        """
        response = await self.prompt(text, model=model)
        if not response:
            return None

        # Try to extract JSON from response
        try:
            # Handle markdown code blocks
            if "```" in response:
                parts = response.split("```")
                for part in parts[1:]:
                    clean = part.strip()
                    if clean.startswith("json"):
                        clean = clean[4:].strip()
                    try:
                        return json.loads(clean)
                    except json.JSONDecodeError:
                        continue

            return json.loads(response)
        except json.JSONDecodeError:
            logger.debug("claude_cli.json_parse_failed", response=response[:100])
            return None

    def _clean_env(self) -> dict:
        """Remove Claude Code session vars so CLI doesn't think it's nested."""
        skip_prefixes = [
            "CLAUDECODE=",
            "CLAUDE_CODE_ENTRYPOINT=",
            "CLAUDE_CODE_SESSION=",
            "CLAUDE_CODE_PARENT=",
        ]
        env = {}
        for key, value in os.environ.items():
            full = f"{key}={value}"
            if not any(full.startswith(p) for p in skip_prefixes):
                env[key] = value
        return env

    @property
    def available(self) -> bool:
        """Check if the CLI binary exists."""
        return os.path.exists(self._cli_path) or shutil.which(self._cli_path) is not None
