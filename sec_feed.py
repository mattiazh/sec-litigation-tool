"""
Reads which litigation releases SEC.gov has actually published.

Two independent sources are used and merged:

1. the HTML listing page  (primary -- always current)
2. the official RSS feed  (secondary -- occasionally lags by weeks)

Merging both means a temporary problem with either source cannot cause the
checker to silently report "nothing new". If BOTH sources fail, this module
raises, and the caller is expected to report the failure loudly rather than
treat it as "no new releases".
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup

LISTING_URL = "https://www.sec.gov/enforcement-litigation/litigation-releases"
RSS_URL = "https://www.sec.gov/enforcement-litigation/litigation-releases/rss"

# SEC's fair-access policy requires a descriptive User-Agent with a contact
# address. Keep this pointing at a mailbox that still exists.
CONTACT = "sec-litigation-tool sec.tool@pqs.ch"
HEADERS = {"User-Agent": f"SEC Litigation Release Tool {CONTACT}"}

REQUEST_TIMEOUT_SECONDS = 45
RELEASE_LINK_PATTERN = re.compile(r"litigation-releases/lr-(\d+)", re.IGNORECASE)


@dataclass
class SecListing:
    numbers: set[int] = field(default_factory=set)
    details: dict[int, dict] = field(default_factory=dict)
    sources_ok: list[str] = field(default_factory=list)
    sources_failed: list[str] = field(default_factory=list)

    @property
    def highest(self) -> int | None:
        return max(self.numbers) if self.numbers else None

    def describe(self, number: int) -> dict:
        return self.details.get(number, {})


def _get(url: str) -> str:
    response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.text


def _parse_listing_html(html: str) -> dict[int, dict]:
    """
    Extracts release numbers from the listing page.

    The table markup is parsed when possible so that the date and the
    respondents can be shown in the notification. If SEC changes the markup,
    the plain link pattern still finds every release number, so the checker
    keeps working with less detail rather than breaking.
    """

    found: dict[int, dict] = {}
    soup = BeautifulSoup(html, "html.parser")

    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        link = row.find("a", href=RELEASE_LINK_PATTERN)

        if not link or not cells:
            continue

        match = RELEASE_LINK_PATTERN.search(link.get("href", ""))

        if not match:
            continue

        number = int(match.group(1))
        texts = [" ".join(cell.get_text(" ", strip=True).split()) for cell in cells]

        found[number] = {
            "number": number,
            "date": texts[0] if texts else "",
            "respondents": " ".join(link.get_text(" ", strip=True).split()),
            "url": f"https://www.sec.gov/enforcement-litigation/litigation-releases/lr-{number}",
        }

    # Fallback: any release number linked anywhere on the page.
    for match in RELEASE_LINK_PATTERN.finditer(html):
        number = int(match.group(1))
        found.setdefault(
            number,
            {
                "number": number,
                "date": "",
                "respondents": "",
                "url": (
                    "https://www.sec.gov/enforcement-litigation/"
                    f"litigation-releases/lr-{number}"
                ),
            },
        )

    return found


ITEM_PATTERN = re.compile(r"<item\b.*?</item>", re.IGNORECASE | re.DOTALL)


def _tag_text(item_xml: str, tag: str) -> str:
    match = re.search(
        rf"<{tag}\b[^>]*>(.*?)</{tag}>", item_xml, re.IGNORECASE | re.DOTALL
    )

    if not match:
        return ""

    text = match.group(1)
    text = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(html.unescape(text).split())


def _parse_rss(xml_text: str) -> dict[int, dict]:
    """
    Parses the RSS feed with plain text matching.

    An XML parser is deliberately not used: BeautifulSoup's HTML parser treats
    <link> as an empty element and silently loses every URL, and pulling in
    lxml just for this would add a dependency for no benefit.
    """

    found: dict[int, dict] = {}

    for item_xml in ITEM_PATTERN.findall(xml_text):
        link = _tag_text(item_xml, "link")
        match = RELEASE_LINK_PATTERN.search(link)

        if not match:
            continue

        number = int(match.group(1))

        found[number] = {
            "number": number,
            "date": _tag_text(item_xml, "pubDate"),
            "respondents": _tag_text(item_xml, "title"),
            "url": link,
        }

    return found


def fetch_published_releases() -> SecListing:
    """Returns every release number SEC.gov currently shows as published."""

    listing = SecListing()

    try:
        html_results = _parse_listing_html(_get(LISTING_URL))
    except Exception as error:  # noqa: BLE001 - reported, not swallowed
        listing.sources_failed.append(f"listing page: {error}")
        html_results = {}
    else:
        if html_results:
            listing.sources_ok.append(f"listing page ({len(html_results)} releases)")
        else:
            listing.sources_failed.append("listing page: no release links found")

    try:
        rss_results = _parse_rss(_get(RSS_URL))
    except Exception as error:  # noqa: BLE001 - reported, not swallowed
        listing.sources_failed.append(f"RSS feed: {error}")
        rss_results = {}
    else:
        if rss_results:
            listing.sources_ok.append(f"RSS feed ({len(rss_results)} releases)")
        else:
            listing.sources_failed.append("RSS feed: no items found")

    for source in (rss_results, html_results):
        for number, detail in source.items():
            listing.numbers.add(number)
            existing = listing.details.get(number, {})
            merged = {**existing}

            for key, value in detail.items():
                if value or not merged.get(key):
                    merged[key] = value

            listing.details[number] = merged

    if not listing.numbers:
        raise RuntimeError(
            "SEC.gov could not be read from either source: "
            + "; ".join(listing.sources_failed)
        )

    return listing
