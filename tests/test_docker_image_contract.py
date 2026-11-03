from pathlib import Path
import unittest


class DockerImageContractTests(unittest.TestCase):
    def test_chatbot_image_packages_the_university_source_allowlist(self):
        root = Path(__file__).resolve().parents[1]
        dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
        config = root / "config" / "universities.json"

        self.assertTrue(config.is_file(), "university source allowlist must exist in the build context")
        self.assertIn("COPY config ./config", dockerfile)


if __name__ == "__main__":
    unittest.main()
