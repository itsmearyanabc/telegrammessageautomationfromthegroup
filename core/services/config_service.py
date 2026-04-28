import json
import os
import tempfile
from utils.logger import logger

class ConfigService:
    def __init__(self, config_path="config.json"):
        self.config_path = config_path

    def load(self):
        if not os.path.exists(self.config_path):
            return self._defaults()
        try:
            with open(self.config_path, "r") as f:
                data = json.load(f)
                data.setdefault("account_settings", {})
                data.setdefault("loop_interval", 15)
                data.setdefault("msg_delay", 5)
                # Prefer env vars for sensitive credentials (keeps them out of git)
                if os.environ.get("API_ID"):
                    data["api_id"] = os.environ["API_ID"]
                if os.environ.get("API_HASH"):
                    data["api_hash"] = os.environ["API_HASH"]
                return data
        except Exception as e:
            logger.error(f"Config load error: {e}")
            return self._defaults()

    def save(self, config):
        fd, temp_path = tempfile.mkstemp(dir=os.path.dirname(self.config_path))
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(config, f, indent=4)
            os.replace(temp_path, self.config_path)
        except Exception as e:
            if os.path.exists(temp_path): os.remove(temp_path)
            logger.error(f"Atomic save failure: {e}")
            return
        # Sync to Supabase (non-blocking best-effort)
        try:
            from core.services.persistence import persistence
            persistence.backup_config()
        except Exception as e:
            logger.warning(f"☁️ Config cloud sync skipped: {e}")

    def update_account(self, phone, key, value):
        config = self.load()
        p_clean = "".join(filter(str.isdigit, str(phone)))
        settings = config.setdefault("account_settings", {}).setdefault(p_clean, {})
        if settings.get(key) != value:
            settings[key] = value
            self.save(config)

    def _defaults(self):
        return {
            "api_id": "", "api_hash": "", "phones": "", 
            "source_channel": "", "loop_interval": 15,
            "msg_delay": 5, "account_settings": {} 
        }

config_service = ConfigService()
