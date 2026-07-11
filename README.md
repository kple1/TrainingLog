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

```bash
timedatectl set-timezone Asia/Seoul   # 서버 시간대를 KST로 변경(권장)
crontab -e
```
다음 줄 추가:
```
10 22 * * * cd /home/ubuntu/TrainingLog && /home/ubuntu/TrainingLog/.venv/bin/python send_daily_report.py >> logs/cron.log 2>&1
```

## 4. 오류 처리

- 당일 날짜의 하위 페이지를 찾지 못하거나, Notion/Gmail 오류가 발생하면 `studyhyunuk@gmail.com` 앞으로
  `[훈련일지 자동발송 오류]` 제목의 알림 메일이 자동 발송된다.
- 실행 로그는 `logs/app.log` 에 누적 기록된다 (최대 1MB x 3개 롤링).

## 5. Oracle Cloud (OCI) Always Free VM에 배포하기

PC가 꺼져 있어도 매일 정확한 시각에 실행되도록, 상시 켜져 있는 Oracle Cloud 무료 VM에 배포하는 것을 권장한다.

1. https://cloud.oracle.com 에서 계정 생성 (신용카드 인증 필요, 무료 티어는 과금되지 않음) — 본인이 직접 진행
2. 콘솔 → Compute → Instances → **Create Instance**
   - Image: **Canonical Ubuntu 22.04** (Always Free 대상)
   - Shape: **VM.Standard.E2.1.Micro** 또는 Ampere A1 (Always Free eligible로 표시된 것 선택)
   - Add SSH keys: 본인 PC의 공개키(`~/.ssh/id_ed25519.pub` 등)를 업로드하거나 새로 생성
3. 생성 후 인스턴스의 **Public IP**를 확인
4. VCN(가상 네트워크) → Security List에서 필요하면 22번 포트(SSH)가 열려 있는지 확인 (기본값으로 보통 열려 있음)
5. 아래 정보를 알려주면 이어서 배포를 진행한다:
   - Public IP
   - SSH 사용자명 (Ubuntu 이미지는 보통 `ubuntu`)
   - 접속에 사용할 SSH 키 (기존에 만들어둔 키를 그대로 써도 되고, 새로 만들어도 된다)
