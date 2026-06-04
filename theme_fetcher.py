# -*- coding: utf-8 -*-
"""
네이버 금융에서 '업종(섹터) + 테마'를 best-effort로 수집·캐시한다.

v3 변경(한글 깨짐/숫자 표기 수정):
- 인코딩 자동 보정: 페이지 바이트를 utf-8/cp949로 디코딩해 '한글이 가장 많은' 쪽 채택.
- 강력 필터 _is_name: 다음은 전부 거부 → 어떤 경우에도 화면에 깨진 값/숫자가 안 나온다.
    · 한자(U+3400~U+9FFF) 포함  (예: "釩泥덕" 같은 깨진 텍스트)
    · ◆ ◇ ■ □ ● ○ 같은 치환문자 포함
    · 숫자·기호만 (예: "278", "+6.11%")
  통과 조건: 한글 또는 영문자를 포함하는 2~20자.
- 데이터 소스: 모바일 JSON(구조적, 우선) → 데스크톱 페이지(보조).
- 캐시 스키마 SCHEMA로 구버전(숫자/깨짐) 캐시 자동 무효화. 읽는 시점에도 재필터.

실행당 신규 호출은 max_new로 제한(코스피+코스닥 합산). 빈 결과는 저장 안 함.
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

SCHEMA = 3
CACHE_TTL_DAYS = 14
MAX_THEMES = 3
THROTTLE_SEC = 0.15
TIMEOUT = 5

_run_attempts = 0

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://finance.naver.com/",
}
_JSON_HEADERS = dict(_HEADERS, **{"Accept": "application/json"})

_HANGUL = re.compile(r"[가-힣]")
_CJK = re.compile(r"[\u3400-\u9fff]")          # 한자 = 깨진 텍스트 신호
_BAD_CHARS = "◆◇■□●○◎▶◀�"


def reset_budget():
    global _run_attempts
    _run_attempts = 0


def _now_ts():
    return datetime.now().timestamp()


def _clean(s):
    return (s or "").replace("\xa0", " ").strip()


def _is_name(s):
    """업종/테마로 인정할 깨끗한 한글/영문 라벨인가."""
    s = _clean(s)
    if not (2 <= len(s) <= 20):
        return False
    if any(c in s for c in _BAD_CHARS):
        return False
    if _CJK.search(s):                         # 한자 포함 -> 깨짐으로 간주, 거부
        return False
    if not re.sub(r"[\d\s,.\-+%()/]", "", s):  # 숫자/기호만 -> 거부
        return False
    return bool(_HANGUL.search(s)) or any("a" <= c.lower() <= "z" for c in s)


def _sanitize(sector, themes):
    sector = sector if _is_name(sector) else ""
    clean = []
    for t in (themes or []):
        t = _clean(t)
        if _is_name(t) and t not in clean and t != sector:
            clean.append(t)
    return sector, clean[:MAX_THEMES]


def _decode_best(content):
    """utf-8 / cp949 중 한글이 가장 많이 나오는 디코딩을 채택."""
    best, best_score = "", -1
    for enc in ("utf-8", "cp949"):
        try:
            s = content.decode(enc, errors="replace")
        except Exception:
            continue
        score = sum(1 for ch in s if "가" <= ch <= "힣")
        if score > best_score:
            best, best_score = s, score
    return best


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
    """모바일 통합 API(JSON). 업종은 industry* 한글 필드만, 테마는 name 필드만 채택."""
    url = f"https://m.stock.naver.com/api/stock/{code}/integration"
    r = requests.get(url, headers=_JSON_HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()

    sector = ""
    for v in _walk_for_keys(data, ("industrygroupkor", "industrykorname", "industryname")):
        if isinstance(v, str) and _is_name(v) and _HANGUL.search(v):
            sector = _clean(v); break
    if not sector:
        for v in _walk_for_keys(data, ("industrycodetype", "industry")):
            if isinstance(v, dict):
                for kk in ("industryGroupKor", "industryKorName", "name", "nameKor", "korName"):
                    val = v.get(kk)
                    if isinstance(val, str) and _is_name(val) and _HANGUL.search(val):
                        sector = _clean(val); break
            if sector:
                break

    themes = []
    for v in _walk_for_keys(data, ("theme",)):
        items = v if isinstance(v, list) else [v]
        for it in items:
            nm = None
            if isinstance(it, dict):
                nm = (it.get("themeName") or it.get("name")
                      or it.get("nameKor") or it.get("text"))
            elif isinstance(it, str):
                nm = it
            if isinstance(nm, str) and _is_name(nm):
                themes.append(_clean(nm))
    return _sanitize(sector, themes)


def _fetch_desktop(code):
    """데스크톱 종목 페이지(인코딩 보정). 업종/테마 링크 텍스트만."""
    url = f"https://finance.naver.com/item/main.naver?code={code}"
    r = requests.get(url, headers=_HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(_decode_best(r.content), "html.parser")
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


def _do_fetch(code):
    sector, themes = "", []
    for fetcher in (_fetch_mobile, _fetch_desktop):
        try:
            s, t = fetcher(code)
        except Exception:
            continue
        sector = sector or s
        if t and not themes:
            themes = t
        if sector and themes:
            break
    return _sanitize(sector, themes)


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
            sector, themes = _do_fetch(code)
            if sector or themes:
                cache[code] = {"sector": sector, "themes": themes,
                               "ts": _now_ts(), "v": SCHEMA}
                new_ok += 1
            time.sleep(THROTTLE_SEC)
        if progress:
            progress(i, total, code)
    _save_cache(cache_path, cache)
    print(f"[+] 테마/섹터: 신규 수집 {new_ok}건 (누적 시도 {_run_attempts}"
          + (f"/{max_new}" if max_new > 0 else "") + ")")

    out = {}
    for t in tickers:
        code = str(t).zfill(6)
        e = cache.get(code, {})
        sector, themes = _sanitize(e.get("sector", ""), e.get("themes", []))
        out[code] = {"sector": sector, "themes": themes}
    return out
