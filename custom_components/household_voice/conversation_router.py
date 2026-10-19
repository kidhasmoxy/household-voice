"""Exact announcement commands and honest acknowledgements, without model calls."""
import re


def _compound_media_command(value):
    return bool(re.search(r'(?:\b(?:and|then)\s+|[.,]\s+)(?:please\s+)?'
        r'(?:play|shuffle|turn|set|stop|pause|resume|announce|broadcast|open|close|'
        r'lock|unlock|send|make|put)\b', value, re.I))


def _music_command(text, language):
    """Extract a whole English playback command, never a quoted/nested request."""
    if (not isinstance(text, str) or len(text) > 512 or not isinstance(language, str)
            or language.lower().split('-')[0] != 'en'
            or re.search(r'[\n\r;:"“”]', text)):
        return None
    value = ' '.join(text.replace('’', "'").split()).rstrip('.!?')
    value = re.sub(r'^(?:(?:um|uh|erm)[.,]?\s+){1,2}', '', value, flags=re.I)
    value = re.sub(r'^(?:please )?(?:(?:can|could|would) you (?:please )?)?', '', value, flags=re.I)
    value = re.sub(r' please$', '', value, flags=re.I)
    match = re.fullmatch(r"(?P<verb>play me|play|put on|put|let me hear|shuffle|"
        r"(?:I'd|I would) like to hear|I want to hear) (?P<body>.+)", value, re.I)
    if match and _compound_media_command(match['body']):
        return None
    if match and re.match(r'(?:a |an |the |some )?(?:games?\b|basketball\b|football\b|'
                          r'soccer\b|tennis\b|chess\b|checkers\b|hide and seek\b|'
                          r'roles?\b|with me\b)', match['body'], re.I):
        return None
    if match and match['verb'].casefold() == 'shuffle' and re.match(
            r'(?:on|off|this|that|these|current)\b', match['body'], re.I):
        return None
    if (match and match['verb'].casefold().startswith('put')
            and not re.match(r'(?:the |my )?(?:album|playlist|artist|band|song|track|music)\b', match['body'], re.I)
            and re.search(r'\b(?:lights?|heating|heat|fans?|blinds?|shades?|curtains?|doors?|locks?|'
                          r'thermostat|air conditioner|oven|stove|kettle|dishwasher)\b', match['body'], re.I)):
        return None
    return (match['verb'].casefold(), match['body']) if match else None


def _style_query(body, styles):
    value = re.sub(r'^(?:some |a little |a bit of )', '', body.casefold())
    value = re.sub(r' music$', '', value)
    return value if value in styles else None


def _media_name(value, *, allow_in=False):
    """Conservative literal slot validation; unsupported constraints go to the agent."""
    if (not 1 <= len(value) <= 120 or re.search(r'[;:!?"“”]', value)
            or re.search(r'\b(?:audiobooks?|audio books?|books?|stories|story|podcasts?|'
                         r'episodes?|chapters?|resume|continue|timer|alarm|announcement|'
                         r'playlist|album|station|radio|artist|band|song|track|soundtrack)\b', value, re.I)
            or (not allow_in and re.search(r'\bin\b', value, re.I))
            or re.search(r'\b(?:on|from|for|at|by|after|before|through|using|with|without|'
                         r'except|excluding|only|instead|then|next|repeat|not)\b', value, re.I)
            or re.search(r'\b(?:and|then)\s+(?:play|shuffle|turn|set|stop|pause|resume|announce|'
                         r'broadcast|open|close|lock|unlock|send|make)\b', value, re.I)
            or value.casefold() in ('music','some music','something','anything','it','that','this','more')):
        return None
    return value


