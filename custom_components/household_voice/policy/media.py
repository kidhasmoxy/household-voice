"""Deterministic Music Assistant plans; never searches or operates a player.

Only the future authenticated HA adapter may construct context/catalog/queue
snapshots. These are not LLM tool arguments and their dataclasses do not prove
provenance. Plans require fresh authoritative validation before execution.

Contracts follow the saved books-and-sleep-stories script and HA action docs:
https://www.home-assistant.io/actions/music_assistant.play_media/
https://www.home-assistant.io/actions/music_assistant.search/
https://www.music-assistant.io/music-providers/audiobookshelf/
"""
from dataclasses import dataclass
from difflib import SequenceMatcher
import math
import re
from typing import Literal

from .routing import RoomRegistry, RoutingError

MediaType = Literal['track', 'album', 'artist', 'playlist', 'radio', 'audiobook']
MediaKind = Literal['music', 'book', 'station', 'sleep_story', 'sleep_stories', 'sleep_collection']
Operation = Literal['play', 'resume', 'pause', 'stop']
SelectionMode = Literal['exact', 'broad_music']
TYPES = ('track', 'album', 'artist', 'playlist', 'radio', 'audiobook')
KINDS = ('music', 'book', 'station', 'sleep_story', 'sleep_stories', 'sleep_collection')
STORY_ALIASES = frozenset(('my sleep stories', 'sleep stories', 'a sleep story',
                         'a bedtime story', 'my bedtime stories', 'bedtime stories'))


def _text(value, name, *, empty=False, limit=512):
    if not isinstance(value, str) or len(value)>limit or (not empty and not value.strip()) or any(ord(c)<32 for c in value):
        raise ValueError(f'Invalid {name}')


def _normal(value):
    return ' '.join(value.casefold().split())


def _music_name(value):
    """Ignore harmless punctuation, never remove version/edition qualifiers."""
    return ' '.join(re.findall(r'\w+', value.casefold()))


def _music_recommendation(query, items):
    """Bounded typo tolerance; near ties and short names still need a choice."""
    name = _music_name(query)
    scores = []
    for item in items:
        if not item.available or item.spoken_content:
            continue
        score = max(SequenceMatcher(None, name, _music_name(title)).ratio()
                    for title in (item.title, *item.aliases))
        scores.append((score, item))
    scores.sort(key=lambda pair: pair[0], reverse=True)
    if (not scores or len(name) < 5 or scores[0][0] < .88
            or (len(scores) > 1 and scores[0][0] - scores[1][0] < .12)):
        return []
    return [scores[0][1]]


def _timestamp(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_catalog_uri(uri: str, media_type: str) -> None:
    """Accept bounded MA item URIs, never a raw network/file URL or a title."""
    _text(uri, 'catalog URI', limit=2048)
    match = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9_.-]*)://(track|album|artist|playlist|radio|audiobook)/([^\s?#]+)', uri)
    if not match or match[2]!=media_type or match[1].lower() in ('http', 'https', 'file', 'ftp', 'data', 'javascript'):
        raise ValueError('Invalid or mismatched Music Assistant catalog URI')


@dataclass(frozen=True)
class MediaRequest:
    query: str = ''
    kind: MediaKind = 'music'
    operation: Operation = 'play'
    media_type: MediaType | None = None
    target_room: str | None = None
    provider: str | None = None
    artist: str | None = None
    album: str | None = None
    selected_uri: str | None = None
    shuffle: bool = False
    radio_mode: bool = False
    selection_mode: SelectionMode = 'exact'

    def __post_init__(self):
        _text(self.query, 'query', empty=True)
        if self.kind not in KINDS or self.operation not in ('play', 'resume', 'pause', 'stop'):
            raise ValueError('Unsupported media kind or operation')
        if self.media_type is not None and self.media_type not in TYPES:
            raise ValueError('Unsupported media type')
        for name in ('target_room', 'provider', 'artist', 'album', 'selected_uri'):
            value = getattr(self, name)
            if value is not None:
                _text(value, name, limit=2048 if name=='selected_uri' else 512)
        if type(self.shuffle) is not bool or type(self.radio_mode) is not bool:
            raise ValueError('Shuffle and radio_mode must be booleans')
        if self.selection_mode not in ('exact', 'broad_music'):
            raise ValueError('Unsupported media selection mode')
        if self.selection_mode == 'broad_music' and (
                self.kind != 'music' or self.operation != 'play'
                or self.media_type not in (None, 'playlist', 'radio')
                or self.artist or self.album or self.selected_uri):
            raise ValueError('Broad music selection requires an unqualified music play request')
        if self.operation in ('pause', 'stop') and (self.query or self.selected_uri):
            raise ValueError('Pause/stop refer to the existing room queue, not a title')


