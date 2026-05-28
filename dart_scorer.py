# -*- coding: utf-8 -*-
"""
DART (전자공시) API 기반 재무 데이터 수집 + 100점 점수 계산
v2.1: account_id 매칭, fs_div 완화, 캐시 버전 관리, 디버그 출력
"""

import os
import io
import json
import time
import zipfile
from datetime import datetime
from pathlib import Path
import xml.etree.ElementTree as ET

import requests

SCRIPT_DIR = Path(__file__).parent.resolve()
CACHE_DIR = SCRIPT_DIR / "fin_cache"
CORP_CODE_FILE = SCRIPT_DIR / "corp_codes.json"
API_KEY_FILE = SCRIPT_DIR / "dart_api_key.txt"
CACHE_DIR.mkdir(exist_ok=True)

DART_BASE = "https://opendart.fss.or.kr/api"

# 캐시 버전 — 매칭 로직 변경 시 증가. 다른 버전 캐시는 자동 무시
CACHE_VERSION = 2


# ============================================================
# API Key
# ============================================================
def get_api_key():
    key = os.environ.get("DART_API_KEY", "").strip()
    if key:
        return key
    if API_KEY_FILE.exists():
        return API_KEY_FILE.read_text(encoding="utf-8").strip()
    return None


# ============================================================
# 회사 코드 매핑
# ============================================================
def ensure_corp_codes(api_key):
    if CORP_CODE_FILE.exists():
        age_days = (datetime.now().timestamp()
                    - CORP_CODE_FILE.stat().st_mtime) / 86400
        if age_days < 30:
            return

    print("[DART] 회사 코드 매핑 다운로드 중...")
    url = f"{DART_BASE}/corpCode.xml"
    resp = requests.get(url, params={"crtfc_key": api_key}, timeout=60)
    resp.raise_for_status()

    if resp.content[:2] != b"PK":
        try:
            err = resp.json()
            raise RuntimeError(f"DART corpCode 오류: {err}")
        except json.JSONDecodeError:
            raise RuntimeError("DART corpCode 응답이 ZIP이 아닙니다.")

    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        with z.open(z.namelist()[0]) as f:
            xml_bytes = f.read()

    root = ET.fromstring(xml_bytes)
    mapping = {}
    for child in root.findall("list"):
        stock_code = (child.findtext("stock_code") or "").strip()
        corp_code = (child.findtext("corp_code") or "").strip()
        corp_name = (child.findtext("corp_name") or "").strip()
        if stock_code and stock_code != " ":
            mapping[stock_code] = {"corp_code": corp_code, "name": corp_name}

    CORP_CODE_FILE.write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[DART] 회사 코드 {len(mapping):,}개 저장")


def load_corp_codes():
    if not CORP_CODE_FILE.exists():
        return {}
    return json.loads(CORP_CODE_FILE.read_text(encoding="utf-8"))


# ============================================================
# 재무 데이터 파싱 (개선된 다단계 매칭)
# ============================================================

# 1차 매칭: K-IFRS 표준 account_id (회사 무관하게 동일)
ACCOUNT_IDS = {
    "revenue": [
        "ifrs-full_Revenue",
        "ifrs_Revenue",
        "ifrs-full_RevenueFromContractsWithCustomers",
        "dart_OperatingRevenue",
        "dart_Revenue",
    ],
    "operating_income": [
        "dart_OperatingIncomeLoss",
        "ifrs-full_OperatingIncomeLoss",
        "ifrs-full_ProfitLossFromOperatingActivities",
    ],
    "net_income": [
        "ifrs-full_ProfitLoss",
        "ifrs_ProfitLoss",
    ],
    "total_assets": [
        "ifrs-full_Assets",
        "ifrs_Assets",
    ],
    "total_liabilities": [
        "ifrs-full_Liabilities",
        "ifrs_Liabilities",
    ],
    "total_equity": [
        "ifrs-full_Equity",
        "ifrs_Equity",
        "ifrs-full_EquityAttributableToOwnersOfParent",
    ],
    "op_cashflow": [
        "ifrs-full_CashFlowsFromUsedInOperatingActivities",
        "ifrs_CashFlowsFromUsedInOperatingActivities",
        "dart_CashFlowsFromUsedInOperatingActivities",
    ],
}

