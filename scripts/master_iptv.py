from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse, parse_qs, urlencode
import html
import json
import os
import re
import subprocess
import time

import requests
from playwright.sync_api import sync_playwright


# ============================================================
# CONFIG
# ============================================================

SITE = "https://livetv.lessgooguys.tech"
API = "https://api.freeforall.dev"

INDIA_PAGE = f"{SITE}/browse/?country=IN"
JIOTV_PAGE = f"{SITE}/jiotv/"

OUTPUT = Path("LiveTV/India/India-JioTV.m3u")
REPORT = Path("LiveTV/India/India-JioTV-report.txt")
DRM_JSON = Path("LiveTV/India/India-JioTV-drm.json")
DEBUG = Path("LiveTV/India/India-JioTV-discovery.json")

PER_PAGE = 60
WORKERS = max(
    1,
    int(os.getenv("WORKERS", "8"))
)

MAX_CHANNELS = int(
    os.getenv("MAX_CHANNELS", "0")
)

API_TIMEOUT = 30
STREAM_TIMEOUT = 15

USER_AGENT = (
    "Mozilla/5.0 "
    "(Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/154.0.0.0 "
    "Safari/537.36"
)


# ============================================================
# BASIC HELPERS
# ============================================================

def clean(value):
    if value is None:
        return ""

    return " ".join(
        str(value).split()
    ).strip()


def safe_text(value):
    return clean(value)


def get_slug(channel):
    return clean(
        channel.get("slug")
    )


def get_name(channel):
    return (
        clean(channel.get("name"))
        or get_slug(channel)
        or "Unknown"
    )


def get_logo(channel):
    return clean(
        channel.get("logo_url")
        or channel.get("logo")
    )


def get_group(channel):
    for key in (
        "group",
        "category",
        "group_title",
        "groupTitle",
        "subcategory",
        "sub_category",
        "subgroup",
        "sub_group"
    ):
        value = clean(
            channel.get(key)
        )

        if value:
            return value

    return "Other"


def make_session(
    cookies=None,
    referer=SITE
):
    session = requests.Session()

    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": (
            "application/json,text/plain,*/*"
        ),
        "Referer": referer
    })

    if cookies:
        for cookie in cookies:
            try:
                session.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie.get("domain"),
                    path=cookie.get("path", "/")
                )
            except Exception:
                pass

    return session


def json_get(
    session,
    url,
    params=None
):
    try:
        response = session.get(
            url,
            params=params,
            timeout=API_TIMEOUT
        )

        if response.status_code >= 400:
            return None

        return response.json()

    except Exception:
        return None


# ============================================================
# FIND CHROME
# ============================================================

def find_local_chrome():

    candidates = [
        os.getenv("PROGRAMFILES") and (
            Path(os.getenv("PROGRAMFILES"))
            / "Google"
            / "Chrome"
            / "Application"
            / "chrome.exe"
        ),
        os.getenv("PROGRAMFILES(X86)") and (
            Path(os.getenv("PROGRAMFILES(X86)"))
            / "Google"
            / "Chrome"
            / "Application"
            / "chrome.exe"
        ),
        os.getenv("LOCALAPPDATA") and (
            Path(os.getenv("LOCALAPPDATA"))
            / "Google"
            / "Chrome"
            / "Application"
            / "chrome.exe"
        )
    ]

    for candidate in candidates:

        if candidate and candidate.exists():
            return str(candidate)

    return None


# ============================================================
# INDIA DISCOVERY
# ============================================================

def discover_india(session):

    print()
    print("=" * 76)
    print("INDIA TAB")
    print("=" * 76)
    print(f"Exact source page: {INDIA_PAGE}")
    print("Using API filter: country=IN")

    result = []

    page = 1

    while True:

        data = json_get(
            session,
            f"{API}/api/channels",
            {
                "page": page,
                "per_page": PER_PAGE,
                "country": "IN"
            }
        )

        if not isinstance(data, dict):
            break

        rows = data.get(
            "channels",
            []
        )

        if not isinstance(rows, list):
            break

        if not rows:
            break

        total = data.get(
            "total"
        )

        print(
            f"Page {page}: "
            f"{len(rows)} channels"
            + (
                f" | API total: {total}"
                if total is not None
                else ""
            )
        )

        for channel in rows:

            slug = get_slug(
                channel
            )

            if not slug:
                continue

            result.append({
                "name": get_name(
                    channel
                ),
                "slug": slug,
                "group": get_group(
                    channel
                ),
                "logo_url": get_logo(
                    channel
                ),
                "source": "India"
            })

        if (
            MAX_CHANNELS
            and len(result) >= MAX_CHANNELS
        ):
            result = result[
                :MAX_CHANNELS
            ]
            break

        if (
            total is not None
            and len(result) >= int(total)
        ):
            break

        if len(rows) < PER_PAGE:
            break

        page += 1

    # Deduplicate while preserving order.
    unique = []
    seen = set()

    for item in result:

        key = (
            item["slug"],
            item["group"]
        )

        if key in seen:
            continue

        seen.add(key)
        unique.append(item)

    print(
        f"India channels discovered: "
        f"{len(unique)}"
    )

    return unique


