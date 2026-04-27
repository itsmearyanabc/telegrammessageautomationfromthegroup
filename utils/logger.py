import logging
import os
import sys
from logging.handlers import RotatingFileHandler

def setup_logger():
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "bot.log")

    logger = logging.getLogger("ARMEDIAS")
    logger.setLevel(logging.INFO)

    # Prevent duplicate handlers
    if not logger.handlers:
        # File Handler (Rotating) — force UTF-8 encoding for emoji support
        file_handler = RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5, encoding='utf-8')
        file_formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s')
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)

        # Console Handler — force UTF-8 stream to avoid Windows cp1252 crashes
        try:
            utf8_stream = open(sys.stdout.fileno(), mode='w', encoding='utf-8', errors='replace', closefd=False)
        except Exception:
            utf8_stream = sys.stdout
        console_handler = logging.StreamHandler(stream=utf8_stream)
        console_handler.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
        logger.addHandler(console_handler)

    return logger

logger = setup_logger()