# 2차 매칭: account_nm 정확
ACCOUNT_NAMES = {
    "revenue": [
        "매출액", "수익(매출액)", "수익", "영업수익",
        "영업수익(매출액)", "보험손익", "매출", "Revenue",
    ],
    "operating_income": [
        "영업이익", "영업이익(손실)", "영업손익", "영업손실",
    ],
    "net_income": [
        "당기순이익", "당기순이익(손실)", "당기순손익",
        "분기순이익", "반기순이익", "당기순손실",
    ],
    "total_assets": ["자산총계"],
    "total_liabilities": ["부채총계"],
    "total_equity": [
        "자본총계",
        "지배기업의 소유주에게 귀속되는 자본",
    ],
    "op_cashflow": [
        "영업활동현금흐름",
        "영업활동으로인한현금흐름",
        "영업활동 현금흐름",
        "영업활동에서 창출된 현금흐름",
        "영업활동으로 인한 현금흐름",
    ],
}

# 3차 매칭: account_nm 키워드 포함
ACCOUNT_KEYWORDS = {
    "revenue": ["매출액", "영업수익"],
    "operating_income": ["영업이익", "영업손익"],
    "net_income": ["당기순이익", "당기순손익"],
    "total_assets": ["자산총계"],
    "total_liabilities": ["부채총계"],
    "total_equity": ["자본총계"],
    "op_cashflow": ["영업활동"],
}


def parse_amount(text):
    if text is None:
        return None
    s = str(text).replace(",", "").strip()
    if not s or s == "-":
        return None
    try:
        return int(s)
    except ValueError:
        try:
            return int(float(s))
        except ValueError:
            return None


def _fs_div_match(item_fs_div, target_fs_div):
    """fs_div 매칭. item에 fs_div가 없으면 통과(완화 정책)"""
    if not item_fs_div:
        return True
    return item_fs_div == target_fs_div


def parse_financials(items, target_fs_div, target_year):
    """다단계 매칭: account_id → account_nm 정확 → 부분 매칭"""
    out = {
        "year": target_year,
        "revenue": 0, "operating_income": 0, "net_income": 0,
        "total_assets": 0, "total_liabilities": 0, "total_equity": 0,
        "op_cashflow": 0,
    }
    matched = set()

    id_to_field = {aid: f for f, aids in ACCOUNT_IDS.items() for aid in aids}
    name_to_field = {n: f for f, ns in ACCOUNT_NAMES.items() for n in ns}

    # --- 1차: account_id (가장 안정적) ---
    for item in items:
        if not _fs_div_match(item.get("fs_div"), target_fs_div):
            continue
        aid = (item.get("account_id") or "").strip()
        field = id_to_field.get(aid)
        if not field or field in matched:
            continue
        amt = parse_amount(item.get("thstrm_amount"))
        if amt is not None:
            out[field] = amt
            matched.add(field)

    # --- 2차: account_nm 정확 매칭 ---
    for item in items:
        if not _fs_div_match(item.get("fs_div"), target_fs_div):
            continue
        nm = (item.get("account_nm") or "").strip()
        field = name_to_field.get(nm)
        if not field or field in matched:
            continue
        amt = parse_amount(item.get("thstrm_amount"))
        if amt is not None:
            out[field] = amt
            matched.add(field)

    # --- 3차: account_nm 키워드 부분 매칭 ---
    for item in items:
        if not _fs_div_match(item.get("fs_div"), target_fs_div):
            continue
        nm = (item.get("account_nm") or "").strip()
        if not nm:
            continue
        for field, keywords in ACCOUNT_KEYWORDS.items():
            if field in matched:
                continue
            if any(kw in nm for kw in keywords):
                # 잘못 매칭 방지 (비용, 원가 제외)
                if field in ("revenue", "operating_income"):
                    if any(x in nm for x in ["비용", "원가", "차감", "매출원가", "감소"]):
                        continue
                amt = parse_amount(item.get("thstrm_amount"))
                if amt is not None:
                    out[field] = amt
                    matched.add(field)
                    break

    # --- 4차: fs_div 완전 무시 (3개 미만만 채워진 경우) ---
    if len(matched) < 4:
        for item in items:
            aid = (item.get("account_id") or "").strip()
            field = id_to_field.get(aid)
            if field and field not in matched:
                amt = parse_amount(item.get("thstrm_amount"))
                if amt is not None:
                    out[field] = amt
                    matched.add(field)

    return out, matched


