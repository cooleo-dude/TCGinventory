"""Upcoming card/TCG shows scraped from tcdb.com (Qt-free).

tcdb lists shows per US state at
``CardShows.cfm?MODE=Location&State=<code>&Display=<name>&Country=United States``.
The list page groups shows under date headers, one ``<li>`` per show carrying
its name, venue, city, time range, and a detail link. That's everything the
Dashboard calendar needs — no per-show detail fetch required.

Network I/O is injected (``fetch_html``) so parsing/refresh are testable offline;
the real fetch sends a browser User-Agent because tcdb 403s generic clients.
Mirrors ``pricing.py``: errors are captured into the summary, never raised.
"""

from __future__ import annotations

import html as _html
import re
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlencode

from tcginventory.data.db import now_iso
from tcginventory.data.repositories import CardShowRepository
from tcginventory.domain.models import CardShow

_BASE_URL = "https://www.tcdb.com/CardShows.cfm"
_SITE = "https://www.tcdb.com"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

# Words that mark a show as TCG/gaming-relevant (used by the UI's "TCG only"
# toggle; everything is still stored regardless).
TCG_KEYWORDS = ("tcg", "pok", "pokémon", "magic", "mtg", "yugioh", "yu-gi-oh", "gaming")

# US states + DC, matching tcdb's ?State=<code>&Display=<name> links.
US_STATES: list[tuple[str, str]] = [
    ("AL", "Alabama"),
    ("AK", "Alaska"),
    ("AZ", "Arizona"),
    ("AR", "Arkansas"),
    ("CA", "California"),
    ("CO", "Colorado"),
    ("CT", "Connecticut"),
    ("DE", "Delaware"),
    ("DC", "District of Columbia"),
    ("FL", "Florida"),
    ("GA", "Georgia"),
    ("HI", "Hawaii"),
    ("ID", "Idaho"),
    ("IL", "Illinois"),
    ("IN", "Indiana"),
    ("IA", "Iowa"),
    ("KS", "Kansas"),
    ("KY", "Kentucky"),
    ("LA", "Louisiana"),
    ("ME", "Maine"),
    ("MD", "Maryland"),
    ("MA", "Massachusetts"),
    ("MI", "Michigan"),
    ("MN", "Minnesota"),
    ("MS", "Mississippi"),
    ("MO", "Missouri"),
    ("MT", "Montana"),
    ("NE", "Nebraska"),
    ("NV", "Nevada"),
    ("NH", "New Hampshire"),
    ("NJ", "New Jersey"),
    ("NM", "New Mexico"),
    ("NY", "New York"),
    ("NC", "North Carolina"),
    ("ND", "North Dakota"),
    ("OH", "Ohio"),
    ("OK", "Oklahoma"),
    ("OR", "Oregon"),
    ("PA", "Pennsylvania"),
    ("RI", "Rhode Island"),
    ("SC", "South Carolina"),
    ("SD", "South Dakota"),
    ("TN", "Tennessee"),
    ("TX", "Texas"),
    ("UT", "Utah"),
    ("VT", "Vermont"),
    ("VA", "Virginia"),
    ("WA", "Washington"),
    ("WV", "West Virginia"),
    ("WI", "Wisconsin"),
    ("WY", "Wyoming"),
]
STATE_NAMES: dict[str, str] = dict(US_STATES)


@dataclass
class ShowRefreshSummary:
    fetched: int = 0  # shows parsed and stored
    states: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    as_of: str = ""


def is_tcg(show: CardShow) -> bool:
    """Heuristic: does the show name look TCG/gaming-relevant?"""
    name = (show.name or "").lower()
    return any(kw in name for kw in TCG_KEYWORDS)


def list_url(state: str) -> str:
    """The tcdb list URL for a 2-letter state code."""
    display = STATE_NAMES.get(state, state)
    query = urlencode(
        {
            "MODE": "Location",
            "State": state,
            "Display": display,
            "Country": "United States",
        }
    )
    return f"{_BASE_URL}?{query}"


