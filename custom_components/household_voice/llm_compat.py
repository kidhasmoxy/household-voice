"""Preserve the native HA tool result contract across supported releases."""

def tool_result(llm, *, data, error=False):
    result_type = getattr(llm, 'ToolResult', None)
    if result_type is not None:
        return result_type(data=data, error=error)
    # HA 2026.9 returns JSON objects directly. Keep its native wire shape;
    # retain an explicit error flag as well as the existing status/reason.
    return {**data, 'error': True} if error else data