# ============================================================
# JIOTV CATEGORY PARSER
# ============================================================

def parse_jiotv_tabs(page_html):

    categories = []
    seen = set()

    # Exact site structure:
    # <a href="/jiotv/?category=News" class="jio-tab">
    pattern = re.compile(
        r'<a\s+[^>]*class=["\'][^"\']*\bjio-tab\b[^"\']*["\'][^>]*'
        r'href=["\']([^"\']*category=[^"\']*)["\'][^>]*>'
        r'(.*?)'
        r'</a>',
        re.I | re.S
    )

    # Attribute order can vary, so also try a generic
    # class-first / href-first extraction.
    matches = pattern.findall(
        page_html
    )

    if not matches:

        pattern2 = re.compile(
            r'<a\s+[^>]*href=["\']([^"\']*category=[^"\']*)["\']'
            r'[^>]*class=["\'][^"\']*\bjio-tab\b[^"\']*["\'][^>]*>'
            r'(.*?)'
            r'</a>',
            re.I | re.S
        )

        matches = pattern2.findall(
            page_html
        )

    for href, body in matches:

        href = html.unescape(
            href
        ).strip()

        absolute = urljoin(
            JIOTV_PAGE,
            href
        )

        parsed = urlparse(
            absolute
        )

        params = parse_qs(
            parsed.query
        )

        values = params.get(
            "category",
            []
        )

        if not values:
            continue

        category = clean(
            values[0]
        )

        if not category:
            continue

        if category.lower() == "all":
            continue

        if category in seen:
            continue

        seen.add(
            category
        )

        categories.append(
            category
        )

    return categories


# ============================================================
# JIOTV CHANNEL CARD PARSER
# ============================================================

def parse_jiotv_channels(
    page_html,
    group
):
    result = []

    # Match channel watch links.
    pattern = re.compile(
        r'<a\s+[^>]*href=["\']([^"\']*/watch\?ch=[^"\']+)["\']'
        r'[^>]*>(.*?)</a>',
        re.I | re.S
    )

    matches = pattern.findall(
        page_html
    )

    seen = set()

    for href, body in matches:

        href = html.unescape(
            href
        ).strip()

        parsed = urlparse(
            urljoin(
                JIOTV_PAGE,
                href
            )
        )

        params = parse_qs(
            parsed.query
        )

        values = params.get(
            "ch",
            []
        )

        if not values:
            continue

        slug = clean(
            values[0]
        )

        if not slug:
            continue

        # Remove HTML tags from card text.
        text = re.sub(
            r"<script\b[^>]*>.*?</script>",
            " ",
            body,
            flags=re.I | re.S
        )

        text = re.sub(
            r"<style\b[^>]*>.*?</style>",
            " ",
            text,
            flags=re.I | re.S
        )

        text = re.sub(
            r"<[^>]+>",
            " ",
            text
        )

        text = html.unescape(
            text
        )

        name = clean(
            text
        )

        if not name:
            name = slug

        # Extract first image URL if present.
        logo = ""

        image_match = re.search(
            r'<img\b[^>]*(?:src|data-src|data-lazy-src)=["\']([^"\']+)["\']',
            body,
            flags=re.I
        )

        if image_match:
            logo = html.unescape(
                image_match.group(1)
            ).strip()

        key = (
            slug,
            group
        )

        if key in seen:
            continue

        seen.add(
            key
        )

        result.append({
            "name": name,
            "slug": slug,
            "group": group,
            "logo_url": logo,
            "source": "JioTV"
        })

    return result


# ============================================================
# DISCOVER NEXT PAGE LINKS
# ============================================================

