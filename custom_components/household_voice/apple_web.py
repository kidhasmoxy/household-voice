"""Optional Apple web discovery. Search text never becomes playback authority.

Only strict Apple share-link IDs enter MA's profile authorization and metadata
lookup. The ordinary planner still selects the title/version and enforces filters.
"""
import asyncio
import re
from urllib.parse import parse_qs, urlsplit


def apple_link(value):
    """Return (canonical share URL, MA URI, type), or reject unsupported links."""
    if (not isinstance(value, str) or len(value) > 2048 or '\\' in value
            or any(ord(c) < 33 for c in value)):
        raise ValueError('invalid_apple_link')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or parsed.netloc != 'music.apple.com'
            or parsed.fragment):
        raise ValueError('invalid_apple_link')
    match = re.fullmatch(r'/([a-z]{2})/(album|song|playlist|artist)/(?:[^/]+/)?([^/]+)/?', parsed.path)
    if not match:
        raise ValueError('unsupported_apple_link')
    country, kind, identifier = match.groups()
    album_id = identifier if kind == 'album' else None
    query = parse_qs(parsed.query, keep_blank_values=True)
    if 'i' in query:
        if kind != 'album' or len(query['i']) != 1:
            raise ValueError('invalid_apple_track')
        kind, identifier = 'song', query['i'][0]
    if not re.fullmatch(r'pl\.[A-Za-z0-9-]{1,160}' if kind == 'playlist' else r'[0-9]{1,30}', identifier):
        raise ValueError('invalid_apple_id')
    media_type = 'track' if kind == 'song' else kind
    # Sonos's share-link parser requires a slug, even for Apple's short URLs.
    slug = parsed.path.rstrip('/').split('/')[-2] if len(parsed.path.rstrip('/').split('/')) == 5 else 'item'
    link = (f'https://music.apple.com/{country}/album/{slug}/{album_id}?i={identifier}'
            if album_id and kind == 'song' else
            f'https://music.apple.com/{country}/{kind}/{slug}/{identifier}')
    return (link,
            f'apple_music://{media_type}/{identifier}', media_type)


def native_track_link(raw, uri):
    """Use an exact available catalog mapping's album-form share link.

    Older Sonos share-link parsers reject /song/ URLs. Never turn an arbitrary
    metadata URL into a playback target or replace a requested track by its album.
    """
    for mapping in raw.get('provider_mappings', []):
        if (not isinstance(mapping, dict) or mapping.get('available') is not True
                or mapping.get('provider_domain') != 'apple_music'
                or f"{mapping.get('provider_instance')}://track/{mapping.get('item_id')}" != uri):
            continue
        try:
            link, mapped, kind = apple_link(mapping.get('url'))
        except (ValueError, TypeError):
            continue
        if kind == 'track' and mapped.rsplit('/', 1)[1] == uri.rsplit('/', 1)[1] and '/album/' in link:
            return link
    return None


async def discover(data, *, search, authorize, lookup):
    """Bounded discovery with canonical catalog metadata, never snippet matching."""
    groups = {'track':'tracks', 'album':'albums', 'artist':'artists', 'playlist':'playlists'}
    result = {group: [] for group in (*groups.values(), 'radio', 'audiobooks', 'podcasts')}
    links, seen = {}, set()
    terms = ' '.join(str(data[key]) for key in ('name', 'artist', 'album') if data.get(key))
    response = await search(terms + ' Apple Music')
    if response.get('status') != 'ready':
        return None, {}
    candidates = []
    for source in response.get('sources', [])[:5]:
        try:
            link, uri, kind = apple_link(source.get('url'))
        except (ValueError, TypeError):
            continue
        if kind not in data['media_type'] or uri in seen:
            continue
        seen.add(uri)
        candidates.append((link, uri, kind))
    # These are independent read-only candidates. Preserve source ranking and
    # ambiguity evidence, while bounding provider concurrency instead of waiting
    # for every candidate's authorization/metadata round trips in sequence.
    gate = asyncio.Semaphore(3)
    async def resolve(candidate):
        async with gate:
            return await resolve_one(candidate)
    async def resolve_one(candidate):
        link, uri, kind = candidate
        if not await authorize(uri):
            return None
        raw = await lookup(uri)
        # MA can resolve to library IDs. Require its exact available Apple mapping
        # rather than accepting an unrelated object returned for the lookup.
        identifier = uri.rsplit('/', 1)[1]
        mappings = raw.get('provider_mappings', []) if isinstance(raw, dict) else []
        matches = [m for m in mappings if isinstance(m, dict)
                   and m.get('provider_domain') == 'apple_music'
                   and str(m.get('item_id')) == identifier and m.get('available') is True]
        if (not isinstance(raw, dict) or raw.get('media_type') != kind
                or raw.get('available') is False or not matches):
            return None
        instance = matches[0].get('provider_instance')
        if not isinstance(instance, str) or not re.fullmatch(r'apple_music(?:--[A-Za-z0-9_-]+)?', instance):
            return None
        bound = f'{instance}://{kind}/{identifier}'
        if not await authorize(bound):
            return None
        raw = dict(raw, uri=bound)
        return kind, bound, raw, (native_track_link(raw, bound) or link) if kind == 'track' else link
    resolved = await asyncio.gather(*(resolve(candidate) for candidate in candidates), return_exceptions=True)
    for value in resolved:
        if isinstance(value, BaseException):
            raise value
        if value is None:
            continue
        kind, bound, raw, link = value
        result[groups[kind]].append(raw)
        links[bound] = link
    return (result if links else None), links


