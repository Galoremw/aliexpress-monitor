from app.collectors.base import CollectorResult, CollectorTarget


class PlaywrightCollector:
    """Extension point for public JS-rendered pages; intentionally disabled in the MVP."""

    name = "playwright_public_page"
    version = "0.0-disabled"

    def collect(self, target: CollectorTarget) -> CollectorResult:
        raise RuntimeError(
            "Playwright collector is not enabled. The MVP does not bypass login, CAPTCHA, "
            "fingerprinting, or platform security controls."
        )

