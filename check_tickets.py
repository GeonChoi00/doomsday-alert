#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
어벤져스: 둠스데이 특별관(IMAX / 돌비 시네마) 예매 오픈 알림 봇
=================================================================

동작 순서 (한 번 '체크'할 때마다)
  1순위  CGV IMAX      → 지점마다 따로 조회 (한 지점이 실패해도 다음 지점 계속)
  2순위  메가박스 돌비  → IMAX 결과와 상관없이 무조건 조회 (서로 독립)
  → 저녁 회차(기본 18:00 이후) 중 아직 알림을 안 보낸 [포맷+지점+날짜+시간]만 텔레그램 발송

데이터 출처
  - CGV     : 네이버 플레이스 '상영시간표' 페이지 (CGV 예매 페이지와 같은 회차 정보 + CGV 예매 딥링크 제공)
              ※ CGV 공식 사이트(cgv.co.kr)는 Cloudflare 봇 차단으로 일반 HTTP 요청을 403으로 막습니다.
  - 메가박스 : 메가박스 홈페이지 극장별 상영시간표가 쓰는 JSON API (schedulePage.do)

설정은 아래 [사용자 설정] 블록만 고치면 됩니다.
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import requests

# ════════════════════════════════════════════════════════════════════
# [사용자 설정] ― 여기만 고치면 됩니다
# ════════════════════════════════════════════════════════════════════

# 영화 제목에 아래 단어 중 하나라도 들어가면 감지.
# 띄어쓰기·대소문자·기호(: - 등)는 무시하고 비교하므로
#   "어벤져스: 둠스데이", "어벤져스 둠스데이", "둠스 데이", "Avengers: Doomsday", "DOOMSDAY" 모두 잡힘
MOVIE_KEYWORDS = ["둠스데이", "doomsday", "dooms day"]

DATE_START = "2026-12-16"          # 감시 시작 날짜 (포함)
DATE_END = "2026-12-20"            # 감시 끝 날짜 (포함)

MIN_START_TIME = "18:00"           # 이 시간 '이상'에 시작하는 회차만 알림 (예: "18:30", "19:00")
                                   # 지점별로 다르게 하고 싶으면 아래 목록의 "min_time"을 쓰세요.
WEEKEND_NO_TIME_LIMIT = True       # True: 토·일 상영 회차는 시간 제한 없이 전부 알림 (12/19 토, 12/20 일)

# ── 1순위: CGV IMAX (서울 IMAX 5곳 전부) ───────────────────────────
#   enabled  : False로 바꾸면 그 지점은 조회 안 함
#   site_no  : CGV 극장코드 (예매 링크 생성용)
#   naver_id : 네이버 플레이스 극장 ID  (m.place.naver.com/theater/<이 숫자>)
#   min_time : 지점별 기준 시간 (None이면 MIN_START_TIME 사용)
CGV_IMAX_THEATERS = [
    {"name": "CGV 왕십리",         "site_no": "0074", "naver_id": "12964379", "enabled": True, "min_time": None},
    {"name": "CGV 용산아이파크몰", "site_no": "0013", "naver_id": "12298207", "enabled": True, "min_time": None},
    {"name": "CGV 압구정",         "site_no": "0040", "naver_id": "12197890", "enabled": True, "min_time": None},
    {"name": "CGV 천호",           "site_no": "0199", "naver_id": "36877562", "enabled": True, "min_time": None},
    {"name": "CGV 영등포",         "site_no": "0059", "naver_id": "13141635", "enabled": True, "min_time": None},
]

# ── CGV 프라이빗 박스 ───────────────────────────────────────────────
#   hall_keyword : 상영관 이름에 이 글자가 들어가면 프라이빗 박스 상영관으로 감지
#   ※ CGV 압구정은 IMAX관 안에 프라이빗 박스(5개)가 있어서, 위 IMAX 알림에 같이 포함돼요.
CGV_PRIVATE_THEATERS = [
    {"name": "CGV 영등포", "site_no": "0059", "naver_id": "13141635", "hall_keyword": "PRIVATE BOX",
     "enabled": True, "min_time": None},   # 'SCREENX관 (리클라이너) with PRIVATE BOX'
    {"name": "CGV 연남",   "site_no": "0292", "naver_id": "1256468787", "hall_keyword": "SUITE CINEMA",
     "enabled": True, "min_time": None},   # 'SUITE CINEMA' (박스형 프라이빗 상영관)
]

