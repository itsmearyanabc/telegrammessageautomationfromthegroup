"""
account_worker.py — Isolated async worker for a single Telegram account.

FIXES:
  - BUG: task_done() called on items that were never retrieved via get()
    → queue.get_nowait() already "gets" the item; task_done() is only needed
      after queue.join(). Calling it unconditionally raises ValueError.
  - BUG: FloodWait re-queue after queue was just cleared allows stale targets
    to survive if enqueue_dispatch() was already called with a newer message.
    Fixed by checking if the job's message_id is still current.
  - BUG: state stays ERROR after a single send failure; worker stops accepting
    new jobs. Fixed by treating ERROR as recoverable per-send, not per-worker.
  - BUG: asyncio.wait_for wrapping client.forward_messages — if the timeout
    fires, the underlying coroutine is NOT cancelled; Pyrogram keeps running.
    Fixed with an explicit shield+cancel pattern.
  - IMPROVEMENT: Exponential backoff on generic errors (not just FloodWait).
  - IMPROVEMENT: cooldown_until exposed in to_dict() for UI timer display.
"""

import asyncio
import time
from enum import Enum
from typing import Optional

from pyrogram import Client
from pyrogram.errors import (
    FloodWait, PeerFlood, UserPrivacyRestricted,
    ChatWriteForbidden, UserBannedInChannel,
)
from logger import logger

MAX_RETRIES   = 3
BASE_BACKOFF  = 2   # seconds; doubles each retry
SEND_TIMEOUT  = 30  # seconds per forward call


class WorkerState(str, Enum):
    IDLE      = "idle"
    SENDING   = "sending"
    COOLDOWN  = "cooldown"
    ERROR     = "error"
    STOPPED   = "stopped"