def find_pagination_urls(
    page_html,
    current_url
):

    result = set()

    hrefs = re.findall(
        r'''href\s*=\s*["']([^"']+)["']''',
        page_html,
        flags=re.I
    )

    for href in hrefs:

        href = html.unescape(
            href
        ).strip()

        if not href:
            continue

        absolute = urljoin(
            current_url,
            href
        )

        parsed = urlparse(
            absolute
        )

        params = parse_qs(
            parsed.query
        )

        if "page" not in params:
            continue

        if not any(
            key.lower() == "category"
            for key in params.keys()
        ):
            continue

        result.add(
            absolute
        )

    return result


def get_page_number(url):

    parsed = urlparse(
        url
    )

    params = parse_qs(
        parsed.query
    )

    try:
        return int(
            params.get(
                "page",
                ["1"]
            )[0]
        )
    except Exception:
        return 1


# ============================================================
# FETCH ONE JIOTV CATEGORY
# ============================================================

def fetch_jiotv_category(
    category,
    browser_cookies
):

    session = make_session(
        browser_cookies,
        referer=JIOTV_PAGE
    )

    first_url = (
        f"{JIOTV_PAGE}?"
        f"{urlencode({'category': category})}"
    )

    collected = []
    seen_slugs = set()

    # --------------------------------------------------------
    # first page
    # --------------------------------------------------------

    try:

        response = session.get(
            first_url,
            timeout=30
        )

        if response.status_code >= 400:
            return (
                category,
                []
            )

        page_html = response.text

    except Exception:

        return (
            category,
            []
        )

    first_items = parse_jiotv_channels(
        page_html,
        category
    )

    for item in first_items:

        slug = item["slug"]

        if slug in seen_slugs:
            continue

        seen_slugs.add(slug)
        collected.append(item)

    # --------------------------------------------------------
    # pagination from actual page
    # --------------------------------------------------------

    discovered_pages = find_pagination_urls(
        page_html,
        first_url
    )

    # --------------------------------------------------------
    # fallback pagination
    #
    # If site doesn't render pagination links,
    # try page=2 onward until an empty page.
    # --------------------------------------------------------

    explicit_pagination = bool(
        discovered_pages
    )

    if explicit_pagination:

        ordered_pages = sorted(
            discovered_pages,
            key=get_page_number
        )

        for page_url in ordered_pages:

            if get_page_number(page_url) <= 1:
                continue

            try:

                response = session.get(
                    page_url,
                    timeout=30
                )

                if response.status_code >= 400:
                    continue

                items = parse_jiotv_channels(
                    response.text,
                    category
                )

            except Exception:
                continue

            new_count = 0

            for item in items:

                slug = item["slug"]

                if slug in seen_slugs:
                    continue

                seen_slugs.add(slug)
                collected.append(item)
                new_count += 1

            if (
                MAX_CHANNELS
                and len(collected) >= MAX_CHANNELS
            ):
                break

    else:

        # 100 pages is a safety cap.
        for page_number in range(
            2,
            101
        ):

            page_url = (
                f"{JIOTV_PAGE}?"
                f"{urlencode({'category': category})}"
                f"&page={page_number}"
            )

            try:

                response = session.get(
                    page_url,
                    timeout=30
                )

                if response.status_code >= 400:
                    break

                items = parse_jiotv_channels(
                    response.text,
                    category
                )

            except Exception:
                break

            if not items:
                break

            new_count = 0

            for item in items:

                slug = item["slug"]

                if slug in seen_slugs:
                    continue

                seen_slugs.add(slug)
                collected.append(item)
                new_count += 1

            if new_count == 0:
                break

            if (
                MAX_CHANNELS
                and len(collected) >= MAX_CHANNELS
            ):
                break

            # A short page indicates end.
            if len(items) < PER_PAGE:
                break

    if MAX_CHANNELS:
        collected = collected[
            :MAX_CHANNELS
        ]

    return (
        category,
        collected
    )


# ============================================================
# JIOTV DISCOVERY
# ============================================================

