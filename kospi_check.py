# -*- coding: utf-8 -*-
import os, sys, builtins
# GitHub Actions 등 비대화형 환경에서는 input() 즉시 빈 문자열 반환
if os.environ.get("CI") or not sys.stdin.isatty():
    builtins.input = lambda prompt="": ""
"""
KOSPI / KOSDAQ 일일 모니터링 v4.0
- 데이터: 네이버 금융(시총 상위) + DART(optional) + 네이버 테마/섹터(optional)
- 시장: 코스피 + 코스닥 (각 시총 상위 200)
- 신규: 종목명 옆 테마/섹터 표기, 기준봉(전일 종가 대비 등락률 +6% 이상) 붉은색 강조
- 출력 폴더: docs/ (GitHub Pages 호환), index.html + 날짜별 리포트
"""
import sys
import time
import json
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    import pandas as pd
    import requests
    from bs4 import BeautifulSoup
except ImportError as e:
    print(f"[!] Required package not installed: {e}")
    input("\nPress Enter to exit...")
    sys.exit(1)

from tier_mapping import BUFFETT_TIER, TIER_COLORS, TIER_LABELS

try:
    import dart_scorer
    DART_AVAILABLE = True
except ImportError:
    DART_AVAILABLE = False

try:
    import theme_fetcher
    THEME_AVAILABLE = True
except ImportError:
    THEME_AVAILABLE = False

SCRIPT_DIR = Path(__file__).parent.resolve()
DATA_DIR = SCRIPT_DIR / "data"
DOCS_DIR = SCRIPT_DIR / "docs"            # GitHub Pages 호스팅 폴더
REPORTS_DIR = DOCS_DIR / "reports"        # 날짜별 리포트
DATA_DIR.mkdir(exist_ok=True)
DOCS_DIR.mkdir(exist_ok=True)
REPORTS_DIR.mkdir(exist_ok=True)
THEME_CACHE = DATA_DIR / "theme_cache.json"

TOP_N = 200
# (시장키, 네이버 sosok 코드, 한글 라벨)
MARKETS = [("KOSPI", 0, "코스피"), ("KOSDAQ", 1, "코스닥")]
ANCHOR_PCT = 6.0          # 기준봉: 전일 종가 대비 등락률 +6% 이상
PAGES_PER_MARKET = 5      # 페이지당 50종목 -> 250 수집 후 상위 200
DEMO = os.environ.get("KM_DEMO") == "1"   # 네트워크 없이 샘플로 렌더 검증

# 실행당 신규 호출 예산 (0 = 무제한, 로컬 기본). CI에서는 워크플로가 env로 지정.
THEME_MAX_NEW = int(os.environ.get("THEME_MAX_NEW", "0") or "0")
DART_MAX_NEW = int(os.environ.get("DART_MAX_NEW", "0") or "0")
_DART_NEW_USED = 0   # 코스피+코스닥 합산

NAVER_URL = "https://finance.naver.com/sise/sise_market_sum.naver"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://finance.naver.com/sise/",
}


# ===== 날짜 =====
def get_effective_date():
    now = datetime.now()
    target = now - timedelta(days=1) if now.hour < 9 else now
    while target.weekday() >= 5:
        target -= timedelta(days=1)
    return target


def fmt(d):
    return d.strftime("%Y%m%d")


# ===== 네이버 금융 파싱 =====
def parse_num(text, default=0.0):
    s = (text or "").replace(",", "").replace("%", "").replace("\xa0", " ").strip()
    if not s or s in ("N/A", "-", ""):
        return default
    try:
        return float(s)
    except ValueError:
        try:
            return float(s.lstrip("+-"))
        except ValueError:
            return default


def parse_change_rate(td):
    text = td.get_text(strip=True)
    cleaned = text.replace(",", "").replace("%", "").strip()
    if not cleaned or cleaned in ("N/A", "-"):
        return 0.0
    try:
        return float(cleaned)
    except ValueError:
        pass
    try:
        value = float(cleaned.lstrip("+-"))
    except ValueError:
        return 0.0
    all_signals = " ".join(td.get("class", []) or []) + " " + str(td)
    if any(k in all_signals for k in ["nv01", "down", "minus", "blue"]):
        return -abs(value)
    return value


def fetch_naver_page(sosok, page):
    resp = requests.get(NAVER_URL, params={"sosok": sosok, "page": page},
                        headers=HEADERS, timeout=20)
    resp.raise_for_status()
    resp.encoding = "euc-kr"
    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", class_="type_2")
    if table is None:
        return []
    rows = []
    for tr in table.find_all("tr"):
        title_link = tr.find("a", class_="tltle")
        if not title_link:
            continue
        tds = tr.find_all("td")
        if len(tds) < 12:
            continue
        href = title_link.get("href", "")
        if "code=" not in href:
            continue
        code = href.split("code=")[1][:6]
        name = title_link.get_text(strip=True)
        price = parse_num(tds[2].get_text())
        change_rate = parse_change_rate(tds[4])
        cap_eok = parse_num(tds[6].get_text())
        market_cap = cap_eok * 1e8
        shares_k = parse_num(tds[7].get_text())
        foreign_ratio = parse_num(tds[8].get_text())
        volume = parse_num(tds[9].get_text())
        per = parse_num(tds[10].get_text(), default=None)
        roe = parse_num(tds[11].get_text(), default=None)
        rows.append({
            "티커": code, "종목명": name,
            "종가": int(price), "등락률": change_rate,
            "시가총액": market_cap, "거래량": int(volume),
            "거래대금": int(price * volume),
            "상장주식수": int(shares_k * 1000) if shares_k else 0,
            "외국인비율": foreign_ratio,
            "PER": per, "ROE": roe,
        })
    return rows


