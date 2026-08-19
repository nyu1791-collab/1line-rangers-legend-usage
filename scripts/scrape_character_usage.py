# File: scripts/scrape_character_usage.py
"""
LINE Rangers HandbookのPvP Trackerから、
レジェンド帯プレイヤーの防衛チームを集計する。

集計項目:
- occurrence_count:
    同一プレイヤー内の重複キャラクターを含む総編成数
- player_count:
    対象キャラクターを1体以上採用したプレイヤー数
- adoption_rate:
    player_count / sampled_players
- slot_rate:
    occurrence_count / character_slots

同じ編成の別プレイヤーは、それぞれ別人として集計する。
同じプレイヤーが同一キャラクターを複数体使用している場合、
occurrence_countには使用された体数分を加算する。
"""

import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from playwright.sync_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


TARGET_URL = "https://rangers.lerico.net/ja/pvp-tracker"
SOURCE_NAME = "LINE Rangers Handbook PvP Tracker"

TARGET_PLAYER_COUNT = int(
    os.environ.get("TARGET_PLAYER_COUNT", "200")
)

MIN_REQUIRED_PLAYERS = int(
    os.environ.get("MIN_REQUIRED_PLAYERS", "50")
)

MIN_CHARACTERS_PER_PLAYER = int(
    os.environ.get("MIN_CHARACTERS_PER_PLAYER", "5")
)

MAX_CHARACTERS_PER_PLAYER = int(
    os.environ.get("MAX_CHARACTERS_PER_PLAYER", "15")
)

DEBUG = os.environ.get("DEBUG", "0") == "1"

OUTPUT_PATH = Path("docs/data/character_usage.json")
DEBUG_DIR = Path(".artifacts/debug")

MAX_PAGES = 30
MAX_DYNAMIC_LOAD_ATTEMPTS = 60
DYNAMIC_LOAD_WAIT_MS = 450
STABLE_LOAD_ATTEMPTS = 6

ROW_SELECTORS = [
    "table tbody tr",
    "[role='rowgroup'] [role='row']",
    ".ranking-table tbody tr",
    ".ranking-list .ranking-row",
    ".player-list .player-row",
    ".player-card",
    "[data-player-id]",
    "[data-rank]",
    "[class*='ranking'] [class*='row']",
    "[class*='player'] [class*='row']",
]

TEAM_CONTAINER_SELECTORS = [
    "[data-team='defense']",
    "[data-team='defence']",
    "[data-type='defense']",
    "[data-type='defence']",
    "[aria-label*='defense' i]",
    "[aria-label*='defence' i]",
    "[aria-label*='防衛']",
    ".defense-team",
    ".defence-team",
    "[class*='defense-team']",
    "[class*='defence-team']",
    "[class*='defense'] [class*='team']",
    "[class*='defence'] [class*='team']",
    "[class*='防衛']",
    ".team-formation",
    ".ranger-team",
    "[class*='formation']",
]

NEXT_PAGE_SELECTORS = [
    "button[aria-label='Go to next page']",
    "a[aria-label='Go to next page']",
    "button[aria-label*='next' i]",
    "a[aria-label*='next' i]",
    "button[title*='next' i]",
    "a[title*='next' i]",
    "button[rel='next']",
    "a[rel='next']",
    "button:has-text('次へ')",
    "a:has-text('次へ')",
    "button:has-text('Next')",
    "a:has-text('Next')",
    "button:has-text('›')",
    "a:has-text('›')",
    "button:has-text('»')",
    "a:has-text('»')",
    ".pagination-next button",
    ".pagination-next a",
    ".pagination .next button",
    ".pagination .next a",
    "[class*='pagination'] [class*='next']",
]

LOAD_MORE_SELECTORS = [
    "button:has-text('もっと見る')",
    "a:has-text('もっと見る')",
    "button:has-text('さらに表示')",
    "a:has-text('さらに表示')",
    "button:has-text('Load more')",
    "a:has-text('Load more')",
    "button:has-text('Show more')",
    "a:has-text('Show more')",
    "[class*='load-more'] button",
    "[class*='load-more'] a",
    "[class*='loadmore'] button",
    "[class*='loadmore'] a",
]

EXCLUDED_SOURCE_WORDS = {
    "avatar",
    "badge",
    "banner",
    "country",
    "emoji",
    "flag",
    "guild",
    "league",
    "logo",
    "profile",
    "rank",
    "tier",
    "user",
}

EXCLUDED_CONTEXT_WORDS = {
    "avatar",
    "badge",
    "country",
    "flag",
    "guild",
    "league",
    "player-icon",
    "profile",
    "rank-icon",
    "user-icon",
}

