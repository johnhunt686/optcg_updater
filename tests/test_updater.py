import unittest

from updater import scrape_page


class ScrapePageVersionSelectionTests(unittest.TestCase):
    def test_prefers_site_version_over_download_link_version(self) -> None:
        html = (
            '<html><body>Latest version 2.0 <a href="https://example.com/files/app-1.0.zip">download</a></body></html>'
        )

        version, download_url = scrape_page(
            html,
            "https://example.com/page",
            r"\d+\.\d+[a-z]?",
            r"\.zip",
            "auto",
        )

        self.assertEqual(version, "2.0")
        self.assertEqual(download_url, "https://example.com/files/app-1.0.zip")

    def test_falls_back_to_link_version_when_site_has_no_version(self) -> None:
        html = '<html><body><a href="https://example.com/files/app-1.2.zip">download</a></body></html>'

        version, download_url = scrape_page(
            html,
            "https://example.com/page",
            r"\d+\.\d+[a-z]?",
            r"\.zip",
            "auto",
        )

        self.assertEqual(version, "1.2")
        self.assertEqual(download_url, "https://example.com/files/app-1.2.zip")

    def test_ignores_version_strings_inside_attribute_urls(self) -> None:
        html = (
            '<html><head>'
            '<link rel="alternate" type="application/json+oembed" href="https://example.com/oembed/1.0/embed" />'
            '</head>'
            '<body>'
            'Latest version 1.41b Release (Action Fix)'
            '<a href="https://example.com/files/app-1.2.zip">download</a>'
            '</body></html>'
        )

        version, download_url = scrape_page(
            html,
            "https://example.com/page",
            r"\d+\.\d+[a-z]?",
            r"\.zip",
            "auto",
        )

        self.assertEqual(version, "1.41b")
        self.assertEqual(download_url, "https://example.com/files/app-1.2.zip")


if __name__ == "__main__":
    unittest.main()