def fetch_naver_market_data(sosok, market_key):
    label = dict((k, l) for k, _, l in MARKETS).get(market_key, market_key)
    print(f"[*] {label} 네이버 금융 수집...")
    all_rows = []
    for page in range(1, PAGES_PER_MARKET + 1):
        try:
            rows = fetch_naver_page(sosok, page)
            print(f"    [{label}] page {page}: {len(rows)} rows")
            all_rows.extend(rows)
        except requests.RequestException as e:
            print(f"    [!] {label} page {page} 실패: {e}")
        time.sleep(0.4)
    if not all_rows:
        return None
    df = pd.DataFrame(all_rows)
    df = df.drop_duplicates(subset="티커", keep="first")
    df = df.sort_values("시가총액", ascending=False).head(TOP_N).reset_index(drop=True)
    df["순위"] = range(1, len(df) + 1)
    df["시장"] = market_key
    df["티어"] = df["티커"].map(lambda x: BUFFETT_TIER.get(x, "?"))
    return df


# ===== DART 자동 점수 =====
def _dart_split(tickers):
    """fin_cache 기준으로 (이미 받은 티커, 신규 티커) 분리. 신규만 예산 적용."""
    try:
        corp_map = dart_scorer.load_corp_codes()
    except Exception:
        corp_map = {}
    yr = datetime.now().year
    cache_ver = getattr(dart_scorer, "CACHE_VERSION", None)
    cache_dir = getattr(dart_scorer, "CACHE_DIR", None)
    warm, cold = [], []
    for t in tickers:
        t6 = str(t).zfill(6)
        info = corp_map.get(t6)
        cc = info.get("corp_code") if isinstance(info, dict) else None
        ok = False
        if cc and cache_dir:
            for y in (yr - 1, yr - 2, yr - 3):
                f = cache_dir / f"{cc}_{y}.json"
                if f.exists():
                    try:
                        if json.loads(f.read_text(encoding="utf-8")).get("_v") == cache_ver:
                            ok = True
                            break
                    except Exception:
                        pass
        (warm if ok else cold).append(t)
    return warm, cold


def add_dart_scores(df, label=""):
    if not DART_AVAILABLE:
        df["자동점수"] = None
        df["점수상세"] = None
        return df, None
    api_key = dart_scorer.get_api_key()
    if not api_key:
        print("[*] DART API key 없음 — 자동 점수 스킵.")
        df["자동점수"] = None
        df["점수상세"] = None
        return df, "no_api_key"

    print(f"[*] {label} DART 자동 점수 계산 중... (캐시 사용)")
    all_tickers = df["티커"].tolist()
    per_map = dict(zip(df["티커"], df["PER"]))

    # 신규(콜드) 종목은 실행당 예산만큼만 채점하고 나머지는 다음 실행으로 이월
    global _DART_NEW_USED
    if DART_MAX_NEW > 0:
        warm, cold = _dart_split(all_tickers)
        remaining = max(0, DART_MAX_NEW - _DART_NEW_USED)
        take = cold[:remaining]
        _DART_NEW_USED += len(take)
        tickers = warm + take
        deferred = len(cold) - len(take)
        if deferred > 0:
            print(f"    [{label}] 캐시 {len(warm)} + 신규 {len(take)} 채점 / "
                  f"{deferred}개는 다음 실행으로 이월(예산 {DART_MAX_NEW})")
    else:
        tickers = all_tickers

    last_progress = [time.time()]

    def progress(i, total, ticker):
        now = time.time()
        if now - last_progress[0] >= 5 or i == total:
            print(f"    [{label} {i}/{total}] {ticker}...", flush=True)
            last_progress[0] = now

    try:
        scores = dart_scorer.score_tickers(tickers, per_map=per_map,
                                            progress_callback=progress)
    except Exception as e:
        print(f"[!] DART 점수 계산 실패: {e}")
        df["자동점수"] = None
        df["점수상세"] = None
        return df, f"error: {e}"

    if scores is None:
        df["자동점수"] = None
        df["점수상세"] = None
        return df, "scoring_failed"

    df["자동점수"] = df["티커"].map(
        lambda t: scores.get(t, {}).get("total") if scores.get(t) else None)
    df["점수상세"] = df["티커"].map(
        lambda t: scores.get(t, {}).get("breakdown") if scores.get(t) else None)
    scored = df["자동점수"].notna().sum()
    print(f"[+] {label} 자동 점수 완료: {scored}/{len(df)}")
    return df, "ok"


# ===== 테마/섹터 =====
def add_themes(df):
    """네이버 테마/섹터를 캐시 기반으로 붙인다. 실패해도 빈 값으로 진행."""
    df["테마"] = [[] for _ in range(len(df))]
    df["섹터"] = ""
    if not THEME_AVAILABLE:
        return df
    try:
        last = [time.time()]

        def progress(i, total, code):
            if time.time() - last[0] >= 5 or i == total:
                print(f"    [테마 {i}/{total}] {code}...", flush=True)
                last[0] = time.time()

        tmap = theme_fetcher.enrich(df["티커"].tolist(), THEME_CACHE,
                                    progress, max_new=THEME_MAX_NEW)
        df["테마"] = df["티커"].map(lambda t: tmap.get(str(t).zfill(6), {}).get("themes", []))
        df["섹터"] = df["티커"].map(lambda t: tmap.get(str(t).zfill(6), {}).get("sector", ""))
    except Exception as e:
        print(f"[!] 테마/섹터 수집 실패(무시하고 진행): {e}")
    return df


