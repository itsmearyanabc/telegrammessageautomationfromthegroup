import asyncio
import time
import random
import traceback
from typing import List, Dict, Optional
from pyrogram import Client, filters, handlers
from pyrogram.types import Message
from pyrogram.errors import (
    FloodWait, PeerFlood, UserPrivacyRestricted,
    ChatWriteForbidden, UserBannedInChannel, AuthKeyUnregistered
)

from utils.logger import logger
from core.services.progress_tracker import ProgressTracker
from core.services.loop_manager import LoopManager
from core.services.config_service import config_service


class BotWorker:
    """
    High-level session worker.
    Uses dedicated services for dispatching, progress tracking, and loop management.
    """
    # Class-level defaults — safety net
    cooldown_until = 0
    current_msg_id = None
    current_from_chat = None
    def __init__(self, client: Client, phone: str, clean_phone: str, 
                 targets: List[str], source_channel: str, loop_interval: int,
                 global_semaphore: asyncio.Semaphore, msg_delay: int = 5):
        self.client = client
        self.phone = phone
        self.clean_phone = clean_phone
        self.targets = [t.strip() for t in targets if t.strip()]
        self.source_channel = str(source_channel).strip()
        self.loop_interval = max(1, int(loop_interval))
        self.global_semaphore = global_semaphore
        self.msg_delay = max(0, int(msg_delay))
        
        # Services
        self.progress = ProgressTracker()
        self.scheduler = LoopManager(phone)
        self.worker_manager = LoopManager(f"{phone}_worker")
        
        self.is_running = False
        self.queue = asyncio.Queue()
        self._dispatch_lock = asyncio.Lock()
        
        # State tracking
        self.current_msg_id = None
        self.current_from_chat = None
        self.cooldown_until = 0
        
        # Idempotency & Coordination
        self.last_processed_msg = None
        self._handler = None
        self._new_msg_event = asyncio.Event()

    async def start(self):
        """Ensure no duplicate starts and return safe response."""
        if self.is_running:
            return False, "Already running"
        
        self.is_running = True
        await self.worker_manager.start_loop(self._process_queue)
        await self._setup_monitor()
        
        if self.current_msg_id:
            # IMMEDIATELY queue the pending messages on restart
            # This avoids the Render 15-minute sleep deadlock where the bot waits 15 mins,
            # gets shut down by Render for inactivity, and never actually sends the message.
            logger.info(f"[{self.phone}] Immediate dispatch on resume to avoid sleep deadlock.")
            await self.trigger_dispatch()
            
        logger.info(f"[{self.phone}] Worker started successfully.")
        return True, "Started"

    async def stop(self):
        self.is_running = False
        await self.worker_manager.stop_loop()
        await self.scheduler.stop_loop()
        await self._remove_monitor()
        await self.progress.set_action("Stopped")
        logger.info(f"[{self.phone}] Worker stopped.")

    async def update_settings(self, source: str, interval: int, targets: List[str], delay: int = 5):
        self.source_channel = str(source).strip()
        self.loop_interval = max(1, int(interval))
        self.targets = [t.strip() for t in targets if t.strip()]
        self.msg_delay = max(0, int(delay))
        
        await self._remove_monitor()
        await self._setup_monitor()
        if self.is_running:
            await self._start_scheduler()

    async def _start_scheduler(self):
        await self.scheduler.start_loop(self._reforward_scheduler)

    def _get_resolved_source(self):
        if self.source_channel and self.source_channel.strip():
            return self.source_channel
        config = config_service.load()
        return config.get("source_channel", "").strip()

    async def _setup_monitor(self):
        if not self.client.is_connected: return
        resolved = self._get_resolved_source()
        if not resolved:
            logger.warning(f"[{self.phone}] No source channel configured - cannot monitor")
            return

        async def dynamic_filter(_, __, m: Message):
            if not m.chat: return False
            target = resolved.lower().replace("@", "").strip()
            if "t.me/" in target:
                target = target.split("t.me/")[-1].split("/")[0]
            if "joinchat/" in target:
                target = target.split("joinchat/")[-1].split("/")[0]
            
            return str(m.chat.id) == resolved.strip() or (m.chat.username or "").lower() == target
            
        async def on_new_message(client, message: Message):
            logger.info(f"[{self.phone}] New message detected in source! ID: #{message.id}")
            await self.trigger_dispatch(message.chat.id, message.id)
            
        self._handler = handlers.MessageHandler(on_new_message, filters.create(dynamic_filter))
        self.client.add_handler(self._handler, group=1)
        logger.info(f"[{self.phone}] Monitoring source channel: {resolved} (waiting for new messages...)")

    async def _remove_monitor(self):
        if self.client.is_connected and self._handler:
            try: 
                self.client.remove_handler(self._handler, group=1)
                self._handler = None
            except: pass

    def _persist_state(self):
        """Save current campaign state to config for crash recovery."""
        try:
            config = config_service.load()
            settings = config.setdefault("account_settings", {}).setdefault(self.clean_phone, {})
            settings["last_msg_id"] = self.current_msg_id
            settings["last_from_chat"] = self.current_from_chat
            config_service.save(config)
        except Exception as e:
            logger.warning(f"[{self.phone}] State persist failed: {e}")

    async def trigger_dispatch(self, from_chat_id=None, message_id=None):
        """Dispatch a message to all targets. Called automatically by monitor or manually by user."""
        async with self._dispatch_lock:
            is_new_message = bool(message_id and from_chat_id)

            # If no message specified, this is a manual re-dispatch of the last message
            if not message_id or not from_chat_id:
                if self.current_msg_id and self.current_from_chat:
                    message_id = self.current_msg_id
                    from_chat_id = self.current_from_chat
                    logger.info(f"[{self.phone}] Manual re-dispatch of last message #{message_id}")
                else:
                    await self.progress.set_action("Waiting for source message... (Start the loop first)")
                    logger.info(f"[{self.phone}] No message to dispatch yet - waiting for source channel")
                    return False
                
            if not self.targets:
                await self.progress.set_action("Error: No targets configured")
                return False
                
            # Queue Flush: clear pending sends without replacing the queue object
            while not self.queue.empty():
                try: 
                    self.queue.get_nowait()
                    self.queue.task_done()
                except: 
                    break
                
            self.last_processed_msg = message_id
            self.current_msg_id = message_id
            self.current_from_chat = from_chat_id

            # Persist state on NEW messages so campaigns survive restarts
            if is_new_message:
                self._persist_state()
            
            # Reset progress tracking for new batch
            await self.progress.reset(len(self.targets))
            
            # Queue up all targets
            for target in self.targets:
                await self.queue.put(target)
            
            logger.info(f"[{self.phone}] Dispatch queued: msg #{message_id} -> {len(self.targets)} targets")
                
            # Trigger scheduler reset
            self._new_msg_event.set()
            if self.is_running and not self.scheduler.is_running:
                await self._start_scheduler()
            
            return True

    async def _reforward_scheduler(self):
        try:
            while self.is_running:
                self._new_msg_event.clear()
                try:
                    await asyncio.wait_for(self._new_msg_event.wait(), timeout=self.loop_interval * 60)
                    continue 
                except asyncio.TimeoutError:
                    if self.current_msg_id:
                        logger.info(f"[{self.phone}] Loop trigger: Re-forwarding...")
                        await self.trigger_dispatch()
        except asyncio.CancelledError: pass

    async def _process_queue(self):
        """Queue processor with optimized progress tracking and delay control."""
        while self.is_running:
            try:
                # Handle Cooldown
                while self.cooldown_until > time.monotonic():
                    rem = int(self.cooldown_until - time.monotonic())
                    await self.progress.set_action(f"Cooldown: {rem}s")
                    await asyncio.sleep(1)
                
                target = await self.queue.get()
                
                async with self.global_semaphore:
                    success, err = await self._send_msg(target)
                
                if success:
                    await self.progress.mark_success(target)
                else:
                    logger.error(f"[{self.phone}] Delivery to {target} failed: {err}")
                    await self.progress.mark_failure(target, err)
                
                # Bug Fix 2: Apply delay AFTER EACH MESSAGE
                if not self.queue.empty() and self.is_running:
                    jitter = random.randint(1, 3) if self.msg_delay > 0 else 0
                    total_delay = self.msg_delay + jitter
                    if total_delay > 0:
                        await self.progress.set_action(f"Next in {total_delay}s...")
                        await asyncio.sleep(total_delay)
                
                self.queue.task_done()
                
                if self.queue.empty():
                    await self.progress.set_action("Idle (Waiting for new source msg or interval)")
                    
            except asyncio.CancelledError: break
            except Exception as e:
                logger.error(f"[{self.phone}] Worker error: {e}\n{traceback.format_exc()}")
                await asyncio.sleep(5)

    async def _send_msg(self, target: str):
        """Bug Fix 7: Meaningful error handling."""
        for attempt in range(1, 4):
            try:
                await asyncio.wait_for(
                    self.client.forward_messages(
                        chat_id=target, 
                        from_chat_id=self.current_from_chat, 
                        message_ids=self.current_msg_id
                    ),
                    timeout=15
                )
                logger.info(f"[{self.phone}] Delivered to {target}")
                return True, ""
            except AuthKeyUnregistered:
                await self.stop()
                return False, "Session Expired"
            except FloodWait as e:
                self.cooldown_until = time.monotonic() + e.value + 5
                return False, f"FloodWait ({e.value}s)"
            except (PeerFlood, UserPrivacyRestricted, ChatWriteForbidden, UserBannedInChannel) as e:
                return False, type(e).__name__
            except Exception as e:
                err_str = str(e)
                if "MESSAGE_ID_INVALID" in err_str or "MessageIdInvalid" in err_str:
                    return False, "MessageIdInvalid"
                if "FORBIDDEN" in err_str or "RESTRICTED" in err_str or "BANNED" in err_str:
                    return False, "Permission Denied"
                if attempt < 3: await asyncio.sleep(2 ** attempt)
                else: return False, err_str
        return False, "Max Retries"

    def to_dict(self):
        stats = self.progress.get_stats()
        cd_val = getattr(self, 'cooldown_until', 0) or 0
        cd_rem = max(0, int(cd_val - time.monotonic()))
        return {
            "phone": self.phone, "clean_phone": self.clean_phone,
            "is_running": self.is_running,
            "state": "sending" if stats["progress"] < 100 and stats["total"] > 0 else "idle",
            "sent": stats["sent"], "errors": stats["failed"], "total": stats["total"],
            "last_action": stats["last_action"], "progress": stats["progress"],
            "targets_count": len(self.targets), "source_channel": self.source_channel,
            "loop_interval": self.loop_interval, "is_loop_active": self.is_running,
            "cooldown_remaining": cd_rem, "msg_delay": self.msg_delay
        }
