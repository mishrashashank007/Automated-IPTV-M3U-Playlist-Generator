from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter, defaultdict
import json
import re
import sys
import time


# ================================================================
# CONFIG
# ================================================================

API_ROOT = "https://api.freeforall.dev"

JIOTV_CHANNEL_API = (
    f"{API_ROOT}/api/jiotv/channels"
)

GENERIC_CHANNEL_API = (
    f"{API_ROOT}/api/channels"
)

OUTPUT = Path(
    "LiveTV/India/India-JioTV.m3u"
)

REPORT = Path(
    "reports/jiotv_master_report.txt"
)

DEBUG_JSON = Path(
    "debug/jiotv_master_debug.json"
)

BACKUP = OUTPUT.with_suffix(
    OUTPUT.suffix + ".bak"
)

WORKERS = int(
    __import__("os").environ.get("WORKERS", "12")
)

TIMEOUT = 25


# Exact JioTV website categories/counts
EXPECTED = {
    "News": 349,
    "Entertainment": 136,
    "International": 122,
    "Faith": 91,
    "Kids": 56,
    "Documentary": 54,
    "Music": 45,
    "Movies": 29,
    "JIO+4 (IND IP)": 26,
    "Science": 25,
    "Local News": 19,
    "Education": 19,
    "JIO TV WW": 11,
    "Broadcast": 11,
    "Regional": 10,
    "Lifestyle": 9,
    "Spiritual": 8,
    "Devotional": 7,
    "Nature": 5,
    "Shopping": 4,
}


BEGIN_MARKER = "# GDLTV-JIOTV-BEGIN"
END_MARKER = "# GDLTV-JIOTV-END"


# ================================================================
# HTTP
# ================================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/154.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
}


def http_json(url):
    request = Request(
        url,
        headers=HEADERS
    )

    with urlopen(
        request,
        timeout=TIMEOUT
    ) as response:

        body = response.read()

        return json.loads(
            body.decode(
                "utf-8",
                errors="replace"
            )
        )


def http_bytes(url):
    request = Request(
        url,
        headers={
            **HEADERS,
            "Accept": "*/*",
        }
    )

    with urlopen(
        request,
        timeout=TIMEOUT
    ) as response:

        content_type = response.headers.get(
            "content-type",
            ""
        )

        data = response.read(
            1024 * 1024
        )

        return response.status, content_type, data


# ================================================================
# HELPERS
# ================================================================

def clean(value):
    if value is None:
        return ""

    return re.sub(
        r"\s+",
        " ",
        str(value)
    ).strip()


def slug_of(channel):
    value = (
        channel.get("slug")
        or channel.get("id")
        or channel.get("channel_id")
    )

    if value is None:
        return ""

    return str(value)


def channel_name(channel):
    return clean(
        channel.get("name")
        or channel.get("title")
        or channel.get("channel_name")
        or slug_of(channel)
    )


def valid_http_url(url):
    return (
        isinstance(url, str)
        and url.startswith(
            (
                "http://",
                "https://"
            )
        )
    )


def unique_by_slug(channels):
    result = {}

    for channel in channels:
        slug = slug_of(channel)

        if slug:
            result[slug] = channel

    return list(
        result.values()
    )


# ================================================================
# CHANNEL DISCOVERY
# ================================================================

