import os
import time
import asyncio
import threading
import traceback
import jwt
import datetime
import requests as http_req
from functools import wraps
from flask import render_template, request, jsonify, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash

from utils.logger import logger
from core.services.config_service import config_service
from core.bot_manager import BotManager
from pyrogram import Client
from pyrogram.errors import (
    AuthKeyUnregistered, PhoneCodeInvalid, PhoneCodeExpired, 
    SessionPasswordNeeded, FloodWait
)

# ──────────────────────────────────────────────
# GLOBAL STATE & SECURITY
# ──────────────────────────────────────────────
bot_manager = BotManager()
_BOT_LOOP = asyncio.new_event_loop()
# UNIFIED SECRET KEY
SECRET_KEY = os.environ.get("SECRET_KEY", "ARMEDIAS_PROD_STABLE_2026")

def _run_bot_loop():
    asyncio.set_event_loop(_BOT_LOOP)
    _BOT_LOOP.run_forever()

threading.Thread(target=_run_bot_loop, daemon=True).start()

def run_async(coro):
    return asyncio.run_coroutine_threadsafe(coro, _BOT_LOOP).result(timeout=30)

_init_complete = False  # Guard flag to prevent re-init storms

def _init_app():
    """System warmup: Restore cloud data, then initialize bot sessions."""
    global _init_complete
    time.sleep(1)
    try:
        from core.services.persistence import persistence
        persistence.restore_all()
    except Exception as e:
        logger.warning(f"Cloud restore skipped: {e}")
        
    try:
        future = asyncio.run_coroutine_threadsafe(bot_manager.initialize(), _BOT_LOOP)
        # Wait without a short timeout since many bots can take minutes to init
        future.result() 
    except Exception as e:
        logger.error(f"Error during bot manager initialization: {e}")
        
    _init_complete = True
    logger.info("🚀 System initialization complete. Bot is ready.")

threading.Thread(target=_init_app, daemon=True).start()

# ──────────────────────────────────────────────
# PRODUCTION AUTH MIDDLEWARE
# ──────────────────────────────────────────────
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
_ADMIN_PASS_HASH = generate_password_hash(os.environ.get("ADMIN_PASS", "telegram2026"))

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        if "Authorization" in request.headers:
            try: token = request.headers["Authorization"].split(" ")[1]
            except IndexError: pass
        
        if not token and "logged_in" in session: return f(*args, **kwargs)
        if not token: return jsonify({"status": "error", "message": "Authentication required"}), 401

        try: jwt.decode(token, SECRET_KEY, algorithms=["HS256"], leeway=10)
        except Exception as e: return jsonify({"status": "error", "message": "Session expired"}), 401

        return f(*args, **kwargs)
    return decorated

# ──────────────────────────────────────────────
# CORE LOGIC HELPER
# ──────────────────────────────────────────────
def _get_accounts_state():
    """Production Sync Logic: Unifies in-memory workers with disk session status."""
    config = config_service.load()
    active_workers = bot_manager.get_all_status()
    phones_list = [p.strip() for p in config.get("phones", "").split("\n") if p.strip()]
    account_settings = config.get("account_settings", {})
    
    processed = []
    final_list = []
    for w in active_workers:
        w["authenticated"] = True
        # Inject nickname from config
        p_clean = w.get("clean_phone", "")
        w["nickname"] = account_settings.get(p_clean, {}).get("nickname", "")
        final_list.append(w)
        processed.append(p_clean)
        
    for p in phones_list:
        p_clean = "".join(filter(str.isdigit, p))
        if p_clean not in processed:
            # If session file exists, the account is authenticated but worker is lazy-loading
            has_session = os.path.exists(f"sessions/session_{p_clean}.session")
            acct_settings = account_settings.get(p_clean, {})
            final_list.append({
                "phone": p, "clean_phone": p_clean, "authenticated": has_session,
                "state": "idle" if has_session else "unauth",
                "sent": 0, "errors": 0, "total": 0, "progress": 0, 
                "last_action": "Ready" if has_session else "Login Required",
                "is_running": False, "source_channel": acct_settings.get("source_channel", ""),
                "loop_interval": acct_settings.get("loop_interval", config.get("loop_interval", 15)),
                "msg_delay": acct_settings.get("msg_delay", config.get("msg_delay", 5)),
                "targets_count": len(acct_settings.get("targets", [])),
                "cooldown_remaining": 0, "is_loop_active": False,
                "nickname": acct_settings.get("nickname", "")
            })
    return final_list