@dataclass(frozen=True)
class MediaContext:
    """Trusted transport/config values; no household-specific defaults."""
    config_entry_id: str
    source_device: str | None = None
    username: str | None = None
    default_sleep_playlist: str = ''

    def __post_init__(self):
        _text(self.config_entry_id, 'Music Assistant instance')
        _text(self.default_sleep_playlist, 'default sleep playlist', empty=True)
        if self.source_device is not None:
            _text(self.source_device, 'source device')
        if self.username is not None:
            _text(self.username, 'username', limit=120)


@dataclass(frozen=True)
class CatalogItem:
    uri: str
    title: str
    media_type: MediaType
    provider: str
    aliases: tuple[str, ...] = ()
    artists: tuple[str, ...] = ()
    album: str | None = None
    available: bool = True
    spoken_content: bool = False
    radio_mode_supported: bool = False

    def __post_init__(self):
        validate_catalog_uri(self.uri, self.media_type)
        _text(self.title, 'catalog title')
        _text(self.provider, 'provider')
        for name in ('aliases', 'artists'):
            values = getattr(self, name)
            if not isinstance(values, tuple) or len(values)>32:
                raise ValueError(f'{name} must be a bounded tuple')
            for value in values:
                _text(value, name)
        if self.album is not None:
            _text(self.album, 'album')
        for name in ('available', 'spoken_content', 'radio_mode_supported'):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f'{name} must be boolean')


@dataclass(frozen=True)
class CatalogSnapshot:
    """Results of the exact query/profile/instance lookup, supplied by adapter.

    complete=True requires the adapter to verify its result was not truncated.
    A partial result can offer choices but cannot auto-select a unique match.
    Explicit broad music requests may recommend one relevant collection without
    claiming an exact or globally unique match.
    """
    query: str
    username: str
    config_entry_id: str
    observed_at: float
    items: tuple[CatalogItem, ...]
    complete: bool = False
    complete_media_types: tuple[MediaType, ...] = ()

    def __post_init__(self):
        for name in ('query', 'username', 'config_entry_id'):
            _text(getattr(self, name), name)
        if not _timestamp(self.observed_at) or type(self.complete) is not bool:
            raise ValueError('Invalid catalog observation')
        if (not isinstance(self.complete_media_types, tuple)
                or any(kind not in TYPES for kind in self.complete_media_types)):
            raise ValueError('Invalid bounded catalog groups')
        if not isinstance(self.items, tuple) or len(self.items)>300 or any(not isinstance(i, CatalogItem) for i in self.items):
            raise ValueError('Catalog must be a bounded tuple of CatalogItems')
        seen = {}
        for item in self.items:
            if item.uri in seen and seen[item.uri]!=item:
                raise ValueError('Conflicting records for one catalog URI')
            seen[item.uri] = item


@dataclass(frozen=True)
class QueueSnapshot:
    player: str
    username: str
    generation: str
    observed_at: float
    has_items: bool
    current_uri: str | None = None
    media_type: MediaType | None = None
    spoken_content: bool = False

    def __post_init__(self):
        for name in ('player', 'username', 'generation'):
            _text(getattr(self, name), name)
        if not _timestamp(self.observed_at) or type(self.has_items) is not bool or type(self.spoken_content) is not bool:
            raise ValueError('Invalid queue observation')
        if self.media_type is not None and self.media_type not in TYPES:
            raise ValueError('Invalid queue media type')
        if self.current_uri is not None:
            validate_catalog_uri(self.current_uri, self.media_type)


def _result(status, reason, **details):
    return {'status': status, 'reason': reason, 'mode': 'dry_run', 'executed': False,
            'actions': [], **details}


def _action(action, player, **data):
    return {'action': action, 'target': {'entity_id': player}, 'data': data}


def _spoken_actions(player):
    return [_action('media_player.shuffle_set', player, shuffle=False),
            _action('media_player.repeat_set', player, repeat='off')]


def _soundtrack_query(request):
    """Recognize an explicit soundtrack category without guessing an edition."""
    if request.kind != 'music' or request.operation != 'play' or request.media_type not in (None, 'album'):
        return None
    match = re.fullmatch(r'(?:the\s+)?(.+?)\s+(?:music\s+)?soundtrack', request.query.strip(), re.I)
    return match[1].strip() if match else None