GENERIC_NAMES = {
    "",
    "character",
    "image",
    "img",
    "ranger",
    "thumbnail",
}


def clean_url(url: str) -> str:
    if not url:
        return ""

    absolute_url = urljoin(TARGET_URL, url)
    parts = urlsplit(absolute_url)

    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path,
            "",
            "",
        )
    )


def safe_debug_url(url: str) -> str:
    return clean_url(url)


def infer_name_from_url(url: str) -> str | None:
    path = unquote(urlsplit(url).path)
    stem = Path(path).stem

    stem = re.sub(
        r"@\d+x$",
        "",
        stem,
    )

    stem = re.sub(
        r"[-_](small|medium|large|thumb|thumbnail)$",
        "",
        stem,
        flags=re.IGNORECASE,
    )

    stem = re.sub(
        r"[_-]+",
        " ",
        stem,
    ).strip()

    if not stem:
        return None

    if stem.lower() in GENERIC_NAMES:
        return None

    if re.fullmatch(
        r"[a-f0-9]{16,}",
        stem,
        flags=re.IGNORECASE,
    ):
        return None

    return stem


def normalize_name(
    raw_name: str | None,
    image_url: str,
) -> str:
    cleaned = re.sub(
        r"\s+",
        " ",
        raw_name or "",
    ).strip()

    if cleaned.lower() not in GENERIC_NAMES:
        return cleaned

    inferred = infer_name_from_url(image_url)

    return inferred or "名称不明"


def character_key(
    image_url: str,
    name: str,
) -> str:
    normalized_url = clean_url(image_url)

    if normalized_url:
        return normalized_url

    return f"name:{name.casefold()}"


def save_json(
    path: Path,
    value,
) -> None:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with path.open(
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            value,
            file,
            ensure_ascii=False,
            indent=2,
        )
        file.write("\n")


def is_disabled(locator) -> bool:
    try:
        disabled = locator.get_attribute("disabled")
        aria_disabled = locator.get_attribute("aria-disabled")
        class_name = (
            locator.get_attribute("class") or ""
        ).lower()

        return (
            disabled is not None
            or aria_disabled == "true"
            or "disabled" in class_name
        )
    except PlaywrightError:
        return True


def dismiss_common_dialogs(page) -> None:
    labels = [
        "同意する",
        "許可する",
        "すべて許可",
        "Accept",
        "Accept all",
        "OK",
        "閉じる",
        "Close",
    ]

    for label in labels:
        try:
            buttons = page.get_by_role(
                "button",
                name=label,
                exact=True,
            )

            if (
                buttons.count() > 0
                and buttons.first.is_visible()
            ):
                buttons.first.click(
                    timeout=2_000,
                )
                page.wait_for_timeout(250)
        except PlaywrightError:
            continue


def select_legend_league(page) -> None:
    selectors = [
        "[role='tab']:has-text('レジェンド')",
        "button:has-text('レジェンド')",
        "a:has-text('レジェンド')",
        "[role='option']:has-text('レジェンド')",
        "[role='tab']:has-text('Legend')",
        "button:has-text('Legend')",
        "a:has-text('Legend')",
        "[role='option']:has-text('Legend')",
    ]

    for selector in selectors:
        try:
            candidates = page.locator(selector)
            candidate_count = min(
                candidates.count(),
                10,
            )
        except PlaywrightError:
            continue

        for index in range(candidate_count):
            candidate = candidates.nth(index)

            try:
                if not candidate.is_visible():
                    continue

                candidate.click(
                    timeout=3_000,
                )

                page.wait_for_timeout(1_500)

                print(
                    "[INFO] レジェンドリーグを選択しました。"
                    f"selector={selector}"
                )
                return
            except PlaywrightError:
                continue

    print(
        "[INFO] レジェンド選択ボタンは見つかりませんでした。"
        "現在表示されているリーグで処理を続行します。"
    )


def image_is_character_candidate(
    item: dict,
) -> bool:
    src = clean_url(
        str(item.get("src") or "")
    )

    if not src:
        return False

    width = int(
        item.get("width") or 0
    )

    height = int(
        item.get("height") or 0
    )

    context = str(
        item.get("context") or ""
    ).lower()

    source_lower = src.lower()

    if any(
        keyword in source_lower
        for keyword in EXCLUDED_SOURCE_WORDS
    ):
        return False

    if any(
        keyword in context
        for keyword in EXCLUDED_CONTEXT_WORDS
    ):
        return False

    if width < 18 or height < 18:
        return False

    if width > 220 or height > 220:
        return False

    return True


