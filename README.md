# 훈련일지 자동 메일 발송

Notion "훈련일지" 페이지 하위에 매일 생성되는 당일 페이지를 PDF로 변환해,
매일 22:10(KST)에 이메일로 자동 발송한다.

- 보내는 사람 / 받는 사람 / 표시 이름: `.env`의 `GMAIL_ADDRESS` / `RECIPIENT_EMAIL` / `SENDER_DISPLAY_NAME` 참고 (실제 값은 서버 `.env`에만 있고 저장소에는 없음)
- 제목: `yyyy MM dd <SENDER_DISPLAY_NAME> 훈련일지`
- 첨부파일: `yyyy MM dd 훈련일지 NN일차.pdf` (PDF만 첨부, 본문 없음)

## 1. 준비물

### 1) Notion Integration
1. https://www.notion.so/my-integrations 에서 Internal Integration 생성 (이미 있다면 생략)
2. 발급된 Secret을 `.env`의 `NOTION_TOKEN`에 입력
3. **중요**: Notion에서 "훈련일지" 페이지로 이동 → 우측 상단 `...` → `연결 추가(Connections)` → 방금 만든 integration 선택
   - 하위 페이지들은 상위 "훈련일지" 페이지 연결을 그대로 상속받으므로 상위 페이지 한 곳만 연결하면 된다.
   - 연결하지 않으면 API가 404를 반환한다.