def _soundtrack_rank(query, item):
    """Prefer an actual soundtrack over a cast recording, excluding spoken/cover media."""
    if item.media_type != 'album' or not item.available or item.spoken_content:
        return None
    names = (item.title, *item.aliases)
    metadata = ' '.join(names).casefold()
    if any(marker in metadata for marker in ('calm kids', 'sleep stor', 'bedtime stor',
            'audiobook', 'podcast', 'karaoke', 'tribute', 'cover version', 'lullab')):
        return None
    required = set(re.findall(r'\w+', query.casefold()))
    if not required or not any(required <= set(re.findall(r'\w+', name.casefold())) for name in names):
        return None
    if re.search(r'\bsoundtrack\b', metadata):
        return 0
    if re.search(r'\b(?:original\s+)?(?:broadway\s+|london\s+)?cast\s+recording\b', metadata):
        return 1
    return None


def _intent(request, context):
    query = request.query.strip()
    generic_story = request.kind in ('music', 'sleep_story', 'sleep_stories') and _normal(query) in STORY_ALIASES
    kind = 'sleep_stories' if generic_story else request.kind
    if kind=='sleep_stories' and (not query or generic_story):
        query = context.default_sleep_playlist
    fixed_type = {'book': 'audiobook', 'station': 'radio', 'sleep_story': 'track',
                  'sleep_stories': 'playlist', 'sleep_collection': 'album'}.get(kind)
    if fixed_type and not generic_story and request.media_type not in (None, fixed_type):
        return None
    types = (fixed_type,) if fixed_type else ((request.media_type,) if request.media_type else ('track', 'album', 'artist', 'playlist', 'radio'))
    if soundtrack_query := _soundtrack_query(request):
        query, types = soundtrack_query, ('album',)
    if request.selection_mode == 'broad_music':
        if generic_story:
            return None
        types = (request.media_type,) if request.media_type else ('playlist', 'radio')
    text = _normal(query + ' ' + (request.album or ''))
    spoken = (kind in ('book', 'sleep_story', 'sleep_stories', 'sleep_collection') or types==('audiobook',)
              or any(marker in text for marker in ('sleep stor', 'bedtime stor', 'calm kids')))
    return query, types, spoken


def _broad_match(query, item):
    """Conservative collection relevance, never an arbitrary search-result URI.

    Preserve provider ranking among relevant collections. Token boundaries keep
    'jazz' from matching 'Jazzercise'; punctuation differences remain harmless.
    Content metadata is data and never changes action, room, or profile scope.
    """
    tokens = tuple(re.findall(r'\w+', query.casefold()))
    if not tokens or item.spoken_content or not item.available:
        return False
    if any(marker in (item.title + ' ' + ' '.join(item.aliases)).casefold()
           for marker in ('sleep stor', 'bedtime stor', 'calm kids', 'audiobook', 'podcast')):
        return False
    return any(tokens == words[start:start + len(tokens)]
               for name in (item.title, *item.aliases)
               for words in (tuple(re.findall(r'\w+', name.casefold())),)
               for start in range(len(words) - len(tokens) + 1))


