"""Short-lived, source-bound catalog choices for native Assist follow-ups.

The agent interprets the reply, but cannot replace the saved request or supply
an arbitrary URI. Tokens authorize one later request, not an automatic retry.
"""
from collections import OrderedDict
from copy import deepcopy
import secrets


class MediaChoiceError(ValueError):
    """A fresh user clarification is needed; no playback is authorized."""


class MediaChoices:
    def __init__(self, clock, ttl=120, maximum=128):
        self.clock, self.ttl, self.maximum = clock, ttl, maximum
        self.pending = OrderedDict()

    @staticmethod
    def source(context):
        device = getattr(context, 'device_id', None)
        request = getattr(getattr(context, 'context', None), 'id', None)
        user = getattr(getattr(context, 'context', None), 'user_id', None)
        # Some providers expose a conversation ID; HA's native LLMContext may
        # omit it. The opaque token must also be present in the agent's history.
        conversation = getattr(context, 'conversation_id', None)
        if not isinstance(device, str) or not device or not isinstance(request, str) or not request:
            return None
        return device, user, conversation, request

    def prune(self):
        for token, row in list(self.pending.items()):
            if row['expires'] <= self.clock():
                self.pending.pop(token, None)

    def clear(self, context):
        source = self.source(context)
        if source:
            for token, row in list(self.pending.items()):
                if row['source'][:3] == source[:3]:
                    self.pending.pop(token, None)

    def has_pending(self, context):
        self.prune()
        source = self.source(context)
        return source is not None and any(row['source'][:3] == source[:3]
            and row['claimed_by'] is None for row in self.pending.values())

    def issue(self, context, operation, args, choices, signature):
        self.prune()
        source = self.source(context)
        if source is None or not choices or len(choices) > 5:
            return None
        self.clear(context)
        if len(self.pending) >= self.maximum:
            return None
        token = secrets.token_urlsafe(32)
        self.pending[token] = dict(source=source, operation=operation, args=deepcopy(args),
            choices=deepcopy(choices), signature=signature, expires=self.clock() + self.ttl,
            claimed_by=None)
        return token

    def prepare(self, context, operation, supplied, call_key):
        self.prune()
        source = self.source(context)
        token, choice = supplied.get('choice_token'), supplied.get('choice')
        row = self.pending.get(token) if isinstance(token, str) else None
        if (row is None or source is None or source[:3] != row['source'][:3]
                or source[3] == row['source'][3] or operation != row['operation']
                or row['claimed_by'] not in (None, call_key)):
            raise MediaChoiceError()
        if type(choice) is not int or not 1 <= choice <= len(row['choices']):
            raise MediaChoiceError()
        # Room and shuffle may change only through explicit follow-up slots.
        # Content constraints stay server-owned; a new title is a new search.
        if set(supplied) - {'choice_token', 'choice', 'target_room', 'shuffle'}:
            raise MediaChoiceError()
        args = deepcopy(row['args'])
        if 'shuffle' in supplied:
            if (operation != 'play' or args.get('kind', 'music') != 'music'
                    or row['choices'][choice - 1]['media_type'] == 'audiobook'):
                raise MediaChoiceError()
            args['shuffle'] = supplied['shuffle']
        if 'target_room' in supplied:
            args['target_room'] = supplied['target_room']
        selected = deepcopy(row['choices'][choice - 1])
        args['selected_uri'] = selected['uri']
        # A broad search can offer a concrete collection. Selection authorizes
        # that result, with all original type/provider constraints retained.
        if args.get('selection_mode') == 'broad_music':
            args['selection_mode'] = 'exact'
        return args, selected, row

    def claim(self, token, row, call_key):
        self.prune()
        if self.pending.get(token) is not row or row['claimed_by'] is not None:
            raise MediaChoiceError()
        row['claimed_by'] = call_key

    def cancel(self, context, operation, token):
        self.prune()
        source = self.source(context)
        row = self.pending.get(token) if isinstance(token, str) else None
        if (row is None or source is None or source[:3] != row['source'][:3]
                or source[3] == row['source'][3] or operation != row['operation']
                or row['claimed_by'] is not None):
            raise MediaChoiceError()
        self.pending.pop(token)


def describe_choices(choices):
    """Stable numbering with useful catalog qualifiers, never private URIs."""
    offered = []
    for index, choice in enumerate(choices, 1):
        label = choice['title']
        if choice.get('artists'):
            label += ' by ' + ', '.join(choice['artists'])
        if choice.get('album'):
            label += ' from ' + choice['album']
        label += ' (' + choice['media_type'] + ')'
        offered.append(dict(choice, choice=index, label=label))
    # Providers distinguish otherwise identical editions, without treating a
    # provider as a person or allowing the model to silently choose one.
    labels = [choice['label'] for choice in offered]
    for choice in offered:
        if labels.count(choice['label']) > 1:
            choice['label'] += ' via ' + choice['provider']
    question = 'Which would you like: ' + '; '.join(
        f"{choice['choice']}, {choice['label']}" for choice in offered) + '?'
    return offered, question
