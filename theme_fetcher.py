# -*- coding: utf-8 -*-
"""
네이버 금융에서 종목별 '테마 + 섹터(업종)'를 best-effort로 수집해 캐시한다.

핵심 설계(타임아웃 방지):
- 실행당 '신규 네트워크 호출 수'를 max_new로 제한(예산제). 한 번에 다 받지 않는다.
- 예산은 한 프로세스(코스피+코스닥) 전체에서 공유된다.
- 빈 결과는 캐시에 저장하지 않는다 -> 다음 실행에서 다시 시도(7일 묶임 방지).
- 짧은 타임아웃(모바일 4s, 데스크톱 5s)으로 느린 응답에 묶이지 않는다.
- 캐시는 data/theme_cache.json. 갱신 주기 CACHE_TTL_DAYS.

cache: { "000660": {"sector":"반도체","themes":["AI","HBM"],"ts":...}, ... }
"""
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

CACHE_TTL_DAYS = 14         # 테마/섹터는 자주 안 바뀐다
MAX_THEMES = 3
THROTTLE_SEC = 0.15
TIMEOUT_MOBILE = 4
TIMEOUT_DESKTOP = 5

# 한 프로세스(코스피+코스닥) 동안 공유되는 신규 호출 카운터
_run_attempts = 0

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://finance.naver.com/",
}
_JSON_HEADERS = dict(_HEADERS, **{"Accept": "application/json"})


def reset_budget():
    global _run_attempts
    _run_attempts = 0


def _now_ts():
    return datetime.now().timestamp()


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


def _clean(s):
    return (s or "").replace("\xa0", " ").strip()


def _walk_for_keys(obj, key_substrings):
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if any(sub in str(k).lower() for sub in key_substrings):
                found.append(v)
            found.extend(_walk_for_keys(v, key_substrings))
    elif isinstance(obj, list):
        for it in obj:
            found.extend(_walk_for_keys(it, key_substrings))
    return found


def _fetch_mobile(code):
    url = f"https://m.stock.naver.com/api/stock/{code}/integration"
    r = requests.get(url, headers=_JSON_HEADERS, timeout=TIMEOUT_MOBILE)
    r.raise_for_status()
    data = r.json()
    sector = ""
    for v in _walk_for_keys(data, ("industrygroupkor", "industryname", "upjong")):
        if isinstance(v, str) and v.strip():
            sector = _clean(v); break
    if not sector:
        for v in _walk_for_keys(data, ("industry",)):
            if isinstance(v, str) and v.strip():
                sector = _clean(v); break
    themes = []
    for v in _walk_for_keys(data, ("theme",)):
        if isinstance(v, list):
            for it in v:
                if isinstance(it, dict):
                    nm = it.get("themeName") or it.get("name") or it.get("nameKor")
                    if nm:
                        themes.append(_clean(nm))
                elif isinstance(it, str):
                    themes.append(_clean(it))
        elif isinstance(v, str) and v.strip():
            themes.append(_clean(v))
    themes = list(dict.fromkeys([t for t in themes if t]))
    return sector, themes


def _fetch_desktop(code):
    url = f"https://finance.naver.com/item/main.naver?code={code}"
    r = requests.get(url, headers=_HEADERS, timeout=TIMEOUT_DESKTOP)
    r.raise_for_status()
    r.encoding = "euc-kr"
    soup = BeautifulSoup(r.text, "html.parser")
    sector, themes = "", []
    for a in soup.find_all("a", href=True):
        href, txt = a["href"], _clean(a.get_text())
        if not txt:
            continue
        if "sise_group_detail" in href and "type=upjong" in href and not sector:
            sector = txt
        elif "sise_group_detail" in href and "type=theme" in href:
            themes.append(txt)
    themes = list(dict.fromkeys([t for t in themes if t]))
    return sector, themes


def _do_fetch(code):
    """네트워크 시도 1회분. (sector, themes) 반환, 실패 시 ('', [])."""
    sector, themes = "", []
    for fetcher in (_fetch_mobile, _fetch_desktop):
        try:
            s, t = fetcher(code)
            sector = sector or s
            if t and not themes:
                themes = t
            if sector and themes:
                break
        except Exception:
            continue
    return sector, themes[:MAX_THEMES]


def enrich(tickers, cache_path: Path, progress=None, max_new=0):
    """
    tickers의 테마/섹터 맵을 만든다.
    max_new>0 이면 이 프로세스 전체에서 신규 네트워크 시도를 max_new회로 제한.
    빈 결과는 저장하지 않아 다음 실행에서 재시도된다.
    """
    global _run_attempts
    cache = _load_cache(cache_path)
    total = len(tickers)
    new_ok = 0
    for i, t in enumerate(tickers, 1):
        code = str(t).zfill(6)
        entry = cache.get(code)
        fresh = entry and (_now_ts() - entry.get("ts", 0) < CACHE_TTL_DAYS * 86400)
        budget_left = (max_new <= 0) or (_run_attempts < max_new)
        if not fresh and budget_left and requests is not None:
            _run_attempts += 1
            sector, themes = _do_fetch(code)
            if sector or themes:           # 성공한 것만 캐시
                cache[code] = {"sector": sector, "themes": themes, "ts": _now_ts()}
                new_ok += 1
            time.sleep(THROTTLE_SEC)
        if progress:
            progress(i, total, code)
    _save_cache(cache_path, cache)
    print(f"[+] 테마/섹터: 신규 수집 {new_ok}건 (누적 시도 {_run_attempts}"
          + (f"/{max_new}" if max_new > 0 else "") + ")")
    return {str(t).zfill(6): {
                "sector": cache.get(str(t).zfill(6), {}).get("sector", ""),
                "themes": cache.get(str(t).zfill(6), {}).get("themes", [])}
            for t in tickers}
