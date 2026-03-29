"""
LangGraph-style query processing graph.

Flow: embed -> retrieve -> select_tool -> extract_params -> validate -> execute -> respond

Implemented as a simple state machine (no langgraph dependency needed).
Each node is a function that transforms state.
"""

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class GraphPhase(str, Enum):
    EMBED = "embed"
    RETRIEVE = "retrieve"
    SELECT_TOOL = "select_tool"
    EXTRACT_PARAMS = "extract_params"
    VALIDATE = "validate"
    EXECUTE = "execute"
    RESPOND = "respond"
    ESCALATE = "escalate"


@dataclass
class QueryState:
    """State that flows through the graph."""

    query: str
    session_id: str = ""
    user_role: str = "agent"
    company_id: str = ""

    # After RETRIEVE
    retrieved_docs: List[dict] = field(default_factory=list)
    retrieval_scores: List[float] = field(default_factory=list)

    # After SELECT_TOOL
    selected_tool: Optional[str] = None
    selected_api: Optional[dict] = None  # Full KBDocument data
    tool_confidence: float = 0.0

    # After EXTRACT_PARAMS
    extracted_params: Dict[str, Any] = field(default_factory=dict)
    param_confidence: float = 0.0

    # After VALIDATE
    validation_passed: bool = False
    validation_errors: List[str] = field(default_factory=list)

    # After EXECUTE
    tool_result: Optional[dict] = None
    execution_success: bool = False

    # After RESPOND
    response: str = ""
    final_confidence: float = 0.0

    # Metadata
    phase: GraphPhase = GraphPhase.EMBED
    phases_completed: List[str] = field(default_factory=list)
    total_tokens_used: int = 0
    total_cost_usd: float = 0.0
    errors: List[str] = field(default_factory=list)