def fetch_jiotv_channels():

    print()
    print("=" * 76)
    print("JIOTV CHANNEL DISCOVERY")
    print("=" * 76)

    first_url = (
        f"{JIOTV_CHANNEL_API}?page=1"
    )

    print()
    print(
        f"API: {first_url}"
    )

    try:
        first = http_json(
            first_url
        )
    except Exception as exc:
        print()
        print(
            f"ERROR: JioTV API failed: {exc}"
        )
        return None

    if not isinstance(first, dict):
        print()
        print(
            "ERROR: Unexpected API response."
        )
        return None

    total = first.get(
        "total"
    )

    total_all = first.get(
        "total_all"
    )

    total_pages = first.get(
        "total_pages"
    )

    per_page = first.get(
        "per_page"
    )

    channels = first.get(
        "channels",
        []
    )

    categories_payload = first.get(
        "categories",
        []
    )

    print()
    print(
        f"API total       : {total}"
    )

    print(
        f"API total_all   : {total_all}"
    )

    print(
        f"API pages       : {total_pages}"
    )

    print(
        f"Per page        : {per_page}"
    )

    print(
        f"Page 1 channels : {len(channels)}"
    )

    # ------------------------------------------------------------
    # Validate category metadata from API
    # ------------------------------------------------------------

    api_category_counts = {}

    if isinstance(
        categories_payload,
        list
    ):
        for item in categories_payload:

            if not isinstance(
                item,
                dict
            ):
                continue

            name = clean(
                item.get("name")
            )

            count = item.get(
                "count"
            )

            if name:
                try:
                    api_category_counts[
                        name
                    ] = int(count)
                except Exception:
                    pass

    print()
    print("API CATEGORY VALIDATION")
    print("-" * 76)

    category_metadata_ok = True

    for category, expected_count in EXPECTED.items():

        actual = api_category_counts.get(
            category
        )

        status = (
            "OK"
            if actual == expected_count
            else "MISMATCH"
        )

        print(
            f"{category:<22}"
            f" API={str(actual):<4}"
            f" WEBSITE={expected_count:<4}"
            f" {status}"
        )

        if actual != expected_count:
            category_metadata_ok = False

    if not category_metadata_ok:

        print()
        print(
            "ERROR: API category metadata does not match JioTV website."
        )
        print(
            "NO M3U FILE WILL BE CHANGED."
        )

        return None

    # ------------------------------------------------------------
    # Pagination metadata is ROOT LEVEL
    # ------------------------------------------------------------

    try:
        total_pages = int(
            total_pages
        )
    except Exception:

        print()
        print(
            "ERROR: total_pages missing."
        )

        return None

    if total_pages <= 0:

        print()
        print(
            "ERROR: invalid total_pages."
        )

        return None

    all_channels = list(
        channels
    )

    print()
    print(
        "FETCHING ALL JIOTV PAGES"
    )
    print("-" * 76)

    for page_number in range(
        2,
        total_pages + 1
    ):

        url = (
            f"{JIOTV_CHANNEL_API}"
            f"?page={page_number}"
        )

        try:

            payload = http_json(
                url
            )

            page_channels = payload.get(
                "channels",
                []
            )

            if not isinstance(
                page_channels,
                list
            ):
                page_channels = []

            all_channels.extend(
                page_channels
            )

            print(
                f"Page {page_number:>2}/{total_pages}: "
                f"{len(page_channels)} channels"
            )

        except Exception as exc:

            print(
                f"Page {page_number:>2}/{total_pages}: "
                f"ERROR {exc}"
            )

            return None

    all_channels = unique_by_slug(
        all_channels
    )

    # ------------------------------------------------------------
    # Select ONLY the 20 actual JioTV categories
    # ------------------------------------------------------------

    selected = defaultdict(list)

    allowed = set(
        EXPECTED
    )

    for channel in all_channels:

        category = clean(
            channel.get(
                "category"
            )
        )

        if category in allowed:
            selected[
                category
            ].append(
                channel
            )

    selected_total = sum(
        len(value)
        for value in selected.values()
    )

    expected_total = sum(
        EXPECTED.values()
    )

    print()
    print("=" * 76)
    print("JIOTV CATEGORY RESULT")
    print("=" * 76)

    for category, expected_count in EXPECTED.items():

        actual = len(
            selected.get(
                category,
                []
            )
        )

        status = (
            "OK"
            if actual == expected_count
            else "MISMATCH"
        )

        print(
            f"{category:<22}"
            f" {actual:<4}"
            f" expected={expected_count:<4}"
            f" {status}"
        )

    print()
    print(
        f"All API channels        : {len(all_channels)}"
    )

    print(
        f"Expected JioTV channels : {expected_total}"
    )

    print(
        f"Selected JioTV channels : {selected_total}"
    )

    # ------------------------------------------------------------
    # HARD SAFETY CHECK
    # ------------------------------------------------------------

    if selected_total != expected_total:

        print()
        print(
            "ERROR: JioTV category selection is incomplete."
        )

        print(
            "NO STREAM DISCOVERY WILL RUN."
        )

        print(
            "NO M3U FILE WILL BE CHANGED."
        )

        return None

    for category, expected_count in EXPECTED.items():

        actual = len(
            selected.get(
                category,
                []
            )
        )

        if actual != expected_count:

            print()
            print(
                f"ERROR: {category} count invalid."
            )

            print(
                "NO M3U FILE WILL BE CHANGED."
            )

            return None

    return selected


# ================================================================
# STREAM DISCOVERY
# ================================================================