def normalize_image(item: dict) -> dict:
    src = clean_url(
        str(item.get("src") or "")
    )

    raw_name = str(
        item.get("alt")
        or item.get("title")
        or ""
    )

    return {
        "image": src,
        "name": normalize_name(
            raw_name,
            src,
        ),
        "width": int(
            item.get("width") or 0
        ),
        "height": int(
            item.get("height") or 0
        ),
        "dom_index": int(
            item.get("dom_index") or 0
        ),
    }


def extract_rows_from_dom(
    page,
    row_selector: str,
) -> list[dict]:
    """
    DOM内のプレイヤー行を一括取得する。

    同じ画像URLが複数存在しても削除しない。
    画像1要素をキャラクター1体として保持する。
    """
    try:
        rows = page.locator(
            row_selector
        ).evaluate_all(
            """
            (rows, teamSelectors) => {
                function imageData(image, index) {
                    const rect = image.getBoundingClientRect();
                    const style = getComputedStyle(image);

                    const contextElement = image.closest(
                        '[class], [data-team], [data-type], td, li'
                    );

                    return {
                        dom_index: index,
                        src:
                            image.currentSrc
                            || image.getAttribute('src')
                            || image.getAttribute('data-src')
                            || image.getAttribute('data-lazy-src')
                            || image.getAttribute('data-original')
                            || '',
                        alt: image.getAttribute('alt') || '',
                        title: image.getAttribute('title') || '',
                        width: Math.round(rect.width),
                        height: Math.round(rect.height),
                        visible:
                            rect.width > 0
                            && rect.height > 0
                            && style.display !== 'none'
                            && style.visibility !== 'hidden'
                            && Number(style.opacity || 1) !== 0,
                        context:
                            contextElement
                            && typeof contextElement.className === 'string'
                                ? contextElement.className
                                : ''
                    };
                }

                return rows.map((row, rowIndex) => {
                    const groups = [];

                    for (const selector of teamSelectors) {
                        let containers = [];

                        try {
                            containers = Array.from(
                                row.querySelectorAll(selector)
                            );
                        } catch {
                            continue;
                        }

                        for (const container of containers) {
                            const images = Array.from(
                                container.querySelectorAll('img')
                            ).map(imageData);

                            if (images.length > 0) {
                                groups.push({
                                    selector: selector,
                                    images: images
                                });
                            }
                        }
                    }

                    const allImages = Array.from(
                        row.querySelectorAll('img')
                    ).map(imageData);

                    const attributes = {};

                    for (const attribute of row.attributes) {
                        if (
                            attribute.name.startsWith('data-')
                            || attribute.name === 'id'
                        ) {
                            attributes[attribute.name] =
                                attribute.value;
                        }
                    }

                    return {
                        row_index: rowIndex,
                        text: (
                            row.innerText
                            || row.textContent
                            || ''
                        )
                            .replace(/\\s+/g, ' ')
                            .trim(),
                        attributes: attributes,
                        groups: groups,
                        all_images: allImages
                    };
                });
            }
            """,
            TEAM_CONTAINER_SELECTORS,
        )
    except PlaywrightError:
        return []

    normalized_rows = []

    for row in rows:
        normalized_groups = []

        for group in row.get(
            "groups",
            [],
        ):
            images = []

            for image in group.get(
                "images",
                [],
            ):
                if not image.get("visible"):
                    continue

                if not image_is_character_candidate(
                    image
                ):
                    continue

                images.append(
                    normalize_image(image)
                )

            if images:
                normalized_groups.append(
                    {
                        "selector": group.get(
                            "selector",
                            "",
                        ),
                        "images": images,
                    }
                )

        all_images = []

        for image in row.get(
            "all_images",
            [],
        ):
            if not image.get("visible"):
                continue

            if not image_is_character_candidate(
                image
            ):
                continue

            all_images.append(
                normalize_image(image)
            )

        normalized_rows.append(
            {
                "row_index": int(
                    row.get("row_index") or 0
                ),
                "text": str(
                    row.get("text") or ""
                ),
                "attributes": row.get(
                    "attributes",
                    {},
                ),
                "groups": normalized_groups,
                "all_images": all_images,
            }
        )

    return normalized_rows


