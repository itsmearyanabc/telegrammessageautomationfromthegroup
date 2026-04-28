"""
Supabase Storage Persistence Layer
───────────────────────────────────
Backs up config.json and .session files to Supabase Storage so data
survives Render container restarts.

Design:
  • If SUPABASE_URL / SUPABASE_KEY are not set → everything is a no-op.
  • Uses the REST API directly with `requests` (no heavy SDK needed).
  • All methods are synchronous and safe to call from Flask/gevent.
"""

import os
import json
import requests as http_requests   # alias to avoid shadowing
from utils.logger import logger

BUCKET = "telegram-sessions"

class PersistenceManager:
    def __init__(self):
        # Fresh read from environ to handle late-loading and strip whitespace
        self.url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
        self.key = os.environ.get("SUPABASE_KEY", "").strip()
        
        self.enabled = bool(self.url and self.key)
        
        if not self.enabled:
            missing = []
            if not self.url: missing.append("SUPABASE_URL")
            if not self.key: missing.append("SUPABASE_KEY")
            logger.warning(f"☁️ Supabase not configured (Missing: {', '.join(missing)}) – data will NOT persist across restarts.")
        else:
            logger.info("☁️ Supabase persistence enabled.")

        self._headers = {
            "Authorization": f"Bearer {self.key}",
            "apikey": self.key,
        }
        self._base = f"{self.url}/storage/v1/object"
        self._ensure_bucket()

    # ─────────────────────────────────────
    # LOW-LEVEL HELPERS
    # ─────────────────────────────────────
    def _ensure_bucket(self):
        """Create the storage bucket if it doesn't exist."""
        if not self.enabled:
            return
        try:
            resp = http_requests.post(
                f"{self.url}/storage/v1/bucket",
                headers={**self._headers, "Content-Type": "application/json"},
                json={"id": BUCKET, "name": BUCKET, "public": False},
                timeout=10,
            )
            if resp.status_code in (200, 201):
                logger.info(f"☁️ Created storage bucket: {BUCKET}")
            # 409 = already exists, which is fine
        except Exception as e:
            logger.warning(f"☁️ Bucket check skipped: {e}")

    def _upload(self, remote_path, local_path, content_type="application/octet-stream"):
        """Upload a local file to Supabase Storage (upsert)."""
        if not self.enabled:
            return False
        try:
            with open(local_path, "rb") as f:
                data = f.read()
            resp = http_requests.post(
                f"{self._base}/{BUCKET}/{remote_path}",
                headers={
                    **self._headers,
                    "Content-Type": content_type,
                    "x-upsert": "true",          # overwrite if exists
                },
                data=data,
                timeout=30,
            )
            if resp.status_code in (200, 201):
                logger.info(f"☁️ Backed up → {remote_path}")
                return True
            else:
                logger.error(f"☁️ Upload failed {remote_path}: {resp.status_code} {resp.text[:200]}")
                return False
        except Exception as e:
            logger.error(f"☁️ Upload error {remote_path}: {e}")
            return False

    def _download(self, remote_path, local_path):
        """Download a file from Supabase Storage to local disk."""
        if not self.enabled:
            return False
        try:
            resp = http_requests.get(
                f"{self._base}/{BUCKET}/{remote_path}",
                headers=self._headers,
                timeout=30,
            )
            if resp.status_code == 200:
                os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
                with open(local_path, "wb") as f:
                    f.write(resp.content)
                logger.info(f"☁️ Restored ← {remote_path}")
                return True
            # 400/404 = file doesn't exist in cloud yet, not an error
            if resp.status_code not in (400, 404):
                logger.warning(f"☁️ Download failed {remote_path}: {resp.status_code}")
            return False
        except Exception as e:
            logger.error(f"☁️ Download error {remote_path}: {e}")
            return False

    def _delete(self, remote_path):
        """Delete a file from Supabase Storage."""
        if not self.enabled:
            return False
        try:
            resp = http_requests.delete(
                f"{self._base}/{BUCKET}",
                headers={**self._headers, "Content-Type": "application/json"},
                json={"prefixes": [remote_path]},
                timeout=10,
            )
            if resp.status_code == 200:
                logger.info(f"☁️ Deleted cloud file: {remote_path}")
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"☁️ Delete error {remote_path}: {e}")
            return False

    def _list_files(self, prefix=""):
        """List files in a bucket path."""
        if not self.enabled:
            return []
        try:
            resp = http_requests.post(
                f"{self._base}/list/{BUCKET}",
                headers={**self._headers, "Content-Type": "application/json"},
                json={"prefix": prefix, "limit": 1000},
                timeout=15,
            )
            if resp.status_code == 200:
                return resp.json()
            return []
        except Exception as e:
            logger.error(f"☁️ List error: {e}")
            return []

    # ─────────────────────────────────────
    # HIGH-LEVEL API
    # ─────────────────────────────────────
    def backup_config(self):
        """Push config.json to Supabase."""
        if os.path.exists("config.json"):
            # Safety Check: Don't backup if the file is suspiciously small (default is ~150-200 bytes)
            if os.path.getsize("config.json") < 100:
                logger.warning("☁️ Config file suspiciously small, skipping cloud backup to prevent data loss.")
                return False
            return self._upload("config/config.json", "config.json", "application/json")
        return False

    def restore_config(self):
        """Pull config.json from Supabase to local disk."""
        return self._download("config/config.json", "config.json")

    def backup_session(self, clean_phone):
        """Push a single .session file to Supabase."""
        local = f"sessions/session_{clean_phone}.session"
        if os.path.exists(local):
            return self._upload(f"sessions/session_{clean_phone}.session", local)
        return False

    def restore_all_sessions(self):
        """Pull ALL .session files from Supabase to local disk."""
        files = self._list_files("sessions")
        restored = 0
        for f in files:
            name = f.get("name", "")
            if name.endswith(".session"):
                if self._download(f"sessions/{name}", f"sessions/{name}"):
                    restored += 1
        if restored:
            logger.info(f"☁️ Restored {restored} session file(s) from cloud.")
        return restored

    def delete_session(self, clean_phone):
        """Remove a session file from Supabase."""
        return self._delete(f"sessions/session_{clean_phone}.session")

    def restore_all(self):
        """Full restore: config + all sessions. Called on startup."""
        if not self.enabled:
            return
        os.makedirs("sessions", exist_ok=True)
        os.makedirs("logs", exist_ok=True)
        logger.info("☁️ Restoring data from Supabase...")
        self.restore_config()
        self.restore_all_sessions()
        logger.info("☁️ Cloud restore complete.")


# Singleton — imported by other modules
persistence = PersistenceManager()
