import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.limits import Clock, QuotaExhausted
from app.schemas import ScanState, ScanStatus
from app.store import BlogState, Store, post_id
from app.tumblr import PAGE_SIZE, InvalidCredentials, TumblrClient, TumblrError, is_original

logger = logging.getLogger(__name__)


class TooManyScans(Exception):
    '''The queue is full.'''


@dataclass
class Run:
    blog: str
    last_poll: float
    status: ScanState = 'queued'
    task: asyncio.Task | None = field(default=None, repr=False)


def _timestamp(post: dict[str, Any]) -> int:
    return post.get('timestamp') or 0


class ScanManager:
    '''Runs scans as background tasks. Results and positions live in the
    store, so only the tasks themselves are lost on restart. Run a single
    worker, since runs are tracked in this process.'''

    def __init__(
        self,
        client: TumblrClient,
        store: Store,
        max_concurrent: int,
        max_pending: int,
        cooldown: float,
        idle_timeout: float,
        clock: Clock = time.time,
    ):
        self.client = client
        self.store = store
        self.max_pending = max_pending
        self.cooldown = cooldown
        self.idle_timeout = idle_timeout
        self.clock = clock
        self.runs: dict[str, Run] = {}
        self.semaphore = asyncio.Semaphore(max_concurrent)

    def _derive_status(self, state: BlogState) -> ScanState:
        run = self.runs.get(state.blog)
        if run is not None:
            return run.status
        if state.error:
            return 'failed'
        if state.synced_until is not None and state.cursor is None:
            return 'complete'
        return 'paused'

    def status(self, blog: str) -> ScanStatus | None:
        state = self.store.get(blog)
        if state is None:
            return None
        scanned = state.scanned if state.total is None else min(state.scanned, state.total)
        return ScanStatus(
            id=blog,
            blog=blog,
            status=self._derive_status(state),
            scanned=scanned,
            total=state.total,
            originals=self.store.count_posts(blog),
            new_originals=state.new_originals,
            error=state.error,
        )

    def touch(self, blog: str) -> None:
        '''Someone is still watching this scan.'''
        run = self.runs.get(blog)
        if run is not None:
            run.last_poll = self.clock()

    def is_current(self, blog: str) -> bool:
        '''True if starting a scan now would spend API calls for nothing.'''
        if blog in self.runs:
            return True
        state = self.store.get(blog)
        return (
            state is not None
            and self._derive_status(state) == 'complete'
            and state.last_synced_at is not None
            and self.clock() - state.last_synced_at < self.cooldown
        )

    def has_capacity(self) -> bool:
        return len(self.runs) < self.max_pending

    def start(self, blog: str) -> None:
        if not self.has_capacity():
            raise TooManyScans
        self.store.ensure(blog)
        run = Run(blog=blog, last_poll=self.clock())
        self.runs[blog] = run
        run.task = asyncio.create_task(self._run(run))

    async def stop(self, blog: str) -> None:
        run = self.runs.get(blog)
        if run is None or run.task is None:
            return
        run.task.cancel()
        await asyncio.gather(run.task, return_exceptions=True)

    async def _run(self, run: Run) -> None:
        state = self.store.ensure(run.blog)
        state.error = None
        state.new_originals = 0
        self.store.save(state)
        # Only posts found after a finished scan count as new
        is_rescan = state.synced_until is not None
        try:
            async with self.semaphore:
                run.status = 'running'
                await self._sync(run, state, is_rescan)
        except InvalidCredentials:
            logger.error('Tumblr rejected TUMBLR_CONSUMER_KEY')
            self._fail(state, 'The server is misconfigured. Try again later.')
        except TumblrError as error:
            logger.warning('Scan of %s failed: %s', run.blog, error)
            self._fail(state, str(error))
        except QuotaExhausted:
            logger.warning('Daily Tumblr quota used up during scan of %s', run.blog)
            self._fail(state, 'Daily Tumblr limit reached. Try again tomorrow.')
        except Exception:
            logger.exception('Scan of %s crashed', run.blog)
            self._fail(state, 'Something went wrong during the scan.')
        finally:
            self.runs.pop(run.blog, None)

    def _fail(self, state: BlogState, message: str) -> None:
        state.error = message
        self.store.save(state)

    async def _sync(self, run: Run, state: BlogState, is_rescan: bool) -> None:
        # An interrupted walk is finished first, then a fresh one from the
        # top collects anything published since. That one costs a single
        # call when there is nothing new.
        if state.cursor is not None and not await self._walk(run, state, is_rescan):
            return
        await self._walk(run, state, is_rescan)

    async def _walk(self, run: Run, state: BlogState, is_rescan: bool) -> bool:
        '''Pages from newest to oldest until reaching what is already known.
        Returns False if nobody is watching any more and the walk paused.'''
        floor = state.synced_until
        seen: set[str] = set()

        while True:
            if self.clock() - run.last_poll > self.idle_timeout:
                logger.info('Pausing scan of %s, nobody is watching', run.blog)
                return False

            posts, state.total = await self.client.fetch_page(run.blog, state.cursor)
            unseen = [post for post in posts if post_id(post) not in seen]
            seen.update(post_id(post) for post in unseen)
            fresh = [post for post in unseen if floor is None or _timestamp(post) > floor]
            originals = [post for post in fresh if is_original(post)]
            state.scanned += len(fresh)

            # A pinned post can be far older than its neighbours
            dated = [_timestamp(post) for post in posts if _timestamp(post) and not post.get('is_pinned')]
            if state.cursor is None and dated:
                state.walk_top = max(dated)
            bottom = min(dated) if dated else None

            finished = bottom is None or (floor is not None and bottom <= floor)
            if not finished and not unseen:
                full_second = len(posts) >= PAGE_SIZE and state.cursor == bottom + 1
                if full_second:
                    # A whole page shares one second, so there is no way
                    # through it except stepping past that second
                    state.cursor = bottom
                    self._save_progress(state, originals, is_rescan)
                    continue
                finished = True

            if finished:
                state.synced_until = max(state.walk_top or 0, floor or 0)
                state.cursor = None
                state.walk_top = None
                state.last_synced_at = self.clock()
                self._save_progress(state, originals, is_rescan)
                return True

            # One second past the oldest post, so a post sharing its
            # timestamp on the next page is not skipped
            state.cursor = bottom + 1
            self._save_progress(state, originals, is_rescan)

    def _save_progress(self, state: BlogState, originals: list[dict[str, Any]], is_rescan: bool) -> None:
        added = self.store.save_page(state, originals)
        if is_rescan and added:
            state.new_originals += added
            self.store.save(state)

    async def close(self) -> None:
        tasks = [run.task for run in self.runs.values() if run.task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
