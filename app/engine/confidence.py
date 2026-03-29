"""
Confidence scoring for the COSMOS ReAct engine.

Weighted formula:
  0.4 * tool_success_rate
  0.3 * result_completeness
  0.2 * intent_clarity
  0.1 * entity_match
"""


def score_confidence(
    tool_success_rate: float,
    result_completeness: float,
    intent_clarity: float,
    entity_match: float,
) -> float:
    """
    Compute a weighted confidence score in [0.0, 1.0].

    Args:
        tool_success_rate:   Fraction of tool calls that succeeded (0-1).
        result_completeness: How complete the results are (0-1).
        intent_clarity:      How clearly the intent was classified (0-1).
        entity_match:        How well the entity matched the query (0-1).

    Returns:
        Weighted confidence score clamped to [0.0, 1.0].
    """
    raw = (
        0.4 * tool_success_rate
        + 0.3 * result_completeness
        + 0.2 * intent_clarity
        + 0.1 * entity_match
    )
    return max(0.0, min(1.0, raw))