def discover_jiotv(
    browser
):

    print()
    print("=" * 76)
    print("JIOTV TAB")
    print("=" * 76)
    print(
        f"Exact source page: {JIOTV_PAGE}"
    )

    context = browser.new_context(
        viewport={
            "width": 1440,
            "height": 1000
        }
    )

    page = context.new_page()

    page.goto(
        JIOTV_PAGE,
        wait_until="domcontentloaded",
        timeout=60000
    )

    try:
        page.wait_for_load_state(
            "networkidle",
            timeout=20000
        )
    except Exception:
        pass

    page.wait_for_timeout(
        2500
    )

    browser_cookies = context.cookies()

    # --------------------------------------------------------
    # Read exact JioTV tab links from DOM
    # --------------------------------------------------------

    try:
        main_html = page.content()
    except Exception:
        main_html = ""

    categories = parse_jiotv_tabs(
        main_html
    )

    # DOM fallback.
    if not categories:

        try:

            dom_tabs = page.locator(
                "a.jio-tab"
            ).evaluate_all(
                """
                els => els.map(a => ({
                    href: a.getAttribute("href") || "",
                    text: (a.innerText || "").trim()
                }))
                """
            )

        except Exception:

            dom_tabs = []

        seen = set()

        for tab in dom_tabs:

            href = clean(
                tab.get(
                    "href",
                    ""
                )
            )

            parsed = urlparse(
                urljoin(
                    JIOTV_PAGE,
                    href
                )
            )

            params = parse_qs(
                parsed.query
            )

            values = params.get(
                "category",
                []
            )

            if not values:
                continue

            category = clean(
                values[0]
            )

            if not category:
                continue

            if category.lower() == "all":
                continue

            if category in seen:
                continue

            seen.add(category)
            categories.append(category)

    context.close()

    print()
    print(
        f"JioTV categories found: "
        f"{len(categories)}"
    )

    for index, category in enumerate(
        categories,
        1
    ):
        print(
            f"  {index:02d}. {category}"
        )

    # --------------------------------------------------------
    # Fetch every category directly by URL.
    # NO clicking.
    # --------------------------------------------------------

    grouped = {}

    print()
    print(
        "Fetching JioTV categories..."
    )

    workers = min(
        WORKERS,
        8
    )

    with ThreadPoolExecutor(
        max_workers=workers
    ) as pool:

        futures = [
            pool.submit(
                fetch_jiotv_category,
                category,
                browser_cookies
            )
            for category in categories
        ]

        for future in as_completed(
            futures
        ):

            try:

                category, items = (
                    future.result()
                )

                grouped[
                    category
                ] = items

                print(
                    f"  {category}: "
                    f"{len(items)} channels"
                )

            except Exception as error:

                print(
                    "  Category error:",
                    error
                )

    # --------------------------------------------------------
    # Preserve website tab order
    # --------------------------------------------------------

    final_channels = []
    group_order = []

    seen = set()

    for category in categories:

        items = grouped.get(
            category,
            []
        )

        if items:
            group_order.append(
                category
            )

        for item in items:

            key = (
                item["slug"],
                category
            )

            if key in seen:
                continue

            seen.add(key)
            final_channels.append(item)

    print()
    print(
        f"JioTV channels discovered: "
        f"{len(final_channels)}"
    )

    print()
    print(
        "JioTV subgroups:"
    )

    for group in group_order:

        count = sum(
            1
            for item in final_channels
            if item.get(
                "group"
            ) == group
        )

        print(
            f"  {group}: {count}"
        )

    return (
        final_channels,
        group_order,
        browser_cookies,
        categories,
        grouped
    )


# ============================================================
# STREAM DISCOVERY
# ============================================================

def fetch_streams(
    session,
    channel
):

    slug = channel["slug"]

    data = json_get(
        session,
        f"{API}/api/channels/{slug}/streams"
    )

    if not isinstance(data, dict):
        return (
            slug,
            []
        )

    streams = data.get(
        "streams",
        []
    )

    if not isinstance(
        streams,
        list
    ):
        return (
            slug,
            []
        )

    return (
        slug,
        streams
    )


# ============================================================
# RESOLVE STREAM
# ============================================================