def get_stream_payloads(slug):

    encoded = quote(
        str(slug),
        safe=""
    )

    urls = [
        (
            f"{JIOTV_CHANNEL_API}"
            f"/{encoded}/streams"
        ),
        (
            f"{GENERIC_CHANNEL_API}"
            f"/{encoded}/streams"
        ),
    ]

    results = []

    for url in urls:

        try:

            payload = http_json(
                url
            )

            if isinstance(
                payload,
                dict
            ):

                results.append(
                    payload
                )

                if payload.get(
                    "streams"
                ):
                    break

            elif isinstance(
                payload,
                list
            ):

                results.append(
                    {
                        "streams": payload
                    }
                )

                break

        except Exception:
            continue

    return results


def extract_streams(payload):

    streams = []

    if isinstance(
        payload,
        dict
    ):

        direct = payload.get(
            "streams"
        )

        if isinstance(
            direct,
            list
        ):
            streams.extend(
                direct
            )

        for key in (
            "data",
            "results"
        ):

            value = payload.get(
                key
            )

            if isinstance(
                value,
                list
            ):

                for item in value:

                    if isinstance(
                        item,
                        dict
                    ):

                        if (
                            "stream_id" in item
                            or "id" in item
                            or "url" in item
                            or "stream_url" in item
                        ):
                            streams.append(
                                item
                            )

    elif isinstance(
        payload,
        list
    ):
        streams.extend(
            payload
        )

    # Deduplicate
    output = []
    seen = set()

    for stream in streams:

        if not isinstance(
            stream,
            dict
        ):
            continue

        sid = (
            stream.get(
                "stream_id"
            )
            or stream.get(
                "id"
            )
        )

        url = (
            stream.get(
                "url"
            )
            or stream.get(
                "stream_url"
            )
        )

        key = (
            str(sid),
            str(url)
        )

        if key in seen:
            continue

        seen.add(
            key
        )

        output.append(
            stream
        )

    return output


def resolve_stream(
    slug,
    stream
):

    stream_id = (
        stream.get(
            "stream_id"
        )
        or stream.get(
            "id"
        )
    )

    direct_url = (
        stream.get(
            "resolved_url"
        )
        or stream.get(
            "url"
        )
        or stream.get(
            "stream_url"
        )
    )

    # ------------------------------------------------------------
    # Direct stream candidate
    # ------------------------------------------------------------

    if valid_http_url(
        direct_url
    ):

        return {
            "url": direct_url,
            "headers": stream.get(
                "headers"
            ) or {},
            "drm": bool(
                stream.get("drm_key")
                or stream.get("license_key_url")
            ),
            "stream_id": stream_id,
            "source": "direct",
        }

    if stream_id is None:
        return None

    encoded_slug = quote(
        str(slug),
        safe=""
    )

    encoded_id = quote(
        str(stream_id),
        safe=""
    )

    resolve_urls = [
        (
            f"{GENERIC_CHANNEL_API}"
            f"/{encoded_slug}"
            f"/streams/{encoded_id}/resolve"
        ),
        (
            f"{JIOTV_CHANNEL_API}"
            f"/{encoded_slug}"
            f"/streams/{encoded_id}/resolve"
        ),
    ]

    for url in resolve_urls:

        try:

            payload = http_json(
                url
            )

            if not isinstance(
                payload,
                dict
            ):
                continue

            resolved_url = (
                payload.get(
                    "resolved_url"
                )
                or payload.get(
                    "url"
                )
            )

            proxy_url = payload.get(
                "proxy_url"
            )

            # ------------------------------------------------
            # DRM
            # ------------------------------------------------

            drm = bool(
                payload.get(
                    "drm_key"
                )
                or payload.get(
                    "license_key_url"
                )
            )

            # ------------------------------------------------
            # Prefer resolved URL, otherwise site proxy
            # ------------------------------------------------

            final_url = None
            source = None

            if valid_http_url(
                resolved_url
            ):

                final_url = resolved_url
                source = "resolved"

            elif valid_http_url(
                proxy_url
            ):

                final_url = proxy_url
                source = "proxy"

            if final_url:

                return {
                    "url": final_url,
                    "headers": (
                        payload.get(
                            "headers"
                        )
                        or stream.get(
                            "headers"
                        )
                        or {}
                    ),
                    "drm": drm,
                    "stream_id": stream_id,
                    "source": source,
                }

        except Exception:
            continue

    return None


