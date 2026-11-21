"""Logging configuration, kept separate so main.py stays clean."""
import logging

from config import CONFIG


def get_logger() -> logging.Logger:
    logger = logging.getLogger("MailSiphon")
    if logger.handlers:
        return logger  # already configured (avoid duplicate handlers on reload)

    logger.setLevel(logging.DEBUG)

    file_handler = logging.FileHandler(CONFIG.log_file, mode="a", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    )

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter("%(message)s"))

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


logger = get_logger()
