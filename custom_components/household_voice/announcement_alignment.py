"""Bounded forced alignment; ordinary ASR word timestamps are not accepted."""
import asyncio
import base64
import io
import json
import math
import re
import wave

import aiohttp

from .capture import CapturedUtterance, MAX_BYTES, MIN_BYTES, RATE, WIDTH
from .options import alignment_url

MAX_RESPONSE_BYTES = 131072
ALIGNMENT_METHOD = 'ctc_forced_v1'
MODEL_REVISION = '22aad52d435eb6dbaf354bdad9b0da84ce7d6156'
# Conservative rejection threshold, not a calibrated acoustic-accuracy claim.
MIN_CONFIDENCE = .8


class AnnouncementAlignmentError(ValueError):
    """Fixed public error; no transcript, endpoint, response or credentials."""


def _words(text):
    return re.findall(r"\w+(?:['’]\w+)*", text.casefold().replace('’', "'"))


def _number(value):
    return type(value) in (float, int) and math.isfinite(value)


def _confidence(value):
    return _number(value) and MIN_CONFIDENCE <= value <= 1


def _wav(pcm):
    output = io.BytesIO()
    with wave.open(output, 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(WIDTH)
        stream.setframerate(RATE)
        stream.writeframes(pcm)
    return output.getvalue()


class AnnouncementAlignmentAdapter:
    def __init__(self, session, *, endpoint, model, api_key='', timeout=15):
        try:
            self._endpoint = alignment_url(endpoint) + '/v1/audio/transcriptions'
            if (not isinstance(model, str) or not model.strip() or len(model)>512
                    or model != model.strip() or any(ord(c)<32 or ord(c)==127 for c in model)
                    or not isinstance(api_key, str) or len(api_key)>4096
                    or any(ord(c)<32 or ord(c)==127 for c in api_key)
                    or not _number(timeout) or not 0<timeout<=15):
                raise ValueError()
        except (ValueError, TypeError):
            raise AnnouncementAlignmentError('alignment_configuration_invalid') from None
        self._session, self._model, self._key, self._timeout = session, model, api_key, timeout

    async def align_announcement(self, inline_capture):
        """Send one bounded current utterance, never retry or return full audio."""
        try:
            from .conversation_router import inline_announcement
            pcm, transcript, body = inline_capture.pcm, inline_capture.transcript, inline_capture.body
            if (not isinstance(pcm, bytes) or not MIN_BYTES<=len(pcm)<=MAX_BYTES or len(pcm)%WIDTH
                    or not isinstance(transcript, str) or len(transcript)>8000
                    or not isinstance(body, str) or not body or not transcript.endswith(body)
                    or inline_announcement(transcript) != body
                    or not _number(inline_capture.duration_seconds)
                    or abs(inline_capture.duration_seconds-len(pcm)/(RATE*WIDTH))>1/RATE):
                raise ValueError()
            prefix = transcript[:-len(body)]
            prefix_words, body_words = _words(prefix), _words(body)
            expected = _words(transcript)
            if (not prefix_words or not body_words or prefix_words+body_words != expected
                    or any(not re.fullmatch(r"[a-z]+(?:'[a-z]+)*", word) for word in expected)):
                raise ValueError()
        except (AttributeError, TypeError, ValueError):
            raise AnnouncementAlignmentError('alignment_input_invalid') from None
        form = aiohttp.FormData()
        form.add_field('file', _wav(pcm), filename='announcement.wav', content_type='audio/wav')
        form.add_field('model', self._model)
        # This is the exact first STT result, not a transcription hint or a new
        # model instruction. The dedicated service aligns this text to the PCM.
        form.add_field('prompt', transcript)
        form.add_field('alignment_method', ALIGNMENT_METHOD)
        form.add_field('body_word_index', str(len(prefix_words)))
        form.add_field('response_format', 'verbose_json')
        form.add_field('timestamp_granularities[]', 'word')
        form.add_field('temperature', '0')
        headers = {'Authorization':'Bearer '+self._key} if self._key else {}
        try:
            async with asyncio.timeout(self._timeout):
                async with self._session.post(self._endpoint, data=form, headers=headers, allow_redirects=False) as response:
                    if response.status != 200:
                        raise AnnouncementAlignmentError('alignment_unavailable')
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(8192):
                        if len(raw)+len(chunk)>MAX_RESPONSE_BYTES:
                            raise AnnouncementAlignmentError('alignment_response_invalid')
                        raw.extend(chunk)
                    payload = json.loads(raw)
        except asyncio.CancelledError:
            raise
        except AnnouncementAlignmentError:
            raise
        except Exception:
            raise AnnouncementAlignmentError('alignment_unavailable') from None
        try:
            if (not isinstance(payload, dict)
                    or payload.get('alignment_method') != ALIGNMENT_METHOD
                    or payload.get('text') != transcript
                    or payload.get('model') != self._model
                    or payload.get('model_revision') != MODEL_REVISION
                    or type(payload.get('sample_rate')) is not int or payload['sample_rate'] != RATE
                    or type(payload.get('sample_count')) is not int or payload['sample_count'] != len(pcm)//WIDTH
                    or payload.get('transcript_match') is not True
                    or not _confidence(payload.get('confidence'))
                    or _words(payload['text']) != expected):
                raise ValueError()
            words = payload.get('words')
            if not isinstance(words, list) or len(words) != len(expected):
                raise ValueError()
            end = 0.0
            for item, token in zip(words, expected, strict=True):
                if (not isinstance(item, dict) or not isinstance(item.get('word'), str)
                        or _words(item['word']) != [token]
                        or not _confidence(item.get('score'))
                        or not _number(item.get('start')) or not _number(item.get('end'))
                        or not 0<=end<=item['start']<item['end']<=inline_capture.duration_seconds):
                    raise ValueError()
                end = item['end']
            boundary = payload.get('boundary')
            body_index = len(prefix_words)
            body_start = words[body_index]['start']
            preceding_end = words[body_index-1]['end']
            if (not isinstance(boundary, dict)
                    or type(boundary.get('word_index')) is not int or boundary['word_index'] != body_index
                    or not _confidence(boundary.get('confidence'))
                    or not _number(boundary.get('frame_seconds')) or not 0 < boundary['frame_seconds'] <= .1
                    or not _number(boundary.get('start')) or abs(boundary['start']-body_start) > 1/RATE
                    or not _number(boundary.get('preceding_word_end')) or abs(boundary['preceding_word_end']-preceding_end) > 1/RATE
                    or not _number(boundary.get('gap_seconds')) or boundary['gap_seconds'] <= 0
                    or abs(boundary['gap_seconds']-(body_start-preceding_end)) > 1/RATE):
                raise ValueError()
            # The reported frame gap is consistency data, not an error margin.
            # Round toward the body: padding must never reintroduce the command.
            first_sample = math.ceil(body_start*RATE)
            cropped = pcm[first_sample*WIDTH:]
            if not MIN_BYTES<=len(cropped)<=MAX_BYTES:
                raise ValueError()
            return CapturedUtterance(profile_id=inline_capture.profile_id, source_device=inline_capture.source_device,
                nonce=inline_capture.nonce, wav_base64=base64.b64encode(_wav(cropped)).decode('ascii'),
                transcript=body, duration_seconds=len(cropped)/(RATE*WIDTH))
        except (KeyError, IndexError, AttributeError, TypeError, ValueError, OverflowError):
            raise AnnouncementAlignmentError('alignment_response_invalid') from None
