"""
Notion "훈련일지" 하위의 오늘 날짜 페이지를 PDF로 변환해 이메일로 발송한다.
cron에서 5~10분 간격으로 실행되며, schedule.json에 정의된 요일별 시각에만 실제로 발송한다.
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import smtplib
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from html import escape
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
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    HRFlowable,
    Image,
    KeepTogether,
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
GMAIL_ADDRESS = require_env("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
RECIPIENT_EMAIL = require_env("RECIPIENT_EMAIL")
SENDER_DISPLAY_NAME = require_env("SENDER_DISPLAY_NAME")

# Lambda에서는 코드 디렉터리(/var/task)가 읽기 전용이라 쓰기는 /tmp에만 가능하다.
IS_LAMBDA = bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
WRITABLE_BASE = Path("/tmp") if IS_LAMBDA else BASE_DIR

OUTPUT_DIR = WRITABLE_BASE / "output"
LOG_DIR = WRITABLE_BASE / "logs"
STATE_DIR = WRITABLE_BASE / "state"
SCHEDULE_FILE = Path(os.environ.get("SCHEDULE_FILE", str(BASE_DIR / "schedule.json")))
# cron이 이 간격(분)으로 실행된다고 가정하고, 목표 시각이 그 창 안에 들어오면 "지금 발송할 시각"으로 판단한다.
CRON_INTERVAL_MINUTES = int(os.environ.get("CRON_INTERVAL_MINUTES", "5"))

WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DEFAULT_SCHEDULE = {day: {"enabled": True, "time": "22:10"} for day in WEEKDAY_KEYS}


# --------------------------------------------------------------------------
# 로깅
# --------------------------------------------------------------------------
def setup_logging() -> logging.Logger:
    logger = logging.getLogger("training_log_mailer")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    # Lambda는 stdout이 그대로 CloudWatch Logs로 가고 /tmp는 호출 간 보존되지 않으므로 파일 로깅을 하지 않는다.
    if not IS_LAMBDA:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
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
def notion_request(
    method: str, path: str, *, timeout: float | tuple[float, float] = 30,
    attempts: int = 5, **kwargs,
) -> dict:
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
    }
    url = f"{NOTION_API_BASE}{path}"
    for attempt in range(attempts):
        resp = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
        if resp.status_code == 429 and attempt + 1 < attempts:
            wait = int(resp.headers.get("Retry-After", "2"))
            log.warning("Notion API rate limited, %s초 대기", wait)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
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


def get_page_icon(page_id: str) -> dict | None:
    try:
        page = notion_request("GET", f"/pages/{page_id}", timeout=(3, 5), attempts=1)
    except requests.RequestException as exc:
        log.warning("Notion 페이지 아이콘을 조회하지 못했습니다 (%s).", type(exc).__name__)
        return None
    return page.get("icon")


# --------------------------------------------------------------------------
# 폰트 / 스타일
# --------------------------------------------------------------------------
# (regular, bold, subfontIndex)
# 참고: Noto Sans CJK(.ttc)는 OpenType/CFF 윤곽선이라 reportlab이 열지 못해 후보에서 제외했다.
# NanumBarunGothic은 fonts-nanum 패키지에 포함된, NanumGothic보다 각지고 문서용으로 무난한 서체.
_BUNDLED_FONT_DIR = BASE_DIR / "fonts"
FONT_CANDIDATES = [
    # 배포 패키지에 동봉된 폰트 (Lambda처럼 시스템 폰트가 없는 환경용). 없으면 아래 시스템 경로로 넘어간다.
    (str(_BUNDLED_FONT_DIR / "NanumBarunGothic.ttf"), str(_BUNDLED_FONT_DIR / "NanumBarunGothicBold.ttf"), None),
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
            # <b>/<i> 태그가 커스텀 TTF에서 동작하려면 폰트 패밀리 등록이 필요하다.
            # 별도의 이탤릭 한글 서체는 없어서 이탤릭은 일반체로 대체한다 (깨지는 것보다 낫다).
            pdfmetrics.registerFontFamily(
                "Korean", normal="Korean", bold="Korean-Bold", italic="Korean", boldItalic="Korean-Bold"
            )
        except Exception:
            log.warning("폰트 등록 실패, 다음 후보로 넘어갑니다: %s", regular, exc_info=True)
            continue
        log.info("폰트 등록: %s / %s (subfontIndex=%s)", regular, bold_path, subfont_index)
        return
    raise RuntimeError(
        "한글 폰트를 찾지 못했습니다. Ubuntu는 `sudo apt install fonts-noto-cjk` 후 재시도하세요."
    )


def build_styles() -> dict[str, ParagraphStyle]:
    base = ParagraphStyle(
        "Body", fontName="Korean", fontSize=12, leading=18, textColor=colors.black,
        spaceAfter=4,
    )
    heading = ParagraphStyle(
        "Heading", parent=base, fontName="Korean-Bold", keepWithNext=True,
    )
    cell = ParagraphStyle("Cell", parent=base, fontSize=10.5, leading=15.75, spaceAfter=0)
    return {
        "title": ParagraphStyle("Title", parent=heading, fontSize=30, leading=36, spaceAfter=24),
        "heading_1": ParagraphStyle("H1", parent=heading, fontSize=22, leading=28, spaceBefore=17, spaceAfter=6),
        "heading_2": ParagraphStyle("H2", parent=heading, fontSize=18, leading=24, spaceBefore=16, spaceAfter=5),
        "heading_3": ParagraphStyle("H3", parent=heading, fontSize=15, leading=20, spaceBefore=12, spaceAfter=4),
        "base": base,
        "bullet": ParagraphStyle(
            "Bullet", parent=base, bulletFontName="Korean", bulletFontSize=11,
            leftIndent=21, bulletIndent=6, spaceAfter=2,
        ),
        "quote": ParagraphStyle("Quote", parent=base, leftIndent=11, spaceBefore=4, spaceAfter=5),
        "callout": ParagraphStyle(
            "Callout", parent=base, leftIndent=9, rightIndent=9, borderPadding=7,
            backColor=colors.HexColor("#F1F0EF"), spaceBefore=10, spaceAfter=10,
        ),
        "cell": cell,
        "cell_bold": ParagraphStyle("CellBold", parent=cell, fontName="Korean-Bold"),
        "caption": ParagraphStyle(
            "Caption", parent=base, fontSize=9, leading=13, textColor=colors.HexColor("#787774"),
            spaceBefore=4, spaceAfter=7,
        ),
        "code": ParagraphStyle(
            "Code", parent=base, fontSize=9, leading=13, backColor=colors.HexColor("#F7F6F3"),
            leftIndent=9, rightIndent=9, borderPadding=7, spaceBefore=10, spaceAfter=10,
        ),
    }


def indented(style: ParagraphStyle, level: int) -> ParagraphStyle:
    if level <= 0:
        return style
    return ParagraphStyle(
        f"{style.name}_L{level}", parent=style,
        leftIndent=style.leftIndent + level * 12, bulletIndent=style.bulletIndent + level * 12,
    )


# --------------------------------------------------------------------------
# 리치 텍스트 / 블록 -> PDF 요소 변환
# --------------------------------------------------------------------------
def esc(text: str) -> str:
    return escape(text, quote=False)


def _code_line_markup(line: str) -> str:
    """코드 블록 한 줄의 선행 공백/탭을 보존한다.
    Paragraph는 HTML처럼 연속 공백을 한 칸으로 줄이므로, 들여쓰기는 &nbsp;로 바꿔줘야 유지된다."""
    stripped = line.lstrip(" \t")
    leading = line[: len(line) - len(stripped)]
    indent_markup = leading.replace("\t", "&nbsp;" * 4).replace(" ", "&nbsp;")
    return indent_markup + esc(stripped)


# Notion 기본 테마의 텍스트와 강조 배경색에 가까운 색상.
NOTION_COLOR_MAP = {
    "gray": "#787774",
    "brown": "#976D57",
    "orange": "#CC782F",
    "yellow": "#C29343",
    "green": "#548164",
    "blue": "#487CA5",
    "purple": "#8A5A83",
    "pink": "#B54C6D",
    "red": "#C4554D",
}
NOTION_BACKGROUND_MAP = {
    "gray_background": "#F1F0EF",
    "brown_background": "#F4EEEE",
    "orange_background": "#FBECDD",
    "yellow_background": "#FBF3DB",
    "green_background": "#EDF3EC",
    "blue_background": "#E7F3F8",
    "purple_background": "#F6F3F9",
    "pink_background": "#FAF1F5",
    "red_background": "#FDEBEC",
}
TRAINING_TIME_RANGE = re.compile(r"(?:\d{1,2}:\d{2}\s*)?[~∼–-]\s*\d{1,2}:\d{2}")


def rich_text_markup(rich_text_list: list[dict] | None) -> str:
    parts = []
    for rt in rich_text_list or []:
        text = esc(rt.get("plain_text", "")).replace("\n", "<br/>")
        if not text:
            continue
        ann = rt.get("annotations", {})
        if ann.get("bold"):
            text = f"<b>{text}</b>"
        if ann.get("italic"):
            text = f"<i>{text}</i>"
        if ann.get("strikethrough"):
            text = f"<strike>{text}</strike>"
        if ann.get("underline"):
            text = f"<u>{text}</u>"
        color_name = ann.get("color", "default")
        color = NOTION_COLOR_MAP.get(color_name)
        background = NOTION_BACKGROUND_MAP.get(color_name)
        if ann.get("code"):
            color = color or "#EB5757"
            background = background or "#F1F0EF"
        if color:
            text = f'<font color="{color}">{text}</font>'
        if background:
            text = f'<span backColor="{background}">{text}</span>'
        href = rt.get("href")
        if href and href.lower().startswith(("https://", "http://", "mailto:", "tel:", "notion://")):
            label = text if ann.get("underline") else f"<u>{text}</u>"
            text = f'<link href="{escape(href)}">{label}</link>'
        parts.append(text)
    return "".join(parts)


def build_image_flowable(image_data: dict, max_width: float, max_height: float) -> Image | None:
    file_info = image_data.get("file") or image_data.get("external")
    url = file_info.get("url") if file_info else None
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=(3, 15))
        resp.raise_for_status()
        from PIL import Image as PILImage

        with PILImage.open(io.BytesIO(resp.content)) as pil_img:
            width, height = pil_img.size
            pil_img.verify()
        scale = min(max_width / width, max_height / height, 1.0)
        return Image(io.BytesIO(resp.content), width=width * scale, height=height * scale)
    except Exception as exc:
        log.warning("Notion 이미지를 읽지 못했습니다 (%s).", type(exc).__name__)
        return None


def build_page_icon(icon: dict) -> Image | None:
    if (icon.get("emoji") or "").rstrip("\ufe0f") == "🗞":
        asset = BASE_DIR / "assets" / "rolled-newspaper.png"
        if not asset.is_file():
            log.warning("훈련일지 아이콘 파일을 찾지 못했습니다.")
            return None
        image = Image(str(asset), width=42, height=42, kind="proportional")
    else:
        image = build_image_flowable(icon, 42, 42)
    if image:
        image.hAlign = "LEFT"
        image.spaceAfter = 12
    return image


def build_table_flowable(table_block: dict, styles: dict, max_width: float, max_height: float) -> Table | None:
    rows = list_children(table_block["id"])
    has_col_header = table_block["table"].get("has_column_header", False)
    has_row_header = table_block["table"].get("has_row_header", False)
    data = []
    col_weight: list[float] = []
    first_column_text = []
    for r_idx, row in enumerate(rows):
        cells = row["table_row"]["cells"]
        row_data = []
        for c_idx, cell in enumerate(cells):
            is_header = (r_idx == 0 and has_col_header) or (c_idx == 0 and has_row_header)
            style = styles["cell_bold"] if is_header else styles["cell"]
            row_data.append(Paragraph(rich_text_markup(cell), style))
            # Blocks API는 표의 개별 열 너비를 제공하지 않는다.
            cell_text = "".join(rt.get("plain_text", "") for rt in cell)
            if c_idx == 0 and not (r_idx == 0 and has_col_header) and cell_text.strip():
                first_column_text.append(cell_text.strip())
            longest_line = max(
                pdfmetrics.stringWidth(line, style.fontName, style.fontSize)
                for line in cell_text.split("\n")
            )
            if c_idx >= len(col_weight):
                col_weight.append(max(longest_line, 1))
            else:
                col_weight[c_idx] = max(col_weight[c_idx], longest_line)
        data.append(row_data)
    if not col_weight:
        return None
    total_weight = sum(col_weight)
    is_training_schedule = len(col_weight) == 2 and bool(first_column_text) and all(
        TRAINING_TIME_RANGE.fullmatch(text) for text in first_column_text
    )
    if is_training_schedule:
        # 훈련일지 시간표 양식의 열 비율을 사용한다.
        col_widths = [max_width * 0.206, max_width * 0.794]
    else:
        min_width = min(20 * mm, max_width / len(col_weight))
        widths = [max(min_width, max_width * weight / total_weight) for weight in col_weight]
        col_widths = [width * max_width / sum(widths) for width in widths]
    padding = min(6, min(col_widths) * 0.15)
    repeat_rows = 0
    if has_col_header:
        header_height = max(
            cell.wrap(width - 2 * padding, max_height)[1]
            for cell, width in zip(data[0], col_widths)
        )
        # 큰 헤더가 본문 한 줄까지 밀어내면 페이지를 나눌 수 없다.
        repeat_rows = int(header_height + styles["cell"].leading + 12 < max_height)
    table = Table(
        data, colWidths=col_widths, hAlign="LEFT", spaceBefore=4, spaceAfter=8,
        repeatRows=repeat_rows, splitInRow=1,
    )
    style_cmds = [
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E7E7E7")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), padding),
        ("RIGHTPADDING", (0, 0), (-1, -1), padding),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    if is_training_schedule:
        style_cmds.append(("VALIGN", (0, 0), (0, -1), "MIDDLE"))
    header_color = colors.HexColor("#F7F6F3")
    if has_col_header:
        style_cmds.append(("BACKGROUND", (0, 0), (-1, 0), header_color))
    if has_row_header:
        style_cmds.append(("BACKGROUND", (0, 0), (0, -1), header_color))
    table.setStyle(TableStyle(style_cmds))
    return table


class QuoteParagraph(Paragraph):
    def draw(self) -> None:
        self.canv.saveState()
        self.canv.setStrokeColor(self.style.textColor)
        self.canv.setLineWidth(1.5)
        x = self.style.leftIndent - 10
        self.canv.line(x, 0, x, self.height)
        self.canv.restoreState()
        super().draw()


def blocks_to_flowables(
    blocks: list[dict], styles: dict, max_width: float, max_height: float, level: int = 0,
) -> list:
    flow = []
    num_counter = 0
    for block in blocks:
        btype = block["type"]
        data = block.get(btype, {})
        if btype != "numbered_list_item":
            num_counter = 0
        content_width = max_width - level * 12

        if btype.startswith("heading_"):
            style = styles.get(btype, styles["heading_3"])
            flow.append(Paragraph(rich_text_markup(data.get("rich_text")), indented(style, level)))
        elif btype == "divider":
            flow.append(HRFlowable(
                width="100%", thickness=0.5, color=colors.HexColor("#E7E7E7"),
                spaceBefore=6, spaceAfter=11,
            ))
        elif btype == "paragraph":
            text = rich_text_markup(data.get("rich_text"))
            flow.append(Paragraph(text, indented(styles["base"], level)) if text else Spacer(1, 6))
        elif btype in ("bulleted_list_item", "numbered_list_item", "to_do", "toggle"):
            if btype == "numbered_list_item":
                num_counter += 1
                marker = f"{num_counter}."
            elif btype == "to_do":
                glyphs = pdfmetrics.getFont(styles["bullet"].bulletFontName).face.charWidths
                if ord("☑") in glyphs and ord("☐") in glyphs:
                    marker = "☑" if data.get("checked") else "☐"
                else:
                    marker = "[x]" if data.get("checked") else "[ ]"
            else:
                marker = "•" if btype == "bulleted_list_item" else "▶"
            style = indented(styles["bullet"], level)
            marker_width = pdfmetrics.stringWidth(marker, style.bulletFontName, style.bulletFontSize)
            if style.bulletIndent + marker_width + 4 > style.leftIndent:
                style = ParagraphStyle("WideBullet", parent=style, leftIndent=style.bulletIndent + marker_width + 4)
            text = rich_text_markup(data.get("rich_text"))
            flow.append(Paragraph(text, style, bulletText=marker))
        elif btype == "quote":
            text = rich_text_markup(data.get("rich_text"))
            flow.append(QuoteParagraph(text, indented(styles["quote"], level)))
        elif btype == "callout":
            icon = data.get("icon") or {}
            emoji = icon.get("emoji", "")
            text = rich_text_markup(data.get("rich_text"))
            style = styles["callout"]
            background = NOTION_BACKGROUND_MAP.get(data.get("color"))
            if background:
                style = ParagraphStyle("ColoredCallout", parent=style, backColor=colors.HexColor(background))
            font = pdfmetrics.getFont(style.fontName)
            if emoji and all(ord(char) in font.face.charWidths for char in emoji):
                text = f"{esc(emoji)} {text}"
            flow.append(Paragraph(text, indented(style, level)))
        elif btype == "code":
            raw_text = "".join(rt.get("plain_text", "") for rt in data.get("rich_text", []))
            text = "<br/>".join(_code_line_markup(line) for line in raw_text.split("\n"))
            flow.append(Paragraph(text, indented(styles["code"], level)))
        elif btype == "image":
            caption_text = rich_text_markup(data.get("caption"))
            caption = Paragraph(caption_text, indented(styles["caption"], level)) if caption_text else None
            caption_height = caption.wrap(max_width, max_height)[1] + 12 if caption else 0
            image_height = max_height * 0.80 - caption_height
            img = build_image_flowable(data, content_width, max(image_height, max_height / 2))
            if img:
                if caption and img.drawHeight <= image_height:
                    flow.append(KeepTogether([img, caption]))
                    caption = None
                else:
                    flow.append(img)
            else:
                flow.append(Paragraph("[이미지를 불러오지 못했습니다]", indented(styles["caption"], level)))
            if caption:
                flow.append(caption)
        elif btype == "table":
            table = build_table_flowable(block, styles, content_width, max_height)
            if table:
                flow.append(table)
        elif btype == "child_page":
            flow.append(Paragraph(f"[하위 페이지: {esc(data.get('title', ''))}]", indented(styles["base"], level)))
        elif btype in ("column_list", "column", "synced_block"):
            pass
        elif isinstance(data, dict) and data.get("rich_text"):
            text = rich_text_markup(data.get("rich_text"))
            if text:
                flow.append(Paragraph(text, indented(styles["base"], level)))

        if block.get("has_children") and btype not in ("table", "child_page"):
            children = list_children(block["id"])
            child_level = level if btype in ("column_list", "column", "synced_block") else level + 1
            flow.extend(blocks_to_flowables(children, styles, max_width, max_height, child_level))
    return flow


def draw_pdf_footer(canvas: Canvas, doc: SimpleDocTemplate) -> None:
    canvas.saveState()
    canvas.setFont("Korean", 7.5)
    canvas.setFillColor(colors.HexColor("#787774"))
    canvas.drawString(27, 30, " ".join(doc.title.split()))
    canvas.drawRightString(doc.pagesize[0] - 27, 30, str(doc.page))
    canvas.restoreState()


def build_pdf(
    date_line: str, title_line: str, blocks: list[dict], out_path: Path,
    *, icon: dict | None = None,
) -> None:
    register_fonts()
    styles = build_styles()
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        title=f"{date_line} {title_line}",
        topMargin=66,
        bottomMargin=66,
        leftMargin=66,
        rightMargin=65,
    )
    # SimpleDocTemplate 기본 Frame의 각 변에는 6pt 패딩이 있다.
    max_width, max_height = doc.width - 12, doc.height - 12
    title = "<br/>".join(esc(line) for line in [date_line, *title_line.splitlines()])
    page_icon = build_page_icon(icon) if icon else None
    story = [page_icon] if page_icon else []
    story.append(Paragraph(title, styles["title"]))
    story.extend(blocks_to_flowables(blocks, styles, max_width, max_height))
    doc.build(story, onFirstPage=draw_pdf_footer, onLaterPages=draw_pdf_footer)


# --------------------------------------------------------------------------
# 이메일 발송
# --------------------------------------------------------------------------
def send_email(subject: str, attachment_path: Path, attachment_name: str) -> None:
    if not GMAIL_APP_PASSWORD:
        raise RuntimeError("환경변수 GMAIL_APP_PASSWORD 이(가) 설정되지 않았습니다. .env 파일을 확인하세요.")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((SENDER_DISPLAY_NAME, GMAIL_ADDRESS))
    msg["To"] = RECIPIENT_EMAIL
    # 본문이 비어 있고 첨부만 있는 메일은 스팸으로 분류되기 쉽다.
    # Date/Message-ID가 없는 것도 같은 이유라 명시적으로 채운다.
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=GMAIL_ADDRESS.rsplit("@", 1)[-1])
    msg.set_content(f"{subject}입니다.\n\n자세한 내용은 첨부된 PDF를 확인해 주세요.\n")
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
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=GMAIL_ADDRESS.rsplit("@", 1)[-1])
    msg.set_content(error_text[-4000:])
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        smtp.send_message(msg)


# --------------------------------------------------------------------------
# 요일별 스케줄 (schedule.json)
# --------------------------------------------------------------------------
def load_schedule() -> dict:
    if not SCHEDULE_FILE.exists():
        return DEFAULT_SCHEDULE
    try:
        with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        log.warning("schedule.json 파싱 실패, 기본 스케줄(매일 22:10) 사용", exc_info=True)
        return DEFAULT_SCHEDULE
    merged = {day: dict(cfg) for day, cfg in DEFAULT_SCHEDULE.items()}
    for day, cfg in data.items():
        if day in merged and isinstance(cfg, dict):
            merged[day].update(cfg)
    return merged


def is_due_now(schedule: dict, now: datetime) -> tuple[bool, str]:
    """schedule.json 기준으로 '지금'이 발송 대상 시각(±CRON_INTERVAL_MINUTES) 안에 있는지 판단한다."""
    day_key = WEEKDAY_KEYS[now.weekday()]
    day_cfg = schedule.get(day_key, {"enabled": False, "time": "22:10"})
    if not day_cfg.get("enabled", True):
        return False, f"{day_key} 발송이 schedule.json에서 비활성화(blocked)되어 있어 건너뜁니다."
    try:
        hh, mm = (int(x) for x in str(day_cfg.get("time", "22:10")).split(":"))
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    except Exception:
        return False, f"{day_key} 스케줄의 time 형식이 올바르지 않습니다: {day_cfg.get('time')!r}"
    window_end = target + timedelta(minutes=CRON_INTERVAL_MINUTES)
    if target <= now < window_end:
        return True, ""
    return False, ""


def already_sent_today(target_date: date) -> bool:
    return (STATE_DIR / f"sent_{target_date.isoformat()}.flag").exists()


def mark_sent_today(target_date: date) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"sent_{target_date.isoformat()}.flag").touch()


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

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = OUTPUT_DIR / attachment_name
    build_pdf(date_line, day_label, blocks, pdf_path, icon=get_page_icon(page_id))
    log.info("PDF 생성 완료: %s", pdf_path)

    subject = f"{date_str} {SENDER_DISPLAY_NAME} 훈련일지"

    if dry_run:
        log.info("[dry-run] 이메일 발송을 건너뜁니다. 제목: %s", subject)
    else:
        send_email(subject, pdf_path, attachment_name)
        log.info("이메일 발송 완료: %s -> %s", GMAIL_ADDRESS, RECIPIENT_EMAIL)

    return RunResult(pdf_path=pdf_path, subject=subject, attachment_name=attachment_name)


def lambda_handler(event, context):
    """AWS Lambda 진입점.

    발송 시각은 EventBridge Scheduler가 관리하므로 schedule.json은 보지 않고,
    호출되면 곧바로 발송한다 (CLI의 --force와 같은 동작).

    event 예시:
      {}                        -> 오늘(KST) 훈련일지 발송
      {"date": "2026-09-16"}    -> 특정 날짜 재발송
      {"dry_run": true}         -> PDF 생성까지만 하고 메일은 보내지 않음
    """
    event = event or {}
    target_date = (
        datetime.strptime(event["date"], "%Y-%m-%d").date()
        if event.get("date")
        else datetime.now(KST).date()
    )
    dry_run = bool(event.get("dry_run"))

    try:
        result = run(target_date, dry_run=dry_run)
    except Exception:
        log.exception("훈련일지 발송 실패")
        try:
            send_error_alert(traceback.format_exc())
        except Exception:
            log.exception("에러 알림 메일 발송도 실패")
        raise  # Lambda 호출을 실패로 기록해 CloudWatch에서 알람을 걸 수 있게 한다

    return {
        "date": target_date.isoformat(),
        "subject": result.subject,
        "attachment": result.attachment_name,
        "dry_run": dry_run,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="훈련일지 PDF 이메일 발송")
    parser.add_argument("--date", help="대상 날짜 YYYY-MM-DD (기본값: 오늘, KST)")
    parser.add_argument("--dry-run", action="store_true", help="PDF만 생성하고 메일은 보내지 않음")
    parser.add_argument(
        "--force",
        action="store_true",
        help="schedule.json의 요일별 시각/차단(blocked) 설정과 중복발송 방지를 무시하고 즉시 발송",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    now = datetime.now(KST)
    target_date = (
        datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else now.date()
    )

    if not args.dry_run and not args.force:
        schedule = load_schedule()
        due, reason = is_due_now(schedule, now)
        if not due:
            if reason:
                log.info(reason)
            return
        if already_sent_today(target_date):
            log.info("%s 은(는) 이미 발송되어 건너뜁니다.", target_date.isoformat())
            return

    run(target_date, dry_run=args.dry_run)
    if not args.dry_run:
        mark_sent_today(target_date)


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