# ===== 비교 =====
def compare_with_previous(today_df, prev_df):
    today_set = set(today_df["티커"])
    prev_tickers = prev_df["티커"].astype(str).str.zfill(6)
    prev_set = set(prev_tickers)
    new_in = today_set - prev_set
    new_out = prev_set - today_set
    in_rows = today_df[today_df["티커"].isin(new_in)].copy()
    prev_df = prev_df.copy()
    prev_df["티커"] = prev_tickers
    out_rows = prev_df[prev_df["티커"].isin(new_out)].copy()
    if "티어" not in out_rows.columns:
        out_rows["티어"] = out_rows["티커"].map(lambda x: BUFFETT_TIER.get(x, "?"))
    prev_rank_map = dict(zip(prev_df["티커"], prev_df["순위"]))
    today_df["순위변동"] = today_df.apply(
        lambda r: (prev_rank_map[r["티커"]] - r["순위"])
        if r["티커"] in prev_rank_map else None, axis=1)
    return in_rows, out_rows, today_df


def find_snapshot_closest_to(market_key, target_date):
    snaps = sorted(DATA_DIR.glob(f"snapshot_{market_key}_*.csv"), reverse=True)
    for snap in snaps:
        try:
            d = datetime.strptime(snap.stem.split("_")[-1], "%Y%m%d")
            if d <= target_date:
                return snap
        except Exception:
            continue
    return None


def compute_period_changes(today_df, period_file):
    if period_file is None or not period_file.exists():
        return None
    try:
        period_df = pd.read_csv(period_file, dtype={"티커": str})
        period_df["티커"] = period_df["티커"].str.zfill(6)
    except Exception:
        return None
    today_set = set(today_df["티커"])
    period_set = set(period_df["티커"])
    return {
        "in_count": len(today_set - period_set),
        "out_count": len(period_set - today_set),
        "period_label": period_file.stem.split("_")[-1],
    }


# ===== HTML 헬퍼 =====
def tier_class(t):
    return t if t in ("A", "B", "C", "D") else "_"


def score_class(s):
    if s is None or pd.isna(s):
        return "score-none"
    if s >= 80: return "score-a"
    if s >= 65: return "score-b"
    if s >= 50: return "score-c"
    return "score-d"


def fmt_per(v):
    if v is None or pd.isna(v):
        return '<span style="color:#64748b">-</span>'
    if v <= 0:
        return '<span style="color:#ef4444">적자</span>'
    return f"{v:.1f}"


def fmt_roe(v):
    if v is None or pd.isna(v):
        return '<span style="color:#64748b">-</span>'
    cls = ""
    if v >= 15: cls = ' style="color:#10b981;font-weight:600"'
    elif v >= 10: cls = ' style="color:#22c55e"'
    elif v < 0: cls = ' style="color:#ef4444"'
    return f'<span{cls}>{v:.2f}%</span>'


def fmt_score(s):
    if s is None or pd.isna(s):
        return '<span style="color:#64748b">-</span>'
    return f'<span class="score-badge {score_class(s)}">{int(s)}</span>'


def render_score_tooltip(breakdown):
    if not isinstance(breakdown, dict):
        return ""
    return " | ".join(f"{k}: {v}" for k, v in breakdown.items())


def render_tags(r):
    """종목명 옆 테마/섹터 칩. 테마 우선, 빈자리에 섹터 보조 표기."""
    themes = r.get("테마")
    if not isinstance(themes, (list, tuple)):
        themes = []
    sector = r.get("섹터") or ""
    chips = [str(t) for t in themes if t][:3]
    parts = [f'<span class="tag">{t}</span>' for t in chips]
    if sector and sector not in chips and len(parts) < 3:
        parts.append(f'<span class="tag tag-sector">{sector}</span>')
    return ('<span class="tags">' + "".join(parts) + "</span>") if parts else ""


def is_anchor(chg):
    try:
        return float(chg) >= ANCHOR_PCT
    except (TypeError, ValueError):
        return False


def name_cell(r, show_tags=True):
    fire = ' <span class="fire" title="기준봉(+6%↑)">🔥</span>' if is_anchor(r.get("등락률", 0)) else ""
    tags = render_tags(r) if show_tags else ""
    return f'<b>{r["종목명"]}</b>{fire}{tags}'


def render_in_out_table(rows, kind):
    if rows is None or len(rows) == 0:
        return '<div class="empty">없음</div>'
    badge = (f'<span class="{kind}-badge">'
             f'{"IN" if kind == "in" else "OUT"}</span>')
    headers = ["구분", "티커", "종목명", "티어"]
    if kind == "in":
        headers += ["순위", "등락률", "PER", "ROE", "점수"]
    th = "".join(f"<th>{h}</th>" for h in headers)
    body = []
    for _, r in rows.iterrows():
        tier = r.get("티어", "?") or "?"
        cells = [
            badge, r["티커"], name_cell(r, show_tags=False),
            f'<span class="tier tier-{tier_class(tier)}">{tier}</span>',
        ]
        if kind == "in":
            chg = float(r.get("등락률", 0) or 0)
            cls = "positive" if chg > 0 else "negative" if chg < 0 else ""
            cells += [
                str(r.get("순위", "-")),
                f'<span class="{cls}">{chg:+.2f}%</span>',
                fmt_per(r.get("PER")),
                fmt_roe(r.get("ROE")),
                fmt_score(r.get("자동점수")),
            ]
        body.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
    return f"<table><thead><tr>{th}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def render_change_table(rows):
    body = []
    for _, r in rows.iterrows():
        tier = r.get("티어", "?") or "?"
        chg = float(r["등락률"])
        cls = "positive" if chg > 0 else "negative" if chg < 0 else ""
        anchor = " anchor" if is_anchor(chg) else ""
        body.append(
            f'<tr class="{anchor.strip()}"><td>{r["순위"]}</td><td>{name_cell(r, show_tags=False)}</td>'
            f'<td><span class="tier tier-{tier_class(tier)}">{tier}</span></td>'
            f'<td class="{cls}">{chg:+.2f}%</td>'
            f'<td>{int(r["거래량"]):,}</td></tr>'
        )
    return ('<table><thead><tr><th>순위</th><th>종목명</th>'
            '<th>티어</th><th>등락률</th><th>거래량</th></tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table>')


