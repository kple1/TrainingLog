"""
Notion "훈련일지" 하위의 오늘 날짜 페이지를 PDF로 변환해 이메일로 발송한다.
cron에서 매일 22:10(KST)에 실행되는 것을 전제로 한다.
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import smtplib
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import date, datetime
from email.message import EmailMessage
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable,
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

NOTION_VERSION = "2022-06-28"
NOTION_API_BASE = "https://api.notion.com/v1"

KST = ZoneInfo("Asia/Seoul")


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"환경변수 {name} 이(가) 설정되지 않았습니다. .env 파일을 확인하세요.")
    return value


NOTION_TOKEN = require_env("NOTION_TOKEN")
NOTION_PARENT_PAGE_ID = os.environ.get(
    "NOTION_PARENT_PAGE_ID", "2fb21ce8524480c8a52cc4e003e2dcdd"
)
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "studyhyunuk@gmail.com")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", "it.sanghun.yoo@gmail.com")
SENDER_DISPLAY_NAME = os.environ.get("SENDER_DISPLAY_NAME", "김현욱")

OUTPUT_DIR = BASE_DIR / "output"
LOG_DIR = BASE_DIR / "logs"


# --------------------------------------------------------------------------
# 로깅
# --------------------------------------------------------------------------
def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    logger = logging.getLogger("training_log_mailer")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = RotatingFileHandler(
        LOG_DIR / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)
    return logger


log = setup_logging()


# --------------------------------------------------------------------------
# Notion API
# --------------------------------------------------------------------------
def notion_request(method: str, path: str, **kwargs) -> dict:
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
    }
    url = f"{NOTION_API_BASE}{path}"
    for attempt in range(5):
        resp = requests.request(method, url, headers=headers, timeout=30, **kwargs)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "2"))
            log.warning("Notion API rate limited, %s초 대기", wait)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()
    raise RuntimeError("unreachable")


def list_children(block_id: str) -> list[dict]:
    results: list[dict] = []
    cursor = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        data = notion_request("GET", f"/blocks/{block_id}/children", params=params)
        results.extend(data["results"])
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return results


DATE_TITLE_RE = None


def _date_title_re():
    import re

    global DATE_TITLE_RE
    if DATE_TITLE_RE is None:
        DATE_TITLE_RE = re.compile(r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.?")
    return DATE_TITLE_RE


def parse_daily_title(title: str) -> tuple[date, str] | None:
    """'2026. 07. 10.\n훈련일지 131일차' 형태의 제목에서 날짜와 나머지 텍스트를 분리한다."""
    m = _date_title_re().search(title)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        parsed_date = date(y, mo, d)
    except ValueError:
        return None
    remainder = title[m.end() :].lstrip("\n ").strip()
    if not remainder:
        lines = [l.strip() for l in title.split("\n") if l.strip()]
        remainder = lines[-1] if lines else ""
    return parsed_date, remainder


def find_daily_page(parent_id: str, target_date: date) -> tuple[str, str] | None:
    for block in list_children(parent_id):
        if block["type"] != "child_page":
            continue
        title = block["child_page"]["title"]
        parsed = parse_daily_title(title)
        if parsed and parsed[0] == target_date:
            return block["id"], parsed[1]
    return None


# --------------------------------------------------------------------------
# 폰트 / 스타일
# --------------------------------------------------------------------------
# (regular, bold, subfontIndex)
# 참고: Noto Sans CJK(.ttc)는 OpenType/CFF 윤곽선이라 reportlab이 열지 못해 후보에서 제외했다.
# NanumBarunGothic은 fonts-nanum 패키지에 포함된, NanumGothic보다 각지고 문서용으로 무난한 서체.
FONT_CANDIDATES = [
    ("/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf", "/usr/share/fonts/truetype/nanum/NanumBarunGothicBold.ttf", None),
    ("/usr/share/fonts/truetype/nanum/NanumGothic.ttf", "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf", None),
    ("C:/Windows/Fonts/malgun.ttf", "C:/Windows/Fonts/malgunbd.ttf", None),
]


def register_fonts() -> None:
    regular_env = os.environ.get("FONT_REGULAR")
    bold_env = os.environ.get("FONT_BOLD")
    candidates = ([(regular_env, bold_env, None)] if regular_env else []) + FONT_CANDIDATES
    for regular, bold, subfont_index in candidates:
        if not (regular and Path(regular).exists()):
            continue
        bold_path = bold if (bold and Path(bold).exists()) else regular
        try:
            kwargs = {"subfontIndex": subfont_index} if subfont_index is not None else {}
            pdfmetrics.registerFont(TTFont("Korean", regular, **kwargs))
            pdfmetrics.registerFont(TTFont("Korean-Bold", bold_path, **kwargs))
        except Exception:
            log.warning("폰트 등록 실패, 다음 후보로 넘어갑니다: %s", regular, exc_info=True)
            continue
        log.info("폰트 등록: %s / %s (subfontIndex=%s)", regular, bold_path, subfont_index)
        return
    raise RuntimeError(
        "한글 폰트를 찾지 못했습니다. Ubuntu는 `sudo apt install fonts-noto-cjk` 후 재시도하세요."
    )


def build_styles() -> dict[str, ParagraphStyle]:
    return {
        "h1": ParagraphStyle("KH1", fontName="Korean-Bold", fontSize=20, leading=26, spaceAfter=4),
        "h2_sub": ParagraphStyle("KH2Sub", fontName="Korean", fontSize=13, leading=18, spaceAfter=14, textColor=colors.grey),
        "h2": ParagraphStyle("KH2", fontName="Korean-Bold", fontSize=15, leading=21, spaceBefore=16, spaceAfter=8),
        "h3": ParagraphStyle("KH3", fontName="Korean-Bold", fontSize=13, leading=19, spaceBefore=12, spaceAfter=5),
        "base": ParagraphStyle("KBase", fontName="Korean", fontSize=11, leading=17),
        "bullet": ParagraphStyle("KBullet", fontName="Korean", fontSize=11, leading=17, leftIndent=14),
        "quote": ParagraphStyle("KQuote", fontName="Korean", fontSize=11, leading=17, leftIndent=14, textColor=colors.grey),
        "cell": ParagraphStyle("KCell", fontName="Korean", fontSize=10.5, leading=15),
        "cell_bold": ParagraphStyle("KCellBold", fontName="Korean-Bold", fontSize=10.5, leading=15),
        "code": ParagraphStyle("KCode", fontName="Courier", fontSize=9, leading=13, backColor=colors.whitesmoke, leftIndent=8),
    }


def indented(style: ParagraphStyle, level: int) -> ParagraphStyle:
    if level <= 0:
        return style
    return ParagraphStyle(
        f"{style.name}_L{level}", parent=style, leftIndent=(style.leftIndent or 0) + level * 12
    )


# --------------------------------------------------------------------------
# 리치 텍스트 / 블록 -> PDF 요소 변환
# --------------------------------------------------------------------------
def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def rich_text_markup(rich_text_list: list[dict]) -> str:
    parts = []
    for rt in rich_text_list or []:
        text = esc(rt.get("plain_text", "")).replace("\n", "<br/>")
        if not text:
            continue
        ann = rt.get("annotations", {})
        if ann.get("code"):
            text = f'<font face="Courier">{text}</font>'
        if ann.get("bold"):
            text = f"<b>{text}</b>"
        if ann.get("italic"):
            text = f"<i>{text}</i>"
        if ann.get("strikethrough"):
            text = f"<strike>{text}</strike>"
        href = rt.get("href")
        if href:
            text = f'<link href="{esc(href)}"><u>{text}</u></link>'
        parts.append(text)
    return "".join(parts)


def build_image_flowable(image_data: dict, max_width: float) -> Image | None:
    file_info = image_data.get("file") or image_data.get("external")
    url = file_info.get("url") if file_info else None
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        from PIL import Image as PILImage

        pil_img = PILImage.open(io.BytesIO(resp.content))
        width, height = pil_img.size
        scale = min(max_width / width, 1.0)
        return Image(io.BytesIO(resp.content), width=width * scale, height=height * scale)
    except Exception:
        log.warning("이미지 다운로드 실패: %s", url, exc_info=True)
        return None


def build_table_flowable(table_block: dict, styles: dict, max_width: float) -> Table | None:
    rows = list_children(table_block["id"])
    has_col_header = table_block["table"].get("has_column_header", False)
    has_row_header = table_block["table"].get("has_row_header", False)
    data = []
    col_weight: list[int] = []
    for r_idx, row in enumerate(rows):
        cells = row["table_row"]["cells"]
        row_data = []
        for c_idx, cell in enumerate(cells):
            is_header = (r_idx == 0 and has_col_header) or (c_idx == 0 and has_row_header)
            style = styles["cell_bold"] if is_header else styles["cell"]
            row_data.append(Paragraph(rich_text_markup(cell), style))
            # 셀 내 가장 긴 줄의 길이를 기준으로 컬럼별 비중을 잡는다 (긴 줄바꿈 텍스트가 폭을 과도하게 넓히지 않도록).
            cell_text = "".join(rt.get("plain_text", "") for rt in cell)
            longest_line = max((len(line) for line in cell_text.split("\n")), default=1) or 1
            if c_idx >= len(col_weight):
                col_weight.append(longest_line)
            else:
                col_weight[c_idx] = max(col_weight[c_idx], longest_line)
        data.append(row_data)
    if not data:
        return None
    col_count = len(data[0])
    min_width = 20 * mm
    total_weight = sum(col_weight) or col_count
    raw_widths = [max(min_width, max_width * (w / total_weight)) for w in col_weight]
    scale = max_width / sum(raw_widths)
    col_widths = [w * scale for w in raw_widths]
    table = Table(data, colWidths=col_widths)
    style_cmds = [
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    if has_col_header:
        style_cmds.append(("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke))
    table.setStyle(TableStyle(style_cmds))
    return table


def blocks_to_flowables(blocks: list[dict], styles: dict, max_width: float, level: int = 0) -> list:
    flow = []
    num_counter = 0
    for block in blocks:
        btype = block["type"]
        data = block.get(btype, {})
        if btype != "numbered_list_item":
            num_counter = 0

        if btype == "heading_1":
            flow.append(Paragraph(rich_text_markup(data.get("rich_text")), indented(styles["h2"], level)))
        elif btype == "heading_2":
            flow.append(Paragraph(rich_text_markup(data.get("rich_text")), indented(styles["h2"], level)))
        elif btype == "heading_3":
            flow.append(Paragraph(rich_text_markup(data.get("rich_text")), indented(styles["h3"], level)))
        elif btype == "divider":
            flow.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey, spaceBefore=4, spaceAfter=8))
        elif btype == "paragraph":
            text = rich_text_markup(data.get("rich_text"))
            flow.append(Paragraph(text, indented(styles["base"], level)) if text else Spacer(1, 6))
        elif btype == "bulleted_list_item":
            text = rich_text_markup(data.get("rich_text"))
            flow.append(Paragraph(f"• {text}", indented(styles["bullet"], level)))
        elif btype == "numbered_list_item":
            num_counter += 1
            text = rich_text_markup(data.get("rich_text"))
            flow.append(Paragraph(f"{num_counter}. {text}", indented(styles["bullet"], level)))
        elif btype == "to_do":
            box = "☑" if data.get("checked") else "☐"
            text = rich_text_markup(data.get("rich_text"))
            flow.append(Paragraph(f"{box} {text}", indented(styles["bullet"], level)))
        elif btype == "toggle":
            text = rich_text_markup(data.get("rich_text"))
            flow.append(Paragraph(f"▶ {text}", indented(styles["bullet"], level)))
        elif btype == "quote":
            text = rich_text_markup(data.get("rich_text"))
            flow.append(Paragraph(text, indented(styles["quote"], level)))
        elif btype == "callout":
            icon = data.get("icon") or {}
            emoji = icon.get("emoji", "💡")
            text = rich_text_markup(data.get("rich_text"))
            flow.append(Paragraph(f"{emoji} {text}", indented(styles["quote"], level)))
        elif btype == "code":
            text = "".join(rt.get("plain_text", "") for rt in data.get("rich_text", [])).replace("\n", "<br/>")
            flow.append(Paragraph(esc(text), indented(styles["code"], level)))
        elif btype == "image":
            img = build_image_flowable(data, max_width - level * 12)
            if img:
                flow.append(img)
        elif btype == "table":
            table = build_table_flowable(block, styles, max_width - level * 12)
            if table:
                flow.append(table)
                flow.append(Spacer(1, 8))
        elif btype == "child_page":
            flow.append(Paragraph(f"[하위 페이지: {esc(data.get('title', ''))}]", indented(styles["base"], level)))
        elif btype in ("column_list", "column", "synced_block"):
            pass
        else:
            text = "".join(rt.get("plain_text", "") for rt in data.get("rich_text", []) if isinstance(data, dict))
            if text:
                flow.append(Paragraph(esc(text), indented(styles["base"], level)))

        if block.get("has_children") and btype not in ("table", "child_page"):
            children = list_children(block["id"])
            flow.extend(blocks_to_flowables(children, styles, max_width, level + 1))
    return flow


def build_pdf(date_line: str, title_line: str, blocks: list[dict], out_path: Path) -> None:
    register_fonts()
    styles = build_styles()
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
    )
    max_width = doc.width
    story = [
        Paragraph(esc(date_line), styles["h1"]),
        Paragraph(esc(title_line), styles["h2_sub"]),
    ]
    story.extend(blocks_to_flowables(blocks, styles, max_width))
    doc.build(story)


# --------------------------------------------------------------------------
# 이메일 발송
# --------------------------------------------------------------------------
def send_email(subject: str, attachment_path: Path, attachment_name: str) -> None:
    if not GMAIL_APP_PASSWORD:
        raise RuntimeError("환경변수 GMAIL_APP_PASSWORD 이(가) 설정되지 않았습니다. .env 파일을 확인하세요.")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = RECIPIENT_EMAIL
    msg.set_content("")
    with open(attachment_path, "rb") as f:
        msg.add_attachment(
            f.read(), maintype="application", subtype="pdf", filename=attachment_name
        )
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)


def send_error_alert(error_text: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = "[훈련일지 자동발송 오류]"
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = GMAIL_ADDRESS
    msg.set_content(error_text[-4000:])
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)


# --------------------------------------------------------------------------
# 메인
# --------------------------------------------------------------------------
@dataclass
class RunResult:
    pdf_path: Path
    subject: str
    attachment_name: str


def run(target_date: date, dry_run: bool) -> RunResult:
    found = find_daily_page(NOTION_PARENT_PAGE_ID, target_date)
    if not found:
        raise RuntimeError(f"{target_date.isoformat()} 날짜의 훈련일지 하위 페이지를 찾지 못했습니다.")
    page_id, day_label = found
    log.info("페이지 발견: %s (%s)", day_label, page_id)

    blocks = list_children(page_id)

    date_str = f"{target_date.year} {target_date.month:02d} {target_date.day:02d}"
    date_line = f"{target_date.year}. {target_date.month:02d}. {target_date.day:02d}."
    attachment_name = f"{date_str} {day_label}.pdf"

    OUTPUT_DIR.mkdir(exist_ok=True)
    pdf_path = OUTPUT_DIR / attachment_name
    build_pdf(date_line, day_label, blocks, pdf_path)
    log.info("PDF 생성 완료: %s", pdf_path)

    subject = f"{date_str} {SENDER_DISPLAY_NAME} 훈련일지"

    if dry_run:
        log.info("[dry-run] 이메일 발송을 건너뜁니다. 제목: %s", subject)
    else:
        send_email(subject, pdf_path, attachment_name)
        log.info("이메일 발송 완료: %s -> %s", GMAIL_ADDRESS, RECIPIENT_EMAIL)

    return RunResult(pdf_path=pdf_path, subject=subject, attachment_name=attachment_name)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="훈련일지 PDF 이메일 발송")
    parser.add_argument("--date", help="대상 날짜 YYYY-MM-DD (기본값: 오늘, KST)")
    parser.add_argument("--dry-run", action="store_true", help="PDF만 생성하고 메일은 보내지 않음")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    target_date = (
        datetime.strptime(args.date, "%Y-%m-%d").date()
        if args.date
        else datetime.now(KST).date()
    )
    run(target_date, dry_run=args.dry_run)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("훈련일지 발송 실패")
        try:
            send_error_alert(traceback.format_exc())
        except Exception:
            log.exception("에러 알림 메일 발송도 실패")
        sys.exit(1)