def validate_media_url(url):

    if not valid_http_url(
        url
    ):
        return False

    try:

        status, content_type, data = http_bytes(
            url
        )

        if status < 200 or status >= 400:
            return False

        lowered_type = content_type.lower()

        text = data.decode(
            "utf-8",
            errors="ignore"
        )[:5000].lower()

        # HLS
        if (
            "#extm3u" in text
            or "mpegurl" in lowered_type
            or "vnd.apple.mpegurl" in lowered_type
        ):
            return True

        # DASH
        if (
            "<mpd" in text
            or "dash+xml" in lowered_type
        ):
            return True

        # Generic video
        if (
            "video/" in lowered_type
            or "application/octet-stream"
            in lowered_type
        ):
            return True

        # Some servers return empty body despite HTTP 200.
        # Treat common media extensions as acceptable.
        lower_url = url.lower()

        if lower_url.endswith(
            (
                ".m3u8",
                ".mpd",
                ".ts",
                ".m4s",
                ".mp4"
            )
        ):
            return True

    except Exception:
        return False

    return False


def discover_one_channel(
    category,
    channel
):

    slug = slug_of(
        channel
    )

    name = channel_name(
        channel
    )

    if not slug:
        return {
            "category": category,
            "channel": channel,
            "streams": [],
            "error": "missing-slug",
        }

    payloads = get_stream_payloads(
        slug
    )

    raw_streams = []

    for payload in payloads:

        raw_streams.extend(
            extract_streams(
                payload
            )
        )

    resolved = []
    drm_count = 0

    seen_stream_ids = set()

    for stream in raw_streams:

        stream_id = (
            stream.get(
                "stream_id"
            )
            or stream.get(
                "id"
            )
        )

        identity = str(
            stream_id
        )

        if identity in seen_stream_ids:
            continue

        seen_stream_ids.add(
            identity
        )

        result = resolve_stream(
            slug,
            stream
        )

        if not result:
            continue

        if result.get(
            "drm"
        ):
            drm_count += 1
            continue

        url = result.get(
            "url"
        )

        if not valid_http_url(
            url
        ):
            continue

        # Validate actual media endpoint
        if not validate_media_url(
            url
        ):
            continue

        resolved.append(
            {
                "category": category,
                "name": name,
                "slug": slug,
                "logo": channel.get(
                    "logo",
                    ""
                ),
                "language": clean(
                    channel.get(
                        "language"
                    )
                ),
                "url": url,
                "headers": result.get(
                    "headers"
                ) or {},
                "stream_id": stream_id,
                "source": result.get(
                    "source"
                ),
            }
        )

    return {
        "category": category,
        "channel": channel,
        "streams": resolved,
        "raw_streams": len(
            raw_streams
        ),
        "drm": drm_count,
    }


# ================================================================
# M3U
# ================================================================

def remove_old_jiotv_block(
    content
):

    pattern = re.compile(
        re.escape(
            BEGIN_MARKER
        )
        + r".*?"
        + re.escape(
            END_MARKER
        )
        + r"\s*",
        re.S
    )

    return pattern.sub(
        "",
        content
    )


def escape_m3u_attr(value):

    value = clean(
        value
    )

    return (
        value
        .replace(
            '"',
            "'"
        )
        .replace(
            "\n",
            " "
        )
    )


def build_m3u_block(
    valid_streams
):

    lines = [
        BEGIN_MARKER,
        "",
        "# JioTV channels generated automatically",
        "",
    ]

    # category -> streams
    grouped = defaultdict(list)

    for item in valid_streams:

        grouped[
            item["category"]
        ].append(
            item
        )

    for category in EXPECTED:

        items = grouped.get(
            category,
            []
        )

        if not items:
            continue

        lines.append(
            f"# ===== JioTV / {category} ====="
        )

        for item in items:

            name = escape_m3u_attr(
                item["name"]
            )

            slug = escape_m3u_attr(
                item["slug"]
            )

            logo = escape_m3u_attr(
                item.get(
                    "logo",
                    ""
                )
            )

            language = escape_m3u_attr(
                item.get(
                    "language",
                    ""
                )
            )

            group = (
                f"JioTV / {category}"
            )

            extinf = (
                '#EXTINF:-1 '
                f'tvg-id="{slug}" '
                f'tvg-name="{name}" '
            )

            if logo:
                extinf += (
                    f'tvg-logo="{logo}" '
                )

            if language:
                extinf += (
                    f'language="{language}" '
                )

            extinf += (
                f'group-title="{group}",'
                f'{name}'
            )

            lines.append(
                extinf
            )

            url = item[
                "url"
            ]

            headers = item.get(
                "headers"
            ) or {}

            # M3U pipe header syntax
            if headers:

                parts = []

                for key, value in headers.items():

                    key = clean(
                        key
                    )

                    value = clean(
                        value
                    )

                    if key and value:
                        parts.append(
                            f"{key}={value}"
                        )

                if parts:
                    url += (
                        "|"
                        + "&".join(
                            parts
                        )
                    )

            lines.append(
                url
            )

            lines.append("")

    lines.append(
        END_MARKER
    )

    return "\n".join(
        lines
    )


