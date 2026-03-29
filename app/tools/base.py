"""Base tool interface for COSMOS tools."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from enum import Enum


class ToolCategory(str, Enum):
    READ = "read"
    ACTION = "action"  # Phase 2


class DataSource(str, Enum):
    MCAPI = "mcapi"
    ELK = "elk"
    PRODDB = "proddb"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class ToolParam:
    name: str
    type: str  # str, int, list
    required: bool
    description: str
    default: Any = None


@dataclass
class ToolDefinition:
    name: str
    category: ToolCategory
    description: str
    parameters: List[ToolParam]
    data_source: DataSource
    allowed_roles: List[str]  # empty = all roles
    risk_level: RiskLevel
    approval_mode: str = "auto"
    rate_limit: int = 60


@dataclass
class ToolResult:
    success: bool
    data: Any = None
    error: Optional[str] = None
    latency_ms: float = 0.0


class BaseTool(ABC):
    """Base class for all COSMOS tools."""

    definition: ToolDefinition

    @abstractmethod
    async def execute(self, params: Dict[str, Any], context: Dict = None) -> ToolResult:
        """Execute the tool with given parameters."""
        pass

    def validate_params(self, params: Dict[str, Any]) -> Optional[str]:
        """Validate required parameters are present. Returns error message or None."""
        for p in self.definition.parameters:
            if p.required and p.name not in params:
                return f"Missing required parameter: {p.name}"
        return None