def music_request(text, language='en', styles=()):
    """Parse everyday playback into literal catalog slots and a literal room name.

    This does not resolve content, a provider, an entity or a profile. The normal
    MediaRequest and source-bound API authorize every parsed request. Complex or
    ambiguous constraints remain intact for the fallback agent.
    """
    command = _music_command(text, language)
    if command is None:
        return None
    verb, body = command
    if re.search(r'[!?]', body):
        return None
    request = {}
    shuffle = verb == 'shuffle'
    # Room and shuffle modifiers can occur in either order. Neither is discarded.
    if re.search(r' on shuffle$', body, re.I):
        body = re.sub(r' on shuffle$', '', body, flags=re.I)
        shuffle = True
    # Last suffix wins syntactically, so a title such as Back in Black remains
    # intact before an explicit final destination. The caller must verify that
    # suffix against HA; an unresolved suffix delegates the original utterance.
    room = re.fullmatch(r'(?P<content>.+) in (?:the )?(?P<room>.+)', body, re.I)
    if room:
        body, target = room['content'], room['room']
        if (not 1 <= len(target) <= 80 or re.search(r"[^\w\s\-'&]", target)
                or re.search(r'\b(?:and|then|everywhere|all rooms|on|by|from|for|after|before|'
                             r'with|without|except|excluding|only|instead|shuffle)\b', target, re.I)):
            return None
        request['target_room'] = target
    if re.search(r' on shuffle$', body, re.I):
        body = re.sub(r' on shuffle$', '', body, flags=re.I)
        shuffle = True
    if verb == 'put':
        if not body.casefold().endswith(' on'):
            return None
        body = body[:-3]
    # Spoken content must retain exact matching and ordered playback. Generic
    # requests use the room's configured playlist, never a guessed title/URI.
    if re.fullmatch(r'(?:my |the |some )?(?:sleep|bedtime) stories|a (?:sleep|bedtime) story', body, re.I):
        if shuffle:
            return None
        return {**request, 'kind': 'sleep_stories', 'selection_mode': 'exact'}
    story = re.fullmatch(r'(?:the )?(sleep|bedtime) story (?:called |named |of )?(.+)', body, re.I)
    trailing_story = re.fullmatch(r'(.+?) (sleep|bedtime) story', body, re.I)
    if trailing_story and not story:
        title = _media_name(trailing_story[1], allow_in=room is not None)
        if shuffle or title is None:
            return None
        return {**request, 'kind': 'sleep_story', 'query': title, 'selection_mode': 'exact'}
    if story:
        title = _media_name(story[2], allow_in=room is not None)
        if shuffle or title is None:
            return None
        return {**request, 'kind': 'sleep_story', 'query': title, 'selection_mode': 'exact'}
    # An artist clause constrains a track/album; music/songs by an artist instead
    # selects that artist. Explicit typed categories are never discarded.
    by = re.fullmatch(r'(?P<title>.+?) by (?P<artist>.+)', body, re.I)
    artist = None
    if by:
        body, artist = by['title'], _media_name(by['artist'], allow_in=room is not None)
        if artist is None:
            return None
        if body.casefold() in ('music','songs','some music','some songs'):
            request.update(query=artist, media_type='artist')
            if shuffle: request['shuffle'] = True
            return request
    typed = re.fullmatch(r'(?:the |my |an? )?(album|playlist|artist|band|song|track) (.+)', body, re.I)
    media_type = None
    if typed:
        media_type = {'band':'artist','song':'track'}.get(typed[1].casefold(), typed[1].casefold())
        body = typed[2]
    # A named track from a soundtrack retains both title and album constraints.
    album = re.fullmatch(r'(?P<title>.+?) from (?:the )?(?P<album>.+?) soundtrack', body, re.I)
    if album:
        if media_type not in (None, 'track') or _media_name(album['album'], allow_in=room is not None) is None:
            return None
        body = album['title']
        media_type = 'track'
        request['album'] = album['album'] + ' soundtrack'
    soundtrack = re.fullmatch(r'(?:the )?(.+?) (?:music )?soundtrack', body, re.I)
    if soundtrack:
        if media_type not in (None, 'album') or _media_name(soundtrack[1], allow_in=room is not None) is None:
            return None
        query, media_type = soundtrack[1] + ' soundtrack', 'album'
    else:
        query = (_style_query(body, styles) if media_type is None and artist is None else None) or _media_name(body, allow_in=room is not None)
    if query is None:
        return None
    if artist is not None:
        if media_type not in (None, 'track', 'album'):
            return None
        request['artist'] = artist
        media_type = media_type or 'track'
    request['query'] = query
    if media_type: request['media_type'] = media_type
    if shuffle: request['shuffle'] = True
    return request


