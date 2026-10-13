"""Exact announcement commands and honest acknowledgements, without model calls."""
import re


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


def acknowledgement(operation, result):
    """Do not equate a queued workflow or provider acceptance with audible delivery."""
    if not isinstance(result, dict):
        result = {}
    status = result.get('status')
    if status == 'queued':
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
