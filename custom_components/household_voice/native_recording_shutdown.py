"""Bounded observation of owned cleanup, never abandonment of running writers."""
import asyncio


class ShutdownIncomplete(ValueError):
    def __init__(self): super().__init__('shutdown_incomplete')


class OwnedShutdown:
    def __init__(self,timeout=3):
        self.timeout=timeout
        self.tasks={}

    async def run(self,steps,finalize):
        # Completed failures may be explicitly retried. Unfinished jobs remain
        # owned and are reused; timeout/caller cancellation never cancels them.
        for name,step in steps.items():
            previous=self.tasks.get(name)
            if previous is None or (previous.done() and (previous.cancelled() or previous.exception() is not None)):
                self.tasks[name]=asyncio.create_task(step())
        try:
            producers=[self.tasks[name] for name in steps]
            if producers:
                _,pending=await asyncio.wait(producers,timeout=self.timeout)
                if pending:raise ShutdownIncomplete()
                if any(task.cancelled() or task.exception() is not None for task in producers):
                    raise ShutdownIncomplete()
            # Run after producers quiesce, so newly queued executor writes cannot
            # appear after a premature snapshot of the client's pending futures.
            if 'final' not in self.tasks:
                self.tasks['final']=asyncio.create_task(finalize())
            final=self.tasks['final']
            if final.done() and (final.cancelled() or final.exception() is not None):
                final=self.tasks['final']=asyncio.create_task(finalize())
            _,pending=await asyncio.wait({final},timeout=self.timeout)
            if pending or final.cancelled() or final.exception() is not None:raise ShutdownIncomplete()
        except asyncio.CancelledError:
            raise ShutdownIncomplete() from None
