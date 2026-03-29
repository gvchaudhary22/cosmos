"""Tool registry that manages all available COSMOS tools."""

from typing import Any, Dict, List, Optional

from app.tools.base import BaseTool, ToolDefinition, ToolResult


class ToolRegistry:
    """Registry of all available tools."""

    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}

    def register(self, tool: BaseTool):
        self._tools[tool.definition.name] = tool

    def get(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def list_all(self) -> List[ToolDefinition]:
        return [t.definition for t in self._tools.values()]

    def list_for_role(self, role: str) -> List[ToolDefinition]:
        """Filter tools by user role."""
        result = []
        for tool in self._tools.values():
            if not tool.definition.allowed_roles or role in tool.definition.allowed_roles:
                result.append(tool.definition)
        return result

    async def execute(self, tool_name: str, params: Dict, context: Dict = None) -> ToolResult:
        """Execute a tool by name with validation."""
        tool = self.get(tool_name)
        if not tool:
            return ToolResult(success=False, error=f"Tool not found: {tool_name}")

        error = tool.validate_params(params)
        if error:
            return ToolResult(success=False, error=error)

        return await tool.execute(params, context)
