# File: scripts/scrape_character_usage.py
"""
LINE Rangers HandbookのPvP Trackerから、レジェンド帯上位プレイヤーの
防衛チームに採用されているキャラクターを集計する。

第三者サイトのHTML構造は変更される可能性があるため、取得件数や画像数を検証し、
品質基準を満たさない場合は公開JSONを更新しない。
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

TARGET_PLAYER_COUNT = int(os.environ.get("TARGET_PLAYER_COUNT", "200"))
MIN_REQUIRED_PLAYERS = int(os.environ.get("MIN_REQUIRED_PLAYERS", "180"))
DEBUG = os.environ.get("DEBUG", "0") == "1"

OUTPUT_PATH = Path("docs/data/character_usage.json")
DEBUG_DIR = Path(".artifacts/debug")

MIN_CHARACTERS_PER_PLAYER = 5
MAX_CHARACTERS_PER_PLAYER = 10
MAX_PAGES = 30

ROW_SELECTORS = [
    "table tbody tr",
    "[role='rowgroup'] [role='row']",
    ".ranking-table tbody tr",
    ".ranking-list .ranking-row",
    ".player-list .player-row",
    ".player-card",
    "[class*='ranking'] [class*='row']",
]

TEAM_CONTAINER_SELECTORS = [
    "[data-team='defense']",
    "[data-type='defense']",
    ".defense-team",
    ".defence-team",
    "[class*='defense-team']",
    "[class*='defence-team']",
    "[class*='defense'] [class*='team']",
    "[class*='defence'] [class*='team']",
    ".team-formation",
    ".ranger-team",
    "[class*='formation']",
]

NEXT_PAGE_SELECTORS = [
    "button[aria-label*='next' i]",
    "a[aria-label*='next' i]",
    "button[title*='next' i]",
    "a[title*='next' i]",
    "button:has-text('次へ')",
    "a:has-text('次へ')",
    "button:has-text('Next')",
    "a:has-text('Next')",
    ".pagination-next",
    "[class*='pagination'] button:last-child",
    "[class*='pagination'] a:last-child",
]

EXCLUDED_SOURCE_WORDS = {
    "avatar",
    "badge",
    "banner",
    "country",
    "emoji",
    "flag",
    "guild",
    "logo",
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
    """クエリ文字列とフラグメントを除去し、比較用URLへ正規化する。"""
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
    """デバッグ情報へ認証情報などを残さないようクエリを除去する。"""
    return clean_url(url)


def infer_name_from_url(url: str) -> str | None:
    """画像ファイル名から表示用の仮名称を生成する。"""
    path = unquote(urlsplit(url).path)
    stem = Path(path).stem
    stem = re.sub(r"@\d+x$", "", stem)
    stem = re.sub(r"[-_](small|medium|large|thumb|thumbnail)$", "", stem, flags=re.I)
    stem = re.sub(r"[_-]+", " ", stem).strip()

    if not stem or stem.lower() in GENERIC_NAMES:
        return None

    if re.fullmatch(r"[a-f0-9]{16,}", stem, flags=re.I):
        return None

    return stem


def normalize_name(name: str | None, image_url: str) -> str:
    """alt、title、ファイル名の順にキャラクター名を決定する。"""
    cleaned = re.sub(r"\s+", " ", name or "").strip()

    if cleaned.lower() not in GENERIC_NAMES:
        return cleaned

    inferred = infer_name_from_url(image_url)
    return inferred or "名称不明"


def character_key(image_url: str, name: str) -> str:
    """画像URLを優先して同一キャラクターを識別する。"""
    normalized_url = clean_url(image_url)

    if normalized_url:
        return normalized_url

    return f"name:{name.casefold()}"


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write("\n")


def collect_images(locator) -> list[dict]:
    """
    指定要素内のキャラクター画像候補をDOM順で取得する。

    同じ画像URLが複数存在しても削除しない。
    同一キャラクターを2体編成している場合、2件として返す。
    """
    try:
        raw_images = locator.locator("img").evaluate_all(
            """
            (images) => images.map((image, index) => {
                const rect = image.getBoundingClientRect();
                const style = getComputedStyle(image);
                const parent = image.closest(
                    '[class], [data-team], [data-type], td, li'
                );

                return {
                    index: index,
                    src:
                        image.currentSrc ||
                        image.getAttribute('src') ||
                        image.getAttribute('data-src') ||
                        image.getAttribute('data-lazy-src') ||
                        '',
                    alt: image.getAttribute('alt') || '',
                    title: image.getAttribute('title') || '',
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                    visible:
                        rect.width > 0 &&
                        rect.height > 0 &&
                        style.display !== 'none' &&
                        style.visibility !== 'hidden' &&
                        Number(style.opacity || 1) !== 0,
                    context:
                        parent && typeof parent.className === 'string'
                            ? parent.className
                            : ''
                };
            })
            """
        )
    except PlaywrightError:
        return []

    results = []

    for item in raw_images:
        src = clean_url(str(item.get("src") or ""))
        context = str(item.get("context") or "").lower()
        width = int(item.get("width") or 0)
        height = int(item.get("height") or 0)

        if not src:
            continue

        if not item.get("visible"):
            continue

        source_lower = src.lower()

        if any(word in source_lower for word in EXCLUDED_SOURCE_WORDS):
            continue

        if any(word in context for word in EXCLUDED_CONTEXT_WORDS):
            continue

        if width < 18 or height < 18:
            continue

        if width > 180 or height > 180:
            continue

        raw_name = str(item.get("alt") or item.get("title") or "")
        name = normalize_name(raw_name, src)

        results.append(
            {
                "image": src,
                "name": name,
                "width": width,
                "height": height,
                "dom_index": int(item.get("index") or 0),
            }
        )

    return results


def extract_team_images(row) -> list[dict]:
    """1プレイヤー行から防衛チーム画像を抽出する。"""
    valid_groups = []

    for selector in TEAM_CONTAINER_SELECTORS:
        try:
            containers = row.locator(selector)
            count = min(containers.count(), 20)
        except PlaywrightError:
            continue

        for index in range(count):
            images = collect_images(containers.nth(index))

            if MIN_CHARACTERS_PER_PLAYER <= len(images) <= MAX_CHARACTERS_PER_PLAYER:
                valid_groups.append(images)

    if valid_groups:
        return max(valid_groups, key=len)

    row_images = collect_images(row)

    if MIN_CHARACTERS_PER_PLAYER <= len(row_images) <= MAX_CHARACTERS_PER_PLAYER:
        return row_images

    return []


def hydrate_lazy_images(page, row_selector: str) -> None:
    """
    全プレイヤー行を順番に画面内へ移動させ、
    遅延読み込みされるキャラクター画像を読み込ませる。
    """
    try:
        rows = page.locator(row_selector)
        row_count = rows.count()

        for index in range(row_count):
            try:
                row = page.locator(row_selector).nth(index)
                row.scroll_into_view_if_needed(timeout=3_000)
                page.wait_for_timeout(100)
            except PlaywrightError:
                continue

        page.evaluate(
            """
            () => {
                window.scrollTo({
                    top: 0,
                    behavior: 'instant'
                });
            }
            """
        )
        page.wait_for_timeout(1_000)

    except PlaywrightError as error:
        print(f"[WARN] 遅延読み込み処理に失敗しました: {error}")


def find_best_row_selector(page) -> tuple[str | None, dict]:
    """各セレクタを採点し、チーム画像を持つプレイヤー行が最も多いものを選ぶ。"""
    diagnostics = {}

    for selector in ROW_SELECTORS:
        try:
            rows = page.locator(selector)
            row_count = rows.count()
        except PlaywrightError:
            diagnostics[selector] = {
                "rows": 0,
                "valid_samples": 0,
            }
            continue

        valid_samples = 0
        image_counts = []

        for index in range(min(row_count, 20)):
            images = extract_team_images(rows.nth(index))

            if images:
                valid_samples += 1
                image_counts.append(len(images))

        diagnostics[selector] = {
            "rows": row_count,
            "valid_samples": valid_samples,
            "image_counts": image_counts,
        }

    ranked = sorted(
        diagnostics.items(),
        key=lambda item: (
            item[1]["valid_samples"],
            item[1]["rows"],
        ),
        reverse=True,
    )

    if not ranked or ranked[0][1]["valid_samples"] == 0:
        return None, diagnostics

    return ranked[0][0], diagnostics


def dismiss_common_dialogs(page) -> None:
    """Cookie確認など、一般的なダイアログを閉じる。"""
    labels = [
        "同意する",
        "許可する",
        "Accept",
        "Accept all",
        "OK",
        "閉じる",
    ]

    for label in labels:
        try:
            button = page.get_by_role("button", name=label, exact=True)

            if button.count() > 0 and button.first.is_visible():
                button.first.click(timeout=2_000)
                page.wait_for_timeout(300)
        except PlaywrightError:
            continue


def select_legend_league(page) -> None:
    """レジェンド選択UIが存在する場合に選択する。"""
    selectors = [
        "button:has-text('レジェンド')",
        "a:has-text('レジェンド')",
        "[role='tab']:has-text('レジェンド')",
        "button:has-text('Legend')",
        "a:has-text('Legend')",
        "[role='tab']:has-text('Legend')",
    ]

    for selector in selectors:
        try:
            candidates = page.locator(selector)

            for index in range(min(candidates.count(), 10)):
                candidate = candidates.nth(index)

                if not candidate.is_visible():
                    continue

                candidate.click(timeout=3_000)
                page.wait_for_timeout(2_000)
                return
        except PlaywrightError:
            continue


def page_signature(page, selector: str) -> str:
    """ページ送りの完了判定に使用する署名を生成する。"""
    try:
        rows = page.locator(selector)
        values = []

        for index in range(min(rows.count(), 5)):
            values.append(rows.nth(index).inner_text(timeout=2_000)[:300])

        raw = "\n".join(values)
    except PlaywrightError:
        raw = page.url

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def go_to_next_page(page, row_selector: str) -> bool:
    """利用可能な次ページボタンを押す。"""
    old_signature = page_signature(page, row_selector)

    for selector in NEXT_PAGE_SELECTORS:
        try:
            candidates = page.locator(selector)
        except PlaywrightError:
            continue

        for index in range(min(candidates.count(), 10)):
            candidate = candidates.nth(index)

            try:
                if not candidate.is_visible():
                    continue

                disabled = (
                    candidate.is_disabled()
                    or candidate.get_attribute("disabled") is not None
                    or candidate.get_attribute("aria-disabled") == "true"
                )

                if disabled:
                    continue

                candidate.click(timeout=5_000)
                page.wait_for_timeout(2_500)

                new_signature = page_signature(page, row_selector)

                if new_signature != old_signature:
                    return True
            except PlaywrightError:
                continue

    return False


def dump_debug(page, response_log: list[dict], extra: dict | None = None) -> None:
    """HTML、画像一覧、スクリーンショットなどを調査用に保存する。"""
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    try:
        (DEBUG_DIR / "page.html").write_text(
            page.content(),
            encoding="utf-8",
        )
    except PlaywrightError:
        pass

    try:
        page.screenshot(
            path=str(DEBUG_DIR / "screenshot.png"),
            full_page=True,
        )
    except PlaywrightError:
        pass

    try:
        images = page.locator("img").evaluate_all(
            """
            (items) => items.map((image) => {
                const rect = image.getBoundingClientRect();

                return {
                    src:
                        image.currentSrc ||
                        image.getAttribute('src') ||
                        image.getAttribute('data-src'),
                    alt: image.getAttribute('alt'),
                    title: image.getAttribute('title'),
                    className:
                        typeof image.className === 'string'
                            ? image.className
                            : '',
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                    parentHtml: image.parentElement
                        ? image.parentElement.outerHTML.slice(0, 1500)
                        : null
                };
            })
            """
        )
        save_json(DEBUG_DIR / "images.json", images)
    except PlaywrightError:
        pass

    try:
        rows = page.locator("tr, [role='row'], .player-card")
        samples = []

        for index in range(min(rows.count(), 10)):
            samples.append(rows.nth(index).evaluate("(element) => element.outerHTML"))

        save_json(DEBUG_DIR / "sample_rows.json", samples)
    except PlaywrightError:
        pass

    save_json(DEBUG_DIR / "responses.json", response_log)
    save_json(DEBUG_DIR / "diagnostics.json", extra or {})


def scrape(page) -> dict:
    """レジェンド帯プレイヤーの防衛チームを集計する。"""
    selector, selector_diagnostics = find_best_row_selector(page)

    if selector is None:
        raise RuntimeError(
            "プレイヤー行を特定できませんでした。"
            "Artifactのdebugファイルを確認してください。"
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
    visited_pages = 0
    total_character_slots = 0
    player_sizes = []
    termination_reason = "unknown"
    visited_page_signatures = set()

    while sampled_players < TARGET_PLAYER_COUNT and visited_pages < MAX_PAGES:
        current_signature = page_signature(page, selector)

        if current_signature in visited_page_signatures:
            termination_reason = "duplicate_page"
            print("[WARN] 既に処理したページが表示されたため、重複集計を防いで終了します。")
            break

        visited_page_signatures.add(current_signature)
        visited_pages += 1

        hydrate_lazy_images(page, selector)

        rows = page.locator(selector)
        row_count = rows.count()
        page_valid_players = 0
        page_skipped_rows = 0
        page_character_slots = 0

        print(f"[INFO] page={visited_pages}, row_candidates={row_count}")

        for index in range(row_count):
            if sampled_players >= TARGET_PLAYER_COUNT:
                break

            row = page.locator(selector).nth(index)

            try:
                row.scroll_into_view_if_needed(timeout=5_000)
                page.wait_for_timeout(100)
            except PlaywrightError:
                pass

            images = extract_team_images(row)

            if not images:
                page_skipped_rows += 1
                continue

            character_count = len(images)

            if not (MIN_CHARACTERS_PER_PLAYER <= character_count <= MAX_CHARACTERS_PER_PLAYER):
                page_skipped_rows += 1
                continue

            sampled_players += 1
            page_valid_players += 1
            player_sizes.append(character_count)

            total_character_slots += character_count
            page_character_slots += character_count

            player_character_keys = set()

            for item in images:
                key = character_key(item["image"], item["name"])

                # 編成数（2体いれば+2加算）
                character_counts[key]["occurrence_count"] += 1
                character_counts[key]["image"] = item["image"]

                if item["name"] != "名称不明":
                    character_counts[key]["name"] = item["name"]

                player_character_keys.add(key)

            # 採用人数（2体いてもプレイヤー1人として+1加算）
            for key in player_character_keys:
                character_counts[key]["player_count"] += 1

        print(
            f"[INFO] page={visited_pages}, "
            f"row_candidates={row_count}, "
            f"valid_players={page_valid_players}, "
            f"skipped_rows={page_skipped_rows}, "
            f"page_character_slots={page_character_slots}, "
            f"total_players={sampled_players}, "
            f"total_character_slots={total_character_slots}"
        )

        if sampled_players >= TARGET_PLAYER_COUNT:
            termination_reason = "target_reached"
            break

        if page_valid_players == 0:
            termination_reason = "no_valid_players"
            print("[WARN] 現在のページから有効なプレイヤーを取得できなかったため終了します。")
            break

        if not go_to_next_page(page, selector):
            termination_reason = "no_next_page"
            print("[INFO] 次ページが見つからなかったため終了します。")
            break

    if termination_reason == "unknown":
        if visited_pages >= MAX_PAGES:
            termination_reason = "max_pages_reached"
        elif sampled_players >= TARGET_PLAYER_COUNT:
            termination_reason = "target_reached"

    if sampled_players < MIN_REQUIRED_PLAYERS:
        raise RuntimeError(
            f"品質基準を満たしません。"
            f"取得人数={sampled_players}, "
            f"必要人数={MIN_REQUIRED_PLAYERS}, "
            f"終了理由={termination_reason}, "
            f"確認ページ数={visited_pages}, "
            f"使用セレクタ={selector}"
        )

    characters = []

    for data in character_counts.values():
        occurrence_count = int(data["occurrence_count"])
        player_count = int(data["player_count"])

        slot_rate = (
            round(occurrence_count / total_character_slots * 100, 2)
            if total_character_slots > 0
            else 0
        )

        adoption_rate = (
            round(player_count / sampled_players * 100, 1)
            if sampled_players > 0
            else 0
        )

        average_copies_when_used = (
            round(occurrence_count / player_count, 2)
            if player_count > 0
            else 0
        )

        characters.append(
            {
                "name": data["name"],
                "image": data["image"],
                "occurrence_count": occurrence_count,
                "player_count": player_count,
                "slot_rate": slot_rate,
                "adoption_rate": adoption_rate,
                "average_copies_when_used": average_copies_when_used,
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

    previous_occurrence_count = None
    previous_rank = 0

    for index, item in enumerate(characters, start=1):
        if item["occurrence_count"] != previous_occurrence_count:
            previous_rank = index
            previous_occurrence_count = item["occurrence_count"]

        item["rank"] = previous_rank

    expected_character_slots = sum(item["occurrence_count"] for item in characters)

    if expected_character_slots != total_character_slots:
        raise RuntimeError(
            f"キャラクター枠数の整合性検証に失敗しました。"
            f"行から取得した枠数={total_character_slots}, "
            f"キャラクター別合計={expected_character_slots}"
        )

    return {
        "schema_version": 2,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "name": SOURCE_NAME,
            "url": TARGET_URL,
        },
        "league": "レジェンド",
        "target_players": TARGET_PLAYER_COUNT,
        "sampled_players": sampled_players,
        "character_slots": total_character_slots,
        "median_characters_per_player": (
            median(player_sizes) if player_sizes else 0
        ),
        "pages_scanned": visited_pages,
        "termination_reason": termination_reason,
        "complete_target": sampled_players >= TARGET_PLAYER_COUNT,
        "counting_method": {
            "occurrence_count": "同一プレイヤー内の重複キャラクターを含む編成総数",
            "player_count": "対象キャラクターを1体以上採用したプレイヤー数",
            "slot_rate": "全編成枠に占める対象キャラクターの割合",
            "adoption_rate": "集計プレイヤーに占める採用プレイヤーの割合",
        },
        "characters": characters,
        "diagnostics": {
            "selected_row_selector": selector,
            "selector_scores": selector_diagnostics,
        },
    }


def write_output(data: dict) -> None:
    """一時ファイルを利用してJSONを安全に置き換える。"""
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = OUTPUT_PATH.with_suffix(".json.tmp")

    save_json(temporary_path, data)
    temporary_path.replace(OUTPUT_PATH)


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
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0 Safari/537.36 "
                "LegendUsageAggregator/1.0"
            ),
        )

        page = context.new_page()
        page.set_default_timeout(10_000)

        def record_response(response) -> None:
            try:
                content_type = response.headers.get("content-type", "")

                if (
                    "json" in content_type.lower()
                    or "javascript" in content_type.lower()
                ):
                    response_log.append(
                        {
                            "url": safe_debug_url(response.url),
                            "status": response.status,
                            "content_type": content_type,
                        }
                    )
            except PlaywrightError:
                pass

        page.on("response", record_record:=record_response)

        try:
            response = page.goto(
                TARGET_URL,
                wait_until="domcontentloaded",
                timeout=60_000,
            )

            if response is not None and response.status >= 400:
                raise RuntimeError(
                    f"対象ページがHTTP {response.status}を返しました。"
                )

            try:
                page.wait_for_load_state(
                    "networkidle",
                    timeout=15_000,
                )
            except PlaywrightTimeoutError:
                print(
                    "[WARN] networkidle待機がタイムアウトしました。"
                    "現在のDOMで処理を続行します。"
                )

            page.wait_for_timeout(3_000)
            dismiss_common_dialogs(page)
            select_legend_league(page)
            page.wait_for_timeout(2_000)

            if DEBUG:
                selector, diagnostics = find_best_row_selector(page)
                dump_debug(
                    page,
                    response_log,
                    {
                        "mode": "debug",
                        "selected_row_selector": selector,
                        "selector_scores": diagnostics,
                    },
                )
                print("[DEBUG] 調査ファイルを.artifacts/debugへ保存しました。")
                return

            data = scrape(page)
            write_output(data)

            print(
                f"[DONE] players={data['sampled_players']}, "
                f"characters={len(data['characters'])}, "
                f"output={OUTPUT_PATH}"
            )

        except Exception as error:
            print(f"[ERROR] {error}", file=sys.stderr)

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
