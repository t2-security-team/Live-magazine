"""Create one daily flight PDF and archive it in Google Drive."""

from __future__ import annotations

import io
import json
import re
import threading
from datetime import date, datetime, timedelta, timezone
from itertools import zip_longest
from typing import Any

import gspread
import pandas as pd
import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials as ServiceCredentials
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


KST = timezone(timedelta(hours=9))
SHEET_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
ARCHIVE_THREAD_NAME = "t2-daily-pdf-archive"
ARCHIVE_RETRY_SECONDS = 300
ARCHIVE_START_DELAY_SECONDS = 20
GATE_COLUMNS = ["편명", "시간", "게이트", "출발지", "출구"]
_worker_lock = threading.Lock()
_worker: threading.Thread | None = None


def _clean_flight_no(value: Any) -> str:
    text = str(value).upper().strip()
    text = text.replace("DAL", "DL").replace("KAL", "KE").replace("AAR", "OZ")
    text = re.sub(r"[\s-]", "", text)
    match = re.search(r"(KE|OZ|DL)0*(\d+)([A-Z]?)", text)
    return "" if not match else f"{match.group(1)}{int(match.group(2))}{match.group(3)}"


def _format_route(value: Any) -> str:
    text = str(value or "").strip()
    return text.upper() if len(text) == 3 and text.isalpha() else text


def _parse_gate_xml(xml_text: str) -> pd.DataFrame:
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml_text)
    for node in root.iter():
        node.tag = node.tag.rsplit("}", 1)[-1].lower()
    code = (root.findtext(".//resultcode") or "").strip()
    message = (root.findtext(".//resultmsg") or "").strip().upper()
    success = code in {"0", "00", "000", "0000"} if code else (
        "NORMAL SERVICE" in message or message in {"OK", "SUCCESS", "정상"}
    )
    if not success:
        raise RuntimeError("airport API returned an error")

    rows = []
    for item in root.findall(".//item"):
        fields = {child.tag: (child.text or "").strip() for child in item}

        def pick(*names: str) -> str:
            return next((fields[name.lower()] for name in names if fields.get(name.lower())), "")

        flight = _clean_flight_no(pick("flightId", "fid"))
        if not re.fullmatch(r"(?:KE|OZ|DL)\d+[A-Z]?", flight):
            continue
        formatted_time = ""
        for name in ("estimatedDateTime", "scheduleDateTime"):
            value = pick(name)
            if re.fullmatch(r"\d{12}", value):
                value = value[-4:]
            elif re.fullmatch(r"\d{14}", value):
                value = value[-6:-2]
            match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?", value)
            if match:
                hour, minute = map(int, match.groups())
            elif re.fullmatch(r"\d{3,4}", value):
                value = value.zfill(4)
                hour, minute = int(value[:2]), int(value[2:])
            else:
                continue
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                formatted_time = f"{hour:02d}:{minute:02d}"
                break
        rows.append(
            {
                "편명": flight,
                "시간": formatted_time,
                "게이트": pick("gateNumber", "fstandPosition"),
                "출발지": pick("airportCode", "airport"),
                "출구": pick("exitNumber"),
            }
        )
    return pd.DataFrame(rows, columns=GATE_COLUMNS).drop_duplicates().reset_index(drop=True)


def _valid_gate_numbers(data: pd.DataFrame) -> pd.Series:
    numbers = pd.to_numeric(data["게이트"], errors="coerce")
    return numbers.where((numbers > 0) & (numbers < float("inf")) & (numbers % 1 == 0))