# ============================================================
# DART API 호출 + 캐시
# ============================================================
def fetch_financial_year(api_key, corp_code, year, debug=False):
    cache_file = CACHE_DIR / f"{corp_code}_{year}.json"

    # 캐시 검사 + 버전 검증
    if cache_file.exists():
        age_days = (datetime.now().timestamp()
                    - cache_file.stat().st_mtime) / 86400
        max_age = 7 if year >= datetime.now().year - 1 else 365
        if age_days < max_age:
            try:
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                if cached.get("_v") == CACHE_VERSION:
                    return cached
                # 구 버전 → 재호출
            except json.JSONDecodeError:
                pass

    url = f"{DART_BASE}/fnlttSinglAcntAll.json"
    params = {
        "crtfc_key": api_key,
        "corp_code": corp_code,
        "bsns_year": str(year),
        "reprt_code": "11011",  # 사업보고서
        "fs_div": "CFS",
    }

    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
    except Exception as e:
        if debug:
            print(f"  [debug] {corp_code} {year} CFS 호출 실패: {e}")
        return None

    used_fs_div = "CFS"
    if data.get("status") != "000":
        if debug:
            print(f"  [debug] {corp_code} {year} CFS 응답 status={data.get('status')}: {data.get('message')}")
        # OFS 재시도
        params["fs_div"] = "OFS"
        try:
            resp = requests.get(url, params=params, timeout=15)
            data = resp.json()
            used_fs_div = "OFS"
        except Exception:
            return None

    if data.get("status") != "000":
        if debug:
            print(f"  [debug] {corp_code} {year} OFS도 실패: {data.get('status')} {data.get('message')}")
        empty_result = {"_v": CACHE_VERSION, "empty": True,
                        "year": year, "status": data.get("status")}
        cache_file.write_text(json.dumps(empty_result), encoding="utf-8")
        return None

    items = data.get("list", [])
    result, matched = parse_financials(items, used_fs_div, year)
    result["_v"] = CACHE_VERSION
    result["_fs_div"] = used_fs_div
    result["_matched_fields"] = sorted(matched)

    if debug:
        print(f"  [debug] {corp_code} {year}: fs_div={used_fs_div}, "
              f"items={len(items)}, matched={sorted(matched)}")
        # 매칭 0개일 때 raw 샘플 출력
        if not matched and items:
            sample = items[:3]
            print(f"  [debug] sample raw items:")
            for it in sample:
                print(f"    fs_div={it.get('fs_div')!r}, "
                      f"account_id={it.get('account_id')!r}, "
                      f"account_nm={it.get('account_nm')!r}, "
                      f"thstrm_amount={it.get('thstrm_amount')!r}")

    cache_file.write_text(json.dumps(result, ensure_ascii=False),
                          encoding="utf-8")
    return result


def fetch_recent_financials(api_key, corp_code, n_years=3, debug=False):
    current_year = datetime.now().year
    if datetime.now().month < 4:
        latest_year = current_year - 2
    else:
        latest_year = current_year - 1

    results = []
    for y in range(latest_year - n_years + 1, latest_year + 1):
        data = fetch_financial_year(api_key, corp_code, y, debug=debug)
        if data and not data.get("empty"):
            results.append(data)
        time.sleep(0.05)
    return results