def resolve_stream(
    session,
    channel,
    stream
):

    slug = channel["slug"]

    stream_id = (
        stream.get("stream_id")
        or stream.get("id")
    )

    if stream_id is None:
        return None

    resolve_url = (
        f"{API}/api/channels/"
        f"{slug}/streams/"
        f"{stream_id}/resolve"
    )

    try:

        response = session.get(
            resolve_url,
            timeout=API_TIMEOUT
        )

        if response.status_code >= 400:
            return None

        data = response.json()

    except Exception:
        return None

    if not isinstance(
        data,
        dict
    ):
        return None

    # --------------------------------------------------------
    # DRM detection.
    #
    # Do not extract/decrypt keys.
    # Keep metadata separately.
    # --------------------------------------------------------

    drm = bool(
        clean(
            data.get(
                "drm_key"
            )
        )
        or clean(
            data.get(
                "license_key_url"
            )
        )
        or clean(
            data.get(
                "license_url"
            )
        )
    )

    license_url = clean(
        data.get(
            "license_key_url"
        )
        or data.get(
            "license_url"
        )
    )

    resolved_url = clean(
        data.get(
            "resolved_url"
        )
        or data.get(
            "url"
        )
        or stream.get(
            "url"
        )
    )

    if not resolved_url.startswith(
        (
            "http://",
            "https://"
        )
    ):
        return None

    needs_proxy = bool(
        data.get(
            "needs_proxy"
        )
    )

    proxy_url = clean(
        data.get(
            "proxy_url"
        )
    )

    if (
        needs_proxy
        and proxy_url
    ):

        if proxy_url.startswith("/"):
            proxy_url = (
                API
                + proxy_url
            )

        final_url = proxy_url
        used_proxy = True

    else:

        final_url = resolved_url
        used_proxy = False

    headers = {}

    raw_headers = data.get(
        "headers"
    )

    if isinstance(
        raw_headers,
        dict
    ):

        for key, value in raw_headers.items():

            if value is not None:
                headers[
                    str(key)
                ] = str(value)

    if (
        "User-Agent"
        not in headers
    ):
        headers[
            "User-Agent"
        ] = USER_AGENT

    return {
        "channel": channel,
        "stream": stream,
        "stream_id": stream_id,
        "url": final_url,
        "resolved_url": resolved_url,
        "headers": headers,
        "proxy": used_proxy,
        "drm": drm,
        "license_url": license_url,
        "type": clean(
            stream.get(
                "type"
            )
        ).lower(),
        "source": clean(
            stream.get(
                "source"
            )
        ),
        "jio": bool(
            data.get(
                "jio"
            )
        )
    }


# ============================================================
# VALIDATE STREAM
# ============================================================

def validate_stream(
    session,
    item
):

    url = item["url"]

    # DRM streams are retained only in sidecar metadata.
    if item.get("drm"):
        return None

    try:

        response = session.get(
            url,
            headers=item.get(
                "headers",
                {}
            ),
            timeout=STREAM_TIMEOUT,
            stream=True,
            allow_redirects=True
        )

        if response.status_code >= 400:
            response.close()
            return None

        content_type = clean(
            response.headers.get(
                "Content-Type",
                ""
            )
        ).lower()

        first = b""

        try:

            for chunk in response.iter_content(
                chunk_size=8192
            ):

                if chunk:
                    first = chunk
                    break

        finally:
            response.close()

        if not first:
            return None

        sample = first.lstrip()

        lower_sample = (
            sample[:4096]
            .decode(
                "utf-8",
                errors="ignore"
            )
            .lower()
        )

        # Reject obvious HTML / JSON.
        if (
            "text/html" in content_type
            or "application/json" in content_type
            or lower_sample.startswith(
                "<!doctype"
            )
            or lower_sample.startswith(
                "<html"
            )
            or lower_sample.startswith(
                "{"
            )
            or lower_sample.startswith(
                "["
            )
        ):
            return None

        url_lower = url.lower()

        # HLS.
        if (
            "#extm3u" in lower_sample
            or "mpegurl" in content_type
            or "m3u8" in url_lower
        ):
            return item

        # DASH.
        if (
            "<mpd" in lower_sample
            or "dash+xml" in content_type
            or ".mpd" in url_lower
        ):
            return item

        # MPEG-TS.
        if (
            len(first) >= 188
            and first[0] == 0x47
        ):
            return item

        # Direct media.
        if (
            "video/" in content_type
            or "audio/" in content_type
            or "octet-stream" in content_type
        ):
            return item

        return None

    except Exception:
        return None


# ============================================================
# M3U
# ============================================================

def m3u_escape(
    value
):
    return (
        clean(value)
        .replace(
            '"',
            "'"
        )
    )


def build_url(
    item
):
    return item["url"]


def write_playlist(
    items
):

    OUTPUT.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temp = OUTPUT.with_suffix(
        ".tmp"
    )

    with temp.open(
        "w",
        encoding="utf-8",
        newline="\n"
    ) as file:

        file.write(
            "#EXTM3U\n"
        )

        for item in items:

            channel = item[
                "channel"
            ]

            name = m3u_escape(
                channel.get(
                    "name"
                )
            )

            slug = m3u_escape(
                channel.get(
                    "slug"
                )
            )

            group = m3u_escape(
                channel.get(
                    "group"
                )
                or "Other"
            )

            logo = m3u_escape(
                channel.get(
                    "logo_url"
                )
            )

            source = m3u_escape(
                channel.get(
                    "source"
                )
                or ""
            )

            line = (
                "#EXTINF:-1 "
                f'tvg-id="{slug}" '
                f'tvg-name="{name}" '
            )

            if logo:
                line += (
                    f'tvg-logo="{logo}" '
                )

            line += (
                f'group-title="{group}",'
                f'{name}\n'
            )

            file.write(
                line
            )

            # Optional VLC metadata for known referrer.
            file.write(
                "#EXTVLCOPT:http-referrer="
                f"{SITE}/\n"
            )

            if source:
                file.write(
                    "#EXTGRP:"
                    f"{group}\n"
                )

            file.write(
                build_url(item)
                + "\n"
            )

    temp.replace(
        OUTPUT
    )


