# Import registry and adapters to trigger registration
from crawlers.base import BaseCrawler
from crawlers.registry import register_crawler, get_crawler, list_registered_crawlers
import crawlers.qnu  # Register QNU adapter

__all__ = [
    "BaseCrawler",
    "register_crawler",
    "get_crawler",
    "list_registered_crawlers",
]
