"""Narrow router-bound category constraints, never a general language parser."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from collections.abc import Mapping
from types import MappingProxyType
from .schedule_language import schedule_edit


class ScopeConflict(ValueError):
    """The selected tool arguments conflict with the trusted current request."""


@dataclass(frozen=True)
class RequestScope:
    family: str
    kind: str
    operations: tuple[str, ...]
    context_id: str
    device_id: str | None
    edit: object | None = None


@dataclass
class RequestEvidence:
    """Request-local, bounded outcomes from trusted tool execution only."""
    context_id: str | None
    device_id: str | None
    user_id: str | None
    _results: list = field(default_factory=list, repr=False)
    _active: bool = field(default=True, repr=False)
    _overflow: bool = field(default=False, repr=False)

    @property
    def media_results(self):
        return tuple(self._results)

    @property
    def media_results_overflow(self):
        return self._overflow


_EVIDENCE = ContextVar('household_voice_execution_evidence', default=None)
_MEDIA_FIELDS = frozenset(('status', 'reason', 'executed', 'playback_confirmed',
    'retry_automatically', 'mode', 'question', 'room', 'actions_issued',
    'candidate_count', 'playback_kind', 'track_count', 'requested_genre',
    'announcement', 'message', 'timeout_phase', 'mutation_dispatch_started',
    'playback_dispatch_started', 'error'))
_CHOICE_FIELDS = frozenset(('title', 'media_type', 'available'))


def _receipt_scalar(value):
    if value is None or type(value) is bool:
        return value
    if type(value) is int and abs(value) <= 1_000_000:
        return value
    if isinstance(value, str) and len(value) <= 2048:
        return value
    raise ValueError('Unsupported receipt value')


def record_media_result(llm_context, operation, result):
    """Record an outcome, never model prose, action arguments or raw exceptions."""
    evidence = _EVIDENCE.get()
    context = getattr(llm_context, 'context', None)
    if (evidence is None or not evidence._active or not evidence.context_id
            or getattr(context, 'id', None) != evidence.context_id
            or getattr(context, 'user_id', None) != evidence.user_id
            or getattr(llm_context, 'device_id', None) != evidence.device_id
            or operation not in ('play', 'resume', 'pause', 'stop')
            or not isinstance(result, Mapping)
            or result.get('status') not in ('action_accepted', 'dry_run', 'clarify', 'unavailable', 'error')):
        return False
    if len(evidence._results) >= 16:
        evidence._overflow = True
        return False
    try:
        outcome = {key: _receipt_scalar(value) for key, value in result.items() if key in _MEDIA_FIELDS}
        def choice(value):
            if not isinstance(value, Mapping):
                raise ValueError('Unsupported receipt choice')
            return MappingProxyType({key: _receipt_scalar(item) for key, item in value.items() if key in _CHOICE_FIELDS})
        if 'selected' in result:
            outcome['selected'] = choice(result['selected'])
        if 'choices' in result:
            if not isinstance(result['choices'], (list, tuple)):
                return False
            outcome['choices'] = tuple(choice(item) for item in result['choices'][:5])
        outcome['operation'] = operation
    except (TypeError, ValueError):
        return False
    evidence._results.append(MappingProxyType(outcome))
    return True


_SCOPE = ContextVar('household_voice_request_scope', default=None)
_ORIGINAL_REQUEST = ContextVar('household_voice_original_request', default=None)
_MEDIA_CONSTRAINT = ContextVar('household_voice_media_constraint', default=None)


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
def bind_request(text, language, context, device_id, *, media_constraints=None):
    """Bind for one awaited fallback call; reset even on failure/cancellation."""
    match = category(text, language)
    context_id = getattr(context, 'id', None)
    edit = schedule_edit(text, language)
    if edit is not None and context_id:
        operations=('stop','dismiss') if edit.operation=='dismiss' else (edit.operation,) if edit.operation else ()
        value = RequestScope('schedules', edit.kind, operations, context_id, device_id, edit)
    else:
        value = RequestScope(*match, context_id, device_id) if match and context_id else None
    evidence = RequestEvidence(context_id, device_id, getattr(context, 'user_id', None))
    # Only trusted router code supplies these literal parsed slots. Freeze them
    # independently from the caller's dictionary before child tasks inherit it.
    constraints = MappingProxyType(dict(media_constraints)) if media_constraints is not None else None
    constraint_token = _MEDIA_CONSTRAINT.set((constraints, context_id, device_id, evidence.user_id)
        if constraints is not None else None)
    evidence_token = _EVIDENCE.set(evidence)
    token = _SCOPE.set(value)
    original_token = _ORIGINAL_REQUEST.set((text, language, context_id, device_id)
        if context_id and isinstance(text, str) and isinstance(language, str) else None)
    try:
        yield evidence
    finally:
        evidence._active = False
        _EVIDENCE.reset(evidence_token)
        _MEDIA_CONSTRAINT.reset(constraint_token)
        _ORIGINAL_REQUEST.reset(original_token)
        _SCOPE.reset(token)


def trusted_request_text(llm_context):
    """Prefer the exact router request; HA 2026.9 LLMContext omits user_prompt."""
    original = _ORIGINAL_REQUEST.get()
    if original is not None:
        text, language, context_id, device_id = original
        if (getattr(getattr(llm_context, 'context', None), 'id', None) != context_id
                or getattr(llm_context, 'device_id', None) != device_id):
            raise ScopeConflict()
        return text, language
    return getattr(llm_context, 'user_prompt', None), getattr(llm_context, 'language', 'en')


def _literal_slot(value):
    return ' '.join(value.casefold().split()).rstrip('.!?') if isinstance(value, str) else None


def _constrain_media_destination(family, operation, arguments, llm_context):
    bound = _MEDIA_CONSTRAINT.get()
    if bound is None:
        return arguments
    expected, context_id, device_id, user_id = bound
    context = getattr(llm_context, 'context', None)
    evidence = _EVIDENCE.get()
    if (evidence is None or not evidence._active
            or not context_id or getattr(context, 'id', None) != context_id
            or getattr(context, 'user_id', None) != user_id
            or getattr(llm_context, 'device_id', None) != device_id
            or family != 'media' or operation != 'play'
            # No user catalog-choice event exists in this ambiguous fallback.
            # selected_uri would bypass the full literal title match in policy.
            or 'selected_uri' in arguments
            or arguments.get('kind', 'music') != 'music'):
        raise ScopeConflict()
    query, target = _literal_slot(expected.get('query')), _literal_slot(expected.get('target_room'))
    if not query or not target:
        raise ScopeConflict()
    values = dict(arguments)
    for key in ('media_type', 'artist', 'album'):
        if key in expected:
            if key in values and _literal_slot(values[key]) != _literal_slot(expected[key]):
                raise ScopeConflict()
            values[key] = expected[key]
    if expected.get('shuffle') is True:
        if 'shuffle' in values and values['shuffle'] is not True:
            raise ScopeConflict()
        values['shuffle'] = True
    actual_query = _literal_slot(values.get('query'))
    if not actual_query and values.get('media_type') == 'album':
        actual_query = _literal_slot(values.get('album'))
    actual_target = _literal_slot(values.get('target_room'))
    if actual_target:
        if actual_target != target or actual_query != query:
            raise ScopeConflict()
    elif actual_query not in (query + ' in ' + target, query + ' in the ' + target):
        raise ScopeConflict()
    return values


def constrain_arguments(family, operation, arguments, llm_context):
    """Supply a missing category or reject conflicts before any side effect."""
    arguments = _constrain_media_destination(family, operation, arguments, llm_context)
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


def trusted_schedule_edit(llm_context):
    """Current original wording, never the model-selected schedule ID.

    The router binds context when delegating. Direct HA LLM APIs also carry the
    original user_prompt; use that conservative guard when available.
    """
    scope=_SCOPE.get()
    if scope is not None:
        if (getattr(getattr(llm_context,'context',None),'id',None)!=scope.context_id
                or getattr(llm_context,'device_id',None)!=scope.device_id):
            raise ScopeConflict()
        if scope.edit is not None:return scope.edit
    return schedule_edit(getattr(llm_context,'user_prompt',None),getattr(llm_context,'language','en'))