def _fetch_html(url: str) -> str:  # pragma: no cover - real network
    """Fetch via the system ``curl``.

    tcdb sits behind Cloudflare, which 403s Python HTTP clients (httpx/requests)
    on their TLS fingerprint while allowing curl through. curl ships with
    Windows 10+/macOS; the request is silent, follows redirects, and fails on
    HTTP errors so a block surfaces as an exception the caller can capture.
    """
    result = subprocess.run(
        [
            "curl",
            "-sSL",  # silent, show errors, follow redirects
            "--fail",  # non-zero exit on HTTP >= 400 (e.g. a Cloudflare 403)
            "--compressed",
            "--max-time",
            "25",
            "-A",
            _UA,
            url,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"curl exit {result.returncode}: {result.stderr.strip() or 'fetch failed'}"
        )
    return result.stdout


def _text(fragment: str) -> str:
    """Strip tags + unescape entities from an HTML fragment."""
    return _html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


_DATE_HEADER = re.compile(
    r"<strong>\s*([A-Za-z]+day,\s+[A-Z][a-z]+\s+\d{1,2},\s+\d{4})\s*</strong>",
    re.IGNORECASE,
)
_LI = re.compile(r"<li>(.*?)</li>", re.IGNORECASE | re.DOTALL)
_ANCHOR = re.compile(
    r'href="([^"]*ID=(\d+)[^"]*)"[^>]*>(.*?)</a>(.*)',
    re.IGNORECASE | re.DOTALL,
)
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_CITY_STATE = re.compile(r"^(.*),\s*([A-Za-z]{2})$")
_TIME = re.compile(r"\b([AP]M)\b", re.IGNORECASE)


def _iso_date(header_text: str) -> str | None:
    try:
        return datetime.strptime(header_text, "%A, %B %d, %Y").date().isoformat()
    except ValueError:
        return None


def _parse_li(li_html: str, state: str, starts_on: str, fetched_at: str):
    m = _ANCHOR.search(li_html)
    if not m:
        return None
    href, source_id, name_html, after = m.groups()
    name = _text(name_html)
    if not name:
        return None

    venue = city = start_time = end_time = None
    for frag in (_text(p) for p in _BR.split(after)):
        if not frag:
            continue
        cs = _CITY_STATE.match(frag)
        if cs and city is None:
            city = cs.group(1).strip()
            continue
        if _TIME.search(frag) and start_time is None:
            parts = re.split(r"\s*[-–]\s*", frag, maxsplit=1)
            start_time = parts[0].strip()
            end_time = parts[1].strip() if len(parts) > 1 else None
            continue
        if venue is None:
            venue = frag

    url = href if href.startswith("http") else f"{_SITE}{href}"
    return CardShow(
        source_id=int(source_id),
        name=name,
        venue=venue,
        city=city,
        state=state,
        starts_on=starts_on,
        start_time=start_time,
        end_time=end_time,
        detail_url=url,
        fetched_at=fetched_at,
    )


def parse_shows(html: str, state: str, fetched_at: str = "") -> list[CardShow]:
    """Parse a tcdb per-state list page into CardShow rows (today-forward)."""
    shows: list[CardShow] = []
    headers = list(_DATE_HEADER.finditer(html))
    for i, h in enumerate(headers):
        starts_on = _iso_date(h.group(1))
        if starts_on is None:
            continue
        end = headers[i + 1].start() if i + 1 < len(headers) else len(html)
        block = html[h.end() : end]
        for li in _LI.findall(block):
            show = _parse_li(li, state, starts_on, fetched_at)
            if show is not None:
                shows.append(show)
    return shows


class CardShowService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        fetch_html: Callable[[str], str] | None = None,
    ) -> None:
        self.conn = conn
        self.shows = CardShowRepository(conn)
        self._fetch = fetch_html or _fetch_html

    def upcoming(self, limit: int = 5, today: str | None = None) -> list[CardShow]:
        """The next few shows on/after today (for the dashboard summary box)."""
        today = today or datetime.now(timezone.utc).date().isoformat()
        return self.shows.upcoming(today, limit)

    def refresh(
        self,
        states: list[str],
        today: str | None = None,
        fetched_at: str | None = None,
    ) -> ShowRefreshSummary:
        """Re-fetch the given states, replacing their stored shows. Atomic.

        Each state's rows are replaced wholesale with the freshly parsed set;
        past shows (globally) are pruned. Network/parse errors for one state are
        captured and the others still process.
        """
        fetched_at = fetched_at or now_iso()
        today = today or datetime.now(timezone.utc).date().isoformat()
        summary = ShowRefreshSummary(as_of=fetched_at, states=list(states))
        try:
            self.shows.delete_before(today)  # drop stale past shows everywhere
            for state in states:
                try:
                    html = self._fetch(list_url(state))
                except Exception as exc:  # one state's failure isn't fatal
                    summary.errors.append(f"{state}: {exc}")
                    continue
                parsed = parse_shows(html, state, fetched_at)
                self.shows.delete_for_state(state)
                for show in parsed:
                    if show.starts_on >= today:
                        self.shows.upsert(show)
                        summary.fetched += 1
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return summary
