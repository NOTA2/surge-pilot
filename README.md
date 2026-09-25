# SurgePilot

미국 주식의 단기 급등 신호를 연구하고, 같은 규칙을 과거 데이터와 실시간 가상매매에 적용하는 프로젝트입니다. GitHub Pages에는 [과거 검증 화면](https://nota2.github.io/surge-pilot/)만 배포합니다. 장중 대시보드는 `docs/live.html`에 있는 로컬 시제품이며, 향후 Vercel·Supabase 백엔드에 연결한 뒤 서비스합니다. Pages에는 최근 30거래일의 **실제 시세 기반 집계 결과**를 표시하고 종목별 시세·거래는 공개하지 않습니다.

## 현재 가능한 작업

- 토스증권 Open API에서 지정한 미국 종목의 1분봉을 수집합니다.
- CSV 전체를 시간순으로 재생해 매수·청산 규칙을 검증합니다. 신호는 분봉 마감 후 생성되고 매수는 다음 분봉 시가로 계산합니다.
- 날짜를 나눠 학습 기간에서 매개변수를 고르고 별도 기간에 적용합니다. 50% 급등 사례 포착률과 무작위 종목·일자 표본도 함께 계산합니다.
- 장중 토스증권 시세로 로컬 가상매매를 수행합니다. 주문 API 호출은 없습니다.
- JSON 보고서를 Pages 과거 검증 화면에서 열어 거래 내역, 신호, 평가금액과 검증 정보를 볼 수 있습니다. 장중 시제품은 로컬에서 보고서 파일을 연결해 확인할 수 있습니다.

현재 저장소에는 실계좌 자동주문을 실행하는 명령이 없습니다. 실거래 단계는 충분한 과거 데이터와 가상매매 결과를 확인하고, 체결·중복 주문·장 종료 실패 대응을 검증한 뒤 진행합니다.

## 시작

Python 3.11 이상이 필요합니다. 외부 Python 패키지는 필요하지 않습니다.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
surge-pilot demo --output reports/private/demo.json
surge-pilot demo-live --output docs/live-report.json
python3 -m http.server 8000 --directory docs
```

`http://localhost:8000`에서 화면을 확인할 수 있습니다. 두 화면의 **로컬 보고서 열기**로 `reports/private/result.json` 또는 `paper.json`을 선택하면 GitHub에 올리지 않고도 자신의 결과를 볼 수 있습니다.

### 실제 과거 데이터 수집

#### 최근 30거래일, 두 단계 검증

이미 발급받은 토스 API 키 파일은 로컬에서만 읽을 수 있습니다. 아래 명령의 경로를 자신의 파일 위치로 바꾸세요. 키와 원본 시세는 `data/private/`, 상세 보고서는 `reports/private/`에만 저장하며 GitHub에 올리지 않습니다.

```bash
python3 -m surgepilot.scan daily --credentials-file '/path/to/toss-api-key' --days 30
python3 -m surgepilot.scan summarize
python3 -m surgepilot.scan minute --credentials-file '/path/to/toss-api-key' --phase winners
python3 -m surgepilot.scan report --phase winners --output reports/private/winners.json
python3 -m surgepilot.scan study --phase winners --output reports/private/winners-liquidity.json
python3 -m surgepilot.scan minute --credentials-file '/path/to/toss-api-key' --phase top_prior --top 100
python3 -m surgepilot.scan report --phase top_prior --top 100 --output reports/private/top100.json
python3 -m surgepilot.scan study --phase top_prior --top 100 --output reports/private/top100-liquidity.json
python3 -m surgepilot.scan cost-study --top 100 --output reports/private/top100-costs.json
python3 -m surgepilot.scan publish --output docs/study-summary.json
```

첫 단계의 50% 급등주는 당일 고가가 전일 종가 또는 당일 시가보다 50% 이상 오른 종목입니다. **사후 선정**이므로 신호가 급등 전에 나왔는지 확인하는 용도입니다. 두 번째 단계는 각 거래일 **전일** 거래대금 추정치 상위 100종목으로 구성합니다. 당일 전체 거래대금 순위는 장 마감 후에야 확정되므로 매수 가능한 후보 선정에 쓰지 않습니다. 추정치는 일봉 거래량 × `(고가 + 저가 + 종가) / 3`입니다. 정확한 체결 거래대금이 아닙니다.

`study`는 신호 시점까지의 누적 분봉 거래대금 추정치가 0, 10만, 50만, 100만, 500만 달러일 때를 비교합니다. 조건을 높일수록 가짜 신호를 줄이는지, 첫 진입이 얼마나 늦어지는지 함께 확인합니다. `cost-study`는 편도 체결 불리함 15/50/100 bp를 비교합니다. 분봉 누락률과 장 시작·종료 분봉의 확보 여부를 먼저 확인한 뒤 성과를 해석하세요. `publish`는 시세·종목별 거래를 제외한 집계 결과만 Pages용 JSON으로 만듭니다.

이 30일 검증은 탐색 단계입니다. 매개변수를 고른 뒤에는 중복되지 않는 과거 100거래일 이상에서 재검증하고, 상위 200종목 및 거래대금·가격대가 비슷한 비급등 종목 표본도 비교해야 합니다. 동일한 30일에 매개변수를 맞추고 그 기간의 수익률을 최종 성능으로 발표해서는 안 됩니다. 급등 사례의 액면병합·분할을 제거하고, 급등주 호가 간격·잔량과 거래정지 가능성도 별도로 확인해야 합니다.

#### 개별 종목 수집

[토스증권 공식 Open API](https://openapi.tossinvest.com/openapi-docs/latest/openapi.json)에서 발급한 `client_id`, `client_secret`을 환경 변수로 설정합니다. 키를 파일이나 GitHub Pages에 넣지 마세요.

```bash
export TOSS_CLIENT_ID='...'
export TOSS_CLIENT_SECRET='...'
surge-pilot collect --symbols AAPL,MSFT --since '2026-09-01T00:00:00-04:00' --output data/private/candles.csv
surge-pilot universe --output data/private/us-universe.json
surge-pilot backtest --data data/private/candles.csv --output reports/private/backtest.json
surge-pilot research --data data/private/candles.csv --output reports/private/research.json
```

CSV 필드: `timestamp,symbol,open,high,low,close,volume`. 시간에는 반드시 UTC 오프셋을 포함해야 합니다. `research`는 최소 4거래일을 요구하지만, 신뢰할 만한 판단에는 훨씬 긴 기간, 많은 종목과 여러 시장 환경이 필요합니다. 이번 30거래일의 분봉과 AAPL 표본의 약 100거래일 전 분봉 제공은 확인했으며, 전체 종목에서 같은 이력이 제공되는지는 별도 확인해야 합니다. 필요한 과거 데이터가 모자라면 동일 형식의 별도 데이터 소스를 사용합니다.

`universe`는 토스에서 거래 가능한 미국 보통주 목록을 저장합니다. `collect --symbols-file data/private/us-universe.json ...`으로 전체 목록의 분봉 수집을 시도할 수 있지만, 종목 수가 많아 호출 한도와 수집 시간이 커집니다. 초기에는 일부 종목으로 데이터 제공 범위와 수집 속도를 확인하세요.

### 실시간 가상매매

```bash
surge-pilot paper --cash 10000 --output reports/private/paper.json
```

미국 정규장에 컴퓨터와 프로세스를 계속 켜 두어야 합니다. 토스의 **시장 거래량 상위 100개 + 당일 상승률 상위 100개**를 후보로 가져와 10초마다 시세를 확인합니다. 시장 전체를 빠짐없이 감시하지 않으므로 급등주를 놓칠 수 있습니다. 가상 체결은 관측 가격에 설정한 불리한 가격 조정과 수수료를 더한 계산입니다. 실제 주문의 체결을 보장하지 않습니다.

같은 컴퓨터의 Chrome 또는 Edge에서 `http://localhost:8000/live.html`을 열고 **로컬 파일 연결**로 `reports/private/paper.json`을 선택하면 15초마다 바뀐 파일을 다시 읽습니다. 파일 자동 갱신 API가 없는 브라우저에서는 **보고서 1회 열기**를 사용하세요. 이 화면은 로컬 가상매매 테스트용이며 Pages에는 배포되지 않습니다.

### 매개변수

기본값은 5분 상승률 8%, 고점 대비 7%, 매수가 대비 5% 손절입니다. `backtest`와 `paper`에 `--lookback`, `--rise`, `--trail`, `--stop`, `--fee-bps`, `--slippage-bps`를 넘길 수 있습니다. 한 거래일의 목표 매수 규모는 시작 금액 대비 40%, 40%, 20%이며, 하루 최대 3회 진입합니다. 같은 종목은 당일 재진입하지 않습니다. 진입은 정규장 종료 20분 전부터 중단하고 5분 전에 청산을 시도합니다.

## 보고서와 Pages

`docs/report.json`은 과거 시세 백테스트의 **공개 집계 보고서**입니다. 종목별 신호·거래와 분봉 시세는 포함하지 않습니다. `docs/live-report.json`은 로컬 장중 시제품의 합성 예제이며 Pages 배포에서 제외합니다. 개인 계좌, 보유 종목, 상세 거래가 담긴 JSON은 `reports/private/`에 두고 웹 페이지의 파일 열기로 확인하세요. 실데이터 상세 보고서를 `docs/`에 넣고 푸시하면 누구나 볼 수 있습니다.

`main` 브랜치에 푸시하면 `.github/workflows/pages.yml`이 `docs/`의 연구 화면 파일만 GitHub Pages에 배포합니다. 테스트는 로컬에서만 실행합니다. 저장소 설정의 **Pages → Build and deployment → Source**는 **GitHub Actions**를 사용합니다.

향후 실제 서비스에서는 Vercel의 서버 측 API와 Supabase 저장소를 통해 장중 보고서를 전달할 계획입니다. 브라우저에 토스 API 키를 노출하지 않고, 개인 계좌 데이터에 인증과 행 단위 접근 제어를 적용하는 것이 연결 전제입니다. 현재의 Pages 화면과 로컬 브리지는 가상매매 검증 단계에 한정됩니다. 데이터 경로와 이전 계획은 [architecture.md](docs/architecture.md)에 정리했습니다.

## 검증 범위와 주의점

- 50% 급등 여부는 검증용 사후 지표이며 매수 신호에 사용하지 않습니다.
- 학습/별도 검증은 날짜 순서로 분리합니다. 무작위 표본 결과가 좋아도 전체 시장에서 안전하다는 뜻은 아닙니다.
- 분봉의 고가와 저가 발생 순서는 알 수 없습니다. 백테스트는 현재 봉에서 새로 형성된 고점을 다음 봉 이후 손절 기준에 반영합니다.
- 수수료·체결 불리함은 가정입니다. 호가 잔량, 거래 정지, 급등주의 실제 체결률은 검증하지 못합니다.
- 데이터에 장 마감 전 분봉이 없으면 `missing_cutoff_bar`로 표시합니다. 이 경우 당일 청산이 가능했음을 입증하지 못합니다.
- 거래 시간은 `America/New_York`으로 해석합니다. 실시간 세션은 토스 시장 일정 API의 정규장 시간을 사용합니다.

테스트: `python3 -m unittest discover -s tests -v`
