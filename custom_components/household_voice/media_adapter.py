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
METADATA_URI = re.compile(r'([A-Za-z0-9][A-Za-z0-9_.-]*)://(artist|album)/(.+)\Z')


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
    # HA may synthesize nested artist/album mappings using a display name as
    # the opaque item ID (including spaces). Only their names become filters;
    # these URIs never become playable catalog items or action arguments.
    match = (METADATA_URI if depth else URI).fullmatch(uri)
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


def _provider_item(raw, kind, provider):
    """Bind a returned item to an available mapping on one authorized instance.

    MA top-tracks resolves provider tracks to library equivalents. Library URIs
    must not escape as an unconstrained playback choice; use the actual mapping
    item ID returned by MA to retain the selected provider and its ranking.
    """
    parsed = _item(raw, kind)
    mappings = raw.get('provider_mappings')
    if not isinstance(mappings, list) or len(mappings) > 32:
        raise MediaAdapterError('Unverified provider mapping')
    available = [mapping for mapping in mappings if isinstance(mapping, dict)
                 and mapping.get('provider_instance') == provider and mapping.get('available') is True]
    if not available:
        return None
    mapping = available[0]
    if parsed['provider'] not in ('library', provider, mapping.get('provider_domain')):
        raise MediaAdapterError('Catalog item crossed provider scope')
    item_id = mapping.get('item_id')
    # For already instance-bound items the URI is sufficient. Library/domain
    # results require the actual available mapping's ID, never a guessed ID.
    if parsed['provider'] != provider:
        uri = f'{provider}://{kind}/{_text(item_id)}'
        if not URI.fullmatch(uri):
            raise MediaAdapterError('Malformed mapped catalog URI')
        parsed.update(uri=uri, provider=provider)
    return parsed


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

    async def _queue_mode(self, player):
        reader = getattr(self.hass.services, 'async_read_queue_mode', None)
        if reader is None:
            return None  # Older injected adapters retain their existing action path.
        value = await reader(player)
        if (not isinstance(value, dict) or type(value.get('is_dynamic')) is not bool
                or type(value.get('shuffle_enabled')) is not bool
                or value.get('repeat_mode') not in ('off', 'one', 'all')):
            raise MediaAdapterError('Unverified queue playback mode')
        _text(value.get('queue_id'), 512)
        return value

    async def _artist_top_tracks(self, result, session):
        """Expand only a profile-authorized provider artist, never a library union.

        MA 2.10.6 artist playback follows a global preference. Its explicit
        top_tracks endpoint does not support user impersonation. The scoped
        search and exact URI verification authorize this provider-specific artist;
        the guarded reader restricts that endpoint to this same provider, and every returned
        playable URI must remain in that namespace.
        """
        selected = result['selected']
        match = URI.fullmatch(selected['uri'])
        reader = getattr(self.hass.services, 'async_read_artist_top_tracks', None)
        if match is None or reader is None:
            return dict(result, status='unavailable', reason='profile_scoped_artist_top_tracks_unavailable', actions=[])
        candidates_reader = getattr(self.hass.services, 'async_read_artist_candidates', None)
        if candidates_reader is not None:
            response, providers = await candidates_reader(selected['title'], match[1])
            if (not isinstance(response, dict) or not isinstance(response.get('artists'), list)
                    or len(response['artists']) > 20 or not isinstance(providers, list)):
                raise MediaAdapterError('Malformed artist provider search')
            candidates = []
            normalize = lambda name: ' '.join(re.findall(r'\w+', name.casefold()))
            for raw in response['artists']:
                original = _item(raw, 'artist')
                if normalize(original['title']) != normalize(selected['title']):
                    continue
                for provider in providers:
                    parsed = _provider_item(raw, 'artist', provider)
                    if parsed is not None:
                        candidates.append((original['provider'] == 'library', parsed))
            if not candidates:
                return dict(result, status='unavailable', reason='profile_scoped_artist_top_tracks_unavailable', actions=[])
            # Same exact artist, authorized provider ranking; never broaden to
            # a similarly named artist after a library artist has been chosen.
            verifier = getattr(self.hass.services, 'async_authorize_provider_uri', None)
            parsed = None
            seen = set()
            # Prefer fresh provider artists over cached library mappings: old
            # library metadata can contain stale provider IDs.
            for _, candidate in sorted(candidates, key=lambda row: row[0]):
                if candidate['uri'] in seen:
                    continue
                seen.add(candidate['uri'])
                if len(seen) > 3:
                    break
                if verifier is not None and await verifier(candidate['uri']) is True:
                    parsed = candidate
                    break
            if parsed is None:
                return dict(result, status='unavailable', reason='profile_scoped_artist_top_tracks_unavailable', actions=[])
            selected = dict(selected, **{key: parsed[key] for key in ('uri', 'title', 'provider')})
            result = deepcopy(result)
            result['selected'] = selected
            match = URI.fullmatch(selected['uri'])
        elif match[1] == 'library':
            return dict(result, status='unavailable', reason='profile_scoped_artist_top_tracks_unavailable', actions=[])
        rows = await reader(selected['uri'])
        if not isinstance(rows, list) or len(rows) > 300:
            raise MediaAdapterError('Malformed artist top tracks')
        tracks = []
        for row in rows:
            if not isinstance(row, dict):
                raise MediaAdapterError('Malformed top track')
            parsed = _provider_item(row, 'track', match[1])
            if parsed is None:
                continue
            if any(term in (parsed['title'] + ' ' + (parsed['album'] or '')).casefold()
                   for term in ('audiobook', 'sleep stor', 'bedtime stor', 'podcast')):
                continue
            if parsed['uri'] not in tracks:
                tracks.append(parsed['uri'])
        tracks = tracks[:25]
        if not tracks:
            return dict(result, status='unavailable', reason='artist_top_tracks_unavailable', actions=[])
        result = deepcopy(result)
        result['top_track_uris'] = tracks
        result['track_count'] = len(tracks)
        result['reason'] = 'artist_top_tracks_selected'
        result['actions'][-1]['data'].update(media_id=tracks, media_type='track', radio_mode=False)
        return result

    async def _genre_fallback(self, result, request, session, context):
        """Prefer a verified seed with background autoplay; otherwise keep station/choice.

        No track becomes a genre seed merely because its title matches a genre.
        All candidates come from a fresh configured-user MA search. Raw genre
        metadata, an available mapping on a similar-tracks provider, and an
        explicit configured-user URI authorization are all required; unknown
        metadata never grants radio mode.
        """
        reader = getattr(self.hass.services, 'async_read_genre_candidates', None)
        if reader is None:
            return result
        try:
            response, supported = await reader(request.query)
        except Exception:
            # This is a read-only enhancement. A provider lookup failure must
            # not discard an already verified continuous station fallback.
            if result.get('playback_kind') == 'continuous_station':
                return result
            return dict(result, status='unavailable', reason='genre_mix_lookup_unavailable', actions=[])
        if (not isinstance(response, dict) or not isinstance(response.get('tracks'), list)
                or len(response['tracks']) > 20 or not isinstance(supported, list)):
            raise MediaAdapterError('Malformed genre seed evidence')
        genre = ' '.join(request.query.casefold().split())
        verifier = getattr(self.hass.services, 'async_authorize_provider_uri', None)
        verified_candidates = set()
        for raw in response['tracks']:
            if not isinstance(raw, dict):
                raise MediaAdapterError('Malformed genre track')
            metadata, mappings = raw.get('metadata'), raw.get('provider_mappings')
            if not isinstance(metadata, dict) or not isinstance(mappings, list) or len(mappings) > 32:
                continue
            genres = metadata.get('genres')
            if (not isinstance(genres, list) or len(genres) > 100 or
                    genre not in {' '.join(value.casefold().split()) for value in genres if isinstance(value, str)}):
                continue
            quality = ' '.join([str(raw.get('name', '')), str(raw.get('version', '')),
                *[str(artist.get('name', '')) for artist in raw.get('artists', []) if isinstance(artist, dict)]]).casefold()
            if re.search(r'\b(?:karaoke|tribute|covers?|soundalike|in the style of)\b', quality):
                continue
            parsed = None
            for provider in supported:
                if request.provider and request.provider.casefold() != provider.casefold():
                    continue
                parsed = _provider_item(raw, 'track', provider)
                if parsed is not None:
                    uri = parsed['uri']
                    if uri in verified_candidates or len(verified_candidates) >= 3:
                        parsed = None
                        continue
                    verified_candidates.add(uri)
                    try:
                        if verifier is not None and await verifier(uri) is True:
                            break
                    except TimeoutError:
                        # No action was issued; do not let an optional seed
                        # authorization timeout discard a verified station.
                        return result
                    parsed = None
            if parsed is None:
                continue
            if any(term in (parsed['title'] + ' ' + (parsed['album'] or '')).casefold()
                   for term in ('audiobook', 'sleep stor', 'bedtime stor', 'podcast')):
                continue
            item = self.bindings.item(**parsed, radio_mode_supported=True)
            seed_request = self.bindings.request(query=item.title, media_type='track',
                target_room=request.target_room, selected_uri=item.uri, radio_mode=False)
            catalog = self.bindings.catalog(query=item.title, username=session.username,
                config_entry_id=session.config_entry_id, observed_at=self.clock(), items=(item,), complete=True)
            planned = self.bindings.plan(seed_request, self.registry, context, now=self.clock(), catalog=catalog)
            if planned.get('status') != 'ready':
                return planned
            return dict(planned, reason='verified_genre_seed_selected', playback_kind='genre_seed_autoplay',
                        autoplay_enabled=True,
                        requested_genre=request.query, evidence_policy='profile_scoped_genre_metadata_and_provider_similar_tracks')
        return result

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
                    result['evidence_policy'] = (
                        'broad_catalog_recommendation_not_exact_title'
                        if request.selection_mode == 'broad_music'
                        else 'bounded_search_below_limit_not_global_uniqueness')
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
                if request.selection_mode == 'broad_music' and (
                        result.get('playback_kind') == 'continuous_station'
                        or result.get('reason') == 'no_relevant_music_collection'):
                    result = await self._genre_fallback(result, request, session, context)
                if result.get('status') == 'ready' and result.get('playback_kind') == 'artist_top_tracks':
                    result = await self._artist_top_tracks(result, session)
                if result.get('status')=='ready' and self.enable_execution:
                    actions = self._validated_actions(result, session, player)
                    dynamic_queue = None
                    replacements = [action for action in actions if action[0:2] == ('music_assistant', 'play_media')]
                    if replacements:
                        mode = await self._queue_mode(player)
                        if mode and mode['is_dynamic']:
                            if result.get('spoken_content') and mode['repeat_mode'] != 'off':
                                return self._record({'status':'unavailable', 'reason':'dynamic_spoken_queue_requires_repeat_off',
                                    'mode':'execution_enabled', 'executed':False, 'actions':[], 'retry_automatically':False})
                            dynamic_queue = mode['queue_id']
                            # MA 2.10.6 rejects standalone mode changes while
                            # dynamic. Its REPLACE _apply_shuffle resets dynamic
                            # shuffle to False before resolving ordered media.
                            # Replace first; then apply finite-queue preferences.
                            # A new radio mix owns shuffle/repeat itself.
                            actions = replacements + ([] if replacements[0][2]['radio_mode'] else
                                [action for action in actions if action not in replacements])
                            result = dict(result, actions=[{'action':domain+'.'+service,
                                'target':{'entity_id':player}, 'data':{key:value for key,value in data.items() if key!='entity_id'}}
                                for domain, service, data in actions])
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
                        autoplay = result.get('autoplay_enabled')
                        autoplay_setter = getattr(self.hass.services, 'async_set_queue_autoplay', None)
                        autoplay_queue = None
                        if autoplay is not None:
                            if type(autoplay) is not bool or autoplay_setter is None:
                                raise MediaAdapterError('Guarded autoplay control unavailable')
                            autoplay_queue = await self._queue_mode(player)
                            if autoplay_queue is None:
                                raise MediaAdapterError('Verified autoplay queue required')
                            if autoplay is False:
                                # Finite replacements must not inherit an old
                                # seed's refill setting. For spoken content this
                                # also prevents automatic next-book playback.
                                changed = await autoplay_setter(player, autoplay_queue, False)
                                issued += int(changed is True)
                        for domain, service, data in actions:
                            if dynamic_queue is not None and (domain, service) != ('music_assistant', 'play_media'):
                                latest_mode = await self._queue_mode(player)
                                if (latest_mode is None or latest_mode['queue_id'] != dynamic_queue
                                        or latest_mode['is_dynamic']):
                                    return self._record({'status':'error', 'reason':'media_queue_changed_after_replacement',
                                        'mode':'execution_enabled', 'executed':False, 'actions_issued':issued,
                                        'playback_confirmed':False, 'retry_automatically':False, 'actions':[]})
                            await self._call(domain, service, data, session)
                            issued += 1
                        if autoplay is True:
                            latest_mode = await self._queue_mode(player)
                            if (latest_mode is None or latest_mode['queue_id'] != autoplay_queue['queue_id']
                                    or latest_mode['is_dynamic']):
                                raise MediaAdapterError('Queue changed before autoplay')
                            # The hook verifies this exact seed still owns the
                            # queue before changing its background refill flag.
                            changed = await autoplay_setter(player, latest_mode, True,
                                expected_source_uri=result['selected']['uri'])
                            issued += int(changed is True)
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
                top_tracks = result.get('top_track_uris')
                expected_id = top_tracks if top_tracks is not None else selected.get('uri')
                expected_type = 'track' if top_tracks is not None else selected.get('media_type')
                if top_tracks is not None and (result.get('playback_kind') != 'artist_top_tracks'
                        or selected.get('media_type') != 'artist' or not isinstance(top_tracks, list)
                        or not 1 <= len(top_tracks) <= 25 or data.get('radio_mode') is not False):
                    raise MediaAdapterError('Invalid artist top track plan')
                if (set(data)!=required or data['media_id']!=expected_id or data['media_type']!=expected_type
                        or data['username']!=session.username or data['enqueue']!='replace' or type(data['radio_mode']) is not bool):
                    raise MediaAdapterError('Unsafe playback plan')
                for uri in (top_tracks if top_tracks is not None else [data['media_id']]):
                    match = URI.fullmatch(uri) if isinstance(uri, str) else None
                    if not match or match[2]!=data['media_type'] or match[1].lower() in ('http', 'https', 'file', 'ftp', 'data', 'javascript'):
                        raise MediaAdapterError('Playback requires a catalog URI')
                    if top_tracks is not None and match[1] != URI.fullmatch(selected['uri'])[1]:
                        raise MediaAdapterError('Top track provider changed')
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
