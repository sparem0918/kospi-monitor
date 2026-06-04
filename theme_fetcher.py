# -*- coding: utf-8 -*-
"""
네이버 금융 종목 페이지에서 '업종(섹터) + 테마'를 best-effort로 수집·캐시한다.

v2 변경(숫자 표기 버그 수정):
- 모바일 통합 API는 테마 자리에 숫자 코드를 반환해 제거. 데스크톱 종목 페이지의
  업종/테마 '링크 텍스트'(한글)만 사용한다.
- 강력 필터: 한글/영문자가 없는 값(숫자·기호만)은 절대 채택하지 않는다.
  -> 어떤 경우에도 "278" 같은 숫자가 칩으로 표시되지 않는다.
- 캐시 스키마 버전(SCHEMA)으로 구버전(숫자 포함) 캐시는 자동 무효화·재수집.
- 읽는 시점에도 한 번 더 필터링하므로, 이미 저장된 숫자는 화면에 나오지 않는다.

실행당 신규 네트워크 호출은 max_new로 제한(코스피+코스닥 합산). 빈 결과는 저장 안 함.
cache: data/theme_cache.json
  { "000660": {"sector":"반도체","themes":["AI","HBM"],"ts":...,"v":2}, ... }
"""
import re
import json
import time
from datetime import datetime
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    requests = None
    BeautifulSoup = None

SCHEMA = 2
CACHE_TTL_DAYS = 14
MAX_THEMES = 3
THROTTLE_SEC = 0.15
TIMEOUT = 5

_run_attempts = 0   # 한 프로세스(코스피+코스닥) 공유 신규호출 카운터

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://finance.naver.com/",
}

_HANGUL = re.compile(r"[가-힣]")


def reset_budget():
    global _run_attempts
    _run_attempts = 0


def _now_ts():
    return datetime.now().timestamp()


def _clean(s):
    return (s or "").replace("\xa0", " ").strip()


def _is_name(s):
    """업종/테마로 인정할 값인가. 숫자·기호만이면 거부, 한글/영문자 포함 + 길이 2~20."""
    s = _clean(s)
    if not (2 <= len(s) <= 20):
        return False
    stripped = re.sub(r"[\d\s,.\-+%()]", "", s)
    if not stripped:                       # 숫자/기호만 남으면 거부
        return False
    return bool(_HANGUL.search(s)) or any(c.isalpha() for c in s)


def _sanitize(sector, themes):
    sector = sector if _is_name(sector) else ""
    clean = []
    for t in (themes or []):
        t = _clean(t)
        if _is_name(t) and t not in clean and t != sector:
            clean.append(t)
    return sector, clean[:MAX_THEMES]


def _load_cache(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(path: Path, cache: dict):
    try:
        path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[!] theme_cache 저장 실패: {e}")


def _fetch_desktop(code):
    """데스크톱 종목 페이지에서 업종(type=upjong)·테마(type=theme) 링크 텍스트 추출."""
    url = f"https://finance.naver.com/item/main.naver?code={code}"
    r = requests.get(url, headers=_HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = "euc-kr"
    soup = BeautifulSoup(r.text, "html.parser")
    sector, themes = "", []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "sise_group_detail" not in href:
            continue
        txt = _clean(a.get_text())
        if "type=upjong" in href and not sector and _is_name(txt):
            sector = txt
        elif "type=theme" in href and _is_name(txt):
            themes.append(txt)
    return _sanitize(sector, themes)


def enrich(tickers, cache_path: Path, progress=None, max_new=0):
    global _run_attempts
    cache = _load_cache(cache_path)
    total = len(tickers)
    new_ok = 0
    for i, t in enumerate(tickers, 1):
        code = str(t).zfill(6)
        entry = cache.get(code)
        fresh = (entry and entry.get("v") == SCHEMA
                 and _now_ts() - entry.get("ts", 0) < CACHE_TTL_DAYS * 86400)
        budget_left = (max_new <= 0) or (_run_attempts < max_new)
        if not fresh and budget_left and requests is not None:
            _run_attempts += 1
            try:
                sector, themes = _fetch_desktop(code)
            except Exception:
                sector, themes = "", []
            if sector or themes:                       # 성공한 것만 저장
                cache[code] = {"sector": sector, "themes": themes,
                               "ts": _now_ts(), "v": SCHEMA}
                new_ok += 1
            time.sleep(THROTTLE_SEC)
        if progress:
            progress(i, total, code)
    _save_cache(cache_path, cache)
    print(f"[+] 테마/섹터: 신규 수집 {new_ok}건 (누적 시도 {_run_attempts}"
          + (f"/{max_new}" if max_new > 0 else "") + ")")

    # 읽는 시점에도 필터링 (구버전 캐시의 숫자 잔재가 화면에 나오지 않도록)
    out = {}
    for t in tickers:
        code = str(t).zfill(6)
        e = cache.get(code, {})
        sector, themes = _sanitize(e.get("sector", ""), e.get("themes", []))
        out[code] = {"sector": sector, "themes": themes}
    return out