def select_team_images(
    row: dict,
) -> list[dict]:
    """
    防衛チーム候補から最も妥当な画像群を選択する。

    同じキャラクター画像が複数存在しても保持する。
    """
    valid_groups = []

    for group in row.get(
        "groups",
        [],
    ):
        images = group.get(
            "images",
            [],
        )

        if (
            MIN_CHARACTERS_PER_PLAYER
            <= len(images)
            <= MAX_CHARACTERS_PER_PLAYER
        ):
            valid_groups.append(images)

    if valid_groups:
        return max(
            valid_groups,
            key=len,
        )

    all_images = row.get(
        "all_images",
        [],
    )

    if (
        MIN_CHARACTERS_PER_PLAYER
        <= len(all_images)
        <= MAX_CHARACTERS_PER_PLAYER
    ):
        return all_images

    return []


def find_best_row_selector(
    page,
) -> tuple[str | None, dict]:
    diagnostics = {}

    for selector in ROW_SELECTORS:
        rows = extract_rows_from_dom(
            page,
            selector,
        )

        valid_rows = 0
        image_counts = []

        for row in rows[:30]:
            images = select_team_images(row)

            if images:
                valid_rows += 1
                image_counts.append(
                    len(images)
                )

        diagnostics[selector] = {
            "row_count": len(rows),
            "valid_rows": valid_rows,
            "image_counts": image_counts,
        }

    ranked = sorted(
        diagnostics.items(),
        key=lambda item: (
            item[1]["valid_rows"],
            item[1]["row_count"],
        ),
        reverse=True,
    )

    if not ranked:
        return None, diagnostics

    best_selector = ranked[0][0]
    best_result = ranked[0][1]

    if best_result["valid_rows"] == 0:
        return None, diagnostics

    return best_selector, diagnostics


def create_player_identity(
    row: dict,
    page_number: int,
    load_cycle: int,
) -> str:
    """
    プレイヤー行の識別子を作成する。

    キャラクター編成内容は識別子に含めない。
    そのため、同じ編成の別プレイヤーは統合されない。
    """
    attributes = row.get(
        "attributes",
        {},
    )

    preferred_attributes = [
        "data-player-id",
        "data-user-id",
        "data-id",
        "data-rank",
        "id",
    ]

    for attribute_name in preferred_attributes:
        value = str(
            attributes.get(attribute_name) or ""
        ).strip()

        if value:
            return (
                f"page:{page_number}:"
                f"{attribute_name}:{value}"
            )

    text = re.sub(
        r"\s+",
        " ",
        row.get("text") or "",
    ).strip()

    if text:
        text_hash = hashlib.sha256(
            text.encode("utf-8")
        ).hexdigest()

        return (
            f"page:{page_number}:"
            f"text:{text_hash}"
        )

    return (
        f"page:{page_number}:"
        f"cycle:{load_cycle}:"
        f"row:{row['row_index']}"
    )


def click_load_more(page) -> bool:
    for selector in LOAD_MORE_SELECTORS:
        try:
            candidates = page.locator(selector)
            candidate_count = min(
                candidates.count(),
                10,
            )
        except PlaywrightError:
            continue

        for index in range(candidate_count):
            candidate = candidates.nth(index)

            try:
                if not candidate.is_visible():
                    continue

                if is_disabled(candidate):
                    continue

                candidate.scroll_into_view_if_needed(
                    timeout=3_000,
                )

                candidate.click(
                    timeout=4_000,
                )

                page.wait_for_timeout(
                    DYNAMIC_LOAD_WAIT_MS
                )

                print(
                    "[INFO] 追加表示ボタンを押しました。"
                    f"selector={selector}"
                )

                return True
            except PlaywrightError:
                continue

    return False


def scroll_dynamic_content(page) -> dict:
    """
    windowと内部スクロール要素を段階的にスクロールする。
    """
    try:
        return page.evaluate(
            """
            () => {
                const scrollables = Array.from(
                    document.querySelectorAll('*')
                ).filter((element) => {
                    const style = getComputedStyle(element);

                    return (
                        element.scrollHeight
                            > element.clientHeight + 100
                        && (
                            style.overflowY === 'auto'
                            || style.overflowY === 'scroll'
                        )
                    );
                });

                let moved = false;

                for (const element of scrollables) {
                    const oldTop = element.scrollTop;
                    const step = Math.max(
                        300,
                        Math.floor(element.clientHeight * 0.8)
                    );

                    element.scrollTop = Math.min(
                        element.scrollTop + step,
                        element.scrollHeight
                    );

                    if (element.scrollTop !== oldTop) {
                        moved = true;
                    }
                }

                const oldWindowY = window.scrollY;
                const windowStep = Math.max(
                    500,
                    Math.floor(window.innerHeight * 0.8)
                );

                window.scrollTo(
                    0,
                    Math.min(
                        window.scrollY + windowStep,
                        document.documentElement.scrollHeight
                    )
                );

                if (window.scrollY !== oldWindowY) {
                    moved = true;
                }

                return {
                    moved: moved,
                    windowY: window.scrollY,
                    documentHeight:
                        document.documentElement.scrollHeight,
                    scrollableCount: scrollables.length
                };
            }
            """
        )
    except PlaywrightError:
        return {
            "moved": False,
            "windowY": 0,
            "documentHeight": 0,
            "scrollableCount": 0,
        }