def _get_active_worker(phone: str):
    """Lazy-loading worker lookup."""
    p_clean = "".join(filter(str.isdigit, str(phone)))
    worker = bot_manager.get_worker(phone)
    if not worker and _init_complete and os.path.exists(f"sessions/session_{p_clean}.session"):
        # Auto-trigger initialization for authorized session (only after startup finishes)
        run_async(bot_manager.initialize())
        worker = bot_manager.get_worker(phone)
    return worker

# ──────────────────────────────────────────────
# ROUTES
# ──────────────────────────────────────────────
def register_routes(app, socketio):

    @app.route("/")
    def index():
        return render_template("index.html", config=config_service.load())

    @app.route("/login", methods=["GET"])
    def login_page():
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login_page"))

    @app.route("/api/login", methods=["POST"])
    def api_login():
        data = request.get_json() or {}
        if data.get("username") == ADMIN_USER and check_password_hash(_ADMIN_PASS_HASH, data.get("password")):
            token = jwt.encode({"sub": ADMIN_USER, "iat": datetime.datetime.utcnow(), "exp": datetime.datetime.utcnow() + datetime.timedelta(days=7)}, SECRET_KEY, algorithm="HS256")
            if isinstance(token, bytes): token = token.decode('utf-8')
            session["logged_in"] = True
            return jsonify({"status": "success", "token": token})
        return jsonify({"status": "error", "message": "Invalid credentials"}), 401

    @app.route("/api/dashboard/sync", methods=["GET"])
    @token_required
    def dashboard_sync():
        return jsonify({"status": "success", "accounts": _get_accounts_state()})

    @app.route("/api/session/start", methods=["POST"])
    @token_required
    def session_start():
        phone = (request.get_json() or {}).get("phone")
        worker = _get_active_worker(phone)
        if not worker: return jsonify({"status": "error", "message": "Session not ready"}), 404
        success, msg = run_async(worker.start())
        if success:
            # Persist running state so campaign auto-resumes after restart
            config_service.update_account(phone, "is_loop_active", True)
        return jsonify({"status": "success" if success else "error", "message": msg})

    @app.route("/api/session/stop", methods=["POST"])
    @token_required
    def session_stop():
        phone = (request.get_json() or {}).get("phone")
        worker = _get_active_worker(phone)
        if worker: run_async(worker.stop())
        # Persist stopped state
        config_service.update_account(phone, "is_loop_active", False)
        return jsonify({"status": "success"})

    @app.route("/api/session/dispatch", methods=["POST"])
    @token_required
    def session_dispatch():
        phone = (request.get_json() or {}).get("phone")
        worker = _get_active_worker(phone)
        if not worker: return jsonify({"status": "error", "message": "Worker not initialized. Refresh page."}), 404
        from_chat = getattr(worker, 'current_from_chat', None)
        msg_id = getattr(worker, 'current_msg_id', None)
        success = run_async(worker.trigger_dispatch(from_chat, msg_id))
        return jsonify({"status": "success" if success else "error", "message": "Dispatch triggered" if success else "No source message available"})

    @app.route("/api/session/settings", methods=["POST"])
    @token_required
    def session_settings():
        data = request.get_json() or {}
        phone = data.get("phone"); p_clean = "".join(filter(str.isdigit, str(phone)))
        config = config_service.load(); settings = config.setdefault("account_settings", {}).setdefault(p_clean, {})
        settings.update({
            "source_channel": data.get("source_channel"), 
            "loop_interval": int(data.get("loop_interval", 15)), 
            "targets": data.get("targets", []), 
            "msg_delay": int(data.get("msg_delay", 5)),
            "nickname": data.get("nickname", settings.get("nickname", ""))
        })
        config_service.save(config)
        worker = _get_active_worker(phone)
        if worker: run_async(worker.update_settings(data.get("source_channel"), int(data.get("loop_interval", 15)), data.get("targets", []), int(data.get("msg_delay", 5))))
        return jsonify({"status": "success"})

    @app.route("/api/session/rename", methods=["POST"])
    @token_required
    def session_rename():
        """Set or update the nickname for an account."""
        data = request.get_json() or {}
        phone = data.get("phone", ""); p_clean = "".join(filter(str.isdigit, str(phone)))
        nickname = data.get("nickname", "").strip()
        config = config_service.load()
        settings = config.setdefault("account_settings", {}).setdefault(p_clean, {})
        settings["nickname"] = nickname
        config_service.save(config)
        return jsonify({"status": "success", "nickname": nickname})

    @app.route("/save-global", methods=["POST"])
    @token_required
    def save_global():
        config = config_service.load()
        # Support both JSON and form-encoded data
        data = request.get_json(silent=True)
        if not data:
            data = request.form.to_dict()
        config.update({
            "api_id": data.get("api_id", "").strip(), 
            "api_hash": data.get("api_hash", "").strip(), 
            "source_channel": data.get("source_channel", "").strip(), 
            "loop_interval": int(data.get("loop_interval", 15)), 
            "msg_delay": int(data.get("msg_delay", 5))
        })
        config_service.save(config)
        return jsonify({"status": "success"})

    @app.route("/api/add-account", methods=["POST"])
    @token_required
    def add_account():
        data = request.get_json() or {}; phone = data.get("phone", "").strip()
        p_clean = "".join(filter(str.isdigit, phone))
        config = config_service.load(); phones = [p.strip() for p in config.get("phones", "").split("\n") if p.strip()]
        if any("".join(filter(str.isdigit, p)) == p_clean for p in phones): return jsonify({"status": "error", "message": "Exists"}), 409
        phones.append(phone); config["phones"] = "\n".join(phones)
        config_service.save(config); run_async(bot_manager.initialize())
        return jsonify({"status": "success"})

    async def _cleanup_reauth(phone: str):
        p_clean = "".join(filter(str.isdigit, phone)); worker = bot_manager.get_worker(phone)
        if worker:
            await worker.stop()
            try: await asyncio.wait_for(worker.client.stop(), timeout=3.0)
            except: pass
            bot_manager.workers.pop(p_clean, None)
        base = f"sessions/session_{p_clean}"
        for ext in [".session", ".session-journal"]:
            if os.path.exists(f"{base}{ext}"):
                try: os.remove(f"{base}{ext}")
                except: pass
        # Also remove from cloud storage
        try:
            from core.services.persistence import persistence
            persistence.delete_session(p_clean)
        except Exception:
            pass

    @app.route("/api/logout-account", methods=["POST"])
    @token_required
    def logout_account():
        phone = (request.get_json() or {}).get("phone", "").strip()
        run_async(_cleanup_reauth(phone))
        return jsonify({"status": "success"})

    @app.route("/api/delete-account", methods=["POST"])
    @token_required
    def delete_account():
        phone = (request.get_json() or {}).get("phone", "").strip()
        p_clean = "".join(filter(str.isdigit, phone))
        run_async(_cleanup_reauth(phone)); config = config_service.load()
        phones = [p.strip() for p in config.get("phones", "").split("\n") if p.strip()]
        config["phones"] = "\n".join([p for p in phones if "".join(filter(str.isdigit, p)) != p_clean])
        # Also remove account_settings for this phone
        config.get("account_settings", {}).pop(p_clean, None)
        config_service.save(config)
        return jsonify({"status": "success"})

    @app.route("/api/account-targets", methods=["GET"])
    @token_required
    def get_targets():
        phone = request.args.get("phone", "").strip(); p_clean = "".join(filter(str.isdigit, phone))
        config = config_service.load(); targets = config.get("account_settings", {}).get(p_clean, {}).get("targets", [])
        return jsonify({"status": "success", "targets": "\n".join(targets)})

    @app.route("/logs")
    @token_required
    def get_logs():
        try:
            with open("logs/bot.log", "r", errors="replace") as f: return "".join(f.readlines()[-100:])
        except: return "No logs found."

    def _status_worker():
        while True:
            try:
                with app.app_context(): socketio.emit("status_update", {"accounts": _get_accounts_state()}, namespace="/")
            except Exception as e: logger.error(f"Status update error: {e}")
            time.sleep(2)

    threading.Thread(target=_status_worker, daemon=True).start()

    _AUTH_CLIENTS = {}
    _AUTH_TIMESTAMPS = {}  # Track when each auth client was created
    _AUTH_TIMEOUT = 300  # 5 minutes timeout for abandoned auth flows

    def _cleanup_stale_auth():
        """Periodically disconnect auth clients that were never completed."""
        while True:
            try:
                now = time.time()
                stale = [k for k, ts in _AUTH_TIMESTAMPS.items() if now - ts > _AUTH_TIMEOUT]
                for p_clean in stale:
                    client = _AUTH_CLIENTS.pop(p_clean, None)
                    _AUTH_TIMESTAMPS.pop(p_clean, None)
                    if client:
                        try: run_async(client.disconnect())
                        except: pass
                        logger.info(f"🧹 Cleaned up abandoned auth client for {p_clean}")
            except Exception as e:
                logger.error(f"Auth cleanup error: {e}")
            time.sleep(60)

    threading.Thread(target=_cleanup_stale_auth, daemon=True).start()

    @app.route("/api/auth/send_code", methods=["POST"])
    @token_required
    def send_otp():
        data = request.get_json() or {}
        phone = data.get("phone"); api_id = data.get("api_id", "").strip(); api_hash = data.get("api_hash", "").strip()
        p_clean = "".join(filter(str.isdigit, str(phone)))
        # Disconnect any previous abandoned auth client for this phone
        old_client = _AUTH_CLIENTS.pop(p_clean, None)
        _AUTH_TIMESTAMPS.pop(p_clean, None)
        if old_client:
            try: run_async(old_client.disconnect())
            except: pass
        async def _logic():
            await _cleanup_reauth(phone)
            client = Client(f"sessions/session_{p_clean}", api_id=int(api_id), api_hash=api_hash, workdir=".", device_model="iPhone 15 Pro Max", max_concurrent_transmissions=1)
            await client.connect()
            try:
                sent = await client.send_code(phone)
                _AUTH_CLIENTS[p_clean] = client
                _AUTH_TIMESTAMPS[p_clean] = time.time()
                return {"status": "success", "phone_code_hash": sent.phone_code_hash}
            except Exception as e:
                try: await client.disconnect()
                except: pass
                raise e
        try: return jsonify(run_async(_logic()))
        except Exception as e: return jsonify({"status": "error", "message": str(e)})

    @app.route("/api/auth/sign_in", methods=["POST"])
    @token_required
    def sign_in_otp():
        data = request.get_json() or {}
        phone = data.get("phone"); p_clean = "".join(filter(str.isdigit, str(phone))); code = data.get("code", "").strip()
        client = _AUTH_CLIENTS.get(p_clean)
        if not client: return jsonify({"status": "error", "message": "Auth session expired"}), 400
        async def _logic():
            try:
                await client.sign_in(phone, data.get("phone_code_hash"), code)
                await asyncio.sleep(1)
                # Properly disconnect auth client before re-init
                try: await client.disconnect()
                except: pass
                _AUTH_CLIENTS.pop(p_clean, None)
                _AUTH_TIMESTAMPS.pop(p_clean, None)
                # Small delay to let session file flush to disk
                await asyncio.sleep(0.5)
                # Backup new session to cloud immediately
                try:
                    from core.services.persistence import persistence
                    persistence.backup_session(p_clean)
                except Exception:
                    pass
                await bot_manager.initialize()
                return {"status": "success", "message": "Authenticated"}
            except SessionPasswordNeeded:
                return {"status": "2fa_required", "message": "2FA password required"}
            except Exception as e: return {"status": "error", "message": str(e)}
        try: return jsonify(run_async(_logic()))
        except Exception as e: return jsonify({"status": "error", "message": str(e)})

    @app.route("/api/auth/check_password", methods=["POST"])
    @token_required
    def check_password():
        data = request.get_json() or {}
        phone = data.get("phone"); p_clean = "".join(filter(str.isdigit, str(phone)))
        password = data.get("password", "")
        client = _AUTH_CLIENTS.get(p_clean)
        if not client: return jsonify({"status": "error", "message": "Auth session expired"}), 400
        async def _logic():
            try:
                await client.check_password(password)
                await asyncio.sleep(1)
                try: await client.disconnect()
                except: pass
                _AUTH_CLIENTS.pop(p_clean, None)
                _AUTH_TIMESTAMPS.pop(p_clean, None)
                await asyncio.sleep(0.5)
                try:
                    from core.services.persistence import persistence
                    persistence.backup_session(p_clean)
                except Exception: pass
                await bot_manager.initialize()
                return {"status": "success", "message": "Authenticated"}
            except Exception as e: return {"status": "error", "message": str(e)}
        try: return jsonify(run_async(_logic()))
        except Exception as e: return jsonify({"status": "error", "message": str(e)})

    @socketio.on('request_sync')
    def handle_request_sync():
        socketio.emit("status_update", {"accounts": _get_accounts_state()})

    # ──────────────────────────────────────────────
    # KEEP-ALIVE: Prevent Render free tier spin-down
    # ──────────────────────────────────────────────
    def _keep_alive():
        """Self-ping every 5 minutes to keep Render from sleeping."""
        # Wait for full startup before pinging
        time.sleep(30)
        ext_url = (
            os.environ.get("RENDER_EXTERNAL_URL")
            or os.environ.get("EXTERNAL_URL")
        )
        # Auto-derive from RENDER_EXTERNAL_HOSTNAME if available
        if not ext_url:
            hostname = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
            if hostname:
                ext_url = f"https://{hostname}"
        if not ext_url:
            logger.info("💤 Keep-alive disabled: Set RENDER_EXTERNAL_URL env var to prevent spin-down.")
            return
        logger.info(f"💓 Keep-alive enabled: pinging {ext_url} every 5 minutes.")
        while True:
            try:
                time.sleep(300)  # 5 minutes
                resp = http_req.get(ext_url, timeout=15)
                logger.debug(f"💓 Keep-alive ping: {resp.status_code}")
            except Exception:
                pass

    threading.Thread(target=_keep_alive, daemon=True).start()