# ── 2순위: 메가박스 돌비 ──────────────────────────────────────────
#   halls    : 감지할 상영관 종류.  "DBC" = 돌비 시네마(정식)
#                                   "DVA" = 돌비 비전+애트모스관(일반 돌비관, 돌비 시네마 아님)
#              둘 다 넣으면 어느 쪽이 열려도 알림 (알림 태그는 실제 열린 관 종류로 붙음)
#   brch_no  : 메가박스 지점번호
D = ["DBC", "DVA"]
MEGABOX_THEATERS = [
    {"name": "메가박스 코엑스",           "brch_no": "1351", "halls": D, "enabled": True, "min_time": None},  # 돌비 시네마
    {"name": "메가박스 구의이스트폴",     "brch_no": "0090", "halls": D, "enabled": True, "min_time": None},  # 돌비 비전+애트모스
    {"name": "메가박스 목동",             "brch_no": "1581", "halls": D, "enabled": True, "min_time": None},  # 돌비 비전+애트모스
    {"name": "메가박스 상암월드컵경기장", "brch_no": "1211", "halls": D, "enabled": True, "min_time": None},  # 돌비 비전+애트모스
    {"name": "메가박스 센트럴",           "brch_no": "1371", "halls": D, "enabled": True, "min_time": None},  # 11월 기준 돌비 회차 없음
    {"name": "메가박스 하남스타필드",     "brch_no": "4651", "halls": D, "enabled": True, "min_time": None},  # 돌비 시네마
    {"name": "메가박스 수원스타필드",     "brch_no": "0062", "halls": D, "enabled": True, "min_time": None},  # 돌비 비전+애트모스
    # 지점 추가 예시 (돌비 시네마):  수원AK플라자 "0052" / 남양주 스페이스원 "0019" / 송도 "4062"
]

# ── 실행 방식 ─────────────────────────────────────────────────────
LOOP_SECONDS = 200        # 한 번 실행될 때 몇 초 동안 반복 체크할지 (0 = 딱 한 번만 체크)
CHECK_INTERVAL = 100      # 반복 체크 간격(초).  200/100 이면 0초·100초·200초에 총 3번 체크
REQUEST_DELAY = 0.7       # 요청 사이 쉬는 시간(초) ― 사이트에 부담 안 주기
STATE_FILE = "sent_state.json"   # 알림 보낸 기록 (중복 알림 방지)

# ════════════════════════════════════════════════════════════════════
# (아래는 고칠 필요 없음) 환경변수로 덮어쓰기 ― 테스트용
#   MOVIE_KEYWORD(쉼표로 여러 개 가능), DATE_START, DATE_END, MIN_START_TIME,
#   DRY_RUN=1 (텔레그램 대신 화면 출력), NO_STATE=1 (기록 파일 안 씀),
#   LOOP_SECONDS, CHECK_INTERVAL, PING=1 (텔레그램 연결 테스트 메시지만 전송)
# ════════════════════════════════════════════════════════════════════

def _env(name: str, default: str) -> str:
    v = os.environ.get(name, "").strip()
    return v if v else default


if os.environ.get("MOVIE_KEYWORD", "").strip():
    MOVIE_KEYWORDS = [k for k in os.environ["MOVIE_KEYWORD"].split(",") if k.strip()]


def _norm(text: str) -> str:
    """비교용: 소문자로 바꾸고 글자·숫자 말고 전부(공백, :, -, · 등) 제거."""
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


_KEYS = [_norm(k) for k in MOVIE_KEYWORDS if _norm(k)]


def title_matches(title: str) -> bool:
    t = _norm(title)
    return any(k in t for k in _KEYS)
