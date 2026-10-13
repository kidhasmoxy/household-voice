"""Conservative English routing of general questions into a read-only lane.

This routes common wording; it is not a content moderation or safety boundary.
The knowledge lane must independently enforce its own policy and tool isolation.
"""
import re


def knowledge_question(text, language='en'):
    if (not isinstance(text, str) or not text.strip() or len(text) > 8000
            or not isinstance(language, str) or language.lower().split('-')[0] != 'en'):
        return False
    value = ' '.join(text.strip().casefold().replace('’', "'").split())
    value = re.sub(r'^please\s+', '', value)
    value = re.sub(r'^(?:can|could|would) you (?:please )?', '', value)
    # Existing live household tools own state, schedules, weather and media.
    # Match their actual question forms, not arbitrary mentions such as
    # "Why do traffic lights have three colors?" or "Who invented the alarm?"
    household = (
        r"^(?:what(?:'s| is)? (?:the )?(?:time|date)(?: is it| today| now)?|what day is it)[?.!]*$",
        r"^what(?:'s| is)? (?:the )?(?:time|date)(?: is it)? in\b",
        r"^how loud (?:is|are)\b.{0,60}\b(?:speakers?|music|tv|television|volume)\b",
        r"^tell me (?:the |my |our )?(?:time|date|weather|forecast|active timers|active alarms)\b",
        # Possessive + state combinations are ordinary live schedule queries
        # ("my active timers"), not general knowledge. Keep indefinite world
        # questions such as "what is an alarm clock" in the knowledge lane.
        r"^(?:what|which)(?:'s| is| are)? (?:(?:my|the|our|all|any|currently|active|paused|running|set|scheduled) )*(?:timers?|alarms?)\b",
        r"^(?:is|are) (?:there )?(?:(?:any|my|our|the|active|currently) )*(?:timers?|alarms?) (?:currently )?(?:set|scheduled|enabled|disabled|running|paused|ringing)\b",
        r"^(?:do|does) (?:i|we) have (?:(?:any|active|currently) )*(?:timers?|alarms?)\b",
        r"^(?:how (?:long|much time)|what time).{0,80}\b(?:left|remaining|timer|alarm)\b",
        r"^(?:what|which)(?:'s| is| are)? (?:currently )?playing\b",
        r"^(?:what|how)(?:'s| is)? (?:the |my |our )?(?:weather|forecast|temperature|volume)\b",
        r"^(?:what|which) (?:lights?|doors?|locks?|windows?|fans?|switches?) (?:are|is)\b",
        r"^(?:is|are) (?:the |my |our )?(?:lights?|doors?|locks?|windows?|fans?|switches?)\b",
        r"^(?:is|are|do|does)\b.{0,60}\b(?:lights?|doors?|locks?|windows?|fans?|switches?|speakers?|timers?|alarms?)\b.{0,30}\b(?:on|off|open|closed|locked|unlocked|running|ringing|playing|paused|muted)\b",
        r"^(?:find|search|look up|show me|give me) (?:a |an |some )?recipes?\b",
    )
    if any(re.search(pattern, value) for pattern in household):
        return False
    return bool(re.match(
        r"^(?:who\b|what\b|what's\b|when\b|where\b|why\b|how\b|"
        r"explain\b|define\b|tell me\b|look up\b|search (?:for\b|the web\b)|"
        r"is\b|are\b|does\b|do\b|did\b|can\b|should\b|"
        r"find (?:information|facts)\b|give me (?:information|facts)\b)", value))
