"""Render the add-on and social-preview PNGs from the shared SVG artwork."""

from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
MARK = ROOT / "thermabot" / "app" / "static" / "brand-mark.svg"
BANNER = ROOT / "docs" / "branding" / "thermabot-banner.svg"


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 128, "height": 128})
        page.goto(MARK.as_uri())
        for name, size in (("icon.png", 128), ("logo.png", 256)):
            page.set_viewport_size({"width": size, "height": size})
            page.locator("svg").evaluate(
                """(svg, size) => {
                    svg.setAttribute('width', size);
                    svg.setAttribute('height', size);
                }""",
                size,
            )
            page.screenshot(path=str(ROOT / "addon" / name), omit_background=True)

        page.set_viewport_size({"width": 1280, "height": 640})
        page.goto(BANNER.as_uri())
        page.locator("svg").evaluate(
            """svg => {
                svg.setAttribute('width', 1280);
                svg.setAttribute('height', 640);
                svg.style.background = '#082a27';
            }"""
        )
        page.screenshot(path=str(ROOT / "docs" / "branding" / "social-preview.png"))
        browser.close()


if __name__ == "__main__":
    main()
