"""Optional Apple web discovery. Search text never becomes playback authority.

Only strict Apple share-link IDs enter MA's profile authorization and metadata
lookup. The ordinary planner still selects the title/version and enforces filters.
"""
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
    return (f'https://music.apple.com/{country}/{kind}/{slug}/{identifier}',
            f'apple_music://{media_type}/{identifier}', media_type)


async def discover(data, *, search, authorize, lookup):
    """Bounded discovery with canonical catalog metadata, never snippet matching."""
    groups = {'track':'tracks', 'album':'albums', 'artist':'artists', 'playlist':'playlists'}
    result = {group: [] for group in (*groups.values(), 'radio', 'audiobooks', 'podcasts')}
    links, seen = {}, set()
    terms = ' '.join(str(data[key]) for key in ('name', 'artist', 'album') if data.get(key))
    response = await search(terms + ' Apple Music')
    if response.get('status') != 'ready':
        return None, {}
    for source in response.get('sources', [])[:5]:
        try:
            link, uri, kind = apple_link(source.get('url'))
        except (ValueError, TypeError):
            continue
        if kind not in data['media_type'] or uri in seen:
            continue
        seen.add(uri)
        if not await authorize(uri):
            continue
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
            continue
        instance = matches[0].get('provider_instance')
        if not isinstance(instance, str) or not re.fullmatch(r'apple_music(?:--[A-Za-z0-9_-]+)?', instance):
            continue
        bound = f'{instance}://{kind}/{identifier}'
        if not await authorize(bound):
            continue
        raw = dict(raw, uri=bound)
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

    Keep filtered, shuffled, artist and endless requests on the normal MA path.
    Never retry through MA after a native dispatch may have started.
    """
    selected = plan.get('selected', {})
    link = getattr(guard, 'apple_links', {}).get(selected.get('uri'))
    options = guard.api.entry.options
    if (not link or options.get('apple_web_native_enabled') is not True
            or options.get('block_explicit_tracks') is True or request.shuffle
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
    guard.phase = 'native_sonos_dispatch'
    guard.mutation_dispatch_started = guard.playback_dispatch_started = True
    await guard._native_response(guard.api.hass.services.async_call('media_player', 'play_media',
        {'entity_id':native, 'media_content_id':link, 'media_content_type':'music', 'enqueue':'replace'},
        blocking=True, context=guard.context.context))
    return dict(plan, executed=True, mode='execution_enabled', actions_issued=1,
                playback_confirmed=False, playback_kind='apple_web_native_sonos',
                evidence_policy='web_link_profile_authorized_catalog_metadata_native_dispatch')