DATE_START = _env("DATE_START", DATE_START)
DATE_END = _env("DATE_END", DATE_END)
MIN_START_TIME = _env("MIN_START_TIME", MIN_START_TIME)
LOOP_SECONDS = int(_env("LOOP_SECONDS", str(LOOP_SECONDS)))
CHECK_INTERVAL = int(_env("CHECK_INTERVAL", str(CHECK_INTERVAL)))
STATE_FILE = _env("STATE_FILE", STATE_FILE)
DRY_RUN = _env("DRY_RUN", "0") == "1"
NO_STATE = _env("NO_STATE", "0") == "1"
PING = _env("PING", "0") == "1"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

KST = timezone(timedelta(hours=9))
WEEKDAY_KO = "월화수목금토일"

UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")
UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

TAGS = {"IMAX": "[IMAX]", "DBC": "[돌비 시네마]", "DVA": "[돌비 비전+애트모스]", "PRIVATE": "[프라이빗 박스]"}

session = requests.Session()


# ────────────────────────────────────────────────────────────────────
# 공통 유틸
# ────────────────────────────────────────────────────────────────────
def log(msg: str) -> None:
    print(f"[{datetime.now(KST):%m-%d %H:%M:%S}] {msg}", flush=True)


def time_to_minutes(text: str) -> int | None:
    """'18:20' → 1100,  '1820' → 1100,  '24:10'(심야, 익일 00:10) → 1450.  형식이 이상하면 None."""
    m = re.fullmatch(r"\s*(\d{1,2}):?(\d{2})\s*", text or "")
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if mm >= 60 or hh > 30:
        return None
    return hh * 60 + mm


def display_time(text: str) -> str:
    """'24:10' 같은 심야 표기는 '24:10(익일 00:10)'으로 보기 좋게."""
    mins = time_to_minutes(text)
    if mins is None:
        return text
    hh, mm = divmod(mins, 60)
    base = f"{hh:02d}:{mm:02d}"
    return f"{base}(익일 {hh - 24:02d}:{mm:02d})" if hh >= 24 else base


def target_dates() -> list[str]:
    """DATE_START~DATE_END 사이의 날짜 중 오늘 이후만 'YYYY-MM-DD' 목록으로."""
    start = date.fromisoformat(DATE_START)
    end = date.fromisoformat(DATE_END)
    today = datetime.now(KST).date()
    out, d = [], max(start, today)
    while d <= end:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def pretty_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.month}월 {d.day}일({WEEKDAY_KO[d.weekday()]})"


def min_rule(theater: dict, date_iso: str) -> tuple[int, str]:
    """그 날짜에 적용할 (기준 분, 표시용 라벨). 주말이면 (0, "") = 제한 없음."""
    if WEEKEND_NO_TIME_LIMIT and date.fromisoformat(date_iso).weekday() >= 5:
        return 0, ""
    label = theater.get("min_time") or MIN_START_TIME
    return time_to_minutes(label) or 0, label


def esc(s: str) -> str:
    """텔레그램 HTML 모드용 이스케이프."""
    return html.escape(s or "", quote=True)


@dataclass
class Show:
    fmt: str            # "IMAX" / "DBC" / "DVA"
    theater: str        # 지점명
    date: str           # YYYY-MM-DD
    start: str          # "19:30"
    movie: str
    hall: str = ""
    link: str = ""      # 회차별 링크
    seats: str = ""
    book_url: str = ""  # 지점 예매 페이지
    min_label: str = "" # 이 지점에 적용된 기준 시간
    key: str = field(init=False)

    def __post_init__(self):
        # 중복 판단 기준: 포맷 + 지점 + 날짜 + 시작시간 (+상영관 이름 ― 같은 시간에 관이 2개인 경우 대비)
        self.key = f"{self.fmt}|{self.theater}|{self.date}|{self.start}|{self.hall}"


