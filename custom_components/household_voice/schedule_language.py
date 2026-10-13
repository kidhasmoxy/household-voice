"""Whole English schedule commands; never infer identity from a model's choice."""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class ScheduleEdit:
    operation: str | None
    kind: str = 'timer'
    seconds: float | None = None
    label: str | None = None


def normalize(text, language='en'):
    if (not isinstance(text,str) or not isinstance(language,str)
            or language.lower().split('-')[0]!='en' or not 0<len(text)<=1000):
        return None
    value=' '.join(text.casefold().replace('’',"'").strip().split()).rstrip('.!?')
    value=re.sub(r'^please\s+','',value)
    value=re.sub(r'^(?:can|could|would) you (?:please )?','',value)
    return re.sub(r'\s+please$','',value)


def bare_stop(text,language='en'):
    return normalize(text,language)=='stop'


def new_timer(text,language='en'):
    value=normalize(text,language) or ''
    return bool(re.match(r'^(?:set|start|create|make) (?:(?:a |an )?new\b|(?:a|an)\b)',value)
        and re.search(r'\btimer\b',value))


def duration(text):
    small=['zero','one','two','three','four','five','six','seven','eight','nine',
        'ten','eleven','twelve','thirteen','fourteen','fifteen','sixteen','seventeen','eighteen','nineteen']
    numbers={word:index for index,word in enumerate(small)}
    for tens,word in ((20,'twenty'),(30,'thirty'),(40,'forty'),(50,'fifty'),(60,'sixty'),(70,'seventy'),(80,'eighty'),(90,'ninety')):
        numbers[word]=tens
        for digit in range(1,10):numbers[word+' '+small[digit]]=tens+digit
    text=text.replace('-',' ')
    match=re.fullmatch(r'([a-z ]+|\d+(?:\.\d+)?) (seconds?|minutes?|hours?)',text)
    if not match:return None
    number=match[1]
    value=float(number) if re.fullmatch(r'\d+(?:\.\d+)?',number) else numbers.get(number)
    if value is None:return None
    seconds=value*({'s':1,'m':60,'h':3600}[match[2][0]])
    return seconds if 0<seconds<=604800 else None


def schedule_edit(text,language='en'):
    """A recognized but unsupported edit has operation=None and must clarify.

    Indefinite creation ('a timer', 'a five minute timer') and explicit 'new'
    remain normal create requests. Definite, bare or named timer references are
    edits; no assistant/model supplied ID makes those references unambiguous.
    """
    value=normalize(text,language)
    if value is None:return None
    if new_timer(text,language):return None
    match=re.fullmatch(r'(?:stop|dismiss|silence) (?:the |my |this |that )?(?:ringing )?(alarm|timer)',value)
    if match:return ScheduleEdit('dismiss',match[1])
    match=re.fullmatch(r'(pause|resume|cancel|delete) (?:the |my |this |that )?(?:(.+?) )?timer',value)
    if match:return ScheduleEdit(match[1],label=match[2])
    match=re.fullmatch(r'(?:set|change|reset|adjust|update) (?:the |my |this |that )?(?:(.+?) )?timer (?:to|for) (.+)',value)
    if match:
        seconds=duration(match[2])
        return ScheduleEdit('set' if seconds is not None else None,seconds=seconds,label=match[1])
    match=re.fullmatch(r'add (.+) to (?:the |my |this |that )?(?:(.+?) )?timer',value)
    if match:
        seconds=duration(match[1])
        return ScheduleEdit('add' if seconds is not None else None,seconds=seconds,label=match[2])
    match=re.fullmatch(r'(?:extend|increase) (?:the |my |this |that )?(?:(.+?) )?timer by (.+)',value)
    if match:
        seconds=duration(match[2])
        return ScheduleEdit('add' if seconds is not None else None,seconds=seconds,label=match[1])
    # Do not let malformed/referential wording invite a guessed create/mutation.
    if re.match(r'^(?:set|change|reset|adjust|update|extend|increase|pause|resume|cancel|delete|add)\b',value) and re.search(r'\btimers?\b',value):
        return ScheduleEdit(None)
    return None
