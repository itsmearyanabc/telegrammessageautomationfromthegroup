import os
import signal
import sys
from dotenv import load_dotenv
load_dotenv()  # Load .env before any module reads os.environ

# Production Diagnostic: Check if Render environment variables are actually present
_s_url = os.environ.get("SUPABASE_URL")
_s_key = os.environ.get("SUPABASE_KEY")
if _s_url and _s_key:
    print(f"DEBUG: Supabase environment variables detected (URL length: {len(_s_url)})")
else:
    missing = []
    if not _s_url: missing.append("SUPABASE_URL")
    if not _s_key: missing.append("SUPABASE_KEY")
    print(f"DEBUG: Missing critical environment variables: {', '.join(missing)}")
from flask import Flask
from flask_socketio import SocketIO
from flask_cors import CORS
from api.routes import register_routes, bot_manager, run_async
from utils.logger import logger

# Initialize Flask
app = Flask(__name__)
# UNIFIED SECRET KEY
app.secret_key = os.environ.get("SECRET_KEY", "ARMEDIAS_PROD_STABLE_2026")

# Enable CORS
CORS(app)

# Initialize SocketIO with production-optimized settings
socketio = SocketIO(
    app, 
    cors_allowed_origins="*", 
    async_mode="threading",
    ping_timeout=60,
    ping_interval=25
)

# Register modular routes
register_routes(app, socketio)

import atexit

def graceful_shutdown():
    """Ensures all Telegram sessions are closed and state is backed up on exit."""
    logger.info("🛑 Shutdown signal received. Saving state & closing sessions...")
    
    def _do_backup():
        try:
            from core.services.persistence import persistence
            persistence.backup_config()
            for p_clean in list(bot_manager.workers.keys()):
                persistence.backup_session(p_clean)
            logger.info("☁️ State backed up to Supabase.")
        except Exception as e:
            logger.warning(f"☁️ Pre-shutdown backup failed: {e}")

    # Run backup directly (gevent handles blocking calls via greenlets)
    _do_backup()
    
    # 2. Gracefully stop all workers
    try:
        import asyncio
        from api.routes import _BOT_LOOP
        future = asyncio.run_coroutine_threadsafe(bot_manager.shutdown(), _BOT_LOOP)
        try:
            future.result(timeout=10)
            logger.info("✅ All sessions closed. Exiting.")
        except Exception:
            logger.info("⏳ Shutdown timed out, forcing exit.")
    except Exception as e:
        logger.error(f"⚠️ Error during shutdown: {e}")

# Register atexit instead of native signals to avoid gevent BlockingSwitchOutError
atexit.register(graceful_shutdown)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    
    # Create necessary directories
    for d in ["sessions", "logs"]:
        if not os.path.exists(d): os.makedirs(d)
        
    logger.info(f"🚀 ARMEDIAS AI Hub starting on http://localhost:{port}")
    socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)