# ────────────────────────────────────────────────────────────────────
# 1순위: CGV IMAX  (네이버 플레이스 상영시간표)
# ────────────────────────────────────────────────────────────────────
APOLLO_RE = re.compile(r"window\.__APOLLO_STATE__\s*=\s*(\{.*?\});\s*(?:window\.|</script>)", re.S)


_NAVER_CACHE: dict = {}     # 한 번 체크하는 동안 같은 페이지를 두 번 받지 않도록 (영등포: IMAX + 프라이빗)


def _naver_page(naver_id: str, date_iso: str) -> tuple[list[dict], set[str]]:
    key = (naver_id, date_iso)
    if key not in _NAVER_CACHE:
        _NAVER_CACHE[key] = _naver_page_fetch(naver_id, date_iso)
    movies, tabs = _NAVER_CACHE[key]
    return list(movies), set(tabs)


def _naver_page_fetch(naver_id: str, date_iso: str) -> tuple[list[dict], set[str]]:
    url = f"https://m.place.naver.com/theater/{naver_id}/movie?datefilter={date_iso}"
    r = session.get(url, timeout=25, headers={
        "User-Agent": UA_MOBILE,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Referer": "https://m.place.naver.com/",
    })
    r.raise_for_status()
    r.encoding = "utf-8"          # 네이버는 charset을 안 알려줘서 한글이 깨질 수 있음 → 강제 지정
    m = APOLLO_RE.search(r.text)
    if not m:
        raise RuntimeError("네이버 페이지에서 상영시간표 데이터를 못 찾음 (페이지 구조 변경 의심)")
    state = json.loads(m.group(1))

    movies: list[dict] = []

    def walk(o):
        if isinstance(o, dict):
            if o.get("__typename") == "MovieTime":
                movies.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(state)
    if not movies:     # 영업 중인 극장에 상영작이 0개일 리 없음 → ID 오류나 구조 변경으로 판단
        raise RuntimeError(f"상영작 0개 (naver_id={naver_id} 확인 필요)")
    tabs =set(re.findall(rf"/theater/{naver_id}/movie\?datefilter=(\d{{4}}-\d{{2}}-\d{{2}})", r.text))
    return movies, tabs


def check_cgv(theater: dict, dates: list[str], fmt: str = "IMAX") -> list[Show]:
    """fmt="IMAX": 상영관 이름에 IMAX /  fmt="PRIVATE": 상영관 이름에 theater["hall_keyword"]"""
    want = set(dates)

    movies, _tabs = _naver_page(theater["naver_id"], dates[0])
    have_dates = {m.get("date") for m in movies}
    # 한 페이지에 보통 '열린 날짜 전체'가 들어있지만, 혹시 빠진 대상 날짜가 있으면 그 날짜로 한 번 더 조회
    for d in dates:
        if d not in have_dates and d != dates[0] and d in _tabs:
            time.sleep(REQUEST_DELAY)
            more, _ = _naver_page(theater["naver_id"], d)
            movies.extend(more)

    shows: list[Show] = []
    for mv in movies:
        name = mv.get("name") or ""
        if not title_matches(name) or mv.get("date") not in want:
            continue
        for hall in mv.get("scheduleList") or []:
            hall_name = hall.get("theaterName") or ""
            if fmt == "IMAX":
                if "IMAX" not in hall_name.upper() and "아이맥스" not in hall_name:
                    continue
            elif theater["hall_keyword"].upper() not in hall_name.upper():
                continue
            min_mins, min_label = min_rule(theater, mv["date"])
            for s in hall.get("schedule") or []:
                start = s.get("rtime") or ""
                mins = time_to_minutes(start)
                if mins is None or mins < min_mins:
                    continue
                if s.get("isBookable") is False:       # 이미 지난 회차/예매 마감
                    continue
                book_url = (f"https://cgv.co.kr/cnm/movieBook/cinema?siteNo={theater['site_no']}"
                            f"&scnYmd={mv['date'].replace('-', '')}")
                link = s.get("ticketMobileUrl") or s.get("ticketPcUrl") or book_url
                shows.append(Show(fmt, theater["name"], mv["date"], start, name,
                                  hall=hall_name, link=_safe_url(link), book_url=book_url,
                                  min_label=min_label))
    return shows