### 2) Gmail 앱 비밀번호
1. 발신용 Gmail 계정에 [2단계 인증](https://myaccount.google.com/security) 활성화
2. https://myaccount.google.com/apppasswords 에서 앱 비밀번호 생성 (앱 이름 예: `training-log-mailer`)
3. 생성된 16자리 비밀번호를 `.env`의 `GMAIL_APP_PASSWORD`에 공백 없이 입력

### 3) .env 파일
```bash
cp .env.example .env
# .env 파일을 열어 NOTION_TOKEN, GMAIL_APP_PASSWORD, GMAIL_ADDRESS, RECIPIENT_EMAIL, SENDER_DISPLAY_NAME 값을 채운다
```

## 2. 로컬/서버 실행

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Ubuntu 서버에서는 한글 폰트가 필요하다:
```bash
sudo apt update && sudo apt install -y fonts-nanum
```

### 테스트 (메일 발송 없이 PDF만 생성)
```bash
python send_daily_report.py --dry-run
# 특정 날짜로 테스트하고 싶을 때
python send_daily_report.py --dry-run --date 2026-07-09
```
생성된 PDF는 `output/` 폴더에서 확인할 수 있다.

### 실제 발송
```bash
python send_daily_report.py
```

## 3. 요일별 발송 시각 / 차단 설정 (schedule.json)

요일마다 다른 시각에 보내거나, 특정 요일은 아예 발송을 막고(blocked) 싶을 때 `schedule.json`을 수정한다.
cron을 다시 등록할 필요 없이 이 파일만 편집하면 바로 적용된다 (다음 5분 이내 반영).

```json
{
  "mon": { "enabled": true,  "time": "22:10" },
  "tue": { "enabled": true,  "time": "22:10" },
  "sat": { "enabled": false, "time": "20:00" }
}
```
- `enabled: false` = 그 요일은 발송하지 않음 (시각 값은 남겨둬도 무시된다)
- `time`은 KST 기준 `HH:MM`
- cron이 5분 간격으로 실행되며 설정된 시각을 지났는지 확인하는 방식이라, 실제 발송은 최대 약 5분 늦게 이루어질 수 있다

### cron 등록

서버 시간대와 무관하게 스크립트 내부에서 KST로 판단하므로, cron 자체는 자주(예: 5분마다) 실행되도록만 등록하면 된다.

```bash
crontab -e
```
다음 줄 추가:
```
*/5 * * * * cd /home/<user>/TrainingLog && /home/<user>/TrainingLog/.venv/bin/python send_daily_report.py >> /home/<user>/TrainingLog/logs/cron.log 2>&1
```

### 수동 실행 / 강제 발송

```bash
python send_daily_report.py --force              # schedule.json 무시하고 오늘자 즉시 발송
python send_daily_report.py --force --date 2026-07-05   # 특정 날짜 재발송
```

## 4. 오류 처리

- 당일 날짜의 하위 페이지를 찾지 못하거나, Notion/Gmail 오류가 발생하면 `GMAIL_ADDRESS`(발신 계정) 앞으로
  `[훈련일지 자동발송 오류]` 제목의 알림 메일이 자동 발송된다.
- 실행 로그는 `logs/app.log` 에 누적 기록된다 (최대 1MB x 3개 롤링). cron 실행 자체의 표준출력/에러는 `logs/cron.log`에 쌓인다.

## 5. AWS Lambda + EventBridge 배포

서버를 24시간 켜두지 않고 발송 시각에만 실행되게 하는 구성. 하루 몇 초만 쓰므로 Lambda 프리티어 안에서 사실상 무료다.
이 구성에서는 **발송 시각/요일을 EventBridge가 관리**하므로 `schedule.json`은 사용하지 않는다.

### 1) 배포 zip 만들기

Lambda 런타임과 같은 Linux x86_64 머신에서 빌드해야 한다 (wheel 호환성).

```bash
sudo apt install -y fonts-nanum zip
bash scripts/build_lambda_package.sh
# -> dist/traininglog-lambda.zip (약 14MB)
```
코드 + 의존성 + 한글 폰트(NanumBarunGothic)가 모두 들어 있어 Lambda에 시스템 폰트가 없어도 동작한다.

### 2) Lambda 함수 생성 (콘솔)

| 항목 | 값 |
| --- | --- |
| 런타임 | Python 3.12 |
| 아키텍처 | x86_64 |
| 핸들러 | `send_daily_report.lambda_handler` |
| 제한 시간 | 60초 (기본 3초로는 부족) |
| 메모리 | 512MB |
| VPC | **연결하지 않음** (Notion API·SMTP 아웃바운드가 필요한데, VPC에 넣으면 NAT 게이트웨이 비용이 발생한다) |

코드는 `dist/traininglog-lambda.zip`을 업로드한다.

환경 변수(구성 → 환경 변수)에 `.env`와 같은 값을 넣는다:
`NOTION_TOKEN`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `RECIPIENT_EMAIL`, `SENDER_DISPLAY_NAME`

### 3) EventBridge Scheduler로 발송 시각 등록

EventBridge **Scheduler**를 쓴다 (기존 EventBridge 규칙은 UTC만 지원하지만, Scheduler는 타임존을 직접 지정할 수 있다).
타임존은 `Asia/Seoul`, 대상은 위 Lambda 함수로 지정한다.

| 요일 | cron 식 |
| --- | --- |
| 월~금 22:10 | `cron(10 22 ? * MON-FRI *)` |
| 토 18:10 | `cron(10 18 ? * SAT *)` |

일요일처럼 보내지 않을 요일은 **스케줄을 만들지 않으면** 된다. 시각을 바꾸려면 해당 스케줄의 cron 식을 수정한다.

### 4) 테스트 / 수동 재발송

Lambda 콘솔의 테스트 이벤트에 아래 JSON을 넣어 호출한다.

```json
{}                              // 오늘자 발송
{"dry_run": true}               // PDF만 생성하고 메일은 보내지 않음
{"date": "2026-07-28"}          // 특정 날짜 재발송
```

실행 로그는 CloudWatch Logs에 남고, 오류가 나면 기존과 동일하게 `GMAIL_ADDRESS` 앞으로 알림 메일이 간다.

> **주의**: 기존 서버의 cron과 Lambda가 동시에 돌면 같은 날 메일이 두 번 간다.
> Lambda 발송이 확인되면 기존 서버에서 `crontab -e`로 해당 줄을 지운다.

## 6. 실제 배포 현황

- 배포 서버: 상시 켜져 있는 Ubuntu 24.04 클라우드 VM의 `~/TrainingLog`에 배치 (다른 개인 프로젝트와 같은 서버를 공유해서 쓰는 중)
- 서버 시간대는 `Etc/UTC` 그대로 두고, cron은 `*/5 * * * *`(5분마다)로 등록해 `schedule.json`의 요일별 시각(KST)을 스크립트가 직접 판단한다
- 한글 폰트는 `fonts-nanum` 패키지의 `NanumBarunGothic`을 사용한다 (Noto Sans CJK의 .ttc는 OpenType/CFF 윤곽선이라 reportlab에서 열리지 않아 제외함)
- 코드 수정 후 재배포:
  ```bash
  scp send_daily_report.py <user>@<host>:~/TrainingLog/
  ssh <user>@<host> "cd ~/TrainingLog && .venv/bin/python send_daily_report.py --dry-run"
  ```
  (실제 접속 정보는 로컬 SSH 설정을 참고)
