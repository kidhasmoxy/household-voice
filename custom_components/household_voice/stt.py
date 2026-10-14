"""Dedicated, explicitly enabled capture STT platform.

HA Core 2026.10.1 interfaces:
https://github.com/home-assistant/core/blob/2026.10.1/homeassistant/components/stt/__init__.py
https://github.com/home-assistant/core/blob/2026.10.1/homeassistant/components/stt/models.py

Setup registers configured wrappers only when stt_capture_enabled is true.
The administrator assigns a dedicated pipeline exclusively to its configured
source device. SpeechMetadata contains no source identity. The broadcast runtime
checks visible pipeline assignments before arming. Setup never edits pipelines.

The injected capture callback owns authenticated transfer and subsequent retention.
Optional runtime.inline_capture_enabled and runtime.inline_announcement(text)
enable provisional first-turn buffering. consume_inline requires the exact source,
transcript and task-context capability, and returns raw PCM without calling the
capture callback. Missing feature properties keep provisional capture disabled.
The broadcast runtime uploads only after the question's response matches the
captured transcript; the server must have matching profile/source/room bindings.
Returned upload metadata is not published as entity state. This wrapper
does not set physical capture_verified, and synthetic tests cannot establish it.
Handoff success is not announcement delivery. It must honor cancellation and avoid
logging audio/transcripts. Core's state-updating internal_async_process_audio_stream
is used deliberately; verify this implementation-helper contract on HA upgrades.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
import re

from homeassistant.components import stt
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .capture import CaptureError, CaptureSessionManager, CapturedUtterance

PROFILE_OPTION = "stt_capture_profiles"
HANDOFF_TIMEOUT = 5


async def async_setup_entry(hass, entry, async_add_entities):
    """Register only explicitly enabled source-specific capture proxies.

    Setup never edits pipeline assignments. The broadcast runtime verifies the
    selected pipeline before arming; the administrator assigns each dedicated
    wrapper to its source satellite after reviewing the integration.
    """
    if entry.options.get("stt_capture_enabled") is not True or entry.options.get("native_recording_enabled") is True:
        return
    coordinator = hass.data["household_voice"][entry.entry_id]
    runtime = getattr(coordinator, "broadcast_runtime", None)
    if runtime is None:
        raise CaptureSttConfigurationError("Broadcast runtime must be initialized before STT setup")
    try:
        entities = build_capture_entities(hass, entry.entry_id, entry.options, runtime.async_capture_ready,
            inline_enabled=lambda: getattr(runtime, 'inline_capture_enabled', False) is True,
            inline_parser=lambda text: runtime.inline_announcement(text))
    except CaptureSttProviderNotReady:
        from homeassistant.exceptions import PlatformNotReady
        raise PlatformNotReady("Configured capture STT provider is not ready") from None
    runtime.register_capture_entities(entities)
    async_add_entities(entities)


class CaptureSttConfigurationError(ValueError):
    """Configuration error with no audio, transcript or credential contents."""


class CaptureSttProviderNotReady(CaptureSttConfigurationError):
    """Transient provider startup; HA retries the STT platform automatically."""


def _provider(hass, entity_id):
    try:
        provider = stt.async_get_speech_to_text_entity(hass, entity_id)
    except KeyError:
        return None
    registered = er.async_get(hass).async_get(entity_id)
    if (registered is None or registered.disabled_by is not None
            or registered.platform == "household_voice"
            or not isinstance(provider, stt.SpeechToTextEntity)
            or isinstance(provider, HouseholdCaptureStt)):
        return None
    return provider


def build_capture_entities(hass, config_entry_id: str, options: Mapping,
                           async_capture_ready: Callable[[CapturedUtterance], Awaitable[object]], *,
                           inline_enabled=None, inline_parser=None):
    """Build only; caller must explicitly register and bind exclusive pipelines."""
    profiles = options.get(PROFILE_OPTION, [])
    if not isinstance(profiles, list) or len(profiles) > 64:
        raise CaptureSttConfigurationError("Configure a list of at most 64 dedicated STT profiles")
    if not profiles:
        return []
    if not isinstance(config_entry_id, str) or not config_entry_id or not callable(async_capture_ready):
        raise CaptureSttConfigurationError("A config entry and capture callback are required")
    bindings, selected = {}, []
    registry = dr.async_get(hass)
    for profile in profiles:
        if not isinstance(profile, Mapping) or set(profile) != {"profile_id", "source_device_id", "provider_entity_id"}:
            raise CaptureSttConfigurationError("Each profile requires explicit profile, source-device and provider IDs")
        name, source, provider_id = (profile[key] for key in ("profile_id", "source_device_id", "provider_entity_id"))
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", name) or name in bindings:
            raise CaptureSttConfigurationError("Profile IDs must be explicit and unique")
        if not isinstance(source, str) or not source or source in bindings.values():
            raise CaptureSttConfigurationError("Each profile requires a different registered source device")
        device = registry.async_get(source)
        if device is None or device.disabled_by is not None:
            raise CaptureSttConfigurationError("Source device is missing or disabled")
        if not isinstance(provider_id, str) or not re.fullmatch(r"stt\.[a-z0-9_]+", provider_id):
            raise CaptureSttConfigurationError("Select an exact registered STT provider entity")
        provider = _provider(hass, provider_id)
        if provider is None or not provider.available:
            raise CaptureSttProviderNotReady("STT provider is unavailable, unregistered or another capture wrapper")
        bindings[name] = source
        selected.append((name, source, provider_id, provider))
    manager = CaptureSessionManager(bindings)
    return [HouseholdCaptureStt(hass, config_entry_id, name, source, provider_id,
                               provider, manager, async_capture_ready,
                               inline_enabled=inline_enabled, inline_parser=inline_parser)
            for name, source, provider_id, provider in selected]


class HouseholdCaptureStt(stt.SpeechToTextEntity):
    """Proxy one STT entity with explicitly gated two-turn/inline capture."""
    _attr_should_poll = False

    def __init__(self, hass, entry_id, profile, source, provider_id, provider,
                 manager, async_capture_ready, *, inline_enabled=None, inline_parser=None):
        self.hass = hass
        self.profile_id, self.source_device_id = profile, source
        self.provider_entity_id, self._bound_provider = provider_id, provider
        self.manager, self._capture_ready = manager, async_capture_ready
        self._attr_unique_id = f"{entry_id}-{profile}-capture-stt"
        self._attr_name = f"Household capture {profile}"
        self._removed = False
        self._delivery_tasks = set()
        self.capture_status = "not_armed"
        self._inline_enabled = inline_enabled or (lambda: False)
        self._inline_parser = inline_parser
        self._inline_generation = object()

    def _current_provider(self):
        if self._removed:
            return None
        device = dr.async_get(self.hass).async_get(self.source_device_id)
        provider = _provider(self.hass, self.provider_entity_id)
        if device is None or device.disabled_by is not None or provider is not self._bound_provider or not provider.available:
            self.manager.inline.invalidate(self.profile_id)
            return None
        return provider

    @property
    def available(self):
        return self._current_provider() is not None

    @property
    def supported_languages(self):
        return list(self._bound_provider.supported_languages)

    @property
    def supported_formats(self):
        return list(self._bound_provider.supported_formats)

    @property
    def supported_codecs(self):
        return list(self._bound_provider.supported_codecs)

    @property
    def supported_bit_rates(self):
        return list(self._bound_provider.supported_bit_rates)

    @property
    def supported_sample_rates(self):
        return list(self._bound_provider.supported_sample_rates)

    @property
    def supported_channels(self):
        return list(self._bound_provider.supported_channels)

    @property
    def audio_processing(self):
        return self._bound_provider.audio_processing

    def check_metadata(self, metadata):
        provider = self._current_provider()
        return provider is not None and provider.check_metadata(metadata)

    def arm(self, trusted_source: str, *, ttl=30):
        """Only a future authenticated, source-bound adapter may call this."""
        if not self.available or self._delivery_tasks:
            raise CaptureError("Dedicated STT profile is unavailable or handing off a capture")
        return self.manager.arm(self.profile_id, trusted_source, ttl=ttl)

    def cancel(self, trusted_source: str, *, nonce=None):
        changed = self.manager.cancel(self.profile_id, trusted_source, nonce=nonce)
        for task, task_nonce in tuple(self._delivery_tasks):
            if nonce is None or nonce == task_nonce:
                task.cancel()
                changed = True
        return changed

    def consume_inline(self, trusted_source, exact_transcript):
        """Return raw first-turn PCM only to its exact current router context."""
        if (trusted_source != self.source_device_id or not self.available
                or not self._inline_is_enabled()):
            self.manager.inline.invalidate(self.profile_id)
            return None
        return self.manager.inline.consume(self.profile_id, trusted_source,
            exact_transcript, self._inline_generation)

    def _inline_is_enabled(self):
        try:
            return self._inline_enabled() is True
        except Exception:
            return False

    async def async_process_audio_stream(self, metadata, stream):
        provider = self._current_provider()
        if provider is None or not provider.check_metadata(metadata):
            self.cancel(self.source_device_id)
            self.capture_status = "provider_unavailable_or_unsupported"
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
        outcome = await self.manager.process(self.profile_id, metadata, stream,
            provider.internal_async_process_audio_stream,
            provisional=self._inline_is_enabled(),
            inline_parser=self._inline_parser, generation=self._inline_generation)
        if self._current_provider() is None or not self._inline_is_enabled():
            self.manager.inline.invalidate(self.profile_id)
        self.capture_status = outcome.capture_status
        if outcome.capture is not None and not self._removed:
            # Keep the result local: no state attribute, log, background retry or
            # retained transcript/audio. Callback cancellation must be cooperative.
            async def handoff():
                await self._capture_ready(outcome.capture)
            task = asyncio.create_task(handoff())
            task.add_done_callback(lambda finished: None if finished.cancelled() else finished.exception())
            tracked = (task, outcome.capture.nonce)
            self._delivery_tasks.add(tracked)
            try:
                async with asyncio.timeout(HANDOFF_TIMEOUT):
                    await asyncio.shield(task)
                self.capture_status = "capture_handed_off"
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    task.cancel()
                    raise
                self.capture_status = "capture_handoff_cancelled"
            except Exception:
                # Ordinary successful STT stays successful when capture delivery
                # fails; never log an exception that may contain captured text.
                task.cancel()
                self.capture_status = "capture_handoff_failed"
            finally:
                if not task.done():
                    task.cancel()
                self._delivery_tasks.discard(tracked)
        return outcome.speech_result

    async def async_will_remove_from_hass(self):
        self._removed = True
        self.cancel(self.source_device_id)
        await super().async_will_remove_from_hass()
