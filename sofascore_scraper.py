"""
sofascore_metrics.py

A flexible Python metaclass that scrapes SofaScore pages and dynamically
creates classes representing individual football metrics (e.g. Ball Possession,
Average Goals, Clean Sheets). Two scraping backends are included:
- Requests + BeautifulSoup (lightweight, works if the data is in server-rendered HTML)
- Playwright (headless browser for JS-rendered pages)

DISCLAIMER & IMPORTANT NOTES
- Check SofaScore's Terms of Service / robots.txt before scraping. Respect rate limits
  and use caching. This example is educational — prefer official APIs where available.
- SofaScore pages are heavily client-side and may require a headless browser (Playwright).
  The Requests backend will only work when the HTML includes the metric data.
"""

from typing import Callable, Dict, Optional, Tuple
import re
import time

# External deps
# pip install requests bs4 playwright
import requests
from bs4 import BeautifulSoup

# Optional import for Playwright. If you want to use it, ensure it's installed and installed browsers:
# pip install playwright
# python -m playwright install
try:
    from playwright.sync_api import sync_playwright
    _PLAYWRIGHT_AVAILABLE = True
except Exception:
    _PLAYWRIGHT_AVAILABLE = False


#######################
# Scraper implementations
#######################
class RequestsScraper:
    """Simple scraper using requests + BeautifulSoup.
    Works when the target page has the metric data in the server-rendered HTML.
    """

    headers = {
        "User-Agent": "sofascore-metrics-bot/1.0 (+https://example.com/contact)"
    }

    def fetch_soup(self, url: str, timeout: int = 10) -> BeautifulSoup:
        r = requests.get(url, headers=self.headers, timeout=timeout)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")

    def extract_by_selector(self, soup: BeautifulSoup, selector: str) -> Optional[str]:
        el = soup.select_one(selector)
        if not el:
            return None
        return el.get_text(strip=True)

    def find_metrics(self, url: str, metric_selectors: Optional[Dict[str, str]] = None
                    ) -> Dict[str, Callable[[BeautifulSoup], Optional[str]]]:
        """
        If metric_selectors is provided (mapping metric_name -> CSS selector),
        return extractors based on those selectors. Otherwise try to guess
        some common metrics by searching document text (best-effort).
        Returns mapping metric_name -> extractor(soup) -> value str or None.
        """
        if metric_selectors:
            def make_extractor(sel: str):
                return lambda soup: self.extract_by_selector(soup, sel)
            return {name: make_extractor(sel) for name, sel in metric_selectors.items()}

        # Best-effort heuristics for common metrics: look for label/value pairs
        soup = self.fetch_soup(url)
        mapping = {}

        # Common labels we might find on a team page summary (language-dependent).
        candidates = {
            "Ball possession": re.compile(r"ball possession", re.I),
            "Average goals": re.compile(r"avg(?:\.|) goals|goals per match|average goals", re.I),
            "Clean sheets": re.compile(r"clean sheets?", re.I),
            "Goals": re.compile(r"^goals$", re.I),
            "Shots on target": re.compile(r"on target", re.I),
        }

        # Attempt: find label nodes and their sibling/parent values
        # This is heuristic — you should provide exact selectors for reliable results.
        for label_text, label_re in candidates.items():
            label_node = soup.find(lambda tag: tag.name in ["div", "span", "p", "td", "th"] and tag.get_text(strip=True) and label_re.search(tag.get_text(" ")))
            if label_node:
                # try sibling
                val = None
                # sibling
                sib = label_node.find_next_sibling()
                if sib:
                    val = sib.get_text(strip=True)
                # parent sibling
                if not val and label_node.parent:
                    nextp = label_node.parent.find_next_sibling()
                    if nextp:
                        val = nextp.get_text(strip=True)
                # fallback: nearest numeric token in next 120 characters
                if not val:
                    tail = label_node.get_text(" ") + " " + (label_node.parent.get_text(" ") if label_node.parent else "")
                    m = re.search(r"(\d+(?:\.\d+)?%?)", tail)
                    val = m.group(1) if m else None

                if val:
                    mapping[label_text] = (lambda captured_val=val: (lambda soup: captured_val))
                else:
                    # construct an extractor that tries to find again at fetch time
                    def make_lazy_extractor(lr=label_re):
                        def extractor(soup):
                            node = soup.find(lambda tag: tag.name in ["div", "span", "p", "td", "th"] and tag.get_text(strip=True) and lr.search(tag.get_text(" ")))
                            if not node:
                                return None
                            sib = node.find_next_sibling()
                            if sib:
                                return sib.get_text(strip=True)
                            if node.parent:
                                nextp = node.parent.find_next_sibling()
                                if nextp:
                                    return nextp.get_text(strip=True)
                            m = re.search(r"(\d+(?:\.\d+)?%?)", node.get_text(" "))
                            return m.group(1) if m else None
                        return extractor
                    mapping[label_text] = make_lazy_extractor()
        return mapping


