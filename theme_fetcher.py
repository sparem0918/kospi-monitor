# -*- coding: utf-8 -*-
"""
네이버 금융에서 종목별 '테마 + 섹터(업종)'를 best-effort로 수집해 캐시한다.

설계 원칙 (대차거래 잔고 모듈과 동일한 철학):
- 테마/섹터는 자주 바뀌지 않으므로 매 실행마다 긁지 않고 캐시(기본 7일) 사용.
- 1차: 모바일 API(JSON) -> 2차: 데스크톱 종목 페이지(HTML) 순으로 시도.
- 어떤 종목이 실패해도 전체 빌드는 멈추지 않는다(빈 값 또는 기존 캐시 유지).
- 네이버 HTML/JSON 구조가 바뀌면 셀렉터만 이 파일에서 조정하면 된다.

캐시 파일: data/theme_cache.json
  { "000660": {"sector": "반도체", "themes": ["AI","HBM"], "ts": 1700000000.0}, ... }
"""
import json
import time
from datetime import datetime
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:  # 빌드 단계에서 import 실패해도 죽지 않게
    requests = None
    BeautifulSoup = None

# ===== 설정 =====
CACHE_TTL_DAYS = 7          # 이 기간이 지난 항목만 다시 받는다
MAX_THEMES = 3              # 종목당 최대 테마 개수
THROTTLE_SEC = 0.3         # 종목 간 호출 간격(네이버 부하/차단 방지)
TIMEOUT = 10

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://finance.naver.com/",
}
_JSON_HEADERS = dict(_HEADERS)
_JSON_HEADERS["Accept"] = "application/json"


def _now_ts():
    return datetime.now().timestamp()


def _load_cache(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(path: Path, cache: dict):
    try:
        path.write_text(json.dumps(cache, ensure_ascii=False, indent=0),
                        encoding="utf-8")
    except Exception as e:
        print(f"[!] theme_cache 저장 실패: {e}")


def _clean(s):
    return (s or "").replace("\xa0", " ").strip()


# ===== 1차: 모바일 통합 API (JSON) =====
def _walk_for_keys(obj, key_substrings):
    """중첩 dict/list를 순회하며 key에 특정 문자열이 포함된 값을 모은다."""
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if any(sub in kl for sub in key_substrings):
                found.append(v)
            found.extend(_walk_for_keys(v, key_substrings))
    elif isinstance(obj, list):
        for it in obj:
            found.extend(_walk_for_keys(it, key_substrings))
    return found


def _fetch_mobile(code):
    """m.stock.naver.com 통합 API. 구조가 다양해 방어적으로 파싱한다."""
    url = f"https://m.stock.naver.com/api/stock/{code}/integration"
    r = requests.get(url, headers=_JSON_HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()

    # 섹터(업종): 'industryGroupKor' 같은 key 우선
    sector = ""
    for v in _walk_for_keys(data, ("industrygroupkor", "industryname", "upjong")):
        if isinstance(v, str) and v.strip():
            sector = _clean(v)
            break
    if not sector:
        for v in _walk_for_keys(data, ("industry",)):
            if isinstance(v, str) and v.strip():
                sector = _clean(v)
                break

    # 테마: 'theme' 가 들어간 리스트에서 name 류 추출
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
    # 중복 제거(순서 유지)
    themes = list(dict.fromkeys([t for t in themes if t]))
    return sector, themes


# ===== 2차: 데스크톱 종목 페이지 (HTML) =====
def _fetch_desktop(code):
    url = f"https://finance.naver.com/item/main.naver?code={code}"
    r = requests.get(url, headers=_HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = "euc-kr"
    soup = BeautifulSoup(r.text, "html.parser")

    sector = ""
    themes = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        txt = _clean(a.get_text())
        if not txt:
            continue
        # 업종: type=upjong
        if "sise_group_detail" in href and "type=upjong" in href and not sector:
            sector = txt
        # 테마: type=theme
        elif "sise_group_detail" in href and "type=theme" in href:
            themes.append(txt)
    themes = list(dict.fromkeys([t for t in themes if t]))
    return sector, themes


def get_one(code, cache):
    """캐시 우선. 만료/없음일 때만 네트워크 시도. 실패 시 기존 값 유지."""
    code = str(code).zfill(6)
    entry = cache.get(code)
    if entry and (_now_ts() - entry.get("ts", 0) < CACHE_TTL_DAYS * 86400):
        return entry
    if requests is None:
        return entry or {"sector": "", "themes": [], "ts": 0}

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

    # 완전 실패면 기존(오래된) 캐시라도 유지
    if not sector and not themes and entry:
        return entry

    entry = {"sector": sector, "themes": themes[:MAX_THEMES], "ts": _now_ts()}
    cache[code] = entry
    return entry


def enrich(tickers, cache_path: Path, progress=None):
    """
    tickers(6자리 문자열 리스트)에 대해 테마/섹터 맵을 만든다.
    반환: {ticker: {"sector":..., "themes":[...]}}
    """
    cache = _load_cache(cache_path)
    fetched = 0
    total = len(tickers)
    for i, code in enumerate(tickers, 1):
        code = str(code).zfill(6)
        before = cache.get(code, {}).get("ts", 0)
        get_one(code, cache)
        after = cache.get(code, {}).get("ts", 0)
        if after != before:
            fetched += 1
            time.sleep(THROTTLE_SEC)   # 새로 받은 종목만 throttle
        if progress:
            progress(i, total, code)
    _save_cache(cache_path, cache)
    print(f"[+] 테마/섹터: 캐시 {total - fetched}건 재사용 / 신규 {fetched}건 수집")
    return {c: {"sector": cache.get(c, {}).get("sector", ""),
                "themes": cache.get(c, {}).get("themes", [])}
            for c in (str(t).zfill(6) for t in tickers)}
