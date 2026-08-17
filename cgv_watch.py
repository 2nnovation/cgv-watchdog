#!/usr/bin/env python3
"""Watch CGV showtimes through the same BFF endpoint used by cgv.co.kr."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


CGV_ORIGIN = "https://cgv.co.kr"
API_PATH = "/api/v1/booking/searchMovScnInfo"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class Theater:
    name: str
    site_no: str
    business_place_no: str


THEATERS = {
    "광교": Theater("광교", "0257", "0257001"),
    "CGV 광교": Theater("광교", "0257", "0257001"),
}


class QueryFailure(RuntimeError):
    """Raised when the schedule could not be read reliably."""


@dataclass(frozen=True)
class Config:
    theater: Theater
    date: str
    movie_keyword: str
    screen_type: str
    state_path: Path
    headless: bool
    page_wait_ms: int
    browser_path: str | None
    user_agent: str
    telegram_bot_token: str | None
    telegram_chat_id: str | None


@dataclass(frozen=True)
class Showtime:
    session_id: str
    date: str
    start_time: str
    end_time: str
    theater_name: str
    auditorium: str
    screen_label: str
    movie_name: str
    free_seats: int | None
    total_seats: int | None
    bookable: bool
    movie_no: str
    product_no: str
    screen_no: str
    sequence: str


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def resolve_theater() -> Theater:
    name = os.getenv("THEATER", "광교").strip()
    known = THEATERS.get(name)
    if known:
        return known

    site_no = os.getenv("THEATER_CODE", "").strip()
    business_place_no = os.getenv("BUSINESS_PLACE_NO", "").strip()
    if site_no and business_place_no:
        return Theater(name=name, site_no=site_no, business_place_no=business_place_no)

    raise ValueError(
        f"알 수 없는 극장 '{name}'입니다. 다른 극장은 THEATER_CODE와 "
        "BUSINESS_PLACE_NO도 함께 설정하세요."
    )


def load_config() -> Config:
    date = os.getenv("DATE", "2026-08-29").strip()
    datetime.strptime(date, "%Y-%m-%d")
    movie_keyword = os.getenv("MOVIE_KEYWORD", "오디세이").strip()
    screen_type = os.getenv("SCREEN_TYPE", "IMAX").strip()
    if not movie_keyword or not screen_type:
        raise ValueError("MOVIE_KEYWORD와 SCREEN_TYPE은 비워둘 수 없습니다.")

    return Config(
        theater=resolve_theater(),
        date=date,
        movie_keyword=movie_keyword,
        screen_type=screen_type,
        state_path=Path(os.getenv("STATE_PATH", "state.json")),
        headless=env_bool("HEADLESS", True),
        page_wait_ms=int(os.getenv("CGV_PAGE_WAIT_MS", "1500")),
        browser_path=os.getenv("BROWSER_PATH") or None,
        user_agent=os.getenv("CGV_USER_AGENT", DEFAULT_USER_AGENT),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
    )


def find_browser_executable(configured: str | None) -> str | None:
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise ValueError(f"BROWSER_PATH 파일을 찾을 수 없습니다: {path}")
        return str(path)

    candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    for command in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        executable = shutil.which(command)
        if executable:
            return executable
    return None


def compact_date(date: str) -> str:
    return date.replace("-", "")


def format_hhmm(value: Any) -> str:
    digits = str(value or "").strip().zfill(4)
    return f"{digits[:2]}:{digits[2:4]}" if len(digits) >= 4 else str(value or "")


def int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def contains_keyword(item: dict[str, Any], keyword: str) -> bool:
    needle = keyword.casefold().replace(" ", "")
    fields = ("movNm", "prodNm", "expoProdNm", "movEnm", "engProdNm")
    return any(
        needle in str(item.get(field) or "").casefold().replace(" ", "")
        for field in fields
    )


def matches_screen(item: dict[str, Any], screen_type: str) -> bool:
    requested = screen_type.casefold().replace(" ", "")
    aliases = {requested}
    if requested == "imax":
        aliases.add("아이맥스")
    fields = (
        "scnsNm",
        "expoScnsNm",
        "scnsEnm",
        "movkndDsplNm",
        "movkndDsplEnm",
        "tcscnsGradNm",
    )
    haystack = " ".join(str(item.get(field) or "") for field in fields).casefold().replace(" ", "")
    return any(alias in haystack for alias in aliases)


def make_showtime(item: dict[str, Any]) -> Showtime:
    free = int_or_none(item.get("frSeatCnt"))
    total = int_or_none(item.get("stcnt"))
    identifiers = [
        item.get("scnYmd"),
        item.get("siteNo"),
        item.get("scnsNo"),
        item.get("scnSseq"),
        item.get("prodNo"),
    ]
    session_id = ":".join(str(value or "") for value in identifiers)
    return Showtime(
        session_id=session_id,
        date=str(item.get("scnYmd") or ""),
        start_time=format_hhmm(item.get("scnsrtTm")),
        end_time=format_hhmm(item.get("scnendTm")),
        theater_name=str(item.get("siteNm") or ""),
        auditorium=str(item.get("expoScnsNm") or item.get("scnsNm") or ""),
        screen_label=str(item.get("movkndDsplNm") or item.get("tcscnsGradNm") or ""),
        movie_name=str(item.get("movNm") or item.get("expoProdNm") or ""),
        free_seats=free,
        total_seats=total,
        bookable=(free is None or free > 0) and str(item.get("cntlYn") or "N") != "Y",
        movie_no=str(item.get("movNo") or ""),
        product_no=str(item.get("prodNo") or ""),
        screen_no=str(item.get("scnsNo") or ""),
        sequence=str(item.get("scnSseq") or ""),
    )


def query_schedule(config: Config) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "coCd": "A420",
            "siteNo": config.theater.site_no,
            "scnYmd": compact_date(config.date),
            "scnsNo": "",
            "scnSseq": "",
            "rtctlScopCd": "08",
            "custNo": "",
        }
    )
    api_url = f"{CGV_ORIGIN}{API_PATH}?{query}"
    bootstrap_url = f"{CGV_ORIGIN}/cnm/bzplcCgv/{config.theater.business_place_no}"
    browser_executable = find_browser_executable(config.browser_path)

    diagnostics: dict[str, Any] = {
        "method": "Playwright browser session + CGV BFF API",
        "bootstrap_url": bootstrap_url,
        "api_path": API_PATH,
        "browser_executable": browser_executable or "Playwright managed Chromium",
    }

    try:
        with sync_playwright() as playwright:
            launch_options: dict[str, Any] = {
                "headless": config.headless,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                ],
            }
            if browser_executable:
                launch_options["executable_path"] = browser_executable

            browser = playwright.chromium.launch(**launch_options)
            try:
                context = browser.new_context(
                    locale="ko-KR",
                    timezone_id="Asia/Seoul",
                    user_agent=config.user_agent,
                )
                page = context.new_page()
                response = page.goto(
                    bootstrap_url,
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                diagnostics["page_http_status"] = response.status if response else None
                page.wait_for_timeout(config.page_wait_ms)
                body_text = page.locator("body").inner_text(timeout=5_000)
                if "비정상적으로 CGV에 접속" in body_text or "이용이 제한" in body_text:
                    raise QueryFailure("CGV 보안 페이지가 브라우저 접근을 차단했습니다.")

                api_result = page.evaluate(
                    """async (url) => {
                        const response = await window.fetch(url, {
                            credentials: 'include',
                            headers: {
                                Accept: 'application/json',
                                'Accept-Language': 'ko-KR'
                            }
                        });
                        return {
                            status: response.status,
                            contentType: response.headers.get('content-type') || '',
                            text: await response.text()
                        };
                    }""",
                    api_url,
                )
                diagnostics["api_http_status"] = api_result["status"]
                diagnostics["api_content_type"] = api_result["contentType"]
                diagnostics["api_response_bytes"] = len(api_result["text"].encode("utf-8"))

                if api_result["status"] != 200:
                    reason = "CGV/Cloudflare 차단 가능성" if api_result["status"] == 403 else "HTTP 오류"
                    raise QueryFailure(f"시간표 API HTTP {api_result['status']} ({reason})")

                try:
                    payload = json.loads(api_result["text"])
                except json.JSONDecodeError as error:
                    preview = api_result["text"][:160].replace("\n", " ")
                    raise QueryFailure(f"시간표 API가 JSON 대신 다른 내용을 반환했습니다: {preview}") from error

                diagnostics["api_status_code"] = payload.get("statusCode")
                diagnostics["api_status_message"] = payload.get("statusMessage")
                if payload.get("statusCode") != 0:
                    raise QueryFailure(
                        f"시간표 API statusCode={payload.get('statusCode')}: "
                        f"{payload.get('statusMessage')}"
                    )
                data = payload.get("data")
                if data is None:
                    raise QueryFailure("시간표 API 응답에 data 필드가 없습니다.")
                if not isinstance(data, list):
                    raise QueryFailure("시간표 API의 data 형식이 목록이 아닙니다.")
                diagnostics["schedule_item_count"] = len(data)
                return data, diagnostics
            finally:
                browser.close()
    except QueryFailure:
        raise
    except (PlaywrightTimeoutError, PlaywrightError) as error:
        raise QueryFailure(f"브라우저 실행/페이지 로드 실패: {error}") from error


def select_matches(
    data: list[dict[str, Any]], config: Config
) -> tuple[list[dict[str, Any]], list[Showtime]]:
    movie_matches = [item for item in data if contains_keyword(item, config.movie_keyword)]
    screen_matches = [make_showtime(item) for item in movie_matches if matches_screen(item, config.screen_type)]
    screen_matches.sort(key=lambda item: (item.start_time, item.screen_no, item.sequence))
    return movie_matches, screen_matches


def print_report(
    config: Config,
    data: list[dict[str, Any]],
    movie_matches: list[dict[str, Any]],
    showtimes: list[Showtime],
    diagnostics: dict[str, Any],
) -> None:
    print(f"CGV {config.theater.name} {config.date} 시간표 조회: 성공")
    print(
        f"조회 방식: {diagnostics['method']} "
        f"(페이지 HTTP {diagnostics.get('page_http_status')}, API HTTP {diagnostics.get('api_http_status')})"
    )
    print(f"전체 시간표 데이터: {len(data)}개")
    print(f"《{config.movie_keyword}》 검색 결과: {len(movie_matches)}개")
    print(f"{config.screen_type} 일치 회차: {len(showtimes)}개")

    if showtimes:
        for showtime in showtimes:
            if showtime.free_seats is None or showtime.total_seats is None:
                seats = "잔여석 정보 없음"
            else:
                seats = f"잔여 {showtime.free_seats}/{showtime.total_seats}석"
            availability = "예매 가능" if showtime.bookable else "예매 불가/제한"
            print(
                f"- {showtime.start_time}-{showtime.end_time} | {showtime.auditorium} | "
                f"{showtime.screen_label} | {seats} | {availability}"
            )
    elif data:
        if movie_matches:
            print(f"판정: 영화 데이터는 있으나 {config.screen_type} 조건의 회차는 없습니다.")
        else:
            print(f"판정: 시간표 조회는 성공했지만 《{config.movie_keyword}》 회차가 없습니다.")
    else:
        print("판정: API 조회는 성공했으며, 해당 극장/날짜의 등록된 회차가 0개입니다.")


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise QueryFailure(f"상태 파일을 읽을 수 없습니다: {path}: {error}") from error
    if not isinstance(state, dict):
        raise QueryFailure(f"상태 파일 최상위 형식은 JSON 객체여야 합니다: {path}")
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def state_key(config: Config) -> str:
    return "|".join(
        (
            config.theater.site_no,
            config.date,
            config.movie_keyword.casefold(),
            config.screen_type.casefold(),
        )
    )


def booking_url(config: Config) -> str:
    return f"{CGV_ORIGIN}/cnm/bzplcCgv/{config.theater.business_place_no}"


def telegram_message(config: Config, new_showtimes: list[Showtime]) -> str:
    times = ", ".join(item.start_time for item in new_showtimes)
    return "\n".join(
        (
            f"CGV {config.theater.name} {config.screen_type} 예매 오픈",
            f"영화: {config.movie_keyword}",
            f"날짜: {config.date}",
            f"상영시간: {times}",
            f"예매: {booking_url(config)}",
        )
    )


def send_telegram(config: Config, message: str) -> None:
    if not config.telegram_bot_token or not config.telegram_chat_id:
        raise QueryFailure(
            "새 회차를 발견했지만 TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID가 없습니다. "
            "알림을 보내지 않았고 상태도 저장하지 않습니다."
        )

    url = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"
    body = urllib.parse.urlencode(
        {
            "chat_id": config.telegram_chat_id,
            "text": message,
            "disable_web_page_preview": "false",
        }
    ).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise QueryFailure(f"Telegram API HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise QueryFailure(f"Telegram 알림 전송 실패: {error}") from error
    if not payload.get("ok"):
        raise QueryFailure(f"Telegram API가 실패를 반환했습니다: {payload.get('description', 'unknown')}")


def monitor(config: Config, showtimes: list[Showtime]) -> int:
    state = load_state(config.state_path)
    key = state_key(config)
    entry = state.get(key, {})
    notified = set(entry.get("notified_session_ids", [])) if isinstance(entry, dict) else set()
    new_showtimes = [item for item in showtimes if item.session_id not in notified]

    if not new_showtimes:
        print("새로 추가된 미알림 회차: 0개")
        return 0

    print(f"새로 추가된 미알림 회차: {len(new_showtimes)}개")
    message = telegram_message(config, new_showtimes)
    send_telegram(config, message)
    print("Telegram 알림: 전송 성공")

    notified.update(item.session_id for item in new_showtimes)
    state[key] = {
        "theater": config.theater.name,
        "date": config.date,
        "movie_keyword": config.movie_keyword,
        "screen_type": config.screen_type,
        "notified_session_ids": sorted(notified),
        "last_notification_at": datetime.now(timezone.utc).isoformat(),
        "last_notified_showtimes": [asdict(item) for item in new_showtimes],
    }
    save_state(config.state_path, state)
    print(f"상태 저장: {config.state_path}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CGV 특정 회차 예매 오픈 감시")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="조회 결과만 출력하고 Telegram 전송과 상태 변경은 하지 않습니다.",
    )
    return parser.parse_args()


def main() -> int:
    _configure_console()
    args = parse_args()
    try:
        config = load_config()
        data, diagnostics = query_schedule(config)
        movie_matches, showtimes = select_matches(data, config)
        print_report(config, data, movie_matches, showtimes, diagnostics)
        if args.check_only:
            print("실행 모드: 조회 전용(알림/상태 변경 없음)")
            return 0
        return monitor(config, showtimes)
    except (ValueError, QueryFailure) as error:
        print("시간표 조회/감시: 실패", file=sys.stderr)
        print(f"원인: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
