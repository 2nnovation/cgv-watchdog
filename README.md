# CGV 상영 회차 감시기

CGV 광교의 특정 날짜 시간표를 주기적으로 읽고, 영화명과 상영관 조건에 맞는 새 회차가 나타나면 Telegram으로 알립니다.

현재 구현은 2026년 8월 18일 기준 CGV 새 사이트가 사용하는 내부 BFF 요청을 브라우저 세션 안에서 호출합니다.

- 시작 페이지: `https://cgv.co.kr/cnm/bzplcCgv/0257001`
- 시간표 BFF: `/api/v1/booking/searchMovScnInfo`
- 이유: `api.cgv.co.kr` 또는 BFF를 일반 HTTP 클라이언트로 직접 호출하면 403이 발생했지만, 정상 CGV 페이지에서 Playwright의 `fetch`로 호출하면 HTTP 200과 실제 시간표 JSON이 반환됐습니다.

## 로컬 실행

Python 3.12 권장:

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
# source .venv/bin/activate

python -m pip install -r requirements.txt
python -m playwright install chromium
```

조회만 검증하려면:

```powershell
$env:THEATER = "광교"
$env:DATE = "2026-08-25"
$env:MOVIE_KEYWORD = "오디세이"
$env:SCREEN_TYPE = "IMAX"
python cgv_watch.py --check-only
```

Windows에 Chrome 또는 Edge가 설치되어 있으면 자동으로 사용합니다. 그 외 환경에서는 `python -m playwright install chromium`으로 설치한 Chromium을 사용합니다. 특정 브라우저는 `BROWSER_PATH`로 지정할 수 있습니다.

실제 감시 실행:

```powershell
$env:THEATER = "광교"
$env:DATE = "2026-08-29"
$env:MOVIE_KEYWORD = "오디세이"
$env:SCREEN_TYPE = "IMAX"
$env:TELEGRAM_BOT_TOKEN = "..."
$env:TELEGRAM_CHAT_ID = "..."
python cgv_watch.py
```

한 번 실행할 때 한 번 조회하고 종료합니다. 반복 실행은 GitHub Actions cron 또는 운영체제 스케줄러가 담당합니다.

## 출력과 성공 판정

프로그램은 다음을 구분해 출력합니다.

- 페이지 HTTP 상태와 시간표 API HTTP 상태
- 해당 날짜 전체 시간표 항목 수
- 영화 키워드 일치 수
- 상영관 종류 일치 수
- 시작/종료 시간, 상영관, 잔여/전체 좌석, 예매 가능 판정

API가 HTTP 200, `statusCode: 0`, 배열 형태의 `data`를 반환했을 때만 조회 성공입니다. `data`가 빈 배열이면 “조회 성공, 등록 회차 0개”로 판정합니다. 403, 차단 안내 페이지, 비 JSON 응답, CGV 오류 코드는 조회 실패로 종료합니다.

## Telegram 봇 설정

1. Telegram에서 `@BotFather`에게 `/newbot`을 보내 봇을 만들고 토큰을 받습니다.
2. 만든 봇과 채팅을 시작하거나 봇을 알림 대상 그룹에 추가합니다.
3. 브라우저에서 `https://api.telegram.org/bot<토큰>/getUpdates`를 열어 대상 `chat.id`를 확인합니다.
4. 토큰을 `TELEGRAM_BOT_TOKEN`, ID를 `TELEGRAM_CHAT_ID`에 넣습니다.

토큰이나 Chat ID가 없는데 새 회차를 발견하면 알림 실패로 종료하며 `state.json`을 변경하지 않습니다. 따라서 자격 증명을 고친 뒤 다음 실행에서 다시 알립니다.

## 중복 알림 방지

각 회차를 다음 값의 조합으로 식별합니다.

```text
날짜 + 극장 코드 + 상영관 번호 + 회차 순번 + 상품 번호
```

알림 전송에 성공한 회차 ID만 `state.json`에 저장합니다. 이미 알린 회차는 반복 알림하지 않고, 이후 새 회차가 추가되면 추가된 회차만 알립니다.

## GitHub Actions 설정