def reset_scroll_positions(page) -> None:
    try:
        page.evaluate(
            """
            () => {
                window.scrollTo(0, 0);

                const scrollables = Array.from(
                    document.querySelectorAll('*')
                ).filter((element) => {
                    const style = getComputedStyle(element);

                    return (
                        element.scrollHeight
                            > element.clientHeight + 100
                        && (
                            style.overflowY === 'auto'
                            || style.overflowY === 'scroll'
                        )
                    );
                });

                for (const element of scrollables) {
                    element.scrollTop = 0;
                }
            }
            """
        )
    except PlaywrightError:
        pass


def page_signature(
    page,
    row_selector: str,
) -> str:
    try:
        rows = extract_rows_from_dom(
            page,
            row_selector,
        )

        values = []

        for row in rows[:10]:
            values.append(
                row.get("text", "")[:300]
            )

        raw = "\n".join(values)

        if not raw:
            raw = page.url
    except PlaywrightError:
        raw = page.url

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()


def go_to_next_page(
    page,
    row_selector: str,
) -> bool:
    old_signature = page_signature(
        page,
        row_selector,
    )

    old_url = page.url

    reset_scroll_positions(page)

    for selector in NEXT_PAGE_SELECTORS:
        try:
            candidates = page.locator(selector)
            candidate_count = min(
                candidates.count(),
                20,
            )
        except PlaywrightError:
            continue

        for index in range(candidate_count):
            candidate = candidates.nth(index)

            try:
                if not candidate.is_visible():
                    continue

                if is_disabled(candidate):
                    continue

                candidate.scroll_into_view_if_needed(
                    timeout=4_000,
                )

                candidate.click(
                    timeout=5_000,
                )

                try:
                    page.wait_for_load_state(
                        "domcontentloaded",
                        timeout=8_000,
                    )
                except PlaywrightTimeoutError:
                    pass

                page.wait_for_timeout(1_500)

                new_signature = page_signature(
                    page,
                    row_selector,
                )

                new_url = page.url

                if (
                    new_signature != old_signature
                    or new_url != old_url
                ):
                    print(
                        "[INFO] 次ページへ移動しました。"
                        f"selector={selector}"
                    )
                    return True
            except PlaywrightError:
                continue

    return False


def collect_current_page(
    page,
    row_selector: str,
    page_number: int,
    sampled_players: int,
) -> tuple[list[dict], dict]:
    """
    現在のページをスクロールしながら全プレイヤー行を収集する。

    仮想スクロールでDOMから古い行が削除される場合にも、
    各スクロール位置で取得した行を保持する。
    """
    collected_rows = {}
    stable_attempts = 0
    previous_total = 0
    load_attempts = 0
    maximum_dom_rows = 0

    while (
        load_attempts < MAX_DYNAMIC_LOAD_ATTEMPTS
        and sampled_players + len(collected_rows)
        < TARGET_PLAYER_COUNT
    ):
        load_attempts += 1

        rows = extract_rows_from_dom(
            page,
            row_selector,
        )

        maximum_dom_rows = max(
            maximum_dom_rows,
            len(rows),
        )

        new_rows = 0

        for row in rows:
            images = select_team_images(row)

            if not images:
                continue

            identity = create_player_identity(
                row,
                page_number,
                load_attempts,
            )

            if identity in collected_rows:
                continue

            collected_rows[identity] = {
                "identity": identity,
                "text": row.get("text", ""),
                "images": images,
            }

            new_rows += 1

        current_total = len(
            collected_rows
        )

        print(
            f"[INFO] page={page_number}, "
            f"load_attempt={load_attempts}, "
            f"dom_rows={len(rows)}, "
            f"new_valid_rows={new_rows}, "
            f"collected_rows={current_total}"
        )

        if current_total == previous_total:
            stable_attempts += 1
        else:
            stable_attempts = 0

        previous_total = current_total

        if (
            sampled_players + current_total
            >= TARGET_PLAYER_COUNT
        ):
            break

        clicked_more = click_load_more(page)

        scroll_result = scroll_dynamic_content(
            page
        )

        if (
            stable_attempts >= STABLE_LOAD_ATTEMPTS
            and not clicked_more
            and not scroll_result.get("moved")
        ):
            break

        if (
            stable_attempts
            >= STABLE_LOAD_ATTEMPTS + 3
        ):
            break

        page.wait_for_timeout(
            DYNAMIC_LOAD_WAIT_MS
        )

    return (
        list(collected_rows.values()),
        {
            "load_attempts": load_attempts,
            "maximum_dom_rows": maximum_dom_rows,
            "collected_rows": len(collected_rows),
        },
    )


