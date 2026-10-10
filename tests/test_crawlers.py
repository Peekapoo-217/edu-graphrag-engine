#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_crawlers.py

Unit tests for crawler adapter architecture and mock university registration.
"""

import unittest
from crawlers.base import BaseCrawler
from crawlers.registry import register_crawler, get_crawler, list_registered_crawlers


class TestCrawlers(unittest.TestCase):

    def test_qnu_crawler_registration(self):
        """Test QNU crawler adapter (DQN) is properly registered."""
        registered = list_registered_crawlers()
        self.assertIn("DQN", registered)

        crawler = get_crawler("DQN")
        self.assertEqual(crawler.university_code, "DQN")

    def test_mock_adapter_registration(self):
        """Test registering a mock adapter for a second university."""
        @register_crawler("HUST")
        class HUSTCrawler(BaseCrawler):
            def run(self, admission_year=2026, cutoff_year=2025):
                return {"offers_csv": "hust_offers.csv"}

        registered = list_registered_crawlers()
        self.assertIn("HUST", registered)

        hust_crawler = get_crawler("HUST")
        self.assertEqual(hust_crawler.university_code, "HUST")
        res = hust_crawler.run()
        self.assertEqual(res["offers_csv"], "hust_offers.csv")


if __name__ == "__main__":
    unittest.main()
