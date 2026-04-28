import os
import asyncio
from typing import Dict, List, Optional
from pyrogram import Client
from pyrogram.errors import AuthKeyUnregistered
from core.bot_worker import BotWorker
from utils.logger import logger
from core.services.config_service import config_service

class BotManager:
    """
    Orchestrates multiple BotWorkers. 
    Uses clean_phone (digits only) as the primary canonical identifier.
    """
    def __init__(self):
        self.workers: Dict[str, BotWorker] = {} # Keyed by clean_phone
        self._lock = asyncio.Lock()
        self.global_semaphore = asyncio.Semaphore(3)
        
    async def initialize(self):
        """Discovers and starts all authorized sessions."""
        async with self._lock:
            config = config_service.load()
            phones = [p.strip() for p in config.get("phones", "").split("\n") if p.strip()]
            
            # Startup diagnostics (only on first init)
            if not self.workers:
                has_api = bool(config.get("api_id") and config.get("api_hash"))
                logger.info(f"🔧 Init: {len(phones)} phone(s) registered, API creds={'✅' if has_api else '❌ MISSING'}")
            
            # Identify active session files on disk
            session_dir = "sessions"
            if not os.path.exists(session_dir): os.makedirs(session_dir)
            
            for phone in phones:
                p_clean = self._clean_id(phone)
                if p_clean in self.workers:
                    continue
                    
                # SAFETY GUARD: Ensure global API ID is set before trying to initialize
                if not config.get("api_id") or not config.get("api_hash"):
                    logger.warning(f"ℹ️ {phone}: Skipping initialization. Global API ID/Hash not set in Config.")
                    continue

                session_file = f"{session_dir}/session_{p_clean}"
                if os.path.exists(f"{session_file}.session"):
                    logger.info(f"🔍 Found session for {phone}. Initializing...")
                    await self._start_worker(phone, p_clean, config)
                else:
                    logger.info(f"ℹ️ {phone} is registered but no session file found. Needs login.")

    async def _start_worker(self, phone: str, p_clean: str, config: dict):
        try:
            settings = config.get("account_settings", {}).get(p_clean, {})
            source = settings.get("source_channel") or config.get("source_channel", "")
            interval = settings.get("loop_interval") or config.get("loop_interval", 15)
            targets = settings.get("targets")
            if not targets:
                global_targets = config.get("targets", [])
                if isinstance(global_targets, str):
                    targets = [t.strip() for t in global_targets.split("\n") if t.strip()]
                else:
                    targets = global_targets

            client = Client(
                f"sessions/session_{p_clean}",
                api_id=int(config["api_id"]),
                api_hash=config["api_hash"],
                workdir=".",
                device_model="iPhone 15 Pro Max",
                max_concurrent_transmissions=1
            )
            
            # Pre-flight check to prevent interactive prompt (EOFError crash)
            is_authorized = await client.connect()
            await client.disconnect()
            
            if not is_authorized:
                logger.error(f"❌ [{phone}] Session file exists but is NOT authorized. Deleting corrupted session.")
                if os.path.exists(f"sessions/session_{p_clean}.session"):
                    os.remove(f"sessions/session_{p_clean}.session")
                try:
                    from core.services.persistence import persistence
                    persistence.delete_session(p_clean)
                except Exception:
                    pass
                raise AuthKeyUnregistered("Session not authorized.")
                
            await client.start()
            
            delay = settings.get("msg_delay") or config.get("msg_delay", 5)
            
            worker = BotWorker(client, phone, p_clean, targets, source, interval, self.global_semaphore, delay)
            self.workers[p_clean] = worker # Canonical Storage

            # ── CRASH RECOVERY: Restore last campaign state ──
            last_msg = settings.get("last_msg_id")
            last_chat = settings.get("last_from_chat")
            if last_msg and last_chat:
                worker.current_msg_id = last_msg
                worker.current_from_chat = last_chat
                logger.info(f"🔄 [{phone}] Restored campaign state: msg #{last_msg} from chat {last_chat}")
            
            if settings.get("is_loop_active"):
                await worker.start()
                logger.info(f"▶️ [{phone}] Auto-resumed campaign (was active before restart).")
                
            logger.info(f"✅ [{phone}] Session linked and active.")
            # Safe Backup: Session is only backed up cleanly in routes.py after sign-in.
            # Backing up here while SQLite is locked/open causes corruption.
        except AuthKeyUnregistered:
            logger.error(f"❌ [{phone}] Session revoked by Telegram.")
        except Exception as e:
            logger.error(f"❌ [{phone}] Worker crash during startup: {e}")

    def get_worker(self, identifier: str) -> Optional[BotWorker]:
        """Permissive lookup using clean identifier."""
        p_clean = self._clean_id(identifier)
        worker = self.workers.get(p_clean)
        
        if not worker:
            logger.debug(f"ℹ️ Worker not found for: '{identifier}' (Cleaned: '{p_clean}')")
        
        return worker

    def get_all_status(self) -> List[dict]:
        # Thread-safe snapshot to avoid 'dictionary changed size during iteration'
        return [w.to_dict() for w in list(self.workers.values())]

    def _clean_id(self, phone: str) -> str:
        return "".join(filter(str.isdigit, str(phone)))

    async def shutdown(self):
        for w in self.workers.values():
            await w.stop()
            try: await w.client.stop()
            except: pass
        self.workers = {}
