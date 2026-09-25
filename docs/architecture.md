# SurgePilot 데이터 경로

## 현재: 로컬 검증 + GitHub Pages

```text
토스 분봉 ──수집──> 비공개 CSV ──> 백테스트/연구 ──> 비공개 JSON ──파일 선택──> Pages 과거 검증 화면
토스 시세 ──후보 스캔──> 로컬 가상매매 ──> 비공개 JSON ──127.0.0.1 읽기──> Pages 장중 화면
                                                   └──파일 선택──> Pages 장중 화면
```

`docs/report.json`과 `docs/live-report.json`은 공개 합성 예제입니다. 개인 데이터는 `data/private/`, `reports/private/`에만 저장합니다. 현재 주문 생성 명령은 제공하지 않습니다.

## 이후: Vercel + Supabase

1. Vercel의 서버 API가 인증된 사용자의 보고서만 읽도록 합니다. 브라우저는 토스 API 키에 접근하지 않습니다.
2. Supabase에는 전략 실행(run), 매수 신호(signal), 가상·실제 주문(order), 체결(fill), 포지션(position), 평가금액 스냅샷(equity snapshot)을 실행 ID와 시장 날짜로 연결해 저장합니다.
3. 사용자별 인증과 행 단위 접근 제어를 먼저 설정한 뒤 개인 데이터 수집을 시작합니다. 공개 Pages에는 개인 데이터용 Supabase 서비스 키를 넣지 않습니다.
4. 장중 화면의 `live-report.json` 입력을 인증된 Vercel API 응답으로 바꿉니다. 보고서 형식(`kind`, `summary`, `positions`, `equity`, `signals`, `trades`)은 유지해 화면 변경을 최소화합니다.
5. 실거래 주문은 독립된 서버 프로세스에서만 실행하고, 주문 ID 멱등성, 체결 상태 확인, 중복 주문 방지, 세션 종료 시 미체결 주문 처리와 비상 정지를 검증한 후 활성화합니다.

GitHub Pages는 두 화면의 검토와 가상매매 테스트에 사용합니다. 빠른 가격 감시와 실제 주문은 Pages나 GitHub Actions에서 실행하지 않습니다.