def render_anchor_table(df):
    rows = df[df["등락률"].apply(is_anchor)].sort_values("등락률", ascending=False)
    if rows.empty:
        return '<div class="empty">오늘 기준 +6% 이상 종목 없음</div>'
    body = []
    for _, r in rows.iterrows():
        tier = r.get("티어", "?") or "?"
        chg = float(r["등락률"])
        body.append(
            f'<tr class="anchor"><td>{r["순위"]}</td><td>{r["티커"]}</td>'
            f'<td class="name-col">{name_cell(r)}</td>'
            f'<td><span class="tier tier-{tier_class(tier)}">{tier}</span></td>'
            f'<td>{int(r["종가"]):,}</td>'
            f'<td class="positive">{chg:+.2f}%</td>'
            f'<td>{int(r["거래량"]):,}</td></tr>'
        )
    return ('<table><thead><tr><th>순위</th><th>티커</th><th>종목명</th>'
            '<th>티어</th><th>종가</th><th>등락률</th><th>거래량</th>'
            f'</tr></thead><tbody>{"".join(body)}</tbody></table>')


def render_main_table(df):
    body = []
    for _, r in df.iterrows():
        tier = r.get("티어", "?") or "?"
        chg = float(r["등락률"])
        cls = "positive" if chg > 0 else "negative" if chg < 0 else ""
        anchor = " anchor" if is_anchor(chg) else ""
        rc = r.get("순위변동")
        if rc is None or pd.isna(rc):
            rc_html = '<span class="in-badge">NEW</span>'
        elif rc > 0:
            rc_html = f'<span class="rank-up">▲{int(rc)}</span>'
        elif rc < 0:
            rc_html = f'<span class="rank-down">▼{int(abs(rc))}</span>'
        else:
            rc_html = '<span style="color:#64748b">-</span>'
        cap_eok = r["시가총액"] / 1e8
        breakdown = r.get("점수상세")
        tooltip = render_score_tooltip(breakdown) if isinstance(breakdown, dict) else ""
        score_cell = fmt_score(r.get("자동점수"))
        if tooltip:
            score_cell = f'<span title="{tooltip}">{score_cell}</span>'
        body.append(
            f'<tr data-tier="{tier}" class="{anchor.strip()}">'
            f'<td>{r["순위"]}</td><td>{rc_html}</td>'
            f'<td>{r["티커"]}</td><td class="name-col">{name_cell(r)}</td>'
            f'<td><span class="tier tier-{tier_class(tier)}">{tier}</span></td>'
            f'<td>{int(r["종가"]):,}</td>'
            f'<td class="{cls}">{chg:+.2f}%</td>'
            f'<td>{cap_eok:,.0f}</td>'
            f'<td>{fmt_per(r.get("PER"))}</td>'
            f'<td>{fmt_roe(r.get("ROE"))}</td>'
            f'<td>{score_cell}</td></tr>'
        )
    return ('<table class="main-table"><thead><tr>'
            '<th>순위</th><th>변동</th><th>티커</th><th>종목명 · 테마/섹터</th><th>티어</th>'
            '<th>종가</th><th>등락률</th><th>시총(억)</th>'
            '<th>PER</th><th>ROE</th><th>점수</th>'
            f'</tr></thead><tbody>{"".join(body)}</tbody></table>')


def render_period_stats(week_stats, month_stats):
    parts = ['<div class="period-row">']
    for label, stats in [("주간 (7일 전 대비)", week_stats),
                          ("월간 (30일 전 대비)", month_stats)]:
        parts.append('<div class="period-card">')
        parts.append(f'<div class="period-label">{label}</div>')
        if stats is None:
            parts.append('<div class="empty">누적 데이터 부족</div>')
        else:
            parts.append('<div class="period-stats">')
            parts.append(
                f'<div><div class="period-num" style="color:#10b981">+{stats["in_count"]}</div>'
                f'<div class="period-sub">신규</div></div>'
                f'<div><div class="period-num" style="color:#ef4444">-{stats["out_count"]}</div>'
                f'<div class="period-sub">탈락</div></div>'
                f'<div><div class="period-num" style="font-size:12px;color:#cbd5e1">{stats["period_label"]}</div>'
                f'<div class="period-sub">기준일</div></div>'
            )
            parts.append('</div>')
        parts.append('</div>')
    parts.append('</div>')
    return "".join(parts)


def render_archive_links(past_reports, current_date):
    if not past_reports:
        return '<div class="empty">아직 누적된 과거 리포트가 없습니다.</div>'
    items = []
    for p in past_reports[:30]:
        d = p.stem.replace("report_", "")
        if d == current_date:
            continue
        pretty = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        items.append(f'<a class="archive-link" href="reports/{p.name}">{pretty}</a>')
    if not items:
        return '<div class="empty">아직 누적된 과거 리포트가 없습니다.</div>'
    return '<div class="archive-grid">' + "".join(items) + '</div>'


