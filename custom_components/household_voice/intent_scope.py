"""Narrow router-bound category constraints, never a general language parser."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


class ScopeConflict(ValueError):
    """The selected tool arguments conflict with the trusted current request."""


@dataclass(frozen=True)
class RequestScope:
    family: str
    kind: str
    operations: tuple[str, ...]
    context_id: str
    device_id: str | None


_SCOPE = ContextVar('household_voice_request_scope', default=None)


def category(text, language):
    """Match only complete, simple English commands; quoted bodies never match."""
    if not isinstance(text, str) or not isinstance(language, str) or language.split('-')[0].lower() != 'en':
        return None
    text = ' '.join(text.casefold().strip().split()).rstrip('.!?')
    if text.startswith('please '):
        text = text[7:]
    if text.endswith(' please'):
        text = text[:-7]
    for kind in ('alarm', 'timer'):
        if text in {f'{verb} {owner}{ringing}{kind}'
                    for verb in ('stop', 'dismiss', 'silence')
                    for owner in ('', 'the ', 'my ')
                    for ringing in ('', 'ringing ')}:
            return 'schedules', kind, ('stop', 'dismiss')
    if text in {f'{verb} {owner}{noun}'
                for verb in ('resume', 'continue')
                for owner in ('', 'the ', 'my ')
                for noun in ('book', 'audiobook', 'audio book')}:
        return 'media', 'book', ('resume',)
    return None


@contextmanager
def bind_request(text, language, context, device_id):
    """Bind for one awaited fallback call; reset even on failure/cancellation."""
    match = category(text, language)
    context_id = getattr(context, 'id', None)
    value = RequestScope(*match, context_id, device_id) if match and context_id else None
    token = _SCOPE.set(value)
    try:
        yield
    finally:
        _SCOPE.reset(token)


def constrain_arguments(family, operation, arguments, llm_context):
    """Supply a missing category or reject conflicts before any side effect."""
    scope = _SCOPE.get()
    if scope is None:
        return arguments
    if (getattr(getattr(llm_context, 'context', None), 'id', None) != scope.context_id
            or getattr(llm_context, 'device_id', None) != scope.device_id):
        raise ScopeConflict()
    if family != scope.family or operation not in (*scope.operations, 'list'):
        raise ScopeConflict()
    if arguments.get('kind') not in (None, scope.kind):
        raise ScopeConflict()
    return {**arguments, 'kind': scope.kind}
