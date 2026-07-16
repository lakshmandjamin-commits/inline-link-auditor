#!/usr/bin/env python3
"""Shared logging module for affiliate cron scripts.
Usage: from cron_logger import get_logger
       log = get_logger("script_name")
       log.info("message")
"""
import os, logging
from datetime import datetime

LOG_DIR = os.path.expanduser("~/.hermes/affiliate-crons/logs")
os.makedirs(LOG_DIR, exist_ok=True)

def get_logger(name):
    """Get a logger that writes to both stdout and a timestamped log file."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    
    logger.setLevel(logging.INFO)
    
    # File handler
    today = datetime.now().strftime("%Y-%m-%d")
    fh = logging.FileHandler(os.path.join(LOG_DIR, f"{today}_{name}.log"))
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    logger.addHandler(fh)
    
    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter('%(message)s'))
    logger.addHandler(ch)
    
    return logger