def _safe_url(url: str) -> str:
    """URL 안의 한글(예: siteNm=왕십리)을 퍼센트 인코딩 ― 텔레그램 링크 깨짐 방지."""
    return urllib.parse.quote(url, safe=":/?&=%#+-_.~")


# ────────────────────────────────────────────────────────────────────
# 2순위: 메가박스 돌비  (메가박스 극장별 상영시간표 JSON)
# ────────────────────────────────────────────────────────────────────
MEGABOX_API = "https://www.megabox.co.kr/on/oh/ohc/Brch/schedulePage.do"


def check_megabox(theater: dict, dates: list[str]) -> list[Show]:
    brch = theater["brch_no"]
    today = datetime.now(KST).strftime("%Y%m%d")
    shows: list[Show] = []
    errors = []

    for d in dates:                       # 날짜별로도 독립 처리 (하루 실패해도 나머지 날짜 계속)
        min_mins, min_label = min_rule(theater, d)
        try:
            body = {"masterType": "brch", "detailType": "area", "brchNo": brch, "brchNo1": brch,
                    "firstAt": "N", "crtDe": today, "playDe": d.replace("-", "")}
            r = session.post(MEGABOX_API, json=body, timeout=25, headers={
                "User-Agent": UA_DESKTOP,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"https://www.megabox.co.kr/theater/time?brchNo={brch}",
            })
            r.raise_for_status()
            data = r.json()
            if str(data.get("statCd")) != "0":
                raise RuntimeError(f"statCd={data.get('statCd')} msg={data.get('msg')}")
            rows = (data.get("megaMap") or {}).get("movieFormList") or []
        except Exception as e:  # noqa: BLE001
            errors.append(f"{d}: {e}")
            continue
        finally:
            time.sleep(REQUEST_DELAY)

        for row in rows:
            name = html.unescape(row.get("movieNm") or row.get("rpstMovieNm") or "")
            if not title_matches(name):
                continue
            kind = (row.get("theabKindCd") or "").upper()
            if kind not in theater["halls"]:
                continue
            if row.get("bokdAbleAt") not in (None, "Y"):     # 예매 불가(매진/마감)
                continue
            start = row.get("playStartTime") or ""
            mins = time_to_minutes(start)
            if mins is None or mins < min_mins:
                continue
            rest, tot = row.get("restSeatCnt"), row.get("totSeatCnt")
            seats = f"잔여 {rest}/{tot}석" if rest is not None and tot else ""
            url = f"https://www.megabox.co.kr/theater/time?brchNo={brch}"
            shows.append(Show(kind, theater["name"], d, start, name,
                              hall=html.unescape(row.get("theabExpoNm") or ""),
                              link=url, book_url=url, seats=seats, min_label=min_label))

    if errors and len(errors) == len(dates):          # 모든 날짜가 실패 = 지점 조회 실패
        raise RuntimeError("; ".join(errors))
    for e in errors:
        log(f"   ⚠ {theater['name']} 일부 날짜 실패 → {e}")
    return shows


# ────────────────────────────────────────────────────────────────────
# 텔레그램
# ────────────────────────────────────────────────────────────────────
def send_telegram(text: str) -> bool:
    if DRY_RUN or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        reason = "DRY_RUN" if DRY_RUN else "텔레그램 토큰/챗ID 없음"
        log(f"[{reason}] 실제 전송 대신 출력합니다 ↓\n" + re.sub(r"<[^>]+>", "", text) + "\n")
        return DRY_RUN       # DRY_RUN이면 '보낸 것으로' 처리, 설정 누락이면 실패 처리
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    for _ in range(3):
        try:
            r = requests.post(url, json=payload, timeout=20)
            if r.status_code == 429:                       # 너무 자주 보냄 → 잠깐 대기 후 재시도
                wait = int(r.json().get("parameters", {}).get("retry_after", 5))
                time.sleep(min(wait, 60))
                continue
            if r.ok:
                time.sleep(1.1)                            # 텔레그램 초당 전송 제한 회피
                return True
            log(f"텔레그램 전송 실패: {r.status_code} {r.text[:200]}")
            return False
        except requests.RequestException as e:
            log(f"텔레그램 전송 에러: {e}")
            time.sleep(3)
    return False