# ===== 시장별 본문 블록 =====
def render_market_block(payload, active):
    mk = payload["market_key"]
    label = payload["label"]
    df = payload["today_df"]
    in_rows, out_rows = payload["in_rows"], payload["out_rows"]
    week_stats, month_stats = payload["week_stats"], payload["month_stats"]
    dart_status = payload["dart_status"]

    tier_counts = df["티어"].value_counts().to_dict()
    top_gainers = df.nlargest(10, "등락률")
    top_losers = df.nsmallest(10, "등락률")
    anchor_n = int(df["등락률"].apply(is_anchor).sum())

    if "자동점수" in df.columns:
        scored = df["자동점수"].dropna()
        score_a = int((scored >= 80).sum())
        score_b = int(((scored >= 65) & (scored < 80)).sum())
        score_c = int(((scored >= 50) & (scored < 65)).sum())
        score_d = int((scored < 50).sum())
    else:
        score_a = score_b = score_c = score_d = 0

    style = "" if active else ' style="display:none"'
    return f"""
<div class="market-block" data-market="{mk}"{style}>

<div class="stats">
  <div class="stat-card"><div class="stat-label">종목수</div>
    <div class="stat-value">{len(df)}</div></div>
  <div class="stat-card anchor-card"><div class="stat-label">🔥 기준봉(+6%↑)</div>
    <div class="stat-value" style="color:#ef4444">{anchor_n}</div></div>
  <div class="stat-card"><div class="stat-label">신규(IN)</div>
    <div class="stat-value" style="color:#10b981">{len(in_rows)}</div></div>
  <div class="stat-card"><div class="stat-label">탈락(OUT)</div>
    <div class="stat-value" style="color:#ef4444">{len(out_rows)}</div></div>
  <div class="stat-card"><div class="stat-label">수동 A</div>
    <div class="stat-value" style="color:{TIER_COLORS['A']}">{tier_counts.get('A', 0)}</div></div>
  <div class="stat-card"><div class="stat-label">수동 B</div>
    <div class="stat-value" style="color:{TIER_COLORS['B']}">{tier_counts.get('B', 0)}</div></div>
  <div class="stat-card"><div class="stat-label">점수 80+</div>
    <div class="stat-value" style="color:#10b981">{score_a}</div></div>
  <div class="stat-card"><div class="stat-label">점수 65-79</div>
    <div class="stat-value" style="color:#3b82f6">{score_b}</div></div>
</div>

<div class="section">
  <h2>🔥 기준봉 — 전일 종가 대비 +{ANCHOR_PCT:.0f}% 이상</h2>
  <div class="scroll-x">{render_anchor_table(df)}</div>
</div>

<div class="alert-row">
  <div class="section">
    <h2>🟢 신규 진입 (IN)</h2>
    <div class="scroll-x">{render_in_out_table(in_rows, "in")}</div>
  </div>
  <div class="section">
    <h2>🔴 탈락 (OUT)</h2>
    <div class="scroll-x">{render_in_out_table(out_rows, "out")}</div>
  </div>
</div>

<div class="section">
  <h2>📅 주간 · 월간 변동</h2>
  {render_period_stats(week_stats, month_stats)}
</div>

<div class="alert-row">
  <div class="section">
    <h2>📈 등락 상위 10</h2>
    <div class="scroll-x">{render_change_table(top_gainers)}</div>
  </div>
  <div class="section">
    <h2>📉 등락 하위 10</h2>
    <div class="scroll-x">{render_change_table(top_losers)}</div>
  </div>
</div>

<div class="section">
  <h2>🗂️ {label} 시총 상위 {len(df)}</h2>
  <div class="legend">
    <span class="legend-item"><b>티어:</b></span>
    <span class="legend-item"><span class="tier tier-A">A</span>정밀</span>
    <span class="legend-item"><span class="tier tier-B">B</span>조건부</span>
    <span class="legend-item"><span class="tier tier-C">C</span>보수적</span>
    <span class="legend-item"><span class="tier tier-D">D</span>부적합</span>
    <span class="legend-item"><span class="tier tier-_">?</span>미분류</span>
  </div>
  <div class="legend">
    <span class="legend-item anchor-legend">🔥 붉은 행 = 기준봉(+{ANCHOR_PCT:.0f}%↑)</span>
    <span class="legend-item"><b>점수:</b></span>
    <span class="legend-item"><span class="score-badge score-a">80+</span></span>
    <span class="legend-item"><span class="score-badge score-b">65-79</span></span>
    <span class="legend-item"><span class="score-badge score-c">50-64</span></span>
    <span class="legend-item"><span class="score-badge score-d">&lt;50</span></span>
  </div>
  <div class="tabs">
    <button class="tab tier-tab active" onclick="showTab('all',this)">전체</button>
    <button class="tab tier-tab" onclick="showTab('A',this)">A</button>
    <button class="tab tier-tab" onclick="showTab('B',this)">B</button>
    <button class="tab tier-tab" onclick="showTab('C',this)">C</button>
    <button class="tab tier-tab" onclick="showTab('D',this)">D</button>
    <button class="tab tier-tab" onclick="showTab('?',this)">?</button>
  </div>
  <div class="scroll-x">
    {render_main_table(df)}
  </div>
</div>

</div>
"""


