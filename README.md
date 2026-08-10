# 도지 스윙 시스템

일봉 도지 → 다음날 장중 돌파 진입 전략의 파라미터 검증 도구.

---

## PC에서 최초 세팅 (10분, 한 번만)

### 1. 파일 올리기
저장소 페이지 → **Add file** → **Upload files** → 아래 파일 드래그 → Commit

```
backtest.py
params.yml
requirements.txt
README.md
.github/workflows/backtest.yml
```

### 2. 텔레그램 봇 만들기
1. 텔레그램에서 **@BotFather** → `/newbot` → 이름 입력 → **토큰** 복사
2. 만든 봇과 대화 시작 (아무 메시지 전송)
3. 브라우저에서 `https://api.telegram.org/bot<토큰>/getUpdates` 접속 → `"chat":{"id":숫자}` 확인

### 3. Secrets 등록
저장소 → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| Name | Value |
|---|---|
| `TELEGRAM_TOKEN` | BotFather 토큰 |
| `TELEGRAM_CHAT_ID` | 위에서 찾은 숫자 |

### 4. 실행
**Actions** → **도지 백테스트** → **Run workflow** → 기간 12, 종목 500 → 실행

---

## 폰에서 하는 일 (이후 계속)

### 파라미터 수정
GitHub 앱 → 저장소 → **코드** → `params.yml` → 연필 아이콘 → 숫자 수정 → 커밋

`backtest.py`는 건드릴 필요 없습니다. 모든 조건이 `params.yml`에 있습니다.

### 재실행
GitHub 앱 → **작업(Actions)** → **도지 백테스트** → **Run workflow**

### 결과 확인
- **텔레그램**으로 요약 자동 수신
- 상세는 앱에서 `results/signals.csv` 열람 (자동 커밋됨)

---

## 결과 읽는 법

```
손절폭 2% 이내      xxx건
  일평균            x.x건   <- 이 값만 보세요
```

| 일평균 | 조치 |
|---|---|
| 0 ~ 1건 | 너무 조임 → `BODY_MAX_PCT` 올리거나 `VOL_MULT` 낮추기 |
| **3 ~ 5건** | **적정** |
| 10건 이상 | 너무 느슨 → `BODY_MAX_PCT` 낮추거나 `FALL_PCT` 올리기 |

민감도 표에 조합별 일평균이 나오니 거기서 3~5 나오는 칸을 고르면 됩니다.

**한 번에 하나씩만 바꾸세요.** 두 개를 동시에 바꾸면 뭐가 효과였는지 알 수 없습니다.

---

## 한계

- 일봉 데이터라 5분봉 돌파를 근사 (다음날 고가 ≥ 진입가 = 돌파로 간주). 실제보다 낙관적
- 같은 날 손절·목표 동시 도달 시 손절 처리 (보수적)
- 슬리피지·거래세 미반영
- **승률 최적화용이 아니라 신호 개수 확인용.** 표본 30건 미만이면 판단 보류