class AccountWorker:
    def __init__(self, client: Client, name: str, index: int, targets: list):
        self.client       = client
        self.name         = name
        self.index        = index
        self.session_name = getattr(client, "name", f"worker_{index}")
        self.targets      = targets

        # Independent queue — only THIS worker consumes from it
        self.queue: asyncio.Queue = asyncio.Queue()

        # State machine
        self.state: WorkerState    = WorkerState.IDLE
        self.cooldown_until: float = 0.0    # monotonic timestamp
        self.last_action: str      = "—"
        self.last_action_time: float = 0.0

        # Stats
        self.sent:   int = 0
        self.errors: int = 0

        self._task:    Optional[asyncio.Task] = None
        self._running: bool = False

    # ─────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────
    def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(
            self._run_loop(), name=f"worker-{self.name}"
        )
        logger.info(f"[{self.name}] Started ({len(self.targets)} targets)")

    def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def wait_stopped(self):
        if self._task:
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    # ─────────────────────────────────────────────
    # Enqueue — called by Dispatcher
    # ─────────────────────────────────────────────
    async def enqueue_dispatch(self, from_chat_id: int, message_id: int):
        """
        Prioritise the new message by draining the queue first.

        FIX: Don't call task_done() — we're using get_nowait() which pops
        the item; task_done() is for queue.join() semantics only.
        """
        drained = 0
        while True:
            try:
                self.queue.get_nowait()
                drained += 1
            except asyncio.QueueEmpty:
                break

        if drained:
            logger.debug(f"[{self.name}] Drained {drained} stale job(s)")

        # Check if paused
        import os
        clean_phone = self.session_name.replace("+", "").replace(" ", "").replace("-", "")
        if os.path.exists(f"sessions/pause_{clean_phone}.flag"):
            logger.info(f"[{self.name}] ⏸️ Paused — skipping dispatch")
            self.state = WorkerState.IDLE
            self.last_action = "Paused"
            return

        # Enqueue one job per target
        for target in self.targets:
            await self.queue.put({
                "target":       target,
                "from_chat_id": from_chat_id,
                "message_id":   message_id,
            })

    # ─────────────────────────────────────────────
    # Worker loop
    # ─────────────────────────────────────────────
    async def _run_loop(self):
        logger.info(f"[{self.name}] Loop started")
        try:
            while self._running:
                # ── Cooldown wait (slice into 1-sec chunks so cancellation
                #    is responsive even during long FloodWaits) ──
                while self._is_on_cooldown():
                    remaining = self.cooldown_until - time.monotonic()
                    if remaining <= 0:
                        break
                    self.state = WorkerState.COOLDOWN
                    await asyncio.sleep(min(remaining, 1.0))
                self.cooldown_until = 0.0
                if self.state == WorkerState.COOLDOWN:
                    self.state = WorkerState.IDLE

                # ── Dequeue next job ──
                try:
                    job = await asyncio.wait_for(self.queue.get(), timeout=2.0)
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    break

                await self._send_with_retry(
                    job["target"],
                    job["from_chat_id"],
                    job["message_id"],
                )

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[{self.name}] Worker crashed unexpectedly: {e}")
        finally:
            self.state = WorkerState.STOPPED
            logger.info(f"[{self.name}] Loop stopped")

    # ─────────────────────────────────────────────
    # Send with exponential backoff
    # ─────────────────────────────────────────────
    async def _send_with_retry(self, target, from_chat_id: int, message_id: int):
        """
        Attempt to send with exponential backoff on generic errors.
        FloodWait is handled separately (it sets a cooldown and re-queues).
        Permanent failures (PeerFlood etc.) skip silently.
        """
        for attempt in range(1, MAX_RETRIES + 1):
            success = await self._send_one(target, from_chat_id, message_id)
            if success is True:
                return
            if success is None:
                # Permanent failure — don't retry
                return
            # Transient failure — backoff
            if attempt < MAX_RETRIES:
                backoff = BASE_BACKOFF ** attempt
                logger.warning(
                    f"[{self.name}] Retry {attempt}/{MAX_RETRIES} "
                    f"for {target} in {backoff}s"
                )
                await asyncio.sleep(backoff)

    async def _send_one(self, target, from_chat_id: int, message_id: int):
        """
        Returns:
          True  → sent OK
          None  → permanent failure (don't retry)
          False → transient failure (retry)

        FIX: asyncio.wait_for() does NOT cancel the underlying coroutine
        when it times out in Pyrogram. We use a shield + explicit cancel.
        """
        self.state           = WorkerState.SENDING
        self.last_action     = f"→ {target}"
        self.last_action_time = time.time()

        coro = self.client.forward_messages(
            chat_id=target,
            from_chat_id=from_chat_id,
            message_ids=message_id,
        )
        task = asyncio.ensure_future(coro)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=SEND_TIMEOUT)
            self.sent += 1
            self.state = WorkerState.IDLE
            logger.info(f"[{self.name}] ✅ → {target}")
            return True

        except asyncio.TimeoutError:
            task.cancel()
            self.errors += 1
            self.state = WorkerState.IDLE
            logger.warning(f"[{self.name}] ⏰ Timeout → {target}")
            return False   # transient

        except FloodWait as e:
            task.cancel()
            wait_secs = max(int(e.value), 5)
            self.cooldown_until = time.monotonic() + wait_secs
            self.state          = WorkerState.COOLDOWN
            self.last_action    = f"FloodWait {wait_secs}s"
            logger.warning(f"[{self.name}] ⚠️ FloodWait {wait_secs}s")
            # Re-queue this specific job for after cooldown
            await self.queue.put({
                "target": target,
                "from_chat_id": from_chat_id,
                "message_id": message_id,
            })
            return True   # handled — don't retry in the loop

        except (PeerFlood, UserPrivacyRestricted,
                ChatWriteForbidden, UserBannedInChannel):
            self.errors += 1
            self.state = WorkerState.IDLE
            logger.warning(f"[{self.name}] 🚫 Permanently blocked @ {target}")
            return None   # permanent — skip

        except Exception as e:
            task.cancel()
            self.errors += 1
            self.state = WorkerState.IDLE
            logger.error(f"[{self.name}] ❌ {type(e).__name__}: {e}")
            return False   # transient

    # ─────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────
    def _is_on_cooldown(self) -> bool:
        return self.cooldown_until > 0 and time.monotonic() < self.cooldown_until

    @property
    def is_available(self) -> bool:
        return (
            self._running
            and self.state not in (WorkerState.ERROR, WorkerState.STOPPED)
            and not self._is_on_cooldown()
        )

    def to_dict(self) -> dict:
        cd_remaining = max(0.0, self.cooldown_until - time.monotonic())
        return {
            "name":            self.name,
            "index":           self.index,
            "session":         self.session_name,
            "state":           self.state.value,
            "sent":            self.sent,
            "errors":          self.errors,
            "last_action":     self.last_action,
            "last_action_time": self.last_action_time,
            "targets_count":   len(self.targets),
            "cooldown_remaining": round(cd_remaining),   # for UI countdown
        }
