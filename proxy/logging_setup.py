"""Logging configuration for stash-jellyfin-proxy.

`setup_logging` is called once at startup with the values already resolved
from config + env overrides. It returns the configured logger object.
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler


def setup_logging(
    log_level: str = "INFO",
    log_file: str = "stash_jellyfin_proxy.log",
    log_dir: str = ".",
    log_max_size_mb: int = 10,
    log_backup_count: int = 3,
) -> logging.Logger:
    """Configure the 'stash-jellyfin-proxy' logger.

    Sets up a console handler (always) and a rotating file handler (when
    log_file is non-empty). Returns the configured logger.
    """
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
    }
    level = level_map.get(log_level.upper(), logging.INFO)
    print(f"  Log level: {log_level.upper()} ({level})")

    log = logging.getLogger("stash-jellyfin-proxy")
    log.setLevel(level)
    log.propagate = False
    log.handlers = []

    # sys.stdout is reconfigured to UTF-8 at startup on Windows.
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter(log_format))
    console_handler.setLevel(level)
    log.addHandler(console_handler)

    if log_file:
        try:
            log_path = os.path.join(log_dir, log_file) if log_dir else log_file
            log_dir_path = os.path.dirname(log_path)
            if log_dir_path and not os.path.exists(log_dir_path):
                os.makedirs(log_dir_path, exist_ok=True)

            if log_max_size_mb > 0:
                max_bytes = log_max_size_mb * 1024 * 1024
                fh = RotatingFileHandler(
                    log_path,
                    maxBytes=max_bytes,
                    backupCount=log_backup_count,
                    encoding="utf-8",
                )
            else:
                fh = logging.FileHandler(log_path, encoding="utf-8")

            fh.setFormatter(logging.Formatter(log_format))
            fh.setLevel(level)
            log.addHandler(fh)
            print(f"  Log file: {os.path.abspath(log_path)}")
        except Exception as e:
            print(f"Warning: Could not set up file logging: {e}")

    return log
