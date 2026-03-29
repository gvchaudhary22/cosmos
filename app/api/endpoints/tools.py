"""
Tool management endpoints.

Provides:
  GET /tools              — List all registered tools
  GET /tools/{name}/schema — Get parameter schema for a tool
"""

from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter()


class ToolParamResponse(BaseModel):
    name: str
    type: str
    required: bool
    description: str
    default: Optional[str] = None


class ToolResponse(BaseModel):
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    requires_approval: bool = False
    risk_level: str = "low"
    data_source: Optional[str] = None
    parameters: list[ToolParamResponse] = Field(default_factory=list)


class ToolListResponse(BaseModel):
    tools: list[ToolResponse] = Field(default_factory=list)
    total: int = 0


class ToolSchemaResponse(BaseModel):
    name: str
    category: Optional[str] = None
    description: Optional[str] = None
    parameters: list[ToolParamResponse] = Field(default_factory=list)
    risk_level: str = "low"
    approval_mode: str = "auto"
    rate_limit: int = 60


def _get_tool_registry(request: Request):
    """Get the tool registry from the ReAct engine on app.state."""
    engine = getattr(request.app.state, "react_engine", None)
    if engine is not None:
        return getattr(engine, "tool_registry", None)
    return None


def _definition_to_response(defn) -> ToolResponse:
    """Convert a ToolDefinition to a ToolResponse."""
    params = [
        ToolParamResponse(
            name=p.name,
            type=p.type,
            required=p.required,
            description=p.description,
            default=str(p.default) if p.default is not None else None,
        )
        for p in defn.parameters
    ]
    return ToolResponse(
        name=defn.name,
        display_name=defn.name.replace("_", " ").title(),
        description=defn.description,
        category=defn.category.value if hasattr(defn.category, "value") else str(defn.category),
        requires_approval=defn.approval_mode != "auto",
        risk_level=defn.risk_level.value if hasattr(defn.risk_level, "value") else str(defn.risk_level),
        data_source=defn.data_source.value if hasattr(defn.data_source, "value") else str(defn.data_source),
        parameters=params,
    )


@router.get("", response_model=ToolListResponse)
async def list_tools(request: Request, category: Optional[str] = None):
    """List all registered tools."""
    registry = _get_tool_registry(request)
    if registry is None:
        return ToolListResponse(tools=[], total=0)

    definitions = registry.list_all()

    if category:
        definitions = [
            d for d in definitions
            if (d.category.value if hasattr(d.category, "value") else str(d.category)) == category
        ]

    tools = [_definition_to_response(d) for d in definitions]
    return ToolListResponse(tools=tools, total=len(tools))


@router.get("/{name}/schema", response_model=ToolSchemaResponse)
async def get_tool_schema(request: Request, name: str):
    """Get the parameter schema for a specific tool."""
    registry = _get_tool_registry(request)
    if registry is None:
        raise HTTPException(status_code=503, detail="Tool registry not initialized")

    tool = registry.get(name)
    if tool is None:
        raise HTTPException(status_code=404, detail=f"Tool not found: {name}")

    defn = tool.definition
    params = [
        ToolParamResponse(
            name=p.name,
            type=p.type,
            required=p.required,
            description=p.description,
            default=str(p.default) if p.default is not None else None,
        )
        for p in defn.parameters
    ]

    return ToolSchemaResponse(
        name=defn.name,
        category=defn.category.value if hasattr(defn.category, "value") else str(defn.category),
        description=defn.description,
        parameters=params,
        risk_level=defn.risk_level.value if hasattr(defn.risk_level, "value") else str(defn.risk_level),
        approval_mode=defn.approval_mode,
        rate_limit=defn.rate_limit,
    )