def build_html(payloads, date_str, past_reports=None, is_index=False):
    pretty_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"

    # DART 안내(시장 통합 상태로 표시)
    statuses = {p["dart_status"] for p in payloads}
    if "ok" in statuses:
        total_scored = sum(p["today_df"]["자동점수"].notna().sum()
                           for p in payloads if "자동점수" in p["today_df"].columns)
        dart_notice = (f'<div class="dart-notice ok">✅ <b>DART 자동 점수 활성화</b>: '
                       f'총 {int(total_scored)}개 종목 채점. 점수 셀에 마우스 올리면 세부.</div>')
    elif "no_api_key" in statuses:
        dart_notice = ('<div class="dart-notice">💡 <b>자동 점수 안내</b>: '
                       'DART API key 설정 시 자동 채점이 표시됩니다.</div>')
    else:
        dart_notice = ""

    market_tabs = "".join(
        f'<button class="market-tab{" active" if i == 0 else ""}" '
        f'onclick="showMarket(\'{p["market_key"]}\',this)">{p["label"]}</button>'
        for i, p in enumerate(payloads))
    blocks = "".join(render_market_block(p, active=(i == 0))
                     for i, p in enumerate(payloads))

    archive_section = ""
    if is_index and past_reports is not None:
        archive_section = f'''
<div class="section">
  <h2>📚 과거 리포트</h2>
  {render_archive_links(past_reports, date_str)}
</div>'''

    home_link = ""
    if not is_index:
        home_link = '<a class="home-link" href="../index.html">← 최신 리포트로</a>'

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="theme-color" content="#0f172a">
<title>KOSPI · KOSDAQ Monitor · {pretty_date}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
html,body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Malgun Gothic","Noto Sans KR",sans-serif;
background:#0f172a;color:#e2e8f0;line-height:1.5;-webkit-text-size-adjust:100%}}
body{{padding:16px}}
@media(min-width:768px){{body{{padding:24px}}}}
.container{{max-width:1500px;margin:0 auto}}
.home-link{{display:inline-block;color:#3b82f6;text-decoration:none;margin-bottom:12px;font-size:14px}}
.home-link:hover{{text-decoration:underline}}
.header{{display:flex;justify-content:space-between;align-items:flex-start;
margin-bottom:16px;padding-bottom:14px;border-bottom:1px solid #334155;flex-wrap:wrap;gap:8px}}
h1{{font-size:20px;font-weight:700}}
@media(min-width:768px){{h1{{font-size:24px}}}}
.date{{color:#94a3b8;font-size:12px;text-align:right}}
.source{{font-size:10px;color:#64748b;margin-top:4px}}
.dart-notice{{background:#1e293b;border:1px solid #334155;border-left:3px solid #f59e0b;
padding:10px 14px;border-radius:6px;margin-bottom:16px;font-size:12px;color:#cbd5e1}}
.dart-notice.ok{{border-left-color:#10b981}}
.market-tabs{{display:flex;gap:8px;margin-bottom:18px}}
.market-tab{{flex:1;background:#1e293b;border:1px solid #334155;color:#cbd5e1;
padding:12px;border-radius:8px;cursor:pointer;font-size:15px;font-weight:700;min-height:46px}}
.market-tab.active{{background:#3b82f6;color:white;border-color:#3b82f6}}
.stats{{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:18px}}
@media(min-width:540px){{.stats{{grid-template-columns:repeat(4,1fr)}}}}
@media(min-width:1024px){{.stats{{grid-template-columns:repeat(8,1fr)}}}}
.stat-card{{background:#1e293b;padding:12px;border-radius:8px;border:1px solid #334155}}
.stat-card.anchor-card{{border-left:3px solid #ef4444}}
.stat-label{{font-size:10px;color:#94a3b8;margin-bottom:4px}}
.stat-value{{font-size:20px;font-weight:700}}
@media(min-width:768px){{.stat-value{{font-size:24px}}}}
.section{{background:#1e293b;padding:14px;border-radius:8px;margin-bottom:16px;border:1px solid #334155}}
@media(min-width:768px){{.section{{padding:20px}}}}
.section h2{{font-size:15px;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid #334155}}
@media(min-width:768px){{.section h2{{font-size:17px}}}}
.alert-row{{display:grid;grid-template-columns:1fr;gap:12px}}
@media(min-width:900px){{.alert-row{{grid-template-columns:1fr 1fr;gap:16px}}}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
@media(min-width:768px){{table{{font-size:13px}}}}
th{{text-align:left;padding:6px 8px;background:#0f172a;color:#94a3b8;
font-weight:500;border-bottom:1px solid #334155;font-size:11px;white-space:nowrap}}
td{{padding:6px 8px;border-bottom:1px solid #1e293b;white-space:nowrap;vertical-align:top}}
@media(min-width:768px){{th,td{{padding:8px 10px}}}}
tr:hover td{{background:#0f172a}}
tr.anchor td{{background:rgba(239,68,68,0.12)}}
tr.anchor td:first-child{{border-left:3px solid #ef4444}}
tr.anchor:hover td{{background:rgba(239,68,68,0.2)}}
.fire{{font-size:11px}}
.name-col{{white-space:normal;max-width:340px}}
.tags{{display:inline-flex;flex-wrap:wrap;gap:3px;margin-left:6px;vertical-align:middle}}
.tag{{display:inline-block;background:#0f172a;border:1px solid #334155;color:#93c5fd;
font-size:10px;padding:1px 6px;border-radius:10px;line-height:1.6;white-space:nowrap}}
.tag-sector{{color:#a3a3a3;border-color:#475569}}
.tier{{display:inline-block;min-width:20px;height:20px;line-height:20px;
text-align:center;border-radius:4px;font-weight:700;font-size:10px;color:white;padding:0 4px}}
.tier-A{{background:{TIER_COLORS['A']}}}
.tier-B{{background:{TIER_COLORS['B']}}}
.tier-C{{background:{TIER_COLORS['C']}}}
.tier-D{{background:{TIER_COLORS['D']}}}
.tier-_{{background:{TIER_COLORS['?']}}}
.positive{{color:#ef4444;font-weight:600}}
.negative{{color:#3b82f6;font-weight:600}}
.in-badge{{background:#10b981;color:white;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:600}}
.out-badge{{background:#ef4444;color:white;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:600}}
.empty{{color:#64748b;padding:14px;text-align:center;font-style:italic;font-size:12px}}
.tabs{{display:flex;gap:4px;margin-bottom:10px;flex-wrap:wrap;overflow-x:auto;-webkit-overflow-scrolling:touch}}
.tab{{background:#334155;padding:8px 14px;border-radius:4px;cursor:pointer;border:none;
color:#e2e8f0;font-size:13px;white-space:nowrap;min-height:36px}}
.tab.active{{background:#3b82f6;color:white}}
.rank-up{{color:#10b981;font-weight:600;font-size:11px}}
.rank-down{{color:#ef4444;font-weight:600;font-size:11px}}
.legend{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px;font-size:11px;color:#94a3b8;padding:6px 0}}
.legend-item{{display:flex;align-items:center;gap:4px;white-space:nowrap}}
.anchor-legend{{color:#fca5a5}}
.period-row{{display:grid;grid-template-columns:1fr;gap:12px}}
@media(min-width:700px){{.period-row{{grid-template-columns:1fr 1fr}}}}
.period-card{{background:#0f172a;padding:12px;border-radius:6px}}
.period-label{{font-size:12px;color:#94a3b8;margin-bottom:8px}}
.period-stats{{display:flex;gap:16px;align-items:center;flex-wrap:wrap}}
.period-num{{font-size:20px;font-weight:700}}
.period-sub{{font-size:10px;color:#94a3b8;margin-top:2px}}
.scroll-x{{overflow-x:auto;-webkit-overflow-scrolling:touch}}
.score-badge{{display:inline-block;min-width:28px;padding:2px 5px;border-radius:4px;
font-weight:700;font-size:11px;color:white;text-align:center;cursor:help}}
.score-a{{background:#10b981}}
.score-b{{background:#3b82f6}}
.score-c{{background:#f59e0b}}
.score-d{{background:#ef4444}}
.score-none{{background:#475569;color:#94a3b8}}
.footer-note{{margin-top:20px;padding:12px;background:#1e293b;border-radius:6px;
color:#94a3b8;font-size:11px;line-height:1.7;border-left:3px solid #3b82f6}}
.archive-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px}}
.archive-link{{display:block;padding:10px;background:#0f172a;color:#cbd5e1;text-decoration:none;
border-radius:6px;text-align:center;font-size:13px;border:1px solid #334155}}
.archive-link:hover{{background:#334155;color:white}}
</style>
</head>
<body>
<div class="container">
{home_link}

<div class="header">
  <div>
    <h1>📊 KOSPI · KOSDAQ Monitor</h1>
    <div class="source">Naver Finance · DART OpenAPI · 시총 상위 {TOP_N}</div>
  </div>
  <div class="date">기준일: <b>{pretty_date}</b><br>{datetime.now():%Y-%m-%d %H:%M} 생성</div>
</div>

{dart_notice}

<div class="market-tabs">
{market_tabs}
</div>

{blocks}

{archive_section}

<div class="footer-note">
<b>📌 안내</b><br>
· <b>티어</b> = 수동 사전 분류(코스피 위주) / <b>점수</b> = DART 자동 채점(마우스 올리면 세부)<br>
· <b>🔥 붉은 행 = 기준봉</b>: 전일 종가 대비 등락률 +{ANCHOR_PCT:.0f}% 이상(상승 마감)<br>
· <b>테마/섹터</b> = 네이버 금융 기준(주기적 캐시 갱신, 일부 종목은 비어 있을 수 있음)<br>
· <b>NEW</b> = 어제 {TOP_N}위 밖 진입 / <b>▲▼</b> = 어제 대비 순위 변동<br>
· 본 자료는 1차 스크리닝이며 매수·매도 추천이 아닙니다.
</div>

</div>

<script>
function showMarket(m, btn) {{
  document.querySelectorAll('.market-tab').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('.market-block').forEach(b => {{
    b.style.display = (b.dataset.market === m) ? '' : 'none';
  }});
}}
function showTab(tier, btn) {{
  const block = btn.closest('.market-block');
  block.querySelectorAll('.tier-tab').forEach(t => t.classList.remove('active'));
  btn.classList.add('active');
  block.querySelectorAll('.main-table tbody tr').forEach(r => {{
    if (tier === 'all') r.style.display = '';
    else r.style.display = (r.dataset.tier === tier) ? '' : 'none';
  }});
}}
</script>
</body>
</html>
"""


# ===== 시장 단위 처리 =====
def process_market(market_key, sosok, label, eff_date, date_str):
    if DEMO:
        today_df = _sample_df(market_key, label)
    else:
        today_df = fetch_naver_market_data(sosok, market_key)
    if today_df is None or today_df.empty:
        print(f"[!] {label} 데이터 수집 실패")
        return None
    print(f"[+] {label} {len(today_df)}개 종목 수집 완료")

    if not DEMO:
        today_df = add_themes(today_df)
        today_df, dart_status = add_dart_scores(today_df, label)
    else:
        dart_status = "no_api_key"
        if "자동점수" not in today_df.columns:
            today_df["자동점수"] = None
            today_df["점수상세"] = None

    # CSV 저장 (시장별)
    today_file = DATA_DIR / f"snapshot_{market_key}_{date_str}.csv"
    df_to_save = today_df.copy()
    import json as _json
    if "점수상세" in df_to_save.columns:
        df_to_save["점수상세"] = df_to_save["점수상세"].apply(
            lambda x: _json.dumps(x, ensure_ascii=False) if isinstance(x, dict) else "")
    if "테마" in df_to_save.columns:
        df_to_save["테마"] = df_to_save["테마"].apply(
            lambda x: _json.dumps(x, ensure_ascii=False) if isinstance(x, list) else "")
    df_to_save.to_csv(today_file, index=False, encoding="utf-8-sig")
    print(f"[+] 저장: {today_file.name}")

    # 어제 비교
    other_snaps = sorted(
        [f for f in DATA_DIR.glob(f"snapshot_{market_key}_*.csv")
         if f.stem != f"snapshot_{market_key}_{date_str}"])
    if other_snaps:
        prev_df = pd.read_csv(other_snaps[-1], dtype={"티커": str})
        prev_df["티커"] = prev_df["티커"].str.zfill(6)
        in_rows, out_rows, today_df = compare_with_previous(today_df, prev_df)
    else:
        in_rows = pd.DataFrame(columns=today_df.columns)
        out_rows = pd.DataFrame(columns=today_df.columns)
        today_df["순위변동"] = None

    week_file = find_snapshot_closest_to(market_key, eff_date - timedelta(days=7))
    month_file = find_snapshot_closest_to(market_key, eff_date - timedelta(days=30))
    if week_file and week_file.stem == f"snapshot_{market_key}_{date_str}":
        week_file = None
    if month_file and month_file.stem == f"snapshot_{market_key}_{date_str}":
        month_file = None

    return {
        "market_key": market_key, "label": label, "today_df": today_df,
        "in_rows": in_rows, "out_rows": out_rows,
        "week_stats": compute_period_changes(today_df, week_file),
        "month_stats": compute_period_changes(today_df, month_file),
        "dart_status": dart_status,
    }


# ===== 데모용 샘플 =====
def _sample_df(market_key, label):
    base = [
        ("000660", "SK하이닉스", 198000, 7.2, ["AI", "HBM"], "반도체"),
        ("005930", "삼성전자", 78900, 1.3, ["반도체", "AI"], "반도체와반도체장비"),
        ("373220", "LG에너지솔루션", 412000, -2.1, ["2차전지"], "전기제품"),
        ("207940", "삼성바이오로직스", 905000, 6.8, ["바이오", "CMO"], "제약"),
        ("012450", "한화에어로스페이스", 333000, 9.4, ["방산", "우주항공"], "기계"),
        ("042660", "한화오션", 78500, 3.2, ["조선"], "조선"),
        ("105560", "KB금융", 88200, 0.4, ["은행", "배당"], "기타금융"),
        ("055550", "신한지주", 61000, -0.8, ["은행", "배당"], "기타금융"),
        ("034020", "두산에너빌리티", 21500, 6.1, ["원자력", "전력기기"], "기계"),
        ("267260", "HD현대일렉트릭", 412000, 11.3, ["전력기기", "전선"], "전기제품"),
        ("000270", "기아", 99800, 2.0, ["자동차"], "운수장비"),
        ("035420", "NAVER", 215000, -1.4, ["인터넷", "AI"], "서비스업"),
    ]
    rows = []
    for i, (code, name, price, chg, themes, sector) in enumerate(base, 1):
        rows.append({
            "순위": i, "티커": code, "종목명": name, "시장": market_key,
            "티어": BUFFETT_TIER.get(code, "?"),
            "종가": price, "등락률": chg, "시가총액": (300 - i * 10) * 1e11,
            "거래량": 1_000_000 + i * 12345, "거래대금": price * 1_000_000,
            "PER": 8 + i, "ROE": 12.5 - i * 0.3, "외국인비율": 30 + i,
            "테마": themes, "섹터": sector,
        })
    return pd.DataFrame(rows)


# ===== 메인 =====
def main():
    print("=" * 60)
    print("  KOSPI / KOSDAQ 일일 모니터링 v4.0" + ("  [DEMO]" if DEMO else ""))
    print("=" * 60)

    eff_date = get_effective_date()
    date_str = fmt(eff_date)
    print(f"[*] 기준일: {date_str}")

    global _DART_NEW_USED
    _DART_NEW_USED = 0
    if THEME_AVAILABLE:
        theme_fetcher.reset_budget()
    if THEME_MAX_NEW or DART_MAX_NEW:
        print(f"[*] 예산제: 테마 신규 {THEME_MAX_NEW or '무제한'} / "
              f"DART 신규 {DART_MAX_NEW or '무제한'} (실행당)")

    payloads = []
    for market_key, sosok, label in MARKETS:
        p = process_market(market_key, sosok, label, eff_date, date_str)
        if p is not None:
            payloads.append(p)

    if not payloads:
        print("[!] 어느 시장도 수집하지 못했습니다.")
        input("\nPress Enter to exit...")
        return

    past_reports = sorted(REPORTS_DIR.glob("report_*.html"), reverse=True)
    report_html = build_html(payloads, date_str, past_reports=past_reports, is_index=False)
    report_file = REPORTS_DIR / f"report_{date_str}.html"
    report_file.write_text(report_html, encoding="utf-8")
    print(f"[+] 리포트: docs/reports/{report_file.name}")

    past_reports = sorted(REPORTS_DIR.glob("report_*.html"), reverse=True)
    index_html = build_html(payloads, date_str, past_reports=past_reports, is_index=True)
    index_file = DOCS_DIR / "index.html"
    index_file.write_text(index_html, encoding="utf-8")
    print(f"[+] 메인: docs/index.html")

    if not DEMO and not os.environ.get("CI"):
        print("[*] 브라우저에서 엽니다...")
        try:
            webbrowser.open(index_file.absolute().as_uri())
        except Exception:
            pass

    print("\n[완료] GitHub에 업로드하려면 push 스크립트를 실행하세요.")
    input()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[중단]")
    except Exception as e:
        import traceback
        print(f"\n[!] 오류: {e}\n")
        traceback.print_exc()
        input("\nPress Enter to exit...")