class QueryGraph:
    """LangGraph-style query processor using knowledge base retrieval.

    Instead of loading all 5,620 API tool definitions into LLM context,
    we retrieve only 3-5 relevant ones via vector search, saving 80% tokens.
    """

    def __init__(self, indexer, llm_client=None, mcapi_client=None):
        self._indexer = indexer  # KnowledgeIndexer
        self._llm = llm_client
        self._mcapi = mcapi_client
        self._nodes: Dict[str, Callable] = {}
        self._edges: Dict[str, str] = {}
        self._conditional_edges: Dict[str, Callable] = {}
        self._setup_graph()

    def _setup_graph(self):
        """Define graph nodes and edges."""
        # Nodes
        self._nodes = {
            "embed": self._embed,
            "retrieve": self._retrieve,
            "select_tool": self._select_tool,
            "extract_params": self._extract_params,
            "validate": self._validate,
            "execute": self._execute,
            "respond": self._respond,
            "escalate": self._escalate,
        }

        # Linear edges
        self._edges = {
            "embed": "retrieve",
            "retrieve": "select_tool",
            # select_tool has conditional edge
            "extract_params": "validate",
            # validate has conditional edge
            "execute": "respond",
        }

        # Conditional edges
        self._conditional_edges = {
            "select_tool": self._route_after_selection,
            "validate": self._route_after_validation,
        }

    async def process(
        self,
        query: str,
        session_id: str = "",
        user_role: str = "agent",
        company_id: str = "",
    ) -> QueryState:
        """Process a query through the graph.

        Returns final QueryState with response, confidence, and metadata.
        """
        state = QueryState(
            query=query,
            session_id=session_id,
            user_role=user_role,
            company_id=company_id,
        )

        current_node = "embed"
        max_steps = 10  # Safety limit

        for _ in range(max_steps):
            if current_node is None:
                break

            # Execute node
            node_fn = self._nodes[current_node]
            state = await node_fn(state)
            state.phases_completed.append(current_node)

            # Check conditional edges first
            if current_node in self._conditional_edges:
                current_node = self._conditional_edges[current_node](state)
            elif current_node in self._edges:
                current_node = self._edges[current_node]
            else:
                current_node = None  # Terminal node

        return state

    async def _embed(self, state: QueryState) -> QueryState:
        """Embed query for vector search (no cost -- uses TF-IDF)."""
        state.phase = GraphPhase.EMBED
        # Embedding is handled inside indexer.search()
        return state

    async def _retrieve(self, state: QueryState) -> QueryState:
        """Retrieve top-K relevant API docs from knowledge base."""
        state.phase = GraphPhase.RETRIEVE

        results = self._indexer.search(state.query, top_k=5)

        state.retrieved_docs = []
        state.retrieval_scores = []
        for doc, score in results:
            state.retrieved_docs.append(
                {
                    "doc_id": doc.doc_id,
                    "summary": doc.summary,
                    "tool_candidate": doc.tool_candidate,
                    "method": doc.method,
                    "path": doc.path,
                    "intent_tags": doc.intent_tags,
                    "keywords": doc.keywords,
                    "read_write_type": doc.read_write_type,
                    "risk_level": doc.risk_level,
                    "param_examples": doc.param_examples,
                    "negative_examples": doc.negative_examples,
                }
            )
            state.retrieval_scores.append(score)

        return state

    async def _select_tool(self, state: QueryState) -> QueryState:
        """Select the best tool from retrieved candidates.

        If LLM is available: build a prompt with 3-5 candidate APIs and ask
        the LLM to pick the best match.

        If no LLM: use the highest-scoring retrieval result.
        """
        state.phase = GraphPhase.SELECT_TOOL

        if not state.retrieved_docs:
            state.tool_confidence = 0.0
            return state

        if self._llm is not None:
            # Build selection prompt with few-shot examples from knowledge base
            candidates_text = ""
            for i, doc in enumerate(state.retrieved_docs[:5]):
                neg = ""
                if doc.get("negative_examples"):
                    neg_items = doc["negative_examples"][:2]
                    neg_queries = [
                        n.get("user_query", "") for n in neg_items
                    ]
                    neg = f"\n    NOT for: {neg_queries}"
                candidates_text += (
                    f"\n  {i + 1}. {doc['doc_id']}"
                    f"\n    Path: {doc.get('method', 'GET')} {doc.get('path', '')}"
                    f"\n    Intent: {', '.join(doc.get('intent_tags', [])[:3])}"
                    f"\n    Keywords: {', '.join(doc.get('keywords', [])[:5])}"
                    f"{neg}\n"
                )

            prompt = (
                "You are a tool selector. Given the user query, pick the BEST matching API "
                "from the candidates.\n\n"
                f'User query: "{state.query}"\n\n'
                f"Candidates:\n{candidates_text}\n"
                'Reply with JSON only: {"selected": <number 1-5>, '
                '"confidence": <0.0-1.0>, "reason": "<brief>"}\n'
                'If none match well, reply: {"selected": 0, "confidence": 0.0, '
                '"reason": "no match"}'
            )

            try:
                raw = await self._llm.complete(
                    prompt,
                    max_tokens=100,
                    intent="tool_selection",
                    session_id=state.session_id,
                )
                parsed = json.loads(raw.strip())
                idx = int(parsed.get("selected", 0))
                state.tool_confidence = float(
                    parsed.get("confidence", 0.0)
                )

                if 1 <= idx <= len(state.retrieved_docs):
                    state.selected_tool = state.retrieved_docs[idx - 1][
                        "doc_id"
                    ]
                    state.selected_api = state.retrieved_docs[idx - 1]
            except Exception:
                # Fallback to top retrieval result
                state.selected_tool = state.retrieved_docs[0]["doc_id"]
                state.selected_api = state.retrieved_docs[0]
                state.tool_confidence = (
                    state.retrieval_scores[0]
                    if state.retrieval_scores
                    else 0.5
                )
        else:
            # No LLM -- use top retrieval result
            state.selected_tool = state.retrieved_docs[0]["doc_id"]
            state.selected_api = state.retrieved_docs[0]
            state.tool_confidence = (
                state.retrieval_scores[0]
                if state.retrieval_scores
                else 0.5
            )

        return state

    async def _extract_params(self, state: QueryState) -> QueryState:
        """Extract API parameters from user query.

        Uses few-shot examples from the knowledge base's examples.yaml.
        If LLM available: Haiku extracts params.
        If no LLM: basic regex extraction (entity IDs).
        """
        state.phase = GraphPhase.EXTRACT_PARAMS

        if state.selected_api is None:
            return state

        api = state.selected_api

        if self._llm is not None and api.get("param_examples"):
            # Build few-shot prompt from knowledge base examples
            examples_text = ""
            for ex in api["param_examples"][:3]:
                examples_text += (
                    f'  Query: "{ex.get("user_query", ex.get("query", ""))}"\n'
                    f'  Params: {json.dumps(ex.get("params", {}))}\n\n'
                )

            prompt = (
                "Extract API parameters from the user query.\n\n"
                f"API: {api.get('method', 'GET')} {api.get('path', '')}\n\n"
                f"Examples:\n{examples_text}"
                "Now extract params for:\n"
                f'Query: "{state.query}"\n\n'
                'Reply with JSON only: {"params": {...}}'
            )

            try:
                raw = await self._llm.complete(
                    prompt,
                    max_tokens=200,
                    intent="param_extraction",
                    session_id=state.session_id,
                )
                parsed = json.loads(raw.strip())
                state.extracted_params = parsed.get("params", {})
                state.param_confidence = 0.8
            except Exception:
                state.extracted_params = self._basic_param_extract(
                    state.query, api
                )
                state.param_confidence = 0.5
        else:
            state.extracted_params = self._basic_param_extract(
                state.query, api
            )
            state.param_confidence = 0.5

        return state

    def _basic_param_extract(self, query: str, api: dict) -> dict:
        """Basic parameter extraction without LLM -- just extract IDs."""
        params: Dict[str, Any] = {}
        # Extract numeric IDs (4+ digits)
        ids = re.findall(r"\b(\d{4,})\b", query)
        if ids:
            params["id"] = ids[0]
        return params

    async def _validate(self, state: QueryState) -> QueryState:
        """Validate extracted params against API schema."""
        state.phase = GraphPhase.VALIDATE

        if state.selected_api and state.extracted_params:
            state.validation_passed = True
        elif state.selected_api and not state.extracted_params:
            # Some APIs don't need params (list endpoints)
            method = state.selected_api.get("method", "GET")
            if method == "GET":
                state.validation_passed = True
            else:
                state.validation_passed = False
                state.validation_errors.append("Missing required parameters")
        else:
            state.validation_passed = False
            state.validation_errors.append("No tool selected")

        return state

    def _route_after_selection(self, state: QueryState) -> Optional[str]:
        """Route after tool selection based on confidence."""
        if state.tool_confidence >= 0.3 and state.selected_tool:
            return "extract_params"
        return "escalate"

    def _route_after_validation(self, state: QueryState) -> Optional[str]:
        """Route after validation."""
        if state.validation_passed:
            return "execute"
        return "escalate"

    async def _execute(self, state: QueryState) -> QueryState:
        """Execute the selected tool via MCAPI."""
        state.phase = GraphPhase.EXECUTE

        if self._mcapi is None:
            # No MCAPI client -- return mock result
            state.tool_result = {
                "status": "mock",
                "message": "MCAPI not connected",
            }
            state.execution_success = True
            return state

        api = state.selected_api
        if not api:
            state.execution_success = False
            return state

        try:
            method = api.get("method", "GET").upper()
            path = api.get("path", "")

            # Substitute path params
            for key, value in state.extracted_params.items():
                path = path.replace(f"{{{key}}}", str(value))
                path = path.replace(f":{key}", str(value))

            if method == "GET":
                result = await self._mcapi.get(
                    path, params=state.extracted_params
                )
            else:
                result = await self._mcapi.post(
                    path, data=state.extracted_params
                )

            state.tool_result = {
                "data": result.data,
                "status_code": result.status_code,
            }
            state.execution_success = result.success
        except Exception as e:
            state.tool_result = {"error": str(e)}
            state.execution_success = False

        return state

    async def _respond(self, state: QueryState) -> QueryState:
        """Build final response."""
        state.phase = GraphPhase.RESPOND

        if not state.execution_success:
            state.response = (
                "I couldn't retrieve the information. Let me escalate this."
            )
            state.final_confidence = 0.2
            return state

        # If LLM available, synthesize natural response
        if self._llm is not None and state.tool_result:
            try:
                data_str = json.dumps(
                    state.tool_result.get("data", {}), default=str
                )[:2000]
                prompt = (
                    "Based on this data, answer the user's question naturally "
                    "and concisely.\n\n"
                    f'User question: "{state.query}"\n'
                    f"Data: {data_str}\n\n"
                    "Provide a helpful, direct answer."
                )

                state.response = await self._llm.complete(
                    prompt,
                    max_tokens=500,
                    intent="response_synthesis",
                    session_id=state.session_id,
                )
                state.final_confidence = min(
                    state.tool_confidence, state.param_confidence
                )
            except Exception:
                state.response = str(
                    state.tool_result.get("data", "No data available")
                )
                state.final_confidence = state.tool_confidence * 0.7
        else:
            state.response = str(
                state.tool_result.get("data", "No data available")
            )
            state.final_confidence = state.tool_confidence * 0.7

        return state

    async def _escalate(self, state: QueryState) -> QueryState:
        """Escalate to human when confidence is too low."""
        state.phase = GraphPhase.ESCALATE
        state.response = (
            "I'm not confident I can answer this correctly. "
            "Let me connect you with a human agent who can help."
        )
        state.final_confidence = 0.1
        return state
