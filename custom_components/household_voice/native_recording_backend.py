"""Review draft. No production registration, recording, or playback on import.

Dependencies are the frozen canonical RecordingSession and worker GrantBook.
Only the HA adapter may supply trusted source/context/event inputs.
"""
import asyncio
from dataclasses import dataclass, field
import time

from .recording_session import RecordingSession
from .recording_protocol import GrantBook, VERSION


class BackendRejected(ValueError):
    """Fixed categories only; never include source, transcript or credentials."""


@dataclass
class Operation:
    session: object = field(repr=False)
    context: object = field(repr=False)
    book: object = field(repr=False)
    changed: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    grant: dict | None = field(default=None, repr=False)
    question: object = field(default=None, repr=False)
    started: bool = False
    failed: bool = False


class RecordingBackend:
    """One HA-loop authority; durable journal holds survive unload/restart.

    journal.reserve(source, operation) persists before returning and rejects any
    existing hold. journal.release requires matching identity and persists first.
    No automatic unknown-hold expiry. A failed service cancel is not mic silence.
    """
    def __init__(self, *, journal, ask, fence, validate_source, context_factory,
                 clock=time.monotonic, timeout=35):
        self.journal, self.ask, self.fence = journal, ask, fence
        self.validate_source, self.context_factory = validate_source, context_factory
        self.clock, self.timeout = clock, timeout
        self.connection = self.book = self.send = None
        self.operations = {}
        self.tasks = set()
        self.questions = set()
        self.paused = False
        self.closed = False

    def register(self, connection, send, *, admin, loaded_entries):
        if not admin: raise BackendRejected('administrator_required')
        if self.closed or self.paused: raise BackendRejected('backend_unavailable')
        if type(loaded_entries) is int and loaded_entries==0:raise BackendRejected('backend_unavailable')
        if type(loaded_entries) is not int or loaded_entries != 1:
            raise BackendRejected('exactly_one_entry_required')
        if self.connection is not None: raise BackendRejected('previous_connection_busy')
        self.connection, self.send = connection, send
        self.book = GrantBook(clock=self.clock)
        return {'connection_id': self.book.connection_id}

    def disconnect(self, connection):
        if connection is not self.connection: return
        self.book.disconnect()
        for op in self.operations.values(): self._fail(op)
        self.connection = self.send = self.book = None

    def _fail(self, op):
        op.failed = True
        try: op.session.cancel()
        except ValueError: pass
        op.changed.set()

    def _rotate_completed_connection(self,book):
        # Rotation is evidence of durable question disposal, not wire delivery.
        if (VERSION<2 or self.paused or self.closed or book is not self.book or self.operations
                or self.journal.holds or len(book.records)<120
                or any(row.phase!='consumed' for row in book.records.values())):
            return
        connection=self.connection
        try:self.send({'type':'rotate','connection_id':book.connection_id})
        finally:self.disconnect(connection)

    def state(self, source, entity, state, context):
        op = self.operations.get(source)
        if op is None: return
        try:
            op.session.state_event(source_device_id=source, satellite_entity_id=entity,
                                   state=state, context_id=context)
            if state == 'responding' and context == op.context.id and op.grant is None:
                op.grant = op.book.issue(op.session, ttl_seconds=self.timeout)
                self.send({'type': 'grant', 'grant': op.grant})
        except Exception: self._fail(op)
        op.changed.set()

    def recording_started(self, source):
        op = self.operations.get(source)
        if op is None: return
        try: op.session.recording_started(source_device_id=source)
        except ValueError: self._fail(op)
        op.changed.set()

    def source_changed(self, source):
        if op := self.operations.get(source): self._fail(op)

    def frame(self, connection, frame):
        if connection is not self.connection or self.book is None:
            raise BackendRejected('wrong_connection')
        try:
            result = self.book.receive(frame, connection_id=self.book.connection_id)
        except ValueError:
            # Malformed/replayed frames invalidate connection, never expose content.
            self.disconnect(connection)
            raise BackendRejected('frame_rejected') from None
        for op in self.operations.values(): op.changed.set()
        return result

    async def record(self, source, entity, operation_id):
        if self.paused or self.closed or self.book is None:
            raise BackendRejected('backend_unavailable')
        # Drain toward rotation before the hard 128-grant tombstone limit. This
        # check occurs before any source reservation or native microphone action.
        if self.book.closed or len(self.book.records)>=120:
            raise BackendRejected('worker_capacity')
        if source in self.operations: raise BackendRejected('source_busy')
        if len(self.operations)>=4: raise BackendRejected('worker_busy')
        if not self.validate_source(source, entity): raise BackendRejected('source_invalid')
        task = asyncio.current_task()
        self.tasks.add(task)
        context = self.context_factory()
        session = RecordingSession(operation_id=operation_id, source_device_id=source,
            satellite_entity_id=entity, question_context_id=context.id,
            timeout_seconds=self.timeout, clock=self.clock)
        op = Operation(session, context, self.book)
        self.operations[source] = op
        reserved = False
        try:
            await self.journal.reserve(source, operation_id)
            reserved = True
            if self.paused or self.closed or self.book is not op.book or not self.validate_source(source, entity):
                raise BackendRejected('backend_unavailable')
            async def ask():
                op.started = True
                try:
                    result = await self.ask(entity, context)
                    session.question_result(context_id=context.id, sentence=result.get('sentence'))
                except asyncio.CancelledError:
                    self._fail(op)
                    raise
                except Exception: self._fail(op)
                finally: op.changed.set()
            op.question = asyncio.create_task(ask())
            self.questions.add(op.question)
            def question_finished(task):
                self.questions.discard(task)
                if not task.cancelled(): task.exception()
            op.question.add_done_callback(question_finished)
            async with asyncio.timeout(self.timeout):
                while True:
                    op.changed.clear()
                    if op.failed: raise BackendRejected('recording_failed')
                    ready = session.ready_revision()
                    if (ready is not None and op.grant is not None and
                            op.book.records[op.grant['grant_id']].phase == 'barrier'):
                        # Public HA event marker drains already-fired synchronous
                        # callbacks (including nested dispatch), not all HA tasks.
                        await self.fence()
                        if op.failed or not self.validate_source(source, entity):
                            raise BackendRejected('source_changed')
                        # No await between latest revision and one-use consume.
                        answer = op.book.consume(op.grant['grant_id'],
                                                 expected_revision=session.ready_revision())
                        await self.journal.release(source, operation_id)
                        reserved = False
                        return answer
                    await op.changed.wait()
        except asyncio.CancelledError:
            self._fail(op)
            raise
        except Exception:
            self._fail(op)
            raise BackendRejected('recording_failed') from None
        finally:
            if op.grant and self.send and self.book is op.book and not session.source_release_proven:
                try: self.send({'type':'cancel', 'grant_id':op.grant['grant_id'],
                               'connection_id':op.book.connection_id})
                except Exception: pass
            if op.question is not None and not op.question.done(): op.question.cancel()
            if op.question is not None:
                # Bounded cancellation; an uncooperative service retains hold.
                await asyncio.wait({op.question}, timeout=1)
            if reserved and not op.started:
                await self.journal.release(source, operation_id)
            self.operations.pop(source, None)
            self.tasks.discard(task)
            if session.source_release_proven and not reserved:
                try:self._rotate_completed_connection(op.book)
                except Exception:pass # Recording completion is already durable.

    async def pause(self):
        """Backup blocks new work and drains ours; unresolved holds remain durable."""
        self.paused = True
        # Close the control epoch without claiming the source microphone stopped.
        if self.connection is not None:
            connection=self.connection
            try:
                if VERSION>=2 and self.send:
                    self.send({'type':'unavailable','connection_id':self.book.connection_id})
            except Exception:pass
            finally:self.disconnect(connection)
        pending = tuple(self.tasks)
        for op in self.operations.values(): self._fail(op)
        for task in pending: task.cancel()
        if pending:
            _, not_done = await asyncio.wait(pending, timeout=2)
            if not_done: raise BackendRejected('shutdown_incomplete')
        questions = tuple(self.questions)
        for question in questions: question.cancel()
        if questions:
            _, not_done = await asyncio.wait(questions, timeout=1)
            if not_done: raise BackendRejected('shutdown_incomplete')
        try: await self.journal.quiesce()
        except ValueError: raise BackendRejected('journal_unavailable') from None

    def resume(self):
        if not self.closed: self.paused = False

    async def unload(self):
        self.closed = True
        try: await self.pause()
        finally:
            if self.connection is not None: self.disconnect(self.connection)