def build_message(group: list[Show], new_keys: set[str]) -> str:
    s0 = group[0]
    tag = TAGS.get(s0.fmt, f"[{s0.fmt}]")
    lines = [
        f"<b>{esc(tag)} 예매 오픈!</b>",
        f"🎬 {esc(s0.movie)}",
        f"📍 <b>{esc(s0.theater)}</b>",
        f"📅 {esc(pretty_date(s0.date))}",
        (f"⏰ {esc(s0.min_label)} 이후 상영 시작 회차:" if s0.min_label
         else "⏰ 예매 가능한 회차 (주말이라 시간 제한 없음):"),
    ]
    for s in sorted(group, key=lambda x: time_to_minutes(x.start) or 0):
        mark = "🆕 " if s.key in new_keys else "　 "
        extra = " · ".join(x for x in (s.hall, s.seats) if x)
        t = esc(display_time(s.start))
        time_txt = f'<a href="{esc(s.link)}">{t}</a>' if s.fmt in ("IMAX", "PRIVATE") else t
        lines.append(f"{mark}{time_txt}" + (f"  <i>{esc(extra)}</i>" if extra else ""))
    lines.append("")
    lines.append(f'👉 <a href="{esc(s0.book_url or s0.link)}">바로 예매하러 가기</a>')
    if s0.fmt in ("IMAX", "PRIVATE"):
        lines.append("<i>(시간을 누르면 해당 회차 예매 화면으로 바로 이동)</i>")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────
# 상태(중복 방지) 저장
# ────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if NO_STATE:
        return {"sent": {}, "alerts": {}}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    st.setdefault("sent", {})
    st.setdefault("alerts", {})
    return st


def save_state(state: dict) -> None:
    if NO_STATE:
        return
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, STATE_FILE)


# ────────────────────────────────────────────────────────────────────
# 한 번 체크
# ────────────────────────────────────────────────────────────────────
def run_check(state: dict, fail_counter: dict) -> None:
    dates = target_dates()
    found: list[Show] = []
    _NAVER_CACHE.clear()

    # ── 1순위: CGV IMAX ──  (지점마다 try/except → 한 곳이 에러여도 다음 지점 계속)
    imax_count = 0
    for th in CGV_IMAX_THEATERS:
        if not th.get("enabled", True):
            continue
        src = f"CGV:{th['name']}"
        try:
            shows = check_cgv(th, dates, "IMAX")
            imax_count += len(shows)
            found += shows
            fail_counter[src] = 0
            log(f" [IMAX] {th['name']}: 조건 맞는 회차 {len(shows)}개")
        except Exception as e:  # noqa: BLE001
            fail_counter[src] = fail_counter.get(src, 0) + 1
            log(f" [IMAX] {th['name']}: 조회 실패 → {type(e).__name__}: {e}")
        time.sleep(REQUEST_DELAY)

    if imax_count == 0:
        log(" → IMAX 회차 없음/조회 불가. 멈추지 않고 2순위(돌비) 조회로 넘어갑니다.")

    # ── 2순위: 메가박스 돌비 ──  (IMAX 결과와 무관하게 항상 실행)
    for th in MEGABOX_THEATERS:
        if not th.get("enabled", True):
            continue
        src = f"MEGABOX:{th['name']}"
        try:
            shows = check_megabox(th, dates)
            found += shows
            fail_counter[src] = 0
            log(f" [돌비] {th['name']}: 조건 맞는 회차 {len(shows)}개")
        except Exception as e:  # noqa: BLE001
            fail_counter[src] = fail_counter.get(src, 0) + 1
            log(f" [돌비] {th['name']}: 조회 실패 → {type(e).__name__}: {e}")

    # ── 3: CGV 프라이빗 박스 ──  (위 결과와 무관하게 항상 실행)
    for th in CGV_PRIVATE_THEATERS:
        if not th.get("enabled", True):
            continue
        src = f"CGV-PRIVATE:{th['name']}"
        try:
            shows = check_cgv(th, dates, "PRIVATE")
            found += shows
            fail_counter[src] = 0
            log(f" [프라이빗] {th['name']}: 조건 맞는 회차 {len(shows)}개")
        except Exception as e:  # noqa: BLE001
            fail_counter[src] = fail_counter.get(src, 0) + 1
            log(f" [프라이빗] {th['name']}: 조회 실패 → {type(e).__name__}: {e}")
        time.sleep(REQUEST_DELAY)

    # ── 새 회차만 골라 알림 ──
    sent = state["sent"]
    new = [s for s in found if s.key not in sent]
    if not new:
        log(" 새로 열린 회차 없음")
        return
    new_keys = {s.key for s in new}

    # [포맷+지점+날짜] 단위로 묶어서 메시지 1개씩 (IMAX 먼저)
    order = {"IMAX": 0, "DBC": 1, "DVA": 2, "PRIVATE": 3}
    groups: dict[tuple, list[Show]] = {}
    for s in found:
        groups.setdefault((s.fmt, s.theater, s.date), []).append(s)
    for gk in sorted(groups, key=lambda k: (order.get(k[0], 9), k[2], k[1])):
        group = groups[gk]
        if not any(s.key in new_keys for s in group):
            continue
        if send_telegram(build_message(group, new_keys)):
            now = datetime.now(KST).isoformat(timespec="seconds")
            for s in group:
                if s.key in new_keys:
                    sent[s.key] = now
            save_state(state)           # 보낼 때마다 즉시 저장 (도중에 죽어도 중복 최소화)
            log(f" 📨 알림 전송: {gk}")


