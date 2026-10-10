# -*- coding: utf-8 -*-
"""
utils/logger.py

Standardized logger initialization module for application-wide logging.
"""

import logging
import sys


def setup_logger(name: str = "graph_rag_api", level: int = logging.INFO) -> logging.Logger:
    """
    Configure and return a structured logger instance.

    Args:
        name (str): Name of the logger instance.
        level (int): Logging severity level (default: logging.INFO).

    Returns:
        logging.Logger: Configured logger.
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(level)
        formatter = logging.Formatter(
            fmt="[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger


logger = setup_logger()