1. 이 폴더 내용을 GitHub 저장소 루트에 올립니다.
2. 저장소의 **Settings → Secrets and variables → Actions**에서 다음 Repository secret을 만듭니다.
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
3. 필요하면 같은 화면의 Variables 탭에 아래 Repository variable을 만듭니다.
   - `CGV_THEATER` (기본 `광교`)
   - `CGV_DATE` (기본 `2026-08-29`)
   - `CGV_MOVIE_KEYWORD` (기본 `오디세이`)
   - `CGV_SCREEN_TYPE` (기본 `IMAX`)
4. **Actions → CGV IMAX watch → Run workflow**로 수동 실행해 로그를 먼저 확인합니다.

워크플로는 UTC 기준 cron으로 약 5분마다 실행되며, 매 실행마다 Chromium을 설치하고 한 번 조회합니다. GitHub Actions 예약 실행은 정확히 5분마다 보장되지 않고 혼잡 시 지연될 수 있습니다.

알림 성공으로 `state.json`이 바뀌면 Actions 봇이 상태 파일만 저장소에 커밋합니다. 저장소 규칙이 봇의 직접 push를 막으면 이 단계가 실패해 다음 실행에서 중복 알림이 갈 수 있으므로, 해당 워크플로의 `contents: write` 권한과 브랜치 규칙을 확인하세요.

## 감시 대상 변경

지원 설정:

| 환경변수 | 기본값 | 설명 |
|---|---:|---|
| `THEATER` | `광교` | 표시할 극장명 |
| `DATE` | `2026-08-29` | `YYYY-MM-DD` |
| `MOVIE_KEYWORD` | `오디세이` | 영화명 부분 일치 |
| `SCREEN_TYPE` | `IMAX` | 상영관/포맷 부분 일치 |
| `STATE_PATH` | `state.json` | 중복 알림 상태 파일 |
| `HEADLESS` | `true` | `false`면 로컬에서 브라우저 표시 |
| `BROWSER_PATH` | 자동 탐지 | Chrome/Edge/Chromium 실행 파일 |

현재 이름 내장값은 광교입니다. 다른 극장은 `THEATER`와 함께 CGV의 4자리 `THEATER_CODE` 및 7자리 `BUSINESS_PLACE_NO`를 설정해야 합니다.

## CGV 사이트 변경 시 확인할 곳

다음 순서로 점검하세요.

1. `cgv.co.kr/cnm/bzplcCgv/0257001`이 브라우저에서 열리는지
2. 개발자 도구 Network에서 극장별 예매 화면의 시간표 요청이 여전히 `searchMovScnInfo`인지
3. `cgv_watch.py`의 `API_PATH`와 쿼리 필드(`coCd`, `siteNo`, `scnYmd` 등)가 현재 요청과 같은지
4. 응답의 영화/상영관/시간/좌석 필드(`movNm`, `scnsEnm`, `movkndDsplNm`, `scnsrtTm`, `frSeatCnt`, `stcnt`)가 유지되는지
5. 차단 화면에 `비정상적으로 CGV에 접속` 또는 Ray ID가 표시되는지

직접 HTTP 호출이 다시 가능해지면 Playwright 없이 BFF를 호출하는 편이 가볍습니다. 반대로 브라우저 세션도 차단되면 우회로 단정하지 말고 실행 환경별로 재검증해야 합니다.

## GitHub Actions 차단 가능성과 대안

로컬 Windows Chrome에서는 실제 시간표 JSON 조회를 확인했지만, GitHub-hosted runner의 데이터센터 IP에서는 아직 검증하지 않았습니다. CGV/Cloudflare가 해당 IP를 차단하면 로그에 페이지 차단 또는 API HTTP 403으로 명확히 실패합니다.

현실적인 대안은 다음과 같습니다.

- 항상 켜진 VPS나 홈 서버의 self-hosted GitHub Actions runner에서 Playwright 실행
- Chromium을 지원하는 서버리스/컨테이너 환경에서 같은 `--check-only` 검증 후 배포
- Cloudflare Browser Rendering을 사용해 페이지 안에서 BFF 요청 실행

Cloudflare Workers의 일반 `fetch`만으로는 현재 확인된 403을 해결한다고 보장할 수 없고, Browser Rendering도 요금제·한도·CGV 차단 여부를 실제 계정에서 검증해야 합니다. 무료 서버리스는 실행 시간, Chromium 크기, 고정 IP 차단 때문에 오히려 불안정할 수 있습니다.
