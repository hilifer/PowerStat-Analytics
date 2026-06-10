"""统一日志模块。"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.config_loader import config


def setup_logger(name: str = "powerstat") -> logging.Logger:
    """创建并配置 logger。"""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    cfg = config.data.get("logging", {})
    level = getattr(logging, cfg.get("level", "INFO").upper(), logging.INFO)
    logger.setLevel(level)

    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)-8s %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(formatter)
    logger.addHandler(console)

    # File handler
    log_file = cfg.get("log_file")
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        max_bytes = cfg.get("max_size_mb", 10) * 1024 * 1024
        backup = cfg.get("backup_count", 5)
        file_handler = RotatingFileHandler(
            str(log_path), maxBytes=max_bytes, backupCount=backup, encoding="utf-8"
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


log = setup_logger()