def plan_media(request: MediaRequest, registry: RoomRegistry, context: MediaContext,
               *, now: float, catalog: CatalogSnapshot | None = None,
               queue: QueueSnapshot | None = None) -> dict:
    """Plan one room's request using fresh observations; never execute a plan.

    Catalog freshness is five minutes; queue freshness is thirty seconds. The
    eventual executor must serialize actions per queue, recheck observations and
    profile, stop on any error, and confirm authoritative playback afterward.
    No title splitting, implicit queue transfer, profile inference or seek occurs.
    """
    if not isinstance(request, MediaRequest) or not isinstance(context, MediaContext) or not _timestamp(now):
        raise ValueError('Invalid media planning input')
    if catalog is not None and not isinstance(catalog, CatalogSnapshot):
        raise ValueError('Invalid catalog snapshot')
    if queue is not None and not isinstance(queue, QueueSnapshot):
        raise ValueError('Invalid queue snapshot')
    try:
        room = registry.resolve(source_device=context.source_device, target=request.target_room)
    except RoutingError:
        return _result('clarify', 'unknown_room', question='Which room should play the audio?')
    player = room['music_player']
    if not isinstance(player, str) or not re.fullmatch(r'media_player\.[a-z0-9_]+', player):
        return _result('unavailable', 'invalid_player_mapping')
    if room.get('music_available', True) is not True:
        return _result('unavailable', 'player_unavailable', room=room['area'])
    if context.username is None:
        return _result('clarify', 'profile_required', question='Which listening profile should I use?')
    target = {'room': room['area'], 'player': player, 'username': context.username}
    existing_queue = request.operation in ('pause', 'stop') or (request.operation=='resume' and not request.query and not request.selected_uri)
    if existing_queue:
        if queue is None or queue.player!=player or queue.username!=context.username or not 0<=now-queue.observed_at<=30:
            return _result('lookup_required', 'fresh_queue_required', **target)
        if not queue.has_items:
            return _result('clarify', 'empty_queue', question='What would you like to play?', **target)
        spoken = queue.spoken_content or queue.media_type=='audiobook'
        if request.operation=='resume' and ((request.kind=='book' and queue.media_type!='audiobook')
                or (request.kind.startswith('sleep_') and not spoken)):
            return _result('clarify', 'different_content_in_queue', question='Which book or story should I play?', **target)
        actions = _spoken_actions(player) if spoken and request.operation=='resume' else []
        service = {'resume': 'media_play', 'pause': 'media_pause', 'stop': 'media_stop'}[request.operation]
        actions.append(_action('media_player.'+service, player))
        return _result('ready', 'existing_queue_control', actions=actions,
                       spoken_content=spoken, autoplay_enabled=False if spoken and request.operation=='resume' else None,
                       preconditions={'queue_generation': queue.generation, 'revalidate_before_execution': True},
                       progress_policy='preserve_existing_queue', **target)
    intent = _intent(request, context)
    if intent is None:
        return _result('clarify', 'content_type_conflict', question='Which kind of content did you mean?', **target)
    query, types, spoken = intent
    soundtrack = _soundtrack_query(request) is not None
    broad = request.selection_mode == 'broad_music'
    if broad and spoken:
        return _result('clarify', 'content_type_conflict', question='Which book or story would you like?', **target)
    if not query:
        return _result('clarify', 'title_required', question='Which title or station would you like?', **target)
    # An author is not a music artist filter; ignore these fields for books.
    artist = request.artist if any(t in ('track', 'album') for t in types) else None
    album = request.album if types==('track',) else None
    lookup_data = {'config_entry_id': context.config_entry_id, 'name': query, 'media_type': list(types),
                   'username': context.username, 'limit': 20}
    if artist:
        lookup_data['artist'] = artist
    if album:
        lookup_data['album'] = album
    lookup = {'action': 'music_assistant.search', 'data': lookup_data}
    if (catalog is None or catalog.username!=context.username or catalog.config_entry_id!=context.config_entry_id
            or _normal(catalog.query)!=_normal(query) or not 0<=now-catalog.observed_at<=300):
        return _result('lookup_required', 'fresh_catalog_required', lookup=lookup, **target)
    eligible = {i.uri: i for i in catalog.items if i.media_type in types
                and (not request.provider or _normal(i.provider)==_normal(request.provider))
                and (not artist or _normal(artist) in {_normal(a) for a in i.artists})
                and (not album or _normal(i.album or '')==_normal(album))}
    artist_preferred = False
    artist_ranked = False
    if soundtrack:
        ranked = [(rank, item) for item in eligible.values()
                  if (rank := _soundtrack_rank(query, item)) is not None]
        if request.selected_uri:
            matches = [item for _, item in ranked if item.uri == request.selected_uri]
            if not matches:
                return _result('clarify', 'selection_not_in_catalog', question='Please choose one of the soundtrack results.', **target)
        else:
            best = min((rank for rank, _ in ranked), default=None)
            matches = [item for rank, item in ranked if rank == best]
    elif broad:
        # This is a recommendation, not an attempt to disambiguate an exact title.
        # The MA adapter preserves search order within collection result groups.
        relevant = [i for i in eligible.values() if _broad_match(query, i)]
        # Prefer a playlist to a station, preserving provider ranking within
        # each group. A station is a continuous stream, not a generated mix.
        matches = sorted(relevant, key=lambda i: i.media_type != 'playlist')[:1]
    elif request.selected_uri:
        matches = [eligible[request.selected_uri]] if request.selected_uri in eligible else []
        if not matches:
            return _result('clarify', 'selection_not_in_catalog', question='Please choose one of the catalog results.', **target)
    else:
        matches = [i for i in eligible.values() if _normal(query) in {_normal(i.title), *(_normal(a) for a in i.aliases)} or query==i.uri]
        if (request.kind == 'music' and not spoken and request.media_type is None
                and not request.artist and not request.album):
            # Bare exact names use the provider's artist recommendation order,
            # even when artists share a name or an album is self-titled. This
            # is not a global uniqueness claim. Fuzzy alternatives still need
            # a clear winner and typed requests retain explicit ambiguity.
            artists = [i for i in eligible.values() if i.media_type == 'artist' and i.available and not i.spoken_content]
            artist_matches = [i for i in matches if i in artists]
            exact_artists = bool(artist_matches)
            if not matches:
                artist_matches = _music_recommendation(query, artists)
            if artist_matches and (exact_artists or catalog.complete or 'artist' in catalog.complete_media_types):
                matches = artist_matches[:1] if exact_artists else artist_matches
                artist_preferred = True
                artist_ranked = exact_artists
        if not matches and request.kind == 'music' and not spoken:
            matches = _music_recommendation(query, eligible.values())
    if not matches:
        # Results without an exact match, relevant broad collection, or clear
        # music-only spelling winner remain suggestions, even if only one exists.
        options = sorted(eligible.values(), key=lambda i: (_normal(i.title), i.provider, i.uri))
        return _result('clarify', 'no_relevant_soundtrack' if soundtrack else 'no_relevant_music_collection' if broad else 'no_exact_catalog_match',
                       question='Which music playlist or station would you like?' if broad else 'Which catalog result did you mean?',
                       choices=[_choice(i) for i in options[:5]], candidate_count=len(options), **target)
    confident_music = (request.kind == 'music' and not spoken and not soundtrack
                       and matches == _music_recommendation(query, eligible.values()))
    if (request.media_type is None and len(matches) == 1 and matches[0].media_type == 'artist'
            and not catalog.complete and 'artist' not in catalog.complete_media_types):
        confident_music = False
    if len(matches)>1 or (not catalog.complete and not request.selected_uri and not broad and not confident_music and not artist_preferred):
        options = sorted(matches, key=lambda i: (_normal(i.title), i.provider, i.uri))
        return _result('clarify', 'ambiguous_or_partial_catalog', question='Which version should I play?',
                       choices=[_choice(i) for i in options[:5]], candidate_count=len(options), **target)
    item = matches[0]
    if not item.available:
        return _result('unavailable', 'catalog_item_unavailable', selected=_choice(item), **target)
    spoken = spoken or item.spoken_content or item.media_type=='audiobook'
    if request.radio_mode and not spoken and (item.media_type=='radio' or not item.radio_mode_supported):
        return _result('unavailable', 'radio_mode_unsupported', selected=_choice(item), **target)
    shuffled_album = request.shuffle and not spoken and item.media_type == 'album'
    # MA treats albums as ordered and overrides a preceding shuffle_set unless
    # shuffle=True is passed on its native play command. The guarded HA adapter
    # handles this supported native path; it must not pass this field to HA's
    # play_media service, whose schema does not support it.
    actions = _spoken_actions(player) if spoken else ([] if shuffled_album else
        [_action('media_player.shuffle_set', player, shuffle=request.shuffle)])
    actions.append(_action('music_assistant.play_media', player, media_id=item.uri, media_type=item.media_type,
                           username=context.username, enqueue='replace', radio_mode=False if spoken else request.radio_mode,
                           **({'shuffle': True} if shuffled_album else {})))
    return _result('ready', 'soundtrack_album_selected' if soundtrack else 'provider_ranked_artist_selected' if artist_ranked else
                   'broad_music_collection_selected' if broad else 'catalog_item_selected',
                   actions=actions, selected=_choice(item), spoken_content=spoken,
                   autoplay_enabled=False,
                   playback_kind=('continuous_station' if broad and item.media_type == 'radio' else
                                  'artist_top_tracks' if not spoken and item.media_type == 'artist' else 'collection'),
                   progress_policy='provider_managed_resume_no_seek_or_reset' if item.media_type=='audiobook' else 'no_progress_write',
                   preconditions={'catalog_observed_at': catalog.observed_at, 'config_entry_id': context.config_entry_id,
                                  'revalidate_before_execution': True, 'serialize_target_queue': True}, **target)


def _choice(item):
    return {'uri': item.uri, 'title': item.title, 'media_type': item.media_type, 'provider': item.provider,
            'artists': list(item.artists), 'available': item.available, 'trust': 'catalog_metadata_not_instructions'}