# ============================================================
# 100점 채점
# ============================================================
def calculate_score(financials, current_per=None):
    if not financials or len(financials) < 2:
        return None

    breakdown = {}

    # 1. 사업 이해도 (10) — 자동화 불가, 기본 5
    breakdown["사업이해도"] = 5

    # 2. 매출 성장성 (10)
    revenues = [f["revenue"] for f in financials if f["revenue"] > 0]
    if len(revenues) >= 2:
        cagr = (revenues[-1] / revenues[0]) ** (1 / (len(revenues) - 1)) - 1
        if cagr >= 0.10: s = 10
        elif cagr >= 0.05: s = 8
        elif cagr >= 0: s = 5
        elif cagr >= -0.05: s = 2
        else: s = 0
    else:
        s = 0
    breakdown["매출성장성"] = s

    # 3. 영업이익 안정성 (10)
    op_incomes = [f["operating_income"] for f in financials]
    if any(op_incomes):  # 모두 0이면 데이터 부재
        neg_years = sum(1 for x in op_incomes if x <= 0)
        if neg_years == 0:
            s = 10 if op_incomes[-1] >= op_incomes[0] else 7
        elif neg_years == 1:
            s = 4
        else:
            s = 0
    else:
        s = 0
    breakdown["영업이익안정성"] = s

    # 4. ROE (15)
    roes = []
    for f in financials:
        if f["total_equity"] > 0:
            roes.append(f["net_income"] / f["total_equity"])
    if roes:
        avg_roe = sum(roes) / len(roes)
        if avg_roe >= 0.15: s = 15
        elif avg_roe >= 0.10: s = 12
        elif avg_roe >= 0.07: s = 8
        elif avg_roe >= 0.05: s = 4
        else: s = 0
    else:
        s = 0
    breakdown["ROE"] = s

    # 5. 현금흐름 (15)
    cf_ratios = []
    positive_cf_years = 0
    for f in financials:
        if f["op_cashflow"] > 0:
            positive_cf_years += 1
        if f["net_income"] > 0 and f["op_cashflow"] != 0:
            cf_ratios.append(f["op_cashflow"] / f["net_income"])
    if cf_ratios and positive_cf_years >= len(financials) - 1:
        avg = sum(cf_ratios) / len(cf_ratios)
        if avg >= 1.0: s = 15
        elif avg >= 0.8: s = 11
        elif avg >= 0.5: s = 6
        else: s = 3
    elif positive_cf_years >= len(financials) - 1 and positive_cf_years > 0:
        s = 5
    else:
        s = 0
    breakdown["현금흐름"] = s

    # 6. 부채 안정성 (10) — 금융업 부채비율(5-10배) 일부 반영
    eq = financials[-1]["total_equity"]
    lia = financials[-1]["total_liabilities"]
    if eq > 0:
        debt_ratio = lia / eq
        if debt_ratio < 0.5: s = 10
        elif debt_ratio < 1.0: s = 7
        elif debt_ratio < 2.0: s = 4
        elif debt_ratio < 8.0: s = 2  # 은행, 보험 등
        else: s = 0
    else:
        s = 0
    breakdown["부채안정성"] = s

    # 7. 해자(영업이익률) (10)
    margins = []
    for f in financials:
        if f["revenue"] > 0:
            margins.append(f["operating_income"] / f["revenue"])
    if margins:
        avg_m = sum(margins) / len(margins)
        if avg_m >= 0.20: s = 10
        elif avg_m >= 0.10: s = 7
        elif avg_m >= 0.05: s = 4
        elif avg_m >= 0: s = 2
        else: s = 0
    else:
        s = 0
    breakdown["해자"] = s

    # 8. 주주환원 (10) — 기본 5
    breakdown["주주환원"] = 5

    # 9. 밸류에이션 (10)
    if current_per is not None:
        try:
            per_val = float(current_per)
            if per_val > 0:
                if per_val <= 10: s = 10
                elif per_val <= 15: s = 8
                elif per_val <= 20: s = 5
                elif per_val <= 30: s = 3
                else: s = 1
            else:
                s = 0
        except (TypeError, ValueError):
            s = 3
    else:
        s = 3
    breakdown["밸류에이션"] = s

    total = sum(breakdown.values())
    return {"total": total, "breakdown": breakdown}


# ============================================================
# 메인 진입점
# ============================================================
def score_tickers(tickers, per_map=None, progress_callback=None, debug_first=2):
    api_key = get_api_key()
    if not api_key:
        return None

    try:
        ensure_corp_codes(api_key)
    except Exception as e:
        print(f"[DART] 회사 코드 다운로드 실패: {e}")
        return None

    corp_map = load_corp_codes()
    if not corp_map:
        return None

    per_map = per_map or {}
    results = {}
    for i, ticker in enumerate(tickers, 1):
        ticker_padded = str(ticker).zfill(6)
        if progress_callback:
            progress_callback(i, len(tickers), ticker_padded)

        info = corp_map.get(ticker_padded)
        if not info:
            results[ticker_padded] = None
            continue

        debug = (i <= debug_first)

        try:
            financials = fetch_recent_financials(
                api_key, info["corp_code"], 3, debug=debug
            )
        except Exception as e:
            print(f"[DART] {ticker_padded} 재무 데이터 실패: {e}")
            financials = None

        if not financials:
            results[ticker_padded] = None
            continue

        score = calculate_score(financials, per_map.get(ticker_padded))
        if score:
            score["financials"] = financials
        results[ticker_padded] = score

    return results
