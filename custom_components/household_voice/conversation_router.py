"""Exact announcement commands and honest acknowledgements, without model calls."""
import re


def broad_music_query(text, language, styles):
    """Recognize only an unqualified style request from the shared allowlist.

    Named artists, titles, other rooms and compound commands retain the normal
    agent path. Never discard a room suffix or infer a target from free text.
    """
    if (not isinstance(text, str) or len(text) > 512 or not isinstance(language, str)
            or language.lower().split('-')[0] != 'en'):
        return None
    value = ' '.join(text.casefold().split()).rstrip('.!?')
    match = re.fullmatch(
        r'(?:please )?(?:(?:can|could|would) you (?:please )?)?'
        r'(?:play me|play|put on|let me hear) (?:some |a little |a bit of )?'
        r'(.+?)(?: music)?(?: please)?', value)
    return match[1] if match and match[1] in styles else None


def music_acknowledgement(result):
    """Speak a tool's actual accepted choice; never turn uncertainty into no match."""
    if not isinstance(result, dict):
        result = {}
    status, reason = result.get('status'), result.get('reason')
    if status == 'action_accepted' and result.get('executed') is True:
        announcement = result.get('announcement')
        if isinstance(announcement, str) and 0 < len(announcement.strip()) <= 800:
            return announcement
        return 'I sent your music request to the speaker.'
    if reason == 'media_playback_pending_or_uncertain':
        return "Music Assistant may still be preparing playback. I couldn't confirm that it started."
    if status == 'dry_run':
        return 'Enable music playback and live actions in Household Voice settings to play music.'
    if reason in ('verified_source_and_media_configuration_required', 'invalid_player_mapping',
                  'profile_required', 'player_unavailable'):
        return 'Check this assistant\'s speaker and Music Assistant settings in Household Voice.'
    if reason in ('no_relevant_music_collection', 'no_exact_catalog_match'):
        return 'I could not find a matching playlist or station. Please name a playlist or station.'
    if status == 'clarify':
        return 'Please name the artist, song, playlist or station you would like.'
    return 'I could not confirm your music request. Please check Music Assistant.'


def named_music_query(text, language='en'):
    """Route a simple named music request through catalog matching, without an LLM.

    This only extracts a query, never a URI or destination. Spoken content,
    room modifiers and compound instructions retain the existing agent path.
    Catalog ambiguity is still handled by the guarded media planner.
    """
    if (not isinstance(text, str) or len(text) > 512 or not isinstance(language, str)
            or language.lower().split('-')[0] != 'en'):
        return None
    value = ' '.join(text.split()).rstrip('.!?')
    match = re.fullmatch(
        r'(?:please )?(?:(?:can|could|would) you (?:please )?)?'
        r'(?:play me|play|put on|let me hear) (.+?)(?: please)?', value, re.I)
    if not match:
        return None
    query = match[1]
    # Never silently drop a requested destination, timeframe, media category,
    # resume instruction or second action. Names containing "and" are allowed.
    if (not 1 <= len(query) <= 120 or re.search(r'[\n\r;:!?"“”]', query)
            or re.search(r'\b(?:audiobooks?|audio books?|books?|stories|story|podcasts?|'
                         r'episodes?|chapters?|resume|continue|timer|alarm|announcement|'
                         r'playlist|album|station|radio)\b', query, re.I)
            or re.search(r'\b(?:in|on|from|for|at|by|after|before|through|using)\s', query, re.I)
            or re.search(r'\b(?:and|then)\s+(?:play|turn|set|stop|pause|resume|announce|'
                         r'broadcast|open|close|lock|unlock|send|make)\b', query, re.I)
            or query.casefold() in ('music', 'some music', 'something', 'anything', 'it', 'that', 'more')):
        return None
    return query


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