def _fetch_gate_data(api_key: str, archive_date: date) -> pd.DataFrame:
    date_text = archive_date.strftime("%Y%m%d")
    common = {"serviceKey": api_key, "type": "xml", "numOfRows": 1800, "pageNo": 1}
    endpoints = [
        (
            "https://apis.data.go.kr/B551177/StatusOfPassengerFlightsDeOdp/getPassengerArrivalsDeOdp",
            {"searchday": date_text, "from_time": "0000", "to_time": "2400"},
        ),
        (
            "https://apis.data.go.kr/B551177/statusOfAllFltDeOdp/getFltArrivalsDeOdp",
            {
                "searchdtCode": "S",
                "searchDate": date_text,
                "searchFrom": "0000",
                "searchTo": "2359",
                "passengerOrCargo": "P",
            },
        ),
    ]
    primary = None
    for url, params in endpoints:
        response = requests.get(
            url,
            params={**common, **params},
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/xml"},
            timeout=(5, 20),
        )
        response.raise_for_status()
        data = _parse_gate_xml(response.text)
        if data.empty:
            continue
        if primary is None:
            primary = data
            if _valid_gate_numbers(data).notna().all():
                return data
            continue
        result = primary.copy(deep=True)
        missing = _valid_gate_numbers(result).isna()
        candidates = data[["편명", "게이트"]].copy()
        candidates["게이트"] = _valid_gate_numbers(candidates)
        candidates = candidates.dropna(subset=["게이트"]).drop_duplicates()
        candidates = candidates[~candidates["편명"].duplicated(keep=False)]
        lookup = candidates.set_index("편명")["게이트"]
        replacement = result["편명"].map(lookup)
        fill = missing & replacement.notna()
        result.loc[fill, "게이트"] = replacement.loc[fill].astype(int).astype(str)
        return result
    if primary is not None:
        return primary
    raise RuntimeError("airport data is not ready")


def _load_pax_data(gcp_info: dict[str, Any], sheet_name: str, archive_date: date) -> pd.DataFrame:
    credentials = ServiceCredentials.from_service_account_info(gcp_info, scopes=SHEET_SCOPES)
    client = gspread.authorize(credentials)
    values = client.open(sheet_name).worksheet("pax_data").get_all_values()
    if len(values) <= 1:
        raise RuntimeError("passenger data is not ready")
    data = pd.DataFrame(values[1:], columns=values[0])
    if "조회일자" not in data.columns:
        raise RuntimeError("passenger data has no date column")
    rename_map = {}
    for column in data.columns:
        normalized = str(column).strip().upper()
        if normalized in {"FLT", "편명", "FLIGHT"}:
            rename_map[column] = "편명"
        elif normalized in {"ROUTE", "출발지", "DEST"}:
            rename_map[column] = "출발지"
        elif normalized in {"ICN/O BKG", "BKG", "승객수", "PAX", "T/S"}:
            if "승객수" not in rename_map.values():
                rename_map[column] = "승객수"
    data = data.rename(columns=rename_map)
    required = {"편명", "승객수"}
    if not required.issubset(data.columns):
        raise RuntimeError("passenger data has missing columns")
    data = data[data["조회일자"].astype(str).str.strip() == archive_date.isoformat()].copy()
    if data.empty:
        raise RuntimeError("passenger data for today is not ready")
    data["편명"] = data["편명"].apply(_clean_flight_no)
    return data.drop_duplicates(["편명"])


def build_archive_frame(pax_data: pd.DataFrame, gate_data: pd.DataFrame) -> pd.DataFrame:
    final = pd.merge(gate_data.drop_duplicates("편명"), pax_data, on="편명", how="right", suffixes=("_api", "_pax"))
    if final.empty:
        raise RuntimeError("flight numbers did not match")
    if "출발지_pax" in final.columns:
        empty = final["출발지_pax"].isna() | (final["출발지_pax"].astype(str).str.strip() == "")
        final["출발지"] = final["출발지_pax"].where(~empty, final["출발지_api"])
    else:
        final["출발지"] = final.get("출발지_api", "")
    final["출발지"] = final["출발지"].apply(_format_route)
    final = final[~final["출발지"].astype(str).str.contains("PUS|김해|부산", case=False, na=False)]
    final["시간"] = final["시간"].fillna("미확인")
    final["승객수"] = pd.to_numeric(final["승객수"].astype(str).str.replace(",", "", regex=False), errors="raise").astype(int)
    gate_numbers = pd.to_numeric(final["게이트"], errors="coerce")
    valid = gate_numbers.notna() & (gate_numbers > 0)
    final["게이트"] = gate_numbers.where(valid).fillna(0).astype(int).astype(str).replace("0", "-")
    west = valid & (gate_numbers <= 250)
    exit_a = final.get("출구", pd.Series("", index=final.index)).astype(str).str.strip().str.upper() == "A"
    final["구역"] = "동편"
    final.loc[west | (~valid & exit_a), "구역"] = "서편"
    return final.sort_values(["구역", "시간", "편명"]).reset_index(drop=True)