def broad_music_query(text, language, styles):
    """Compatibility view for unqualified broad-selection policy hooks."""
    request = music_request(text, language, styles)
    return request['query'] if request and set(request) == {'query'} and request['query'] in styles else None


def music_acknowledgement(result):
    """Speak a tool's actual accepted choice; never turn uncertainty into no match."""
    if not isinstance(result, dict):
        result = {}
    status, reason = result.get('status'), result.get('reason')
    if status in ('clarify', 'unavailable') and isinstance(result.get('question'), str) and result['question'].strip():
        return result['question']
    if status == 'action_accepted' and result.get('executed') is True:
        announcement = result.get('announcement')
        if isinstance(announcement, str) and 0 < len(announcement.strip()) <= 800:
            return announcement
        return 'I sent your music request to the speaker.'
    if reason == 'media_playback_pending_or_uncertain':
        if result.get('mutation_dispatch_started') and not result.get('playback_dispatch_started'):
            return "Music Assistant took too long. I couldn't confirm your request."
        return "Music Assistant may still be preparing playback. I couldn't confirm that it started."
    if reason == 'media_provider_timeout':
        return 'Music Assistant took too long to find the music. No playback was requested.'
    if reason == 'media_result_uncertain':
        return "I couldn't confirm the earlier music request."
    if status == 'dry_run':
        return 'Enable music playback and live actions in Household Voice settings to play music.'
    if reason in ('verified_source_and_media_configuration_required', 'invalid_player_mapping',
                  'profile_required', 'player_unavailable'):
        return 'Check this assistant\'s speaker and Music Assistant settings in Household Voice.'
    if reason == 'no_relevant_soundtrack':
        return 'I could not find that soundtrack. Please name the film, show, or soundtrack version.'
    if reason in ('no_relevant_music_collection', 'no_exact_catalog_match'):
        return 'I could not find that music in your configured providers. Try another title, artist, album, playlist or station.'
    if status == 'clarify':
        return 'Which artist, song, album, playlist, station or soundtrack did you mean?'
    return 'I could not confirm your music request. Please check Music Assistant.'


def named_music_query(text, language='en'):
    """Compatibility view: only a simple unconstrained catalog query."""
    request = music_request(text, language)
    return request['query'] if request and set(request) == {'query'} else None


def qualified_music_request(text, language='en', styles=()):
    """Compatibility view for a request with explicit semantic modifiers."""
    request = music_request(text, language, styles)
    return request if request and set(request) != {'query'} else None


def media_action_request(text, language='en'):
    """Bounded fallback receipt guard, not a general natural-language classifier."""
    if _music_command(text, language) is not None:
        return True
    if (not isinstance(text, str) or len(text) > 512 or not isinstance(language, str)
            or language.lower().split('-')[0] != 'en' or re.search(r'[\n\r;:"“”]', text)):
        return False
    value = ' '.join(text.casefold().split()).rstrip('.!?')
    value = re.sub(r'^(?:please )?(?:(?:can|could|would) you (?:please )?)?', '', value)
    if _compound_media_command(value):
        return False
    if re.fullmatch(r'listen to (?:me|us|my question|what I(?: am|\'m) saying|this|that)', value, re.I):
        return False
    media_words = r'(?:music|songs?|tracks?|albums?|playlists?|artists?|bands?|radio|station|' \
                  r'audiobooks?|books?|stories|story|podcasts?|chapters?|shuffle)'
    # Only operations owned by HouseholdMediaTool have verifiable receipts.
    # Native Assist volume/next/queue/group controls may use other tool APIs;
    # lack of our receipt must not turn those valid actions into false failures.
    return bool(re.fullmatch(r'(?:pause|resume|continue|stop)(?: please)?', value)
        or re.fullmatch(r'(?:listen to|resume|continue|pause|stop|read) .*\b' + media_words + r'\b.*', value)
        or re.fullmatch(r'listen to .+', value))