def alert_failures(state: dict, fail_counter: dict, checks: int) -> None:
    """이번 실행의 모든 체크에서 계속 실패한 소스가 있으면 하루 1번만 경고."""
    if checks == 0:
        return
    today = datetime.now(KST).date().isoformat()
    broken = [src for src, n in fail_counter.items() if n >= checks]
    for src in broken:
        k = f"{src}|{today}"
        if k in state["alerts"]:
            continue
        if send_telegram(f"⚠️ <b>조회 오류</b>\n{esc(src)} 조회가 계속 실패하고 있어요.\n"
                         f"GitHub Actions 로그를 확인해 주세요. (다른 지점 감시는 계속 진행 중)"):
            state["alerts"][k] = datetime.now(KST).isoformat(timespec="seconds")
            save_state(state)


def main() -> int:
    if PING:
        ok = send_telegram("✅ <b>텔레그램 연결 테스트 성공!</b>\n둠스데이 예매 알림 봇이 메시지를 보낼 수 있어요.")
        return 0 if ok else 1

    if not target_dates():
        log(f"감시 기간({DATE_START}~{DATE_END})이 지났습니다. 워크플로를 꺼 주세요.")
        return 0

    log(f"감시 시작: {MOVIE_KEYWORDS} / {DATE_START}~{DATE_END} / {MIN_START_TIME} 이후"
        f"{' / DRY_RUN' if DRY_RUN else ''}{' / NO_STATE' if NO_STATE else ''}")
    state = load_state()
    fail_counter: dict[str, int] = {}
    started = time.monotonic()
    checks = 0
    while True:
        checks += 1
        log(f"── 체크 #{checks} ──")
        try:
            run_check(state, fail_counter)
        except Exception as e:  # noqa: BLE001  (예상 못한 에러도 다음 체크는 계속)
            log(f"체크 중 예외: {type(e).__name__}: {e}")
        elapsed = time.monotonic() - started
        if elapsed + CHECK_INTERVAL > LOOP_SECONDS:
            break
        time.sleep(CHECK_INTERVAL)

    alert_failures(state, fail_counter, checks)
    save_state(state)
    log("완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
