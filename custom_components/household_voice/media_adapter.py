"""MA runtime adapter, deliberately unregistered and execution disabled by default.

Contracts verified against home-assistant/core tag 2026.10.1:
homeassistant/components/music_assistant/{services,schemas,media_player}.py
tests/components/music_assistant/snapshots/test_media_player.ambr
https://github.com/home-assistant/core/tree/2026.10.1/homeassistant/components/music_assistant

Inject the pure household_voice.media module through PlannerBindings; this HA
package does not import the separately deployed coordinator. The caller resolver
must use authenticated HA/session context and configured room/profile bindings,
never LLM-provided source IDs, usernames, HA Context, or configuration entry IDs.

HA search has no completeness flag/provider mappings/availability field. A unique
exact match below the requested per-type result limit is accepted as bounded
evidence, never a guarantee of global uniqueness. URI namespace 'library' does
not identify an underlying provider. get_queue has no owner or revision: its
fingerprint is an observation, not CAS. An injected authoritative profile resolver
guards resume; an unverified book queue can resume by a profile-scoped catalog
lookup and replay without seeking. Pause/stop need only trusted target scope.
Locks serialize only this adapter's calls;
other controllers can still race. No installation/registration occurs here.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import re
import time
from typing import Any, Callable


GROUPS = {'artists': 'artist', 'albums': 'album', 'tracks': 'track',
          'playlists': 'playlist', 'radio': 'radio', 'audiobooks': 'audiobook',
          'podcasts': 'podcast'}
ENTITY = re.compile(r'media_player\.[a-z0-9_]+\Z')
URI = re.compile(r'([A-Za-z0-9][A-Za-z0-9_.-]*)://(track|album|artist|playlist|radio|audiobook|podcast)/([^\s?#]+)\Z')


class MediaAdapterError(ValueError):
    """Invalid or insufficient authoritative MA response; never fall back."""


def _text(value, maximum=2048, empty=False):
    if not isinstance(value, str) or len(value)>maximum or (not empty and not value.strip()) or any(ord(c)<32 for c in value):
        raise MediaAdapterError('Malformed Music Assistant text')
    return value


def _kind(value):
    return value.value if hasattr(value, 'value') else value


def _item(raw, expected=None, depth=0):
    if not isinstance(raw, dict) or depth>2:
        raise MediaAdapterError('Malformed catalog item')
    kind = _kind(raw.get('media_type'))
    uri = _text(raw.get('uri'))
    match = URI.fullmatch(uri)
    if not match or match[1].lower() in ('http', 'https', 'file', 'ftp', 'data', 'javascript') or match[2]!=kind or (expected and kind!=expected):
        raise MediaAdapterError('Catalog URI/type mismatch')
    _text(raw.get('version'), empty=True)
    name = _text(raw.get('name'), 512)
    artists = raw.get('artists', [])
    if not isinstance(artists, list) or len(artists)>32:
        raise MediaAdapterError('Malformed artists')
    names = tuple(_item(a, 'artist', depth+1)['title'] for a in artists)
    album = _item(raw['album'], 'album', depth+1)['title'] if raw.get('album') is not None else None
    return dict(uri=uri, title=name, media_type=kind, provider=match[1], artists=names, album=album)


@dataclass(frozen=True)
class PlannerBindings:
    """Pass plan_media and the five named dataclass factories from pure media.py."""
    plan: Callable
    request: Callable
    context: Callable
    item: Callable
    catalog: Callable
    queue: Callable


@dataclass(frozen=True)
class TrustedMediaSession:
    """Produced solely by trusted runtime resolver, not a public tool schema."""
    source_device: str | None
    username: str
    config_entry_id: str
    ha_context: Any
    allowed_players: frozenset[str]

    def __post_init__(self):
        for value in (self.username, self.config_entry_id):
            _text(value, 120)
        if self.source_device is not None:
            _text(self.source_device, 512)
        if self.ha_context is None or not isinstance(self.allowed_players, frozenset) or not self.allowed_players or any(not isinstance(p, str) or not ENTITY.fullmatch(p) for p in self.allowed_players):
            raise MediaAdapterError('Invalid trusted session')


def parse_search(response, lookup, session, bindings, observed_at):
    """Translate grouped response with documented bounded-search confidence.

    The pure planner's complete flag here means no requested group reached this
    query's result limit. It cannot establish global catalog completeness. A
    provider may still omit matches; callers surface evidence_policy with plans.
    """
    if not isinstance(response, dict) or not set(GROUPS).issubset(response):
        raise MediaAdapterError('Incomplete Music Assistant search response')
    items = []
    count = 0
    for group, kind in GROUPS.items():
        rows = response[group]
        if not isinstance(rows, list) or len(rows)>300:
            raise MediaAdapterError('Malformed search group')
        count += len(rows)
        if count>300:
            raise MediaAdapterError('Search result too large')
        for row in rows:
            parsed = _item(row, kind)
            if kind!='podcast':  # The pure planner currently supports six media types.
                items.append(bindings.item(**parsed))
    limit = lookup.get('limit')
    requested = lookup.get('media_type')
    if type(limit) is not int or not 1<=limit<=300 or not isinstance(requested, list) or not requested:
        raise MediaAdapterError('Invalid bounded search parameters')
    bounded = all(len(response[group])<limit for group, kind in GROUPS.items() if kind in requested)
    return bindings.catalog(query=lookup['name'], username=session.username,
                            config_entry_id=session.config_entry_id, observed_at=observed_at,
                            items=tuple(items), complete=bounded)


def parse_queue(response, player, username, bindings, observed_at):
    """Parse entity-keyed get_queue; do not invent profile ownership from response."""
    if not isinstance(response, dict) or set(response)!={player} or not isinstance(response[player], dict):
        raise MediaAdapterError('Wrong Music Assistant queue target')
    q = response[player]
    required = {'queue_id', 'active', 'name', 'items', 'shuffle_enabled', 'repeat_mode',
                'current_index', 'elapsed_time', 'current_item', 'next_item'}
    if not required.issubset(q):
        raise MediaAdapterError('Incomplete queue response')
    _text(q['queue_id']); _text(q['name'])
    if (type(q['items']) is not int or not 0<=q['items']<=1000000
            or type(q['active']) is not bool or type(q['shuffle_enabled']) is not bool
            or q['repeat_mode'] not in ('off', 'one', 'all', 'unknown')
            or (q['current_index'] is not None and (type(q['current_index']) is not int or not 0<=q['current_index']<q['items']))
            or type(q['elapsed_time']) is not int or q['elapsed_time']<0):
        raise MediaAdapterError('Malformed queue fields')
    current = None
    for key in ('current_item', 'next_item'):
        row = q[key]
        if row is not None:
            if not isinstance(row, dict):
                raise MediaAdapterError('Malformed queue item')
            _text(row.get('queue_item_id')); _text(row.get('name'))
            parsed = _item(row['media_item']) if row.get('media_item') is not None else None
            if key=='current_item':
                current = parsed
    if q['items']==0 and (q['current_item'] is not None or q['next_item'] is not None):
        raise MediaAdapterError('Inconsistent empty queue')
    if current is not None and current['media_type']=='podcast':
        raise MediaAdapterError('Podcast queue is not supported')
    # Exclude ticking elapsed_time; include queue/item IDs, count and mode changes.
    stable = {k: q[k] for k in ('queue_id', 'active', 'items', 'current_index', 'shuffle_enabled', 'repeat_mode')}
    stable['current_item_id'] = q['current_item']['queue_item_id'] if q['current_item'] else None
    stable['uri'] = current['uri'] if current else None
    fingerprint = hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()
    return bindings.queue(player=player, username=username, generation=fingerprint,
                          observed_at=observed_at, has_items=q['items']>0,
                          current_uri=current['uri'] if current else None,
                          media_type=current['media_type'] if current else None,
                          spoken_content=bool(current and (current['media_type']=='audiobook' or
                              any(s in (current['title']+' '+(current['album'] or '')).casefold() for s in ('sleep stor', 'bedtime stor', 'calm kids')))))


class MusicAssistantAdapter:
    """Inject hass, pure factories, registry and async trusted context resolvers.

    resolve_session(caller) -> TrustedMediaSession. resolve_queue_profile(session,
    player, queue_id) -> str|None must consult actual runtime ownership evidence.
    It must not simply echo session.username. Missing ownership prevents bare
    resume; books may resolve the same URI for the requested profile instead.
    enable_execution is installation policy, never a per-tool argument.
    """
    def __init__(self, hass, bindings, registry, resolve_session, *,
                 resolve_queue_profile=None, enable_execution=False, clock=time.time):
        if not isinstance(bindings, PlannerBindings) or type(enable_execution) is not bool:
            raise MediaAdapterError('Invalid adapter dependencies')
        self.hass, self.bindings, self.registry = hass, bindings, registry
        self.resolve_session, self.resolve_queue_profile = resolve_session, resolve_queue_profile
        self.enable_execution, self.clock = enable_execution, clock
        self._locks = {}
        self.last_plan = None

    async def _call(self, domain, service, data, session, *, response=False):
        if not self.hass.services.has_service(domain, service):
            raise MediaAdapterError('Required Music Assistant action unavailable')
        return await self.hass.services.async_call(domain, service, data, blocking=True,
                                                   return_response=response, context=session.ha_context)

    async def async_handle(self, request_data, caller):
        """Construct, resolve and optionally execute a fresh plan under a player lock."""
        if not isinstance(request_data, dict):
            raise MediaAdapterError('Media request must be an object')
        try:
            request = self.bindings.request(**request_data)
        except (TypeError, ValueError) as err:
            raise MediaAdapterError('Invalid media request fields') from err
        session = await self.resolve_session(caller)
        if not isinstance(session, TrustedMediaSession):
            raise MediaAdapterError('Trusted session required')
        context = self.bindings.context(config_entry_id=session.config_entry_id,
                                        source_device=session.source_device, username=session.username)
        def plan(**snapshots):
            now = self.clock()
            if type(now) not in (int, float) or not math.isfinite(now):
                raise MediaAdapterError('Invalid clock')
            return self.bindings.plan(request, self.registry, context, now=now, **snapshots)
        initial = plan()
        player = initial.get('player')
        if player is None:
            return self._record(initial)
        if player not in session.allowed_players:
            raise MediaAdapterError('Player is outside trusted caller scope')
        async with self._locks.setdefault(player, asyncio.Lock()):
            result = plan()
            try:
                if result.get('reason')=='fresh_catalog_required':
                    lookup = result['lookup']
                    if lookup.get('action')!='music_assistant.search':
                        raise MediaAdapterError('Invalid planner lookup')
                    data = dict(lookup['data'])
                    if data.get('config_entry_id')!=session.config_entry_id or data.get('username')!=session.username:
                        raise MediaAdapterError('Wrong catalog scope')
                    response = await self._call('music_assistant', 'search', data, session, response=True)
                    result = plan(catalog=parse_search(response, data, session, self.bindings, self.clock()))
                    result['evidence_policy'] = 'bounded_search_below_limit_not_global_uniqueness'
                elif result.get('reason')=='fresh_queue_required':
                    response = await self._call('music_assistant', 'get_queue', {'entity_id': player}, session, response=True)
                    # Validate before passing any queue ID to the trusted profile resolver.
                    snapshot = parse_queue(response, player, session.username, self.bindings, self.clock())
                    if request.operation in ('pause', 'stop'):
                        result = plan(queue=snapshot)
                    else:
                        owner = await self.resolve_queue_profile(session, player, response[player]['queue_id']) if self.resolve_queue_profile else None
                        if owner==session.username and response[player]['active']:
                            result = plan(queue=snapshot)
                        elif snapshot.media_type=='audiobook' and request.kind in ('music', 'book'):
                            # Re-resolve the current book for this authenticated profile;
                            # never inherit another user's bare queue resume position.
                            request = self.bindings.request(query=response[player]['current_item']['media_item']['name'],
                                kind='book', operation='resume', target_room=request.target_room,
                                selected_uri=snapshot.current_uri)
                            lookup = plan()['lookup']['data']
                            found = await self._call('music_assistant', 'search', lookup, session, response=True)
                            result = plan(catalog=parse_search(found, lookup, session, self.bindings, self.clock()))
                            result['evidence_policy'] = 'current_book_uri_revalidated_for_trusted_profile'
                        else:
                            return self._record({'status': 'clarify', 'reason': 'queue_profile_unverified',
                                                 'mode': 'dry_run', 'executed': False, 'actions': []})
                if result.get('status')=='ready' and self.enable_execution:
                    actions = self._validated_actions(result, session, player)
                    if result.get('reason')=='existing_queue_control':
                        latest = await self._call('music_assistant', 'get_queue', {'entity_id': player}, session, response=True)
                        current = parse_queue(latest, player, session.username, self.bindings, self.clock())
                        same = current.generation==result['preconditions']['queue_generation']
                        if request.operation=='resume':
                            owner = await self.resolve_queue_profile(session, player, latest[player]['queue_id']) if self.resolve_queue_profile else None
                            same = same and owner==session.username
                        if not same:
                            return self._record({'status': 'clarify', 'reason': 'queue_changed_before_execution',
                                'mode': 'execution_enabled', 'executed': False, 'actions': []})
                    issued = 0
                    try:
                        for domain, service, data in actions:
                            await self._call(domain, service, data, session)
                            issued += 1
                    except Exception:
                        # No rollback/replay can safely restore progress after a partial playback action.
                        return self._record({'status': 'error', 'reason': 'media_action_failed',
                            'mode': 'execution_enabled', 'executed': False, 'actions_issued': issued,
                            'playback_confirmed': False, 'retry_automatically': False, 'actions': []})
                    result = dict(result, mode='execution_enabled', executed=True,
                                  actions_issued=issued, playback_confirmed=False)
            except MediaAdapterError:
                raise
            except Exception as err:
                raise MediaAdapterError('Music Assistant lookup failed') from err
            return self._record(result)

    def _record(self, result):
        self.last_plan = deepcopy(result)
        return deepcopy(result)

    @staticmethod
    def _validated_actions(result, session, player):
        actions = result.get('actions')
        if not isinstance(actions, list) or not 1<=len(actions)<=4:
            raise MediaAdapterError('Invalid action plan')
        prepared = []
        for action in actions:
            if not isinstance(action, dict) or action.get('target')!={'entity_id': player}:
                raise MediaAdapterError('Wrong action target')
            name, data = action.get('action'), action.get('data')
            if not isinstance(data, dict):
                raise MediaAdapterError('Invalid action data')
            if name=='music_assistant.play_media':
                required = {'media_id', 'media_type', 'username', 'enqueue', 'radio_mode'}
                selected = result.get('selected', {})
                if (set(data)!=required or data['media_id']!=selected.get('uri') or data['media_type']!=selected.get('media_type')
                        or data['username']!=session.username or data['enqueue']!='replace' or type(data['radio_mode']) is not bool):
                    raise MediaAdapterError('Unsafe playback plan')
                match = URI.fullmatch(data['media_id']) if isinstance(data['media_id'], str) else None
                if not match or match[2]!=data['media_type'] or match[1].lower() in ('http', 'https', 'file', 'ftp', 'data', 'javascript'):
                    raise MediaAdapterError('Playback requires a catalog URI')
            elif name=='media_player.shuffle_set':
                if set(data)!={'shuffle'} or type(data['shuffle']) is not bool:
                    raise MediaAdapterError('Invalid shuffle plan')
            elif name=='media_player.repeat_set':
                if data!={'repeat': 'off'}:
                    raise MediaAdapterError('Invalid repeat plan')
            elif name in ('media_player.media_play', 'media_player.media_pause', 'media_player.media_stop'):
                if data:
                    raise MediaAdapterError('Invalid queue control')
            else:
                raise MediaAdapterError('Action is not allowed')
            domain, service = name.split('.')
            prepared.append((domain, service, dict(data, entity_id=player)))
        return prepared