COMMANDS = {
    'start': frozenset(('make an announcement', 'make announcement', 'start an announcement',
                        'start announcement', 'broadcast an announcement', 'broadcast', 'announce',
                        'make a broadcast', 'start a broadcast', 'broadcast a message', 'send an announcement')),
    'cancel': frozenset(('cancel announcement', 'cancel the announcement', 'cancel my announcement',
                         'stop announcement', 'stop the announcement', 'stop my announcement')),
    'status': frozenset(('announcement status', 'check announcement status',
                         'what is the announcement status', "what's the announcement status",
                         'what is the status of my announcement')),
}


def command(text, language='en'):
    """Match a whole English command; never extract commands from a message body."""
    if not isinstance(text, str) or not isinstance(language, str) or language.lower().split('-')[0] != 'en':
        return None
    value = re.sub(r'\s+', ' ', text.strip().casefold()).rstrip('.!?').strip()
    if value.startswith('please '):
        value = value[7:]
    for prefix in ('can you ', 'could you ', 'would you '):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    if value.startswith('please '):
        value = value[7:]
    if value.endswith(' please'):
        value = value[:-7]
    for operation, phrases in COMMANDS.items():
        if value in phrases:
            return operation
    return None


def inline_announcement(text, language='en'):
    """Extract only an anchored announcement body, preserving its exact text.

    The returned suffix is data, never a second command. Audio alignment must
    independently prove its boundary before any original recording is replayed.
    """
    if (not isinstance(text, str) or not isinstance(language, str)
            or language.lower().split('-')[0] != 'en' or len(text) > 8000
            or command(text, language) is not None):
        return None
    prefix = (r'^\s*(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?'
              r'(?:(?:make|start|send)\s+(?:an?\s+)?(?:announcement|broadcast)'
              r'|broadcast\s+(?:a\s+message|an\s+announcement)|announce|broadcast)'
              r'(?:\s+(?:that|saying))?(?:\s*[,.:;!?]\s*|\s+)')
    match = re.match(prefix, text, flags=re.IGNORECASE)
    if match is None:
        return None
    body = text[match.end():].strip()
    if not body or not re.search(r'\w', body) or body.casefold().rstrip('.!?') in ('that', 'saying', 'please'):
        return None
    return body


def acknowledgement(operation, result):
    """Do not equate a queued workflow or provider acceptance with audible delivery."""
    if not isinstance(result, dict):
        result = {}
    status = result.get('status')
    if status == 'queued':
        if operation == 'start':
            # HA skips TTS on an empty response. The native question workflow
            # supplies the sole prompt, or the inline recording plays directly.
            return ''
        if result.get('reason') == 'recording_queued_for_delivery':
            return 'Your recording is queued. Speaker and phone delivery has not been confirmed.'
        return 'Wait for the announcement prompt, then say your message.'
    if status == 'cancelled':
        return 'The pending announcement is cancelled. Audio already accepted by a speaker may still play.'
    if status == 'no_change':
        return 'There is no announcement in progress on this device.'
    if status == 'in_progress':
        if result.get('reason') == 'asking':
            return 'I am waiting for your announcement after the prompt.'
        return 'The announcement workflow is in progress. Delivery has not been confirmed.'
    if status == 'delivery_status':
        return 'The recording has delivery results. I cannot confirm what was heard or seen on phones.'
    if status == 'dry_run':
        return 'Announcements and recording must be enabled in Household Voice before I can start.'
    if status == 'clarify':
        return 'I cannot start an announcement here. Check the dedicated recording pipeline and default speakers in Household Voice.'
    if operation == 'cancel':
        return 'I could not confirm cancellation. An announcement may still be pending.'
    if operation == 'status':
        return 'I could not check the announcement status.'
    return 'I could not start the announcement. Check Household Voice settings and try again.'
