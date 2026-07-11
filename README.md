# 훈련일지 자동 메일 발송

Notion "훈련일지" 페이지 하위에 매일 생성되는 당일 페이지를 PDF로 변환해,
매일 22:10(KST)에 이메일로 자동 발송한다.

- 보내는 사람: studyhyunuk@gmail.com
- 받는 사람: it.sanghun.yoo@gmail.com
- 제목: `yyyy MM dd 김현욱 훈련일지`
- 첨부파일: `yyyy MM dd 훈련일지 NN일차.pdf` (PDF만 첨부, 본문 없음)

## 1. 준비물

### 1) Notion Integration
1. https://www.notion.so/my-integrations 에서 Internal Integration 생성 (이미 있다면 생략)
2. 발급된 Secret을 `.env`의 `NOTION_TOKEN`에 입력
3. **중요**: Notion에서 "훈련일지" 페이지로 이동 → 우측 상단 `...` → `연결 추가(Connections)` → 방금 만든 integration 선택
   - 하위 페이지들은 상위 "훈련일지" 페이지 연결을 그대로 상속받으므로 상위 페이지 한 곳만 연결하면 된다.
   - 연결하지 않으면 API가 404를 반환한다.

### 2) Gmail 앱 비밀번호
1. studyhyunuk@gmail.com 계정에 [2단계 인증](https://myaccount.google.com/security) 활성화
2. https://myaccount.google.com/apppasswords 에서 앱 비밀번호 생성 (앱 이름 예: `training-log-mailer`)
3. 생성된 16자리 비밀번호를 `.env`의 `GMAIL_APP_PASSWORD`에 공백 없이 입력

### 3) .env 파일
```bash
cp .env.example .env
# .env 파일을 열어 NOTION_TOKEN, GMAIL_APP_PASSWORD 값을 채운다
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

## 3. 매일 22:10 자동 실행 (cron)

서버 시간대를 한국시간(KST)으로 맞추거나, cron에 UTC 기준 시각(13:10 UTC)을 사용한다.
(공유 서버라 시스템 시간대를 바꾸지 않는 게 안전할 때는 UTC 기준으로 등록한다.)

```bash
crontab -e
```
다음 줄 추가:
```
10 13 * * * cd /home/<user>/TrainingLog && /home/<user>/TrainingLog/.venv/bin/python send_daily_report.py >> /home/<user>/TrainingLog/logs/cron.log 2>&1
```

## 4. 오류 처리

- 당일 날짜의 하위 페이지를 찾지 못하거나, Notion/Gmail 오류가 발생하면 `studyhyunuk@gmail.com` 앞으로
  `[훈련일지 자동발송 오류]` 제목의 알림 메일이 자동 발송된다.
- 실행 로그는 `logs/app.log` 에 누적 기록된다 (최대 1MB x 3개 롤링). cron 실행 자체의 표준출력/에러는 `logs/cron.log`에 쌓인다.

## 5. 실제 배포 현황

- 배포 서버: 상시 켜져 있는 Ubuntu 24.04 클라우드 VM의 `~/TrainingLog`에 배치 (다른 개인 프로젝트와 같은 서버를 공유해서 쓰는 중)
- 서버 시간대는 `Etc/UTC` 그대로 두고, cron은 `10 13 * * *`(UTC 13:10 = KST 22:10)로 등록했다
- 한글 폰트는 `fonts-nanum` 패키지의 `NanumBarunGothic`을 사용한다 (Noto Sans CJK의 .ttc는 OpenType/CFF 윤곽선이라 reportlab에서 열리지 않아 제외함)
- 코드 수정 후 재배포:
  ```bash
  scp send_daily_report.py <user>@<host>:~/TrainingLog/
  ssh <user>@<host> "cd ~/TrainingLog && .venv/bin/python send_daily_report.py --dry-run"
  ```
  (실제 접속 정보는 로컬 SSH 설정을 참고)