# ============================================================
# MAIN
# ============================================================

def main():

    started = time.time()

    print()
    print("=" * 76)
    print(
        "GDLTV INDIA + JIOTV MASTER EXTRACTOR"
    )
    print("=" * 76)

    # --------------------------------------------------------
    # Browser
    # --------------------------------------------------------

    chrome = find_local_chrome()

    browser_kwargs = {}

    if chrome:

        browser_kwargs[
            "executable_path"
        ] = chrome

        print(
            "Browser: system Chrome"
        )

    else:

        print(
            "Browser: Playwright Chromium"
        )

    # --------------------------------------------------------
    # JioTV category discovery
    # --------------------------------------------------------

    with sync_playwright() as playwright:

        browser = playwright.chromium.launch(
            headless=True,
            **browser_kwargs
        )

        (
            jio_channels,
            jio_group_order,
            browser_cookies,
            jio_categories,
            jio_category_map
        ) = discover_jiotv(
            browser
        )

        browser.close()

    # --------------------------------------------------------
    # API session
    # --------------------------------------------------------

    session = make_session(
        browser_cookies,
        referer=SITE
    )

    # --------------------------------------------------------
    # India
    # --------------------------------------------------------

    india_channels = discover_india(
        session
    )

    # --------------------------------------------------------
    # Combine India + JioTV
    # --------------------------------------------------------

    all_channels = []

    seen_channels = set()

    for channel in (
        india_channels
        + jio_channels
    ):

        key = (
            channel["slug"],
            channel.get(
                "group",
                ""
            )
        )

        if key in seen_channels:
            continue

        seen_channels.add(
            key
        )

        all_channels.append(
            channel
        )

    if MAX_CHANNELS:
        all_channels = all_channels[
            :MAX_CHANNELS
        ]

    print()
    print("=" * 76)
    print(
        f"India channels : "
        f"{len(india_channels)}"
    )
    print(
        f"JioTV channels : "
        f"{len(jio_channels)}"
    )
    print(
        f"Combined       : "
        f"{len(all_channels)}"
    )
    print("=" * 76)

    if not all_channels:

        print(
            "ERROR: No channels discovered."
        )

        print(
            "Existing playlist was NOT overwritten."
        )

        return 1

    # --------------------------------------------------------
    # Unique slugs for stream API calls
    # --------------------------------------------------------

    slug_map = {}

    for channel in all_channels:

        slug = channel["slug"]

        if slug not in slug_map:
            slug_map[
                slug
            ] = channel

    print()
    print(
        "Unique channel slugs:",
        len(slug_map)
    )

    # --------------------------------------------------------
    # Fetch streams
    # --------------------------------------------------------

    stream_cache = {}

    print()
    print(
        f"Fetching streams for "
        f"{len(slug_map)} unique channels..."
    )

    with ThreadPoolExecutor(
        max_workers=WORKERS
    ) as pool:

        futures = [
            pool.submit(
                fetch_streams,
                session,
                channel
            )
            for channel in slug_map.values()
        ]

        for future in as_completed(
            futures
        ):

            try:

                slug, streams = (
                    future.result()
                )

                stream_cache[
                    slug
                ] = streams

            except Exception:
                pass

    # --------------------------------------------------------
    # Make jobs.
    #
    # A JioTV channel can belong to multiple categories;
    # every category keeps its own group-title.
    # --------------------------------------------------------

    jobs = []

    for channel in all_channels:

        streams = stream_cache.get(
            channel["slug"],
            []
        )

        for stream in streams:

            jobs.append(
                (
                    channel,
                    stream
                )
            )

    print(
        f"Source streams found: "
        f"{len(jobs)}"
    )

    # --------------------------------------------------------
    # Resolve
    # --------------------------------------------------------

    print(
        "Resolving streams..."
    )

    resolved = []

    drm_items = []

    with ThreadPoolExecutor(
        max_workers=WORKERS
    ) as pool:

        futures = [
            pool.submit(
                resolve_stream,
                session,
                channel,
                stream
            )
            for channel, stream in jobs
        ]

        for future in as_completed(
            futures
        ):

            try:

                item = future.result()

                if not item:
                    continue

                resolved.append(
                    item
                )

                if item.get(
                    "drm"
                ):

                    channel = item[
                        "channel"
                    ]

                    drm_items.append({
                        "name": channel.get(
                            "name"
                        ),
                        "slug": channel.get(
                            "slug"
                        ),
                        "group": channel.get(
                            "group"
                        ),
                        "source": channel.get(
                            "source"
                        ),
                        "stream_id": item.get(
                            "stream_id"
                        ),
                        "manifest_or_stream_url":
                            item.get(
                                "resolved_url"
                            ),
                        "license_url":
                            item.get(
                                "license_url"
                            ),
                        "type": item.get(
                            "type"
                        ),
                        "proxy": item.get(
                            "proxy"
                        )
                    })

            except Exception:
                pass

    print(
        f"Resolved candidates: "
        f"{len(resolved)}"
    )

    print(
        f"DRM candidates: "
        f"{len(drm_items)}"
    )

    # --------------------------------------------------------
    # Validate non-DRM
    # --------------------------------------------------------

    print(
        "Checking streams..."
    )

    valid = []

    non_drm = [
        item
        for item in resolved
        if not item.get(
            "drm"
        )
    ]

    with ThreadPoolExecutor(
        max_workers=WORKERS
    ) as pool:

        futures = [
            pool.submit(
                validate_stream,
                session,
                item
            )
            for item in non_drm
        ]

        for future in as_completed(
            futures
        ):

            try:

                item = future.result()

                if item:
                    valid.append(
                        item
                    )

            except Exception:
                pass

    print(
        f"Valid streams: "
        f"{len(valid)}"
    )

    # --------------------------------------------------------
    # Deduplicate
    # --------------------------------------------------------

    unique = []
    seen = set()

    for item in valid:

        channel = item[
            "channel"
        ]

        key = (
            channel.get(
                "slug"
            ),
            channel.get(
                "group",
                ""
            ),
            item.get(
                "url"
            ),
            json.dumps(
                item.get(
                    "headers",
                    {}
                ),
                sort_keys=True
            )
        )

        if key in seen:
            continue

        seen.add(key)

        unique.append(
            item
        )

    # --------------------------------------------------------
    # Group ordering:
    # JioTV categories first in exact website order,
    # then India groups in discovery order.
    # --------------------------------------------------------

    group_rank = {}

    rank = 0

    for group in jio_group_order:

        key = clean(
            group
        ).lower()

        if key not in group_rank:

            group_rank[
                key
            ] = rank

            rank += 1

    for channel in india_channels:

        group = clean(
            channel.get(
                "group"
            )
        ) or "Other"

        key = group.lower()

        if key not in group_rank:

            group_rank[
                key
            ] = rank

            rank += 1

    channel_rank = {}

    for index, channel in enumerate(
        all_channels
    ):

        channel_rank[
            (
                channel["slug"],
                channel.get(
                    "group",
                    ""
                )
            )
        ] = index

    unique.sort(
        key=lambda item: (
            group_rank.get(
                clean(
                    item["channel"].get(
                        "group"
                    )
                ).lower(),
                999999
            ),
            channel_rank.get(
                (
                    item["channel"]["slug"],
                    item["channel"].get(
                        "group",
                        ""
                    )
                ),
                999999
            ),
            str(
                item.get(
                    "stream_id",
                    ""
                )
            )
        )
    )

    # --------------------------------------------------------
    # Safety:
    # Never destroy an existing playlist because an API
    # failure produced zero playable streams.
    # --------------------------------------------------------

    if not unique:

        print()
        print(
            "ERROR: 0 valid playable streams."
        )

        print(
            "Existing playlist was NOT overwritten."
        )

        return 1

    # --------------------------------------------------------
    # Write playlist
    # --------------------------------------------------------

    write_playlist(
        unique
    )

    # --------------------------------------------------------
    # DRM metadata
    # --------------------------------------------------------

    DRM_JSON.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with DRM_JSON.open(
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            drm_items,
            file,
            ensure_ascii=False,
            indent=2
        )

    # --------------------------------------------------------
    # Report
    # --------------------------------------------------------

    valid_groups = {}

    for item in unique:

        group = clean(
            item["channel"].get(
                "group"
            )
        ) or "Other"

        valid_groups[group] = (
            valid_groups.get(
                group,
                0
            ) + 1
        )

    india_groups = {}

    for channel in india_channels:

        group = clean(
            channel.get(
                "group"
            )
        ) or "Other"

        india_groups[group] = (
            india_groups.get(
                group,
                0
            ) + 1
        )

    jio_groups = {}

    for channel in jio_channels:

        group = clean(
            channel.get(
                "group"
            )
        ) or "Other"

        jio_groups[group] = (
            jio_groups.get(
                group,
                0
            ) + 1
        )

    elapsed = (
        time.time()
        - started
    )

    report = [
        "GDLTV INDIA + JIOTV MASTER EXTRACTION REPORT",
        "=" * 76,
        "",
        "SOURCES",
        "-" * 76,
        f"in India : {INDIA_PAGE}",
        f"JioTV    : {JIOTV_PAGE}",
        "",
        "DISCOVERY",
        "-" * 76,
        f"India channels        : {len(india_channels)}",
        f"JioTV channels        : {len(jio_channels)}",
        f"Combined entries      : {len(all_channels)}",
        f"Unique slugs          : {len(slug_map)}",
        f"Source streams        : {len(jobs)}",
        f"Resolved              : {len(resolved)}",
        f"DRM detected          : {len(drm_items)}",
        f"Non-DRM valid         : {len(valid)}",
        f"Unique saved          : {len(unique)}",
        "",
        "JIOTV CATEGORIES",
        "-" * 76
    ]

    for group in jio_group_order:

        report.append(
            f"{group}: "
            f"{jio_groups.get(group, 0)}"
        )

    report += [
        "",
        "INDIA GROUPS",
        "-" * 76
    ]

    for group, count in sorted(
        india_groups.items(),
        key=lambda x: x[0].lower()
    ):

        report.append(
            f"{group}: {count}"
        )

    report += [
        "",
        "VALID PLAYLIST STREAMS BY GROUP",
        "-" * 76
    ]

    for group, count in sorted(
        valid_groups.items(),
        key=lambda x: x[0].lower()
    ):

        report.append(
            f"{group}: {count}"
        )

    report += [
        "",
        f"Generated in: {elapsed:.1f}s"
    ]

    REPORT.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with REPORT.open(
        "w",
        encoding="utf-8"
    ) as file:

        file.write(
            "\n".join(report)
        )

    # --------------------------------------------------------
    # Debug JSON
    # --------------------------------------------------------

    debug = {
        "generated_at": time.strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "sources": {
            "india": INDIA_PAGE,
            "jiotv": JIOTV_PAGE
        },
        "jiotv_categories": jio_categories,
        "jiotv_category_counts": {
            key: len(value)
            for key, value
            in jio_category_map.items()
        },
        "counts": {
            "india_channels":
                len(india_channels),
            "jiotv_channels":
                len(jio_channels),
            "combined_channels":
                len(all_channels),
            "unique_slugs":
                len(slug_map),
            "stream_jobs":
                len(jobs),
            "resolved":
                len(resolved),
            "drm":
                len(drm_items),
            "valid":
                len(valid),
            "unique":
                len(unique)
        },
        "india_groups":
            india_groups,
        "jiotv_groups":
            jio_groups,
        "valid_groups":
            valid_groups,
        "jiotv_channels":
            jio_channels
    }

    DEBUG.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with DEBUG.open(
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            debug,
            file,
            ensure_ascii=False,
            indent=2
        )

    # --------------------------------------------------------
    # FINAL
    # --------------------------------------------------------

    print()
    print("=" * 76)
    print("COMPLETE")
    print("=" * 76)
    print(
        f"India channels : {len(india_channels)}"
    )
    print(
        f"JioTV channels : {len(jio_channels)}"
    )
    print(
        f"Combined       : {len(all_channels)}"
    )
    print(
        f"JioTV groups   : {len(jio_group_order)}"
    )
    print(
        f"Streams found  : {len(jobs)}"
    )
    print(
        f"DRM detected   : {len(drm_items)}"
    )
    print(
        f"Streams saved  : {len(unique)}"
    )
    print(
        f"Playlist       : {OUTPUT}"
    )
    print(
        f"Report         : {REPORT}"
    )
    print(
        f"DRM metadata   : {DRM_JSON}"
    )
    print(
        f"Debug          : {DEBUG}"
    )
    print(
        f"Time           : {elapsed:.1f}s"
    )
    print("=" * 76)

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