def dump_debug(
    page,
    response_log: list[dict],
    extra: dict | None = None,
) -> None:
    DEBUG_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:
        (
            DEBUG_DIR / "page.html"
        ).write_text(
            page.content(),
            encoding="utf-8",
        )
    except PlaywrightError:
        pass

    try:
        page.screenshot(
            path=str(
                DEBUG_DIR / "screenshot.png"
            ),
            full_page=True,
        )
    except PlaywrightError:
        pass

    try:
        images = page.locator(
            "img"
        ).evaluate_all(
            """
            (items) => items.map((image, index) => {
                const rect = image.getBoundingClientRect();

                return {
                    index: index,
                    src:
                        image.currentSrc
                        || image.getAttribute('src')
                        || image.getAttribute('data-src')
                        || image.getAttribute('data-lazy-src'),
                    alt: image.getAttribute('alt'),
                    title: image.getAttribute('title'),
                    className:
                        typeof image.className === 'string'
                            ? image.className
                            : '',
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                    parentHtml:
                        image.parentElement
                            ? image.parentElement.outerHTML.slice(
                                0,
                                1500
                            )
                            : null
                };
            })
            """
        )

        save_json(
            DEBUG_DIR / "images.json",
            images,
        )
    except PlaywrightError:
        pass

    try:
        pagination_candidates = page.locator(
            "button, a, [role='button']"
        ).evaluate_all(
            """
            (elements) => elements
                .map((element) => {
                    const text = (
                        element.innerText
                        || element.textContent
                        || ''
                    ).trim();

                    return {
                        tag: element.tagName.toLowerCase(),
                        text: text.slice(0, 200),
                        ariaLabel:
                            element.getAttribute('aria-label'),
                        title:
                            element.getAttribute('title'),
                        rel:
                            element.getAttribute('rel'),
                        disabled:
                            element.hasAttribute('disabled')
                            || element.getAttribute(
                                'aria-disabled'
                            ) === 'true',
                        className:
                            typeof element.className === 'string'
                                ? element.className
                                : '',
                        outerHtml:
                            element.outerHTML.slice(0, 1500)
                    };
                })
                .filter((item) => {
                    const value = [
                        item.text,
                        item.ariaLabel,
                        item.title,
                        item.rel,
                        item.className
                    ]
                        .filter(Boolean)
                        .join(' ')
                        .toLowerCase();

                    return (
                        value.includes('next')
                        || value.includes('次')
                        || value.includes('pagination')
                        || value.includes('more')
                        || value.includes('もっと')
                        || value.includes('さらに')
                        || value.includes('›')
                        || value.includes('»')
                    );
                });
            """
        )

        save_json(
            DEBUG_DIR
            / "pagination_candidates.json",
            pagination_candidates,
        )
    except PlaywrightError:
        pass

    save_json(
        DEBUG_DIR / "responses.json",
        response_log,
    )

    save_json(
        DEBUG_DIR / "diagnostics.json",
        extra or {},
    )


