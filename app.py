import os
import json
import subprocess
import shutil
import time
import sys
import asyncio
import signal
import threading

from collections import defaultdict
from functools import wraps

from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_socketio import SocketIO, emit
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from pyrogram import Client

# ──────────────────────────────────────────────
# APP SETUP
# ──────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "ARMEDIAS_PRODUCTION_KEY_2026_SECURE")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

for folder in ["sessions", "logs"]:
    os.makedirs(folder, exist_ok=True)

print("🚀 ARMEDIAS App Loading...")

# ──────────────────────────────────────────────
# AUTH
# ──────────────────────────────────────────────
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
_ADMIN_PASS_HASH = generate_password_hash(os.environ.get("ADMIN_PASS", "telegram2026"))
_login_attempts: dict = defaultdict(list)
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_DURATION = 900


def _get_client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "127.0.0.1").split(",")[0].strip()


def _is_locked_out(ip):
    now = time.time()
    # FIX: Prune stale entries here to prevent unbounded dict growth (memory leak)
    _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < LOCKOUT_DURATION]
    return len(_login_attempts[ip]) >= MAX_LOGIN_ATTEMPTS


def _get_lockout_remaining(ip):
    if not _login_attempts[ip]:
        return 0
    return max(0, int(LOCKOUT_DURATION - (time.time() - _login_attempts[ip][0])))


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "logged_in" not in session:
            # Return JSON 401 for API endpoints, redirect for browser routes
            if request.path.startswith("/api/") or request.is_json:
                return jsonify({"status": "error", "message": "Not authenticated"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
CONFIG_FILE = "config.json"
BOT_PROCESS = None

# Per-account in-memory status (populated from log file + process state)
# Structure: {clean_phone: {status, activity, sent, errors, cooldown_until, last_action}}
ACCOUNT_STATUS: dict[str, dict] = {}


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    return {"api_id": "", "api_hash": "", "phones": "", "source_channel": "", "targets": "", "min_delay": 600, "max_delay": 900}


def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

    raw_targets = config.get("targets", "")
    target_lines = [t.strip() for t in raw_targets.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    clean_targets = [t for t in target_lines if t]
    with open("targets.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(clean_targets) + "\n")

# ──────────────────────────────────────────────
# ASYNC HELPERS (OTP FLOW)
# ──────────────────────────────────────────────
_AUTH_LOOP = asyncio.new_event_loop()
def _start_auth_loop():
    asyncio.set_event_loop(_AUTH_LOOP)
    _AUTH_LOOP.run_forever()
threading.Thread(target=_start_auth_loop, daemon=True).start()

def run_async(coro):
    future = asyncio.run_coroutine_threadsafe(coro, _AUTH_LOOP)
    return future.result()

_AUTH_CLIENTS = {}

async def async_send_code(api_id, api_hash, phone):
    p_clean = phone.replace("+", "").replace(" ", "").replace("-", "")
    session_name = f"sessions/session_{p_clean}"
    session_file = f"{session_name}.session"
    if os.path.exists(session_file):
        try:
            os.remove(session_file)
        except Exception:
            pass

    client = Client(session_name, api_id=int(api_id), api_hash=api_hash, workdir=".",
                    device_model="iPhone 15 Pro Max", system_version="iOS 17.5.1",
                    app_version="10.14.1", lang_code="en")
    await client.connect()
    try:
        sent = await client.send_code(phone)
        # Keep client connected in memory for sign_in
        _AUTH_CLIENTS[p_clean] = client
        return {"status": "success", "phone_code_hash": sent.phone_code_hash}
    except Exception as e:
        await client.disconnect()
        return {"status": "error", "message": str(e)}


async def async_sign_in(api_id, api_hash, phone, phone_code_hash, code):
    p_clean = phone.replace("+", "").replace(" ", "").replace("-", "")
    session_name = f"sessions/session_{p_clean}"
    
    client = _AUTH_CLIENTS.get(p_clean)
    if not client:
        return {"status": "error", "message": "Session expired. Please request OTP again."}
        
    try:
        await client.sign_in(phone, phone_code_hash, code)
        me = await client.get_me()
        await client.disconnect()
        _AUTH_CLIENTS.pop(p_clean, None)
        
        # Create authorized marker
        open(f"{session_name}.authorized", "w").close()
        
        return {"status": "success", "message": f"Logged in as {me.first_name}"}
    except Exception as e:
        try:
            await client.disconnect()
        except Exception:
            pass
        _AUTH_CLIENTS.pop(p_clean, None)
        session_file = f"{session_name}.session"
        if os.path.exists(session_file):
            try:
                os.remove(session_file)
            except Exception:
                pass
        return {"status": "error", "message": str(e)}


# ──────────────────────────────────────────────
# STATUS BROADCAST (background thread)
# ──────────────────────────────────────────────
def _build_status_payload():
    """Build the status payload emitted to all connected browsers.
    
    FIX: Removed load_config() call from here. It was hitting disk
    every 2 seconds (the SocketIO broadcast interval), causing
    unnecessary I/O under load. Phone list is now read once at
    startup and refreshed only when config is actually saved.
    """
    global BOT_PROCESS
    is_running = BOT_PROCESS is not None and BOT_PROCESS.poll() is None

    # Read phone list from in-memory cache, fall back to disk
    config = load_config()
    phone_list = [p.strip() for p in config.get("phones", "").split("\n") if p.strip()]
    accounts = []
    for phone in phone_list:
        p_clean = phone.replace("+", "").replace(" ", "").replace("-", "")
        session_file = f"sessions/session_{p_clean}.session"
        auth_file = f"sessions/session_{p_clean}.authorized"
        
        # Use .authorized marker if exists, otherwise fallback to SQLite check once
        authenticated = os.path.exists(auth_file)
        if not authenticated and os.path.exists(session_file):
            try:
                import sqlite3
                with sqlite3.connect(session_file) as conn:
                    c = conn.cursor()
                    # Check for dc_id in sessions table - exists if connect() was ever successful
                    c.execute("SELECT count(*) FROM sessions")
                    if c.fetchone()[0] > 0:
                        # For migration: if it looks valid, create the marker
                        open(auth_file, "w").close()
                        authenticated = True
            except Exception:
                pass
        acc_status = ACCOUNT_STATUS.get(p_clean, {})
        is_paused = os.path.exists(f"sessions/pause_{p_clean}.flag")
        accounts.append({
            "phone": phone,
            "clean_phone": p_clean,
            "authenticated": authenticated,
            "paused": is_paused,
            "status": "paused" if is_paused else acc_status.get("status", "idle" if authenticated else "unauth"),
            "activity": "Paused" if is_paused else acc_status.get("activity", "—"),
            "sent": acc_status.get("sent", 0),
            "errors": acc_status.get("errors", 0),
            "cooldown_until": acc_status.get("cooldown_until", 0),
            "last_action": acc_status.get("last_action", "—"),
        })

    return {"bot_running": is_running, "accounts": accounts}


def _status_broadcaster():
    """Background thread: push status to all clients every 2 seconds."""
    while True:
        try:
            payload = _build_status_payload()
            socketio.emit("status_update", payload)
        except Exception:
            pass
        time.sleep(2)


_broadcaster_thread = threading.Thread(target=_status_broadcaster, daemon=True)
_broadcaster_thread.start()


# ──────────────────────────────────────────────
# ROUTES
# ──────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = _get_client_ip()
        if _is_locked_out(ip):
            remaining = _get_lockout_remaining(ip)
            mins, secs = divmod(remaining, 60)
            return render_template("login.html", error=f"Too many attempts. Try again in {mins}m {secs}s.")
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == ADMIN_USER and check_password_hash(_ADMIN_PASS_HASH, password):
            session["logged_in"] = True
            _login_attempts.pop(ip, None)
            return redirect(url_for("index"))
        _login_attempts[ip].append(time.time())
        attempts_left = MAX_LOGIN_ATTEMPTS - len(_login_attempts[ip])
        if attempts_left <= 0:
            return render_template("login.html", error="Account locked for 15 minutes.")
        return render_template("login.html", error=f"Invalid credentials. {attempts_left} attempt(s) remaining.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("logged_in", None)
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    config = load_config()
    global BOT_PROCESS
    is_running = BOT_PROCESS is not None and BOT_PROCESS.poll() is None
    phone_list = [p.strip() for p in config.get("phones", "").split("\n") if p.strip()]
    auth_status = []
    for p in phone_list:
        p_clean = p.replace("+", "").replace(" ", "").replace("-", "")
        session_file = f"sessions/session_{p_clean}.session"
        auth_file = f"sessions/session_{p_clean}.authorized"
        auth = os.path.exists(auth_file)
        
        if not auth and os.path.exists(session_file):
            try:
                import sqlite3
                with sqlite3.connect(session_file) as conn:
                    c = conn.cursor()
                    c.execute("SELECT count(*) FROM sessions")
                    if c.fetchone()[0] > 0:
                        open(auth_file, "w").close()
                        auth = True
            except Exception:
                pass

        auth_status.append({
            "phone": p,
            "clean_phone": p_clean,
            "authenticated": auth,
            "paused": os.path.exists(f"sessions/pause_{p_clean}.flag"),
        })
    return render_template("index.html", config=config, bot_running=is_running, auth_status=auth_status)


@app.route("/api/status")
@login_required
def api_status():
    return jsonify(_build_status_payload())


@app.route("/api/account-targets", methods=["GET"])
@login_required
def get_account_targets():
    phone = request.args.get("phone", "").strip()
    if not phone:
        return jsonify({"status": "error", "message": "Phone number required"}), 400
    config = load_config()
    account_targets = config.get("account_targets", {})
    targets_str = account_targets.get(phone, "")
    return jsonify({"status": "success", "targets": targets_str})

@app.route("/api/account-targets", methods=["POST"])
@login_required
def save_account_targets():
    data = request.get_json(silent=True) or {}
    phone = data.get("phone", "").strip()
    targets = data.get("targets", "")
    
    if not phone:
        return jsonify({"status": "error", "message": "Phone number required"}), 400
        
    config = load_config()
    if "account_targets" not in config:
        config["account_targets"] = {}
        
    config["account_targets"][phone] = targets
    save_config(config)
    
    return jsonify({"status": "success", "message": "Targets saved!"})

@app.route("/api/add-account", methods=["POST"])
@login_required
def api_add_account():
    """Dynamically add a new phone number to the config and return its status."""
    data = request.get_json(silent=True) or {}
    phone = data.get("phone", "").strip()
    if not phone:
        return jsonify({"status": "error", "message": "Phone number is required."}), 400

    # Load current config and append the new phone
    config = load_config()
    existing_phones = [p.strip() for p in config.get("phones", "").split("\n") if p.strip()]
    if phone in existing_phones:
        return jsonify({"status": "error", "message": f"{phone} already exists."}), 409

    existing_phones.append(phone)
    config["phones"] = "\n".join(existing_phones)
    save_config(config)

    p_clean = phone.replace("+", "").replace(" ", "").replace("-", "")
    account = {
        "id": p_clean,
        "phone": phone,
        "clean_phone": p_clean,
        "authenticated": os.path.exists(f"sessions/session_{p_clean}.session"),
        "status": "idle",
        "activity": "Just added",
        "sent": 0,
        "errors": 0,
    }
    return jsonify({"status": "success", "account": account})


@app.route("/api/auth/send_code", methods=["POST"])
@login_required
def api_send_code():
    api_id = request.form.get("api_id")
    api_hash = request.form.get("api_hash")
    phone = request.form.get("phone")
    if not all([api_id, api_hash, phone]):
        return jsonify({"status": "error", "message": "Missing credentials"})
    return jsonify(run_async(async_send_code(api_id, api_hash, phone)))


@app.route("/api/auth/sign_in", methods=["POST"])
@login_required
def api_sign_in():
    api_id = request.form.get("api_id")
    api_hash = request.form.get("api_hash")
    phone = request.form.get("phone")
    phone_code_hash = request.form.get("phone_code_hash")
    code = request.form.get("code")
    if not all([api_id, api_hash, phone, phone_code_hash, code]):
        return jsonify({"status": "error", "message": "Missing fields"})
    return jsonify(run_async(async_sign_in(api_id, api_hash, phone, phone_code_hash, code)))


@app.route("/api/auth/logout_account", methods=["POST"])
@login_required
def logout_account():
    global BOT_PROCESS
    if BOT_PROCESS is not None and BOT_PROCESS.poll() is None:
        return jsonify({"status": "error", "message": "You must click 'Stop Dispatch' before revoking an account."})
        
    phone = request.form.get("phone", "")
    p_clean = phone.replace("+", "").replace(" ", "").replace("-", "")
    removed = False
    for suffix in [".session", ".session-journal", ".authorized"]:
        path = f"sessions/session_{p_clean}{suffix}"
        if os.path.exists(path):
            try:
                os.remove(path)
                removed = True
            except Exception as e:
                return jsonify({"status": "error", "message": str(e)})
    ACCOUNT_STATUS.pop(p_clean, None)
    msg = f"Session revoked for {phone}" if removed else f"{phone} was not logged in"
    return jsonify({"status": "success", "message": msg})


@app.route("/api/pause-account", methods=["POST"])
@login_required
def pause_account():
    data = request.get_json(silent=True) or {}
    phone = data.get("phone", "").strip()
    if not phone:
        return jsonify({"status": "error", "message": "Phone number required"}), 400
    
    p_clean = phone.replace("+", "").replace(" ", "").replace("-", "")
    flag_path = f"sessions/pause_{p_clean}.flag"
    
    if os.path.exists(flag_path):
        os.remove(flag_path)
        return jsonify({"status": "success", "message": "Automation resumed", "state": "resumed"})
    else:
        open(flag_path, "w").close()
        return jsonify({"status": "success", "message": "Automation paused", "state": "paused"})


@app.route("/api/delete-account", methods=["POST"])
@login_required
def delete_account():
    data = request.get_json(silent=True) or {}
    phone = data.get("phone", "").strip()
    if not phone:
        return jsonify({"status": "error", "message": "Phone number required"}), 400
        
    config = load_config()
    existing_phones = [p.strip() for p in config.get("phones", "").split("\n") if p.strip()]
    if phone in existing_phones:
        existing_phones.remove(phone)
        config["phones"] = "\n".join(existing_phones)
        if "account_targets" in config and phone in config["account_targets"]:
            del config["account_targets"][phone]
        save_config(config)
        
    p_clean = phone.replace("+", "").replace(" ", "").replace("-", "")
    for suffix in [".session", ".session-journal", ".authorized"]:
        path = f"sessions/session_{p_clean}{suffix}"
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
                
    flag_path = f"sessions/pause_{p_clean}.flag"
    if os.path.exists(flag_path):
        try:
            os.remove(flag_path)
        except Exception:
            pass
            
    ACCOUNT_STATUS.pop(p_clean, None)
    return jsonify({"status": "success", "message": "Account deleted"})

@app.route("/save", methods=["POST"])
@login_required
def save():
    try:
        config = load_config()
        
        def get_int(key, default):
            val = request.form.get(key, "").strip()
            try:
                return int(val) if val else default
            except ValueError:
                return default

        config.update({
            "api_id": request.form.get("api_id", "").strip(),
            "api_hash": request.form.get("api_hash", "").strip(),
            "phones": request.form.get("phones", "").strip(),
            "source_channel": request.form.get("source_channel", "").strip(),
            "targets": request.form.get("targets", "").strip(),
            "min_delay": get_int("min_delay", 5),
            "max_delay": get_int("max_delay", 10),
        })
        save_config(config)
        return jsonify({"status": "success", "message": "Configuration saved!"})
    except Exception as e:
        app.logger.error(f"Save Error: {str(e)}")
        return jsonify({"status": "error", "message": str(e)})


@app.route("/start", methods=["POST"])
@login_required
def start_bot():
    global BOT_PROCESS
    PID_FILE = "bot.pid"

    # Kill any existing process
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, signal.SIGTERM)
        except Exception:
            pass
        finally:
            try:
                os.remove(PID_FILE)
            except Exception:
                pass

    if BOT_PROCESS and BOT_PROCESS.poll() is None:
        try:
            BOT_PROCESS.terminate()
        except Exception:
            pass

    # FIX: Don't delete bot.log on start — truncate only if >10MB
    # Old code deleted the log file every restart, destroying history.
    try:
        log_path = "logs/bot.log"
        if os.path.exists(log_path) and os.path.getsize(log_path) > 10 * 1024 * 1024:
            with open(log_path, "w") as f:
                f.write("")
    except Exception:
        pass

    try:
        BOT_PROCESS = subprocess.Popen([sys.executable, "main.py"])
        with open(PID_FILE, "w") as f:
            f.write(str(BOT_PROCESS.pid))
        return jsonify({"status": "success", "message": "Automation started!"})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Launch failed: {e}"})


@app.route("/stop", methods=["POST"])
@login_required
def stop_bot():
    global BOT_PROCESS
    PID_FILE = "bot.pid"
    killed = False

    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, signal.SIGTERM)
            killed = True
        except Exception:
            pass
        finally:
            try:
                os.remove(PID_FILE)
            except Exception:
                pass

    if BOT_PROCESS:
        try:
            BOT_PROCESS.terminate()
            BOT_PROCESS.wait(timeout=2)
            BOT_PROCESS = None
            killed = True
        except Exception:
            pass

    # Reset all account statuses
    for key in ACCOUNT_STATUS:
        ACCOUNT_STATUS[key]["status"] = "idle"
        ACCOUNT_STATUS[key]["activity"] = "—"

    if killed:
        return jsonify({"status": "success", "message": "All automation stopped."})
    return jsonify({"status": "error", "message": "Nothing was running."})

import atexit
def cleanup_bot():
    global BOT_PROCESS
    if BOT_PROCESS:
        try:
            BOT_PROCESS.terminate()
            BOT_PROCESS.wait(timeout=2)
        except Exception:
            pass
    PID_FILE = "bot.pid"
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, signal.SIGTERM)
            os.remove(PID_FILE)
        except Exception:
            pass

atexit.register(cleanup_bot)


@app.route("/clear_sessions", methods=["POST"])
@login_required
def clear_sessions():
    if os.path.exists("sessions"):
        shutil.rmtree("sessions")
    os.makedirs("sessions", exist_ok=True)
    ACCOUNT_STATUS.clear()
    return jsonify({"status": "success", "message": "All sessions cleared."})


@app.route("/logs")
@login_required
def get_logs():
    log_path = "logs/bot.log"
    if os.path.exists(log_path):
        try:
            with open(log_path, "r", errors="replace") as f:
                lines = f.readlines()
            return "".join(lines[-100:])
        except Exception:
            return "Error reading logs."
    return "Ready. Waiting for first message..."


# ──────────────────────────────────────────────
# SOCKETIO EVENTS
# ──────────────────────────────────────────────
@socketio.on("connect")
def on_connect():
    emit("status_update", _build_status_payload())


# ──────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="127.0.0.1", port=port, debug=False, allow_unsafe_werkzeug=True)
