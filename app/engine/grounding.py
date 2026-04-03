"""
Grounding Verifier — Post-generation claim verification.

After Claude generates a response, this module:
1. Extracts factual claims from the response
2. Matches each claim against retrieved evidence chunks
3. Annotates claims as VERIFIED or UNVERIFIED
4. Appends source citations to the response
5. Calculates a grounding score (0-1)

This prevents hallucination by ensuring every factual statement
in the response is backed by evidence from the knowledge base or tools.

Usage:
    verifier = GroundingVerifier()
    result = await verifier.verify(response_text, retrieved_chunks, tool_results)
"""

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()


@dataclass
class VerifiedClaim:
    claim: str
    status: str  # "verified", "unverified", "tool_verified"
    source_id: str = ""
    source_type: str = ""  # "kb_chunk", "tool_result", "none"
    confidence: float = 0.0


@dataclass
class GroundingResult:
    original_response: str
    grounded_response: str  # Response with citations appended
    grounding_score: float  # 0-1, ratio of verified claims
    total_claims: int
    verified_claims: int
    unverified_claims: int
    claims: List[VerifiedClaim] = field(default_factory=list)
    sources_used: List[str] = field(default_factory=list)
    latency_ms: float = 0.0


class GroundingVerifier:
    """Verifies that LLM responses are grounded in retrieved evidence."""

    def __init__(self, model: str = "claude-opus-4-6"):
        self.model = model
        self._cli = None

    def _get_cli(self):
        if self._cli is None:
            from app.engine.claude_cli import ClaudeCLI
            self._cli = ClaudeCLI(model=self.model, timeout_seconds=60)
        return self._cli

    async def verify(
        self,
        response_text: str,
        retrieved_chunks: List[Dict[str, Any]],
        tool_results: Optional[List[Dict[str, Any]]] = None,
    ) -> GroundingResult:
        """Verify all factual claims in the response against evidence."""
        start = time.time()

        if not response_text or len(response_text) < 20:
            return GroundingResult(
                original_response=response_text,
                grounded_response=response_text,
                grounding_score=1.0,
                total_claims=0,
                verified_claims=0,
                unverified_claims=0,
                latency_ms=(time.time() - start) * 1000,
            )

        # Step 1: Extract factual claims
        claims = await self._extract_claims(response_text)
        if not claims:
            return GroundingResult(
                original_response=response_text,
                grounded_response=response_text,
                grounding_score=1.0,
                total_claims=0,
                verified_claims=0,
                unverified_claims=0,
                latency_ms=(time.time() - start) * 1000,
            )

        # Step 2: Verify each claim against evidence
        verified = []
        sources_used = set()

        for claim_text in claims:
            result = self._verify_claim(claim_text, retrieved_chunks, tool_results or [])
            verified.append(result)
            if result.source_id:
                sources_used.add(result.source_id)

        # Step 3: Calculate grounding score
        verified_count = sum(1 for c in verified if c.status in ("verified", "tool_verified"))
        unverified_count = sum(1 for c in verified if c.status == "unverified")
        total = len(verified)
        score = verified_count / max(total, 1)

        # Step 4: Build grounded response with citations
        grounded_response = self._build_grounded_response(
            response_text, verified, retrieved_chunks, score
        )

        return GroundingResult(
            original_response=response_text,
            grounded_response=grounded_response,
            grounding_score=round(score, 2),
            total_claims=total,
            verified_claims=verified_count,
            unverified_claims=unverified_count,
            claims=verified,
            sources_used=sorted(sources_used),
            latency_ms=(time.time() - start) * 1000,
        )

    async def _extract_claims(self, response_text: str) -> List[str]:
        """Extract factual claims from the response using Claude CLI or heuristics."""
        cli = self._get_cli()

        if cli and cli.available:
            try:
                result = await cli.prompt_json(
                    f"""Extract every factual claim from this response. A factual claim is a statement that can be verified against data (status, dates, numbers, names, policies).

Do NOT include: opinions, recommendations, greetings, or questions.

Response:
{response_text[:3000]}

Return ONLY a JSON array of claim strings:
["claim 1", "claim 2", ...]

If no factual claims, return: []"""
                )
                if isinstance(result, list):
                    return result
            except Exception as e:
                logger.debug("grounding.extract_claims_cli_failed", error=str(e))

        # Fallback: heuristic claim extraction
        return self._extract_claims_heuristic(response_text)

    def _extract_claims_heuristic(self, text: str) -> List[str]:
        """Simple heuristic: sentences with numbers, dates, statuses, or entity names."""
        claims = []
        sentences = re.split(r'[.!?\n]', text)

        for sentence in sentences:
            s = sentence.strip()
            if len(s) < 15:
                continue
            # Contains a number, date, status keyword, or looks factual
            if re.search(r'\d+|status|delivered|picked|transit|pending|cancelled|AWB|order|shipment', s, re.I):
                claims.append(s)

        return claims[:15]  # Cap at 15 claims

    def _verify_claim(
        self,
        claim: str,
        chunks: List[Dict],
        tool_results: List[Dict],
    ) -> VerifiedClaim:
        """Verify a single claim against retrieved chunks and tool results."""
        claim_lower = claim.lower()

        # Check against tool results first (highest confidence)
        for tool_result in tool_results:
            tool_data = json.dumps(tool_result).lower() if isinstance(tool_result, dict) else str(tool_result).lower()
            # Extract key terms from claim
            key_terms = [w for w in claim_lower.split() if len(w) > 3 and w.isalnum()]
            matches = sum(1 for t in key_terms if t in tool_data)
            if matches >= 2:
                return VerifiedClaim(
                    claim=claim,
                    status="tool_verified",
                    source_id=tool_result.get("tool_name", "tool"),
                    source_type="tool_result",
                    confidence=min(0.9, 0.5 + matches * 0.1),
                )

        # Check against retrieved KB chunks
        best_match_score = 0
        best_match_id = ""

        for chunk in chunks:
            chunk_text = ""
            if isinstance(chunk, dict):
                chunk_text = chunk.get("content", chunk.get("text", ""))
                if isinstance(chunk_text, dict):
                    chunk_text = json.dumps(chunk_text)
            chunk_lower = chunk_text.lower()

            # Term overlap scoring
            key_terms = [w for w in claim_lower.split() if len(w) > 3 and w.isalnum()]
            if not key_terms:
                continue
            matches = sum(1 for t in key_terms if t in chunk_lower)
            score = matches / len(key_terms) if key_terms else 0

            if score > best_match_score:
                best_match_score = score
                best_match_id = chunk.get("entity_id", chunk.get("id", "")) if isinstance(chunk, dict) else ""

        if best_match_score >= 0.4:
            return VerifiedClaim(
                claim=claim,
                status="verified",
                source_id=best_match_id,
                source_type="kb_chunk",
                confidence=round(best_match_score, 2),
            )

        return VerifiedClaim(
            claim=claim,
            status="unverified",
            source_type="none",
            confidence=0.0,
        )

    def _build_grounded_response(
        self,
        original: str,
        claims: List[VerifiedClaim],
        chunks: List[Dict],
        score: float,
    ) -> str:
        """Append grounding metadata to the response."""
        parts = [original]

        # Add sources section if there are verified claims
        sources = set()
        for c in claims:
            if c.source_id and c.status in ("verified", "tool_verified"):
                sources.add(f"• {c.source_type}: {c.source_id}")

        if sources:
            parts.append("\n\n---\n**Sources:**")
            for s in sorted(sources):
                parts.append(s)

        # Add confidence indicator
        if score >= 0.8:
            parts.append(f"\n*Confidence: HIGH ({score:.0%} claims verified)*")
        elif score >= 0.5:
            parts.append(f"\n*Confidence: MEDIUM ({score:.0%} claims verified)*")
        elif claims:
            parts.append(f"\n*Confidence: LOW ({score:.0%} claims verified) — please verify critical details*")

        return "\n".join(parts)
