"""Preserve the native HA tool result contract across supported releases."""

import hashlib


def api_name(family, entry_id):
    """Bound HA MergedAPI namespaces while keeping config-entry API IDs stable."""
    if family not in {'schedules', 'media', 'broadcasts', 'research'}:
        raise ValueError('Unknown Household Voice API family')
    suffix = hashlib.sha256(entry_id.encode('utf-8')).hexdigest()[:12]
    return f'Household {family} {suffix}'


def tool_result(llm, *, data, error=False):
    result_type = getattr(llm, 'ToolResult', None)
    if result_type is not None:
        return result_type(data=data, error=error)
    # HA 2026.9 returns JSON objects directly. Keep its native wire shape;
    # retain an explicit error flag as well as the existing status/reason.
    return {**data, 'error': True} if error else data