def build_daily_pdf(data: pd.DataFrame, archive_date: date, generated_at: datetime) -> bytes:
    buffer = io.BytesIO()
    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    page_width, _ = landscape(A4)
    document = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=10 * mm,
        leftMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
        title=f"T2 보안검색 환승부 잡지 {archive_date.isoformat()}",
        author="T2 잡지 PDF 자동저장",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "KoreanTitle",
        parent=styles["Title"],
        fontName="HYSMyeongJo-Medium",
        fontSize=17,
        leading=21,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#1E3A8A"),
        spaceAfter=3 * mm,
    )
    meta_style = ParagraphStyle(
        "KoreanMeta",
        parent=styles["Normal"],
        fontName="HYSMyeongJo-Medium",
        fontSize=8.5,
        leading=11,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#334155"),
    )
    total = int(data["승객수"].sum())
    airline_totals = {
        code: int(data.loc[data["편명"].astype(str).str.startswith(code), "승객수"].sum())
        for code in ("KE", "OZ", "DL")
    }
    story = [
        Paragraph("T2 보안검색 환승부 잡지", title_style),
        Paragraph(
            f"대상일: {archive_date:%Y-%m-%d} | "
            f"생성시각(KST): {generated_at:%Y-%m-%d %H:%M:%S} | "
            f"총 승객수: {total:,}명 | "
            f"KE {airline_totals['KE']:,} / OZ {airline_totals['OZ']:,} / DL {airline_totals['DL']:,}",
            meta_style,
        ),
        Spacer(1, 4 * mm),
    ]

    columns = ["시간", "편명", "출발지", "게이트", "승객수"]
    east = data[data["구역"] == "동편"][columns].values.tolist()
    west = data[data["구역"] == "서편"][columns].values.tolist()
    rows = [
        ["동편", "", "", "", "", "서편", "", "", "", ""],
        columns + columns,
    ]
    for east_row, west_row in zip_longest(east, west, fillvalue=["", "", "", "", ""]):
        left = [f"{value:,}" if isinstance(value, int) else str(value) for value in east_row]
        right = [f"{value:,}" if isinstance(value, int) else str(value) for value in west_row]
        rows.append(left + right)

    available = page_width - 20 * mm
    widths = [16, 19, 29, 16, 18, 16, 19, 29, 16, 18]
    scale = available / (sum(widths) * mm)
    table = Table(rows, colWidths=[width * mm * scale for width in widths], repeatRows=2)
    table.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "HYSMyeongJo-Medium"),
                ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                ("LEADING", (0, 0), (-1, -1), 9.5),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("SPAN", (0, 0), (4, 0)),
                ("SPAN", (5, 0), (9, 0)),
                ("BACKGROUND", (0, 0), (4, 0), colors.HexColor("#DBEAFE")),
                ("BACKGROUND", (5, 0), (9, 0), colors.HexColor("#FEE2E2")),
                ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#E2E8F0")),
                ("TEXTCOLOR", (0, 0), (4, 0), colors.HexColor("#1D4ED8")),
                ("TEXTCOLOR", (5, 0), (9, 0), colors.HexColor("#B91C1C")),
                ("FONTSIZE", (0, 0), (-1, 1), 8.5),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#94A3B8")),
                ("ROWBACKGROUNDS", (0, 2), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
                ("TOPPADDING", (0, 0), (-1, -1), 2.2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2),
            ]
        )
    )
    story.append(table)
    def draw_footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("HYSMyeongJo-Medium", 7)
        canvas.setFillColor(colors.HexColor("#64748B"))
        canvas.drawString(10 * mm, 5 * mm, archive_date.isoformat())
        canvas.drawRightString(page_width - 10 * mm, 5 * mm, f"{doc.page} 페이지")
        canvas.restoreState()

    document.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)
    return buffer.getvalue()