def scrape(page) -> dict:
    selector, selector_diagnostics = (
        find_best_row_selector(page)
    )

    if selector is None:
        raise RuntimeError(
            "プレイヤー行を特定できませんでした。"
            "Artifactの調査ファイルを確認してください。"
        )

    print(
        "[INFO] 使用する行セレクタ: "
        f"{selector}"
    )

    character_counts = defaultdict(
        lambda: {
            "occurrence_count": 0,
            "player_count": 0,
            "image": "",
            "name": "名称不明",
        }
    )

    sampled_players = 0
    total_character_slots = 0
    player_sizes = []
    visited_pages = 0
    termination_reason = "unknown"

    visited_page_signatures = set()
    page_diagnostics = []

    while (
        sampled_players < TARGET_PLAYER_COUNT
        and visited_pages < MAX_PAGES
    ):
        current_page_signature = page_signature(
            page,
            selector,
        )

        if (
            current_page_signature
            in visited_page_signatures
        ):
            termination_reason = "duplicate_page"

            print(
                "[WARN] 同じページが再表示されたため、"
                "ページの重複集計を防いで終了します。"
            )
            break

        visited_page_signatures.add(
            current_page_signature
        )

        visited_pages += 1

        page_rows, load_diagnostics = (
            collect_current_page(
                page,
                selector,
                visited_pages,
                sampled_players,
            )
        )

        valid_players_on_page = 0
        character_slots_on_page = 0

        for row in page_rows:
            if (
                sampled_players
                >= TARGET_PLAYER_COUNT
            ):
                break

            images = row["images"]

            if not (
                MIN_CHARACTERS_PER_PLAYER
                <= len(images)
                <= MAX_CHARACTERS_PER_PLAYER
            ):
                continue

            sampled_players += 1
            valid_players_on_page += 1

            character_count = len(images)

            player_sizes.append(
                character_count
            )

            total_character_slots += (
                character_count
            )

            character_slots_on_page += (
                character_count
            )

            player_character_keys = set()

            for image in images:
                key = character_key(
                    image["image"],
                    image["name"],
                )

                character_counts[
                    key
                ]["occurrence_count"] += 1

                character_counts[
                    key
                ]["image"] = image["image"]

                if image["name"] != "名称不明":
                    character_counts[
                        key
                    ]["name"] = image["name"]

                player_character_keys.add(key)

            for key in player_character_keys:
                character_counts[
                    key
                ]["player_count"] += 1

        page_diagnostics.append(
            {
                "page": visited_pages,
                "valid_players": valid_players_on_page,
                "character_slots": character_slots_on_page,
                **load_diagnostics,
            }
        )

        print(
            f"[INFO] page={visited_pages}, "
            f"valid_players={valid_players_on_page}, "
            f"page_character_slots={character_slots_on_page}, "
            f"total_players={sampled_players}, "
            f"total_character_slots={total_character_slots}"
        )

        if (
            sampled_players
            >= TARGET_PLAYER_COUNT
        ):
            termination_reason = "target_reached"
            break

        if valid_players_on_page == 0:
            termination_reason = "no_valid_players"
            break

        if not go_to_next_page(
            page,
            selector,
        ):
            termination_reason = "no_next_page"
            break

    if termination_reason == "unknown":
        if (
            sampled_players
            >= TARGET_PLAYER_COUNT
        ):
            termination_reason = "target_reached"
        elif visited_pages >= MAX_PAGES:
            termination_reason = "max_pages_reached"

    if sampled_players < MIN_REQUIRED_PLAYERS:
        raise RuntimeError(
            "品質基準を満たしません。"
            f"取得人数={sampled_players}, "
            f"必要人数={MIN_REQUIRED_PLAYERS}, "
            f"終了理由={termination_reason}, "
            f"確認ページ数={visited_pages}, "
            f"使用セレクタ={selector}"
        )

    characters = []

    for data in character_counts.values():
        occurrence_count = int(
            data["occurrence_count"]
        )

        player_count = int(
            data["player_count"]
        )

        adoption_rate = (
            round(
                player_count
                / sampled_players
                * 100,
                1,
            )
            if sampled_players > 0
            else 0
        )

        slot_rate = (
            round(
                occurrence_count
                / total_character_slots
                * 100,
                2,
            )
            if total_character_slots > 0
            else 0
        )

        average_copies = (
            round(
                occurrence_count
                / player_count,
                2,
            )
            if player_count > 0
            else 0
        )

        characters.append(
            {
                "name": data["name"],
                "image": data["image"],
                "occurrence_count": occurrence_count,
                "player_count": player_count,
                "adoption_rate": adoption_rate,
                "slot_rate": slot_rate,
                "average_copies_when_used": average_copies,

                # 既存のWeb表示との互換性
                "count": occurrence_count,
                "rate": adoption_rate,
            }
        )

    characters.sort(
        key=lambda item: (
            -item["occurrence_count"],
            -item["player_count"],
            item["name"].casefold(),
        )
    )

    previous_count = None
    current_rank = 0

    for index, item in enumerate(
        characters,
        start=1,
    ):
        if (
            item["occurrence_count"]
            != previous_count
        ):
            current_rank = index

        item["rank"] = current_rank
        previous_count = item[
            "occurrence_count"
        ]

    calculated_slots = sum(
        item["occurrence_count"]
        for item in characters
    )

    if calculated_slots != total_character_slots:
        raise RuntimeError(
            "キャラクター枠数の整合性検査に失敗しました。"
            f"行から取得した枠数={total_character_slots}, "
            f"キャラクター別合計={calculated_slots}"
        )

    return {
        "schema_version": 2,
        "updated_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "source": {
            "name": SOURCE_NAME,
            "url": TARGET_URL,
        },
        "league": "レジェンド",
        "target_players": TARGET_PLAYER_COUNT,
        "sampled_players": sampled_players,
        "character_slots": total_character_slots,
        "unique_characters": len(characters),
        "median_characters_per_player": (
            median(player_sizes)
            if player_sizes
            else 0
        ),
        "pages_scanned": visited_pages,
        "termination_reason": termination_reason,
        "complete_target": (
            sampled_players
            >= TARGET_PLAYER_COUNT
        ),
        "counting_method": {
            "occurrence_count": (
                "同一プレイヤー内の重複を含む総編成数"
            ),
            "player_count": (
                "対象キャラクターを1体以上採用したプレイヤー数"
            ),
            "adoption_rate": (
                "採用人数を集計人数で割った割合"
            ),
            "slot_rate": (
                "総編成数を全キャラクター枠数で割った割合"
            ),
        },
        "characters": characters,
        "diagnostics": {
            "selected_row_selector": selector,
            "selector_scores": selector_diagnostics,
            "pages": page_diagnostics,
        },
    }


