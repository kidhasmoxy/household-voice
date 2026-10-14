"""Durable write-ahead holds. Inject public HA Store; contains no audio/text.

An entry-independent singleton Store is required so two integration entries or
reloads cannot create independent microphone owners. Unknown holds never expire.
"""
import asyncio


class SourceJournal:
    def __init__(self, store):
        self.store = store
        self.holds = {}
        self.lock = asyncio.Lock()
        self.loaded = False
        self.failed = False

    async def load(self):
        async with self.lock:
            if self.loaded: return
            try:
                data = await self.store.async_load()
                if data is not None:
                    if (not isinstance(data,dict) or set(data) != {'version','holds'} or
                            type(data['version']) is not int or data['version'] != 1 or
                            not isinstance(data['holds'],dict) or len(data['holds']) > 256 or
                            any(not isinstance(k,str) or not 0<len(k)<=255 or
                                not isinstance(v,str) or not 0<len(v)<=255 for k,v in data['holds'].items())):
                        raise ValueError('journal_invalid')
                    self.holds = dict(data['holds'])
                self.loaded = True
            except BaseException:
                self.failed = True
                raise

    async def _save(self, holds):
        work = asyncio.create_task(self.store.async_save({'version':1,'holds':holds}))
        try: await asyncio.shield(work)
        except asyncio.CancelledError:
            # Keep the serialization lock until write outcome is known. A backup
            # must not race a canceled write still persisting a new source owner.
            self.failed = True
            while not work.done():
                try: await asyncio.shield(work)
                except asyncio.CancelledError: continue
                except Exception: break
            if not work.cancelled(): work.exception()
            raise
        except Exception:
            self.failed = True
            raise ValueError('journal_failed') from None

    async def reserve(self, source, operation):
        async with self.lock:
            if not self.loaded or self.failed: raise ValueError('journal_unavailable')
            if source in self.holds or len(self.holds)>=256: raise ValueError('source_held')
            for value in (source,operation):
                if not isinstance(value,str) or not 0<len(value)<=255: raise ValueError('identity_invalid')
            proposed = {**self.holds,source:operation}
            await self._save(proposed)
            self.holds = proposed

    async def release(self, source, operation):
        async with self.lock:
            if not self.loaded or self.failed: raise ValueError('journal_unavailable')
            if self.holds.get(source) != operation: raise ValueError('owner_mismatch')
            proposed = dict(self.holds); proposed.pop(source)
            await self._save(proposed)
            self.holds = proposed

    async def quiesce(self):
        async with self.lock:
            if self.failed: raise ValueError('journal_unavailable')


SHARED_KEY='_household_voice_recording_source_journal'

async def async_get_shared_journal(hass):
    """One writer across recording modes/reloads; load errors only disable capture."""
    from homeassistant.helpers.storage import Store
    journal=hass.data.get(SHARED_KEY)
    if journal is None:
        journal=SourceJournal(Store(hass,1,'household_voice_native_recording_sources'))
        hass.data[SHARED_KEY]=journal
    if not journal.loaded and not journal.failed:
        try:await journal.load()
        except Exception:pass # Fixed fail-closed state; alarms remain available.
    return journal


def source_is_held(hass,device):
    if not isinstance(device,str):return False
    journal=getattr(hass,'data',{}).get(SHARED_KEY)
    return bool(journal is not None and (journal.failed or not journal.loaded or device in journal.holds))