async def search_web(hass, entry, query):
    """Reuse existing Brave credentials, budget ledger, cache and backup worker."""
    from .knowledge_llm import _brave_key, _storage_worker
    from .knowledge_policy import validate_knowledge_settings
    from ._core.knowledge_search import KnowledgeSearch
    settings = validate_knowledge_settings(entry.options)
    key = _brave_key(hass, entry, settings)
    if not key:
        return {'status':'unavailable', 'reason':'brave_api_key_not_configured'}
    def retrieve(text):
        engine = KnowledgeSearch(hass.config.path('.storage', 'household_voice', 'knowledge.sqlite3'),
            enabled=True, api_key=key, allowed_domains=['music.apple.com'],
            daily_limit=settings['knowledge_daily_limit'], monthly_limit=settings['knowledge_monthly_limit'])
        return engine.search(text)
    return await _storage_worker(hass, retrieve, query)


async def try_native(guard, plan, request, *, resolve, entity_registry, configuration_error):
    """Experimental direct path only with an empty MA queue and native Sonos.

    Keep filtered collections, shuffled, artist and endless requests on MA.
    Never retry through MA after a native dispatch may have started.
    """
    selected = plan.get('selected', {})
    link = getattr(guard, 'apple_links', {}).get(selected.get('uri'))
    options = guard.api.entry.options
    if (not link or options.get('apple_web_native_enabled') is not True
            or (options.get('block_explicit_tracks') is True and selected.get('media_type') != 'track') or request.shuffle
            or request.kind != 'music' or request.operation != 'play'
            or selected.get('media_type') not in ('track', 'album', 'playlist')
            or plan.get('autoplay_enabled') is True):
        return None
    from .area_registry import collect_pairings
    def target():
        _, row, signature, enabled = resolve(guard.api.hass, guard.api.entry, guard.context, guard.target)
        if signature != guard.signature or not enabled:
            raise configuration_error()
        output = collect_pairings(guard.api.hass, options)['outputs'].get(row['music_player'], {})
        candidates = []
        for entity_id in output.get('physical_aliases', []):
            entity = entity_registry.async_get(guard.api.hass).async_get(entity_id)
            state = guard.api.hass.states.get(entity_id)
            if (entity is not None and entity.platform == 'sonos' and entity.disabled_by is None
                    and state is not None and state.state in ('idle', 'paused')
                    and isinstance(state.attributes.get('group_members'), list)
                    and set(state.attributes['group_members']) <= {entity_id}
                    and state.attributes.get('shuffle') is False
                    and state.attributes.get('repeat') == 'off'):
                candidates.append(entity_id)
        return row['music_player'], candidates[0] if len(candidates) == 1 else None
    player, native = target()
    if native is None:
        return None
    mode = await guard.async_read_queue_mode(player)
    if (mode.get('items') != 0 or mode.get('autoplay_enabled') is not False
            or mode.get('is_dynamic') is not False):
        return None
    if target() != (player, native):
        raise configuration_error()
    if selected.get('media_type') == 'track':
        # Recheck profile, exact identity and current content policy even when
        # discovery supplied a link. This also recovers the supported album URL.
        if not await guard._track_allowed(selected['uri']):
            raise configuration_error()
        link = guard.apple_links.get(selected['uri'])
        if not link or '/album/' not in link or '?i=' not in link:
            return None
        if target() != (player, native):
            raise configuration_error()
    guard.phase = 'native_sonos_dispatch'
    guard.mutation_dispatch_started = guard.playback_dispatch_started = True
    await guard._native_response(guard.api.hass.services.async_call('media_player', 'play_media',
        {'entity_id':native, 'media_content_id':link, 'media_content_type':'music', 'enqueue':'replace'},
        blocking=True, context=guard.context.context))
    return dict(plan, executed=True, mode='execution_enabled', actions_issued=1,
                playback_confirmed=False, playback_kind='apple_web_native_sonos',
                evidence_policy='web_link_profile_authorized_catalog_metadata_native_dispatch')