def write_output(data: dict) -> None:
    OUTPUT_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path = (
        OUTPUT_PATH.with_suffix(".json.tmp")
    )

    save_json(
        temporary_path,
        data,
    )

    temporary_path.replace(
        OUTPUT_PATH
    )


def main() -> None:
    response_log = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )

        context = browser.new_context(
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            viewport={
                "width": 1440,
                "height": 1200,
            },
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/131.0 Safari/537.36 "
                "LegendUsageAggregator/2.0"
            ),
        )

        page = context.new_page()
        page.set_default_timeout(10_000)

        def record_response(response) -> None:
            try:
                content_type = response.headers.get(
                    "content-type",
                    "",
                )

                if (
                    "json" in content_type.lower()
                    or "javascript"
                    in content_type.lower()
                ):
                    response_log.append(
                        {
                            "url": safe_debug_url(
                                response.url
                            ),
                            "status": response.status,
                            "content_type": content_type,
                        }
                    )
            except PlaywrightError:
                pass

        page.on(
            "response",
            record_response,
        )

        try:
            response = page.goto(
                TARGET_URL,
                wait_until="domcontentloaded",
                timeout=60_000,
            )

            if (
                response is not None
                and response.status >= 400
            ):
                raise RuntimeError(
                    "対象ページが"
                    f"HTTP {response.status}"
                    "を返しました。"
                )

            try:
                page.wait_for_load_state(
                    "networkidle",
                    timeout=12_000,
                )
            except PlaywrightTimeoutError:
                print(
                    "[WARN] networkidle待機が"
                    "タイムアウトしました。"
                    "現在のDOMで処理を続行します。"
                )

            page.wait_for_timeout(2_000)

            dismiss_common_dialogs(page)
            select_legend_league(page)

            page.wait_for_timeout(1_500)

            if DEBUG:
                selector, diagnostics = (
                    find_best_row_selector(page)
                )

                dump_debug(
                    page,
                    response_log,
                    {
                        "mode": "debug",
                        "selected_row_selector": selector,
                        "selector_scores": diagnostics,
                    },
                )

                print(
                    "[DEBUG] 調査ファイルを"
                    ".artifacts/debugへ保存しました。"
                )
                return

            data = scrape(page)

            write_output(data)

            dump_debug(
                page,
                response_log,
                {
                    "mode": "success",
                    "sampled_players": data[
                        "sampled_players"
                    ],
                    "character_slots": data[
                        "character_slots"
                    ],
                    "unique_characters": len(
                        data["characters"]
                    ),
                    "termination_reason": data[
                        "termination_reason"
                    ],
                    "diagnostics": data[
                        "diagnostics"
                    ],
                },
            )

            print(
                "[DONE] "
                f"players={data['sampled_players']}, "
                f"slots={data['character_slots']}, "
                f"characters={len(data['characters'])}, "
                f"termination="
                f"{data['termination_reason']}, "
                f"output={OUTPUT_PATH}"
            )

        except Exception as error:
            print(
                f"[ERROR] {error}",
                file=sys.stderr,
            )

            dump_debug(
                page,
                response_log,
                {
                    "mode": "error",
                    "error": str(error),
                },
            )

            raise

        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