def _drive_credentials(config: dict[str, str]) -> UserCredentials:
    credentials = UserCredentials(
        token=None,
        refresh_token=config["refresh_token"],
        token_uri=config.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        scopes=[DRIVE_SCOPE],
    )
    credentials.refresh(Request())
    return credentials


def _drive_file_exists(config: dict[str, str], folder_id: str, filename: str) -> bool:
    credentials = _drive_credentials(config)
    escaped = filename.replace("\\", "\\\\").replace("'", "\\'")
    response = requests.get(
        "https://www.googleapis.com/drive/v3/files",
        params={
            "q": f"name = '{escaped}' and '{folder_id}' in parents and trashed = false",
            "fields": "files(id)",
            "pageSize": 1,
        },
        headers={"Authorization": f"Bearer {credentials.token}"},
        timeout=30,
    )
    response.raise_for_status()
    return bool(response.json().get("files"))


def _upload_pdf(config: dict[str, str], folder_id: str, filename: str, pdf_bytes: bytes) -> None:
    credentials = _drive_credentials(config)
    boundary = "t2-daily-pdf-boundary"
    metadata = json.dumps(
        {"name": filename, "parents": [folder_id], "mimeType": "application/pdf"},
        ensure_ascii=False,
    ).encode("utf-8")
    body = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
        + metadata
        + f"\r\n--{boundary}\r\nContent-Type: application/pdf\r\n\r\n".encode()
        + pdf_bytes
        + f"\r\n--{boundary}--\r\n".encode()
    )
    response = requests.post(
        "https://www.googleapis.com/upload/drive/v3/files",
        params={"uploadType": "multipart", "fields": "id"},
        data=body,
        headers={
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": f"multipart/related; boundary={boundary}",
        },
        timeout=60,
    )
    response.raise_for_status()


def archive_date_once(
    gcp_info: dict[str, Any],
    gate_api_key: str,
    drive_config: dict[str, str],
    sheet_name: str,
    archive_date: date,
) -> bool:
    folder_id = drive_config["folder_id"]
    filename = f"T2_보안검색_환승부_잡지_{archive_date.isoformat()}.pdf"
    if _drive_file_exists(drive_config, folder_id, filename):
        return False
    pax_data = _load_pax_data(gcp_info, sheet_name, archive_date)
    try:
        gate_data = _fetch_gate_data(gate_api_key, archive_date)
    except Exception:
        # Passenger evidence must still be archived when the live airport API is unavailable.
        gate_data = pd.DataFrame(columns=GATE_COLUMNS)
    archive_frame = build_archive_frame(pax_data, gate_data)
    generated_at = datetime.now(KST)
    pdf_bytes = build_daily_pdf(archive_frame, archive_date, generated_at)
    _upload_pdf(drive_config, folder_id, filename, pdf_bytes)
    return True


def start_daily_archive_worker(
    gcp_json: str,
    gate_api_key: str,
    drive_json: str,
    sheet_name: str,
) -> threading.Thread:
    global _worker
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return _worker

        gcp_info = json.loads(gcp_json)
        drive_config = json.loads(drive_json)

        def run() -> None:
            last_success: date | None = None
            threading.Event().wait(ARCHIVE_START_DELAY_SECONDS)
            while True:
                now = datetime.now(KST)
                today = now.date()
                if last_success != today:
                    try:
                        archive_date_once(gcp_info, gate_api_key, drive_config, sheet_name, today)
                        last_success = today
                        print(f"[T2_DAILY_PDF] archived {today.isoformat()}", flush=True)
                    except Exception as exc:
                        print(f"[T2_DAILY_PDF_ERROR] {today.isoformat()} {type(exc).__name__}", flush=True)
                now = datetime.now(KST)
                next_midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), KST)
                if last_success == now.date():
                    wait_seconds = max(1, (next_midnight - now).total_seconds())
                else:
                    wait_seconds = ARCHIVE_RETRY_SECONDS
                threading.Event().wait(wait_seconds)

        _worker = threading.Thread(target=run, name=ARCHIVE_THREAD_NAME, daemon=True)
        _worker.start()
        return _worker
