# KOSPI Top 100 일일 모니터링 v3.0

매일 코스피 시총 상위 100 종목의 변동·티어·재무 점수를 자동 수집하고, **모바일에서도 GitHub Pages를 통해 어디서나 확인**할 수 있는 도구입니다.

---

## 🎯 두 가지 사용 방식

### 방식 A: PC 실행형 (간단)

- **매일 PC에서 `run.bat` 더블클릭** → 데이터 수집 → 결과 HTML 생성 → 묻는 창에서 Y 누르면 GitHub에 자동 업로드
- **모바일에서는** GitHub Pages URL 접속해서 확인
- PC를 켜지 않은 날은 갱신 안 됨

### 방식 B: GitHub Actions 자동형 (PC 불필요)

- GitHub 클라우드 서버가 **매일 평일 17:00 (KST) 자동으로 실행**
- PC 안 켜도 매일 데이터 갱신
- 모바일에서 URL 접속만 하면 끝
- 휴가·주말에도 자동 작동

**둘은 같은 폴더 구조를 공유**하므로 동시에 운영 가능합니다. 처음엔 방식 A로 시작하고, 안정화되면 방식 B를 추가하는 게 좋습니다.

---

## 🚀 첫 설치 단계

### 공통 (필수)

1. **Python 설치** — https://www.python.org/downloads/ → 3.10+ → "Add Python to PATH" 체크
2. **`setup.bat`** 더블클릭 → 패키지 설치 (1-2분)

### 자동 점수가 필요하면 (선택)

3. **DART OpenAPI 가입** — https://opendart.fss.or.kr/
   - 회원가입 후 인증키 신청 → 40자리 키 발급 (즉시)
4. **`dart_setup.bat`** 더블클릭 → 키 붙여넣기

### GitHub Pages를 쓰려면 (선택)

5. **Git 설치** — https://git-scm.com/download/win → 기본값으로 설치
6. **GitHub 계정 가입** — https://github.com/ 회원가입
7. **저장소 생성**
   - GitHub 우측 상단 + → New repository
   - Repository name: `kospi-monitor` (또는 원하는 이름)
   - **Public** 선택 (Private도 가능하지만 Pages는 유료)
   - "Add a README file" 체크 후 Create
8. **GitHub Pages 활성화**
   - 저장소 → Settings → Pages
   - Source: "Deploy from a branch"
   - Branch: `main`, folder: `/docs`
   - Save
9. **Personal Access Token 발급** (push 인증용)
   - GitHub 우측 상단 프로필 → Settings → Developer settings → Personal access tokens → Tokens (classic)
   - Generate new token (classic)
   - Note: `kospi-monitor`
   - Expiration: 90일 이상
   - Scopes: **`repo`** 체크
   - Generate → **40자리 토큰 복사** (다시 못 봄!)
10. **`github_setup.bat`** 더블클릭
    - 저장소 URL 입력 (예: `https://github.com/yourname/kospi-monitor.git`)
    - Git 자동 설정
11. **`run.bat` + `push.bat`** 첫 실행
    - push할 때 username과 위 토큰 입력 (다음부터는 자동 기억)
12. **2-3분 후** https://yourname.github.io/kospi-monitor/ 접속 → 동작 확인

### GitHub Actions로 자동 실행하려면 (선택, 위 9~12 완료 후)

13. **DART API key를 GitHub Secrets에 등록** (자동 점수가 필요하면)
    - 저장소 → Settings → Secrets and variables → Actions
    - New repository secret
    - Name: `DART_API_KEY`
    - Secret: 40자리 DART 키
14. **워크플로 활성화**
    - 저장소 → Actions 탭 → "I understand my workflows, go ahead and enable them"
    - 좌측에 "Daily KOSPI Monitor" 보이면 OK
    - 매일 평일 17:00 KST 자동 실행됨
    - 수동 실행: Actions → Daily KOSPI Monitor → Run workflow

---

## 📅 매일 사용

### 방식 A (PC 실행형)

`run.bat` 더블클릭 → 끝나면 "Push to GitHub now? (Y/N)" → Y → 모바일에서 URL 확인

### 방식 B (GitHub Actions)

아무것도 하지 마세요. 매일 17:00 (KST) 자동으로 갱신됩니다.

수동으로 즉시 실행하고 싶으면:
- 저장소 → Actions 탭 → Daily KOSPI Monitor → Run workflow → Run

---

## 📁 폴더 구조

```
kospi_monitor/
├── kospi_check.py         메인 스크립트
├── tier_mapping.py        수동 티어 매핑
├── dart_scorer.py         DART API + 점수 계산
├── setup.bat              기본 패키지 설치
├── dart_setup.bat         DART key 등록
├── github_setup.bat       Git 초기 설정
├── run.bat                매일 실행 ★
├── push.bat               GitHub 업로드
├── diagnose.bat           문제 진단
├── .gitignore             민감 정보 제외
├── .github/workflows/
│   └── daily.yml          GitHub Actions 자동 실행
├── docs/                  ← GitHub Pages 호스팅 폴더
│   ├── index.html         최신 리포트 (모바일에서 이 URL)
│   └── reports/
│       └── report_YYYYMMDD.html
├── data/                  일별 CSV (로컬 누적)
└── fin_cache/             DART 재무제표 캐시
```

---

## 📊 자동 점수 (DART)

DART API로 최근 3년치 재무제표를 받아 9개 항목으로 자동 채점 (총 100점):

| 항목 | 배점 | 기준 |
|---|---:|---|
| 사업 이해도 | 10 | 자동 5점 (수동 항목) |
| 매출 성장성 | 10 | 3년 CAGR |
| 영업이익 안정성 | 10 | 적자 연수 |
| ROE | 15 | 3년 평균 |
| 현금흐름 | 15 | 영업현금흐름/순이익 |
| 부채 안정성 | 10 | 부채비율 |
| 해자(영업이익률) | 10 | 3년 평균 |
| 주주환원 | 10 | 자동 5점 (수동 항목) |
| 밸류에이션 | 10 | 현재 PER |

점수 80+ A급 / 65-79 B급 / 50-64 C급 / <50 D급

---

## 🔧 문제 발생 시

`diagnose.bat` 더블클릭 → 14가지 항목 자동 점검 결과를 보내주세요.

특히 다음 단계가 핵심:
- `[5]` 패키지 설치 여부
- `[6]` 네이버 금융 연결
- `[7][8]` DART API key
- `[9][10]` Git/저장소
- `[11]` docs 폴더

---

## 🔐 보안 안내

- **`dart_api_key.txt`는 `.gitignore`에 포함**되어 있어 GitHub에 절대 올라가지 않습니다
- GitHub Actions에서는 `DART_API_KEY` Secret으로 안전하게 주입됨
- 저장소를 Public으로 해도 API key는 노출되지 않습니다

---

## ⚠️ 면책

본 자료는 1차 스크리닝·정보 제공 목적이며 매수·매도 추천이 아닙니다. DART 사업보고서를 직접 확인 후 본인 책임 하에 투자하세요.