def update_playlist(
    valid_streams
):

    if not OUTPUT.exists():

        print()
        print(
            "ERROR: Existing India-JioTV.m3u not found:"
        )

        print(
            OUTPUT
        )

        return False

    original = OUTPUT.read_text(
        encoding="utf-8",
        errors="replace"
    )

    original_size = len(
        original
    )

    cleaned = remove_old_jiotv_block(
        original
    )

    block = build_m3u_block(
        valid_streams
    )

    final = (
        cleaned.rstrip()
        + "\n\n"
        + block
        + "\n"
    )

    # Backup current playlist
    BACKUP.write_text(
        original,
        encoding="utf-8"
    )

    OUTPUT.write_text(
        final,
        encoding="utf-8"
    )

    print()
    print(
        "PLAYLIST UPDATED"
    )

    print(
        f"Original size : {original_size}"
    )

    print(
        f"Final size    : {len(final)}"
    )

    print(
        f"Backup        : {BACKUP}"
    )

    return True


# ================================================================
# MAIN
# ================================================================

def main():

    started = time.time()

    print("=" * 76)
    print("GDLTV JIOTV FINAL MASTER")
    print("=" * 76)

    print()
    print(
        f"Workers: {WORKERS}"
    )

    # ------------------------------------------------------------
    # Existing playlist must exist
    # ------------------------------------------------------------

    if not OUTPUT.exists():

        print()
        print(
            "ERROR: Existing India-JioTV.m3u does not exist."
        )

        print(
            "Aborting so India content cannot be lost."
        )

        return 1

    # ------------------------------------------------------------
    # 1. Exact JioTV channel discovery
    # ------------------------------------------------------------

    categories = fetch_jiotv_channels()

    if categories is None:
        return 1

    all_jobs = []

    for category in EXPECTED:

        for channel in categories.get(
            category,
            []
        ):

            all_jobs.append(
                (
                    category,
                    channel
                )
            )

    print()
    print("=" * 76)
    print("STREAM DISCOVERY")
    print("=" * 76)

    print()
    print(
        f"JioTV channels to process: {len(all_jobs)}"
    )

    results = []

    completed = 0

    # ------------------------------------------------------------
    # Concurrent stream discovery
    # ------------------------------------------------------------

    with ThreadPoolExecutor(
        max_workers=WORKERS
    ) as pool:

        futures = [
            pool.submit(
                discover_one_channel,
                category,
                channel
            )
            for category, channel
            in all_jobs
        ]

        for future in as_completed(
            futures
        ):

            completed += 1

            try:

                result = future.result()

                results.append(
                    result
                )

            except Exception as exc:

                results.append(
                    {
                        "category": "",
                        "channel": {},
                        "streams": [],
                        "error": str(exc),
                    }
                )

            if (
                completed % 25 == 0
                or completed == len(futures)
            ):

                print(
                    f"Progress: "
                    f"{completed}/{len(futures)}"
                )

    # ------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------

    stream_found_channels = 0
    raw_streams = 0
    drm_count = 0

    valid_streams = []

    for result in results:

        streams = result.get(
            "streams",
            []
        )

        if streams:
            stream_found_channels += 1

        raw_streams += int(
            result.get(
                "raw_streams",
                0
            )
            or 0
        )

        drm_count += int(
            result.get(
                "drm",
                0
            )
            or 0
        )

        valid_streams.extend(
            streams
        )

    # ------------------------------------------------------------
    # Global URL deduplication
    # ------------------------------------------------------------

    unique_valid = []

    seen_urls = set()

    for item in valid_streams:

        url = item[
            "url"
        ]

        if url in seen_urls:
            continue

        seen_urls.add(
            url
        )

        unique_valid.append(
            item
        )

    # ------------------------------------------------------------
    # Per-category valid count
    # ------------------------------------------------------------

    valid_groups = {}

    for category in EXPECTED:

        valid_groups[
            category
        ] = len(
            [
                x
                for x in unique_valid
                if x["category"] == category
            ]
        )

    print()
    print("=" * 76)
    print("STREAM RESULT")
    print("=" * 76)

    print()
    print(
        f"Channels processed : {len(all_jobs)}"
    )

    print(
        f"Channels w/stream  : {stream_found_channels}"
    )

    print(
        f"Raw streams        : {raw_streams}"
    )

    print(
        f"DRM streams        : {drm_count}"
    )

    print(
        f"Valid streams      : {len(valid_streams)}"
    )

    print(
        f"Unique valid URLs  : {len(unique_valid)}"
    )

    print()

    for category in EXPECTED:

        print(
            f"{category:<22}"
            f"{valid_groups[category]}"
        )

    # ------------------------------------------------------------
    # Safety threshold
    # ------------------------------------------------------------

    MIN_VALID = 20

    if len(unique_valid) < MIN_VALID:

        print()
        print(
            "=" * 76
        )

        print(
            "ERROR: Too few valid streams."
        )

        print(
            f"Valid={len(unique_valid)}, "
            f"minimum={MIN_VALID}"
        )

        print()
        print(
            "Existing M3U was NOT changed."
        )

        # Debug only
        DEBUG_JSON.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        DEBUG_JSON.write_text(
            json.dumps(
                {
                    "status": "ABORTED",
                    "reason": "too_few_valid_streams",
                    "expected_channels": len(all_jobs),
                    "valid_streams": len(unique_valid),
                    "drm": drm_count,
                    "valid_groups": valid_groups,
                },
                indent=2,
                ensure_ascii=False
            ),
            encoding="utf-8"
        )

        return 2

    # ------------------------------------------------------------
    # 2. Update ONLY JioTV block
    # ------------------------------------------------------------

    if not update_playlist(
        unique_valid
    ):
        return 3

    # ------------------------------------------------------------
    # REPORT
    # ------------------------------------------------------------

    elapsed = (
        time.time()
        - started
    )

    REPORT.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    report = [
        "GDLTV JIOTV FINAL MASTER",
        "=" * 76,
        "",
        "SOURCE",
        "-" * 76,
        JIOTV_CHANNEL_API,
        "",
        "CHANNELS",
        "-" * 76,
        f"JioTV channels selected: {len(all_jobs)}",
        f"Raw streams: {raw_streams}",
        f"Channels with stream: {stream_found_channels}",
        f"DRM streams skipped: {drm_count}",
        f"Valid streams: {len(valid_streams)}",
        f"Unique valid URLs: {len(unique_valid)}",
        "",
        "VALID STREAMS BY CATEGORY",
        "-" * 76,
    ]

    for category in EXPECTED:

        report.append(
            f"{category}: "
            f"{valid_groups[category]}"
        )

    report += [
        "",
        "PLAYLIST BEHAVIOR",
        "-" * 76,
        "Existing India section: PRESERVED",
        "Existing JioTV block: REPLACED",
        "Append mode: YES",
        "",
        f"Output: {OUTPUT}",
        f"Backup: {BACKUP}",
        "",
        f"Time: {elapsed:.1f}s",
    ]

    REPORT.write_text(
        "\n".join(
            report
        ),
        encoding="utf-8"
    )

    # ------------------------------------------------------------
    # DEBUG
    # ------------------------------------------------------------

    debug = {
        "source": JIOTV_CHANNEL_API,
        "expected_categories": EXPECTED,
        "counts": {
            "channels_processed": len(all_jobs),
            "channels_with_stream": stream_found_channels,
            "raw_streams": raw_streams,
            "drm": drm_count,
            "valid": len(valid_streams),
            "unique_valid": len(unique_valid),
        },
        "valid_groups": valid_groups,
    }

    DEBUG_JSON.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    DEBUG_JSON.write_text(
        json.dumps(
            debug,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )

    # ------------------------------------------------------------
    # FINAL
    # ------------------------------------------------------------

    print()
    print("=" * 76)
    print("JIOTV APPEND COMPLETE")
    print("=" * 76)

    print()
    print(
        f"JioTV channels : {len(all_jobs)}"
    )

    print(
        f"Unique streams : {len(unique_valid)}"
    )

    print(
        f"DRM skipped    : {drm_count}"
    )

    print()
    print(
        "India section: PRESERVED"
    )

    print(
        "JioTV section: REFRESHED"
    )

    print()
    print(
        f"UPDATED FILE: {OUTPUT}"
    )

    print(
        f"REPORT: {REPORT}"
    )

    print(
        f"Time: {elapsed:.1f}s"
    )

    print("=" * 76)

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