class PlaywrightScraper:
    """Scraper using Playwright to render JS-heavy SofaScore pages.
    Uses the sync Playwright API. Requires playwright to be installed.
    """

    def __init__(self, headless: bool = True, timeout: int = 10_000):
        if not _PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("Playwright is not available. Install playwright and run 'playwright install'.")
        self.headless = headless
        self.timeout = timeout

    def fetch_html(self, url: str) -> str:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            try:
                page = browser.new_page()
                page.goto(url, timeout=self.timeout)
                # wait some sensible time for metrics to load
                page.wait_for_timeout(1500)
                return page.content()
            finally:
                browser.close()

    def fetch_soup(self, url: str) -> BeautifulSoup:
        html = self.fetch_html(url)
        return BeautifulSoup(html, "html.parser")

    def find_metrics(self, url: str, metric_selectors: Optional[Dict[str, str]] = None
                    ) -> Dict[str, Callable[[BeautifulSoup], Optional[str]]]:
        # Reuse RequestsScraper behavior but fetch via Playwright
        soup = self.fetch_soup(url)
        rs = RequestsScraper()
        if metric_selectors:
            def make_extractor(sel: str):
                return lambda _: rs.extract_by_selector(soup, sel)
            return {name: make_extractor(sel) for name, sel in metric_selectors.items()}
        # fall back to heuristic detection like RequestsScraper
        return RequestsScraper().find_metrics(url, None)


#######################
# Metric metaclass & base
#######################
class MetricBase:
    """Base for generated metric classes. Generated classes attach:
    - metric_name (str)
    - extractor: Callable[[BeautifulSoup], Optional[str]]         (class attr _extractor)
    - sofascore_url (str), and optionally 'scraper' (object with fetch_soup)
    """

    metric_name: str = "unnamed"
    sofascore_url: Optional[str] = None
    _extractor: Optional[Callable[[BeautifulSoup], Optional[str]]] = None
    _scraper_instance = None  # will be set to a scraper object that has fetch_soup(url)

    @classmethod
    def _get_scraper(cls):
        if cls._scraper_instance:
            return cls._scraper_instance
        return RequestsScraper()

    @classmethod
    def fetch_raw(cls) -> Optional[str]:
        """Fetch the raw metric text (string) from the sofascore_url using the extractor."""
        if not cls.sofascore_url:
            raise RuntimeError("sofascore_url not set on metric class")
        if not cls._extractor:
            raise RuntimeError("extractor not provided for metric")
        scraper = cls._get_scraper()
        soup = scraper.fetch_soup(cls.sofascore_url)
        return cls._extractor(soup)

    @classmethod
    def get_value(cls) -> Optional[float]:
        """Return a parsed numeric value when possible; else return raw string.
        Handles percentages and integers/floats."""
        raw = cls.fetch_raw()
        if raw is None:
            return None
        raw = raw.strip()
        # common patterns: "43%", "1.25", "12"
        m = re.match(r"^(-?\d+(?:\.\d+)?)\s*%?$", raw)
        if m:
            val = float(m.group(1))
            if raw.endswith("%"):
                return val  # percentage as number (e.g., 43)
            return val
        # try to extract numeric inside string
        m2 = re.search(r"(-?\d+(?:\.\d+)?)", raw)
        if m2:
            return float(m2.group(1))
        # otherwise return raw
        return raw

    @classmethod
    def refresh(cls, delay_seconds: float = 0.0) -> Optional[str]:
        """Convenience: optionally wait for delay (rate-limiting), then fetch raw."""
        if delay_seconds:
            time.sleep(delay_seconds)
        return cls.fetch_raw()

    def __repr__(self):
        return f"<Metric {self.metric_name} @ {self.sofascore_url}>"


class MetricMeta(type):
    """
    Metaclass that, when a class defines 'sofascore_url', will scrape the page and
    dynamically create attributes on the class for each discovered metric.

    Usage:
      class TeamMetrics(metaclass=MetricMeta):
          sofascore_url = "https://www.sofascore.com/team/..."
          scraper = PlaywrightScraper()   # optional; defaults to RequestsScraper
          metric_selectors = {
              "Ball possession": "css-selector-for-possession",
              "Clean sheets": "css-selector-for-clean-sheets",
          }

    After definition, TeamMetrics will have attributes like TeamMetrics.BallPossessionMetric
    which are classes subclassing MetricBase and exposing get_value()/refresh().
    """

    def __new__(mcls, name, bases, namespace):
        # accept sofascore_url, scraper, metric_selectors if present in namespace
        sofascore_url = namespace.get("sofascore_url", None)
        scraper = namespace.get("scraper", None)
        metric_selectors = namespace.get("metric_selectors", None)

        # Create the base class first (without metric classes attached)
        cls = super().__new__(mcls, name, bases, dict(namespace))

        if not sofascore_url:
            # nothing to discover
            return cls

        # default scraper
        if not scraper:
            scraper = RequestsScraper()

        # Ensure the scraper exposes a find_metrics(url, metric_selectors) method
        if not hasattr(scraper, "find_metrics"):
            raise RuntimeError("Provided scraper must implement find_metrics(url, metric_selectors)")

        # Discover metrics: mapping metric_name -> extractor(soup)->value
        discovered = scraper.find_metrics(sofascore_url, metric_selectors)

        for metric_name, extractor in discovered.items():
            # create a safe pythonic class name
            class_name = re.sub(r"[^0-9a-zA-Z]+", "", metric_name.title()) or "Metric"
            class_name = class_name if class_name.endswith("Metric") else class_name + "Metric"

            metric_attrs = {
                "metric_name": metric_name,
                "sofascore_url": sofascore_url,
                "_extractor": extractor,
                "_scraper_instance": scraper,
                "__doc__": f"Auto-generated metric class for '{metric_name}' from {sofascore_url}"
            }

            metric_cls = type(class_name, (MetricBase,), metric_attrs)
            # Attach to parent class
            setattr(cls, class_name, metric_cls)

        # Also attach a convenience mapping of names -> classes
        setattr(cls, "_discovered_metrics", {k: getattr(cls, re.sub(r"[^0-9a-zA-Z]+", "", k.title()) + "Metric") for k in discovered.keys()})

        return cls
