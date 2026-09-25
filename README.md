# SurgePilot

미국 주식의 단기 급등 신호를 연구하고, 같은 규칙을 과거 데이터와 실시간 가상매매에 적용하는 프로젝트입니다. GitHub Pages에 [과거 검증 화면](https://nota2.github.io/surge-pilot/)과 [장중 대시보드](https://nota2.github.io/surge-pilot/live.html)를 배포합니다. 최초 화면의 수치는 **합성 예제**입니다.

## 현재 가능한 작업

- 토스증권 Open API에서 지정한 미국 종목의 1분봉을 수집합니다.
- CSV 전체를 시간순으로 재생해 매수·청산 규칙을 검증합니다. 신호는 분봉 마감 후 생성되고 매수는 다음 분봉 시가로 계산합니다.
- 날짜를 나눠 학습 기간에서 매개변수를 고르고 별도 기간에 적용합니다. 50% 급등 사례 포착률과 무작위 종목·일자 표본도 함께 계산합니다.
- 장중 토스증권 시세로 로컬 가상매매를 수행합니다. 주문 API 호출은 없습니다.
- JSON 보고서를 GitHub Pages에서 열어 거래 내역, 신호, 평가금액과 검증 정보를 볼 수 있습니다. 장중 화면은 로컬 가상매매 수집기가 실행 중이면 `127.0.0.1`의 최신 보고서를 우선 읽습니다.

현재 저장소에는 실계좌 자동주문을 실행하는 명령이 없습니다. 실거래 단계는 충분한 과거 데이터와 가상매매 결과를 확인하고, 체결·중복 주문·장 종료 실패 대응을 검증한 뒤 진행합니다.

## 시작

Python 3.11 이상이 필요합니다. 외부 Python 패키지는 필요하지 않습니다.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
surge-pilot demo --output docs/report.json
surge-pilot demo-live --output docs/live-report.json
python3 -m http.server 8000 --directory docs
```

`http://localhost:8000`에서 화면을 확인할 수 있습니다. 두 화면의 **로컬 보고서 열기**로 `reports/private/result.json` 또는 `paper.json`을 선택하면 GitHub에 올리지 않고도 자신의 결과를 볼 수 있습니다.

### 실제 과거 데이터 수집

[토스증권 공식 Open API](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json)에서 발급한 `client_id`, `client_secret`을 환경 변수로 설정합니다. 키를 파일이나 GitHub Pages에 넣지 마세요.

```bash
export TOSS_CLIENT_ID='...'
export TOSS_CLIENT_SECRET='...'
surge-pilot collect --symbols AAPL,MSFT --since '2026-09-01T00:00:00-04:00' --output data/private/candles.csv
surge-pilot universe --output data/private/us-universe.json
surge-pilot backtest --data data/private/candles.csv --output reports/private/backtest.json
surge-pilot research --data data/private/candles.csv --output reports/private/research.json
```

CSV 필드: `timestamp,symbol,open,high,low,close,volume`. 시간에는 반드시 UTC 오프셋을 포함해야 합니다. `research`는 최소 4거래일을 요구하지만, 신뢰할 만한 판단에는 훨씬 긴 기간, 많은 종목과 여러 시장 환경이 필요합니다. 토스 API의 분봉 제공 이력 깊이는 실제 계정으로 확인해야 합니다. 필요한 과거 데이터가 모자라면 동일 형식의 별도 데이터 소스를 사용합니다.

`universe`는 토스에서 거래 가능한 미국 보통주 목록을 저장합니다. `collect --symbols-file data/private/us-universe.json ...`으로 전체 목록의 분봉 수집을 시도할 수 있지만, 종목 수가 많아 호출 한도와 수집 시간이 커집니다. 초기에는 일부 종목으로 데이터 제공 범위와 수집 속도를 확인하세요.

### 실시간 가상매매

```bash
surge-pilot paper --cash 10000 --output reports/private/paper.json
```

미국 정규장에 컴퓨터와 프로세스를 계속 켜 두어야 합니다. 토스의 **시장 거래량 상위 100개 + 당일 상승률 상위 100개**를 후보로 가져와 10초마다 시세를 확인합니다. 시장 전체를 빠짐없이 감시하지 않으므로 급등주를 놓칠 수 있습니다. 가상 체결은 관측 가격에 설정한 불리한 가격 조정과 수수료를 더한 계산입니다. 실제 주문의 체결을 보장하지 않습니다.

`paper` 명령은 `127.0.0.1:8765`에 읽기 전용 보고서 서버도 띄웁니다. 같은 컴퓨터의 브라우저에서 [Pages 장중 화면](https://nota2.github.io/surge-pilot/live.html)을 열면 15초마다 이 로컬 보고서를 읽습니다. Pages 자체에는 개인 보고서가 게시되지 않습니다. 브라우저에서 로컬 접근을 허용하지 않으면 `http://localhost:8000/live.html`로 보거나 보고서를 직접 선택할 수 있습니다. 이 연결은 **가상매매 테스트용**이며, 컴퓨터를 끄면 끊깁니다.

### 매개변수

기본값은 5분 상승률 8%, 고점 대비 7%, 매수가 대비 5% 손절입니다. `backtest`와 `paper`에 `--lookback`, `--rise`, `--trail`, `--stop`, `--fee-bps`, `--slippage-bps`를 넘길 수 있습니다. 한 거래일의 목표 매수 규모는 시작 금액 대비 40%, 40%, 20%이며, 하루 최대 3회 진입합니다. 같은 종목은 당일 재진입하지 않습니다. 진입은 정규장 종료 20분 전부터 중단하고 5분 전에 청산을 시도합니다.

## 보고서와 Pages

`docs/report.json`과 `docs/live-report.json`은 저장소에 공개되는 **합성 예제**입니다. 개인 계좌, 보유 종목, 실제 거래 이력이 담긴 JSON은 `reports/private/`에 두고 웹 페이지의 파일 열기로 확인하세요. 실데이터 보고서를 `docs/`에 넣고 푸시하면 누구나 볼 수 있습니다.

`main` 브랜치에 푸시하면 `.github/workflows/pages.yml`이 `docs/`를 GitHub Pages에 배포합니다. 테스트는 로컬에서만 실행합니다. 저장소 설정의 **Pages → Build and deployment → Source**는 **GitHub Actions**를 사용합니다.

향후 실제 서비스에서는 Vercel의 서버 측 API와 Supabase 저장소를 통해 장중 보고서를 전달할 계획입니다. 브라우저에 토스 API 키를 노출하지 않고, 개인 계좌 데이터에 인증과 행 단위 접근 제어를 적용하는 것이 연결 전제입니다. 현재의 Pages 화면과 로컬 브리지는 가상매매 검증 단계에 한정됩니다. 데이터 경로와 이전 계획은 [architecture.md](docs/architecture.md)에 정리했습니다.

## 검증 범위와 주의점

- 50% 급등 여부는 검증용 사후 지표이며 매수 신호에 사용하지 않습니다.
- 학습/별도 검증은 날짜 순서로 분리합니다. 무작위 표본 결과가 좋아도 전체 시장에서 안전하다는 뜻은 아닙니다.
- 분봉의 고가와 저가 발생 순서는 알 수 없습니다. 백테스트는 현재 봉에서 새로 형성된 고점을 다음 봉 이후 손절 기준에 반영합니다.
- 수수료·체결 불리함은 가정입니다. 호가 잔량, 거래 정지, 급등주의 실제 체결률은 검증하지 못합니다.
- 데이터에 장 마감 전 분봉이 없으면 `missing_cutoff_bar`로 표시합니다. 이 경우 당일 청산이 가능했음을 입증하지 못합니다.
- 거래 시간은 `America/New_York`으로 해석합니다. 실시간 세션은 토스 시장 일정 API의 정규장 시간을 사용합니다.

테스트: `python3 -m unittest discover -s tests -v`
