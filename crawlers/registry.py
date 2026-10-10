#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crawlers/registry.py

Registry for university crawler adapters.
Allows dynamic lookup and registration of crawlers by university code.
"""

from typing import Dict, Type, List
from crawlers.base import BaseCrawler

_CRAWLER_REGISTRY: Dict[str, Type[BaseCrawler]] = {}


def register_crawler(university_code: str):
    """Decorator or function to register a university crawler adapter."""
    def decorator(cls: Type[BaseCrawler]):
        code = university_code.upper()
        _CRAWLER_REGISTRY[code] = cls
        return cls
    return decorator


def get_crawler(university_code: str, output_dir: str = "data") -> BaseCrawler:
    """Instantiate and return crawler for given university code."""
    code = university_code.upper()
    if code not in _CRAWLER_REGISTRY:
        raise ValueError(
            f"No crawler registered for university code '{code}'. "
            f"Available crawlers: {list(_CRAWLER_REGISTRY.keys())}"
        )
    crawler_cls = _CRAWLER_REGISTRY[code]
    return crawler_cls(university_code=code, output_dir=output_dir)


def list_registered_crawlers() -> List[str]:
    """Return list of registered university codes."""
    return list(_CRAWLER_REGISTRY.keys())
