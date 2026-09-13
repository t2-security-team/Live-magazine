# -*- coding: utf-8 -*-
# T2 중앙 게이트 공유 버전 — 2026-09-11
import html
import streamlit as st
import pandas as pd
import numpy as np
import gspread
from google.oauth2.service_account import Credentials
import re
import threading
import xml.etree.ElementTree as ET
from urllib.parse import unquote
import requests
import time
from datetime import datetime, timedelta, timezone
st.set_page_config(page_title="T2 보안검색 환승부 잡지", layout="wide", initial_sidebar_state="collapsed")
# Fork와 GitHub 아이콘이 있는 도구 모음만 숨깁니다.
# 사이드바 화살표와 점 세 개 메뉴는 유지합니다.
st.markdown(
    """
    <style>
    [data-testid="stToolbarActions"] {
        display: none !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# KST 시간 세팅
KST = timezone(timedelta(hours=9))
now_kst_time = datetime.now(KST)
today_date_str = now_kst_time.strftime("%Y-%m-%d")
tomorrow_date_str = (now_kst_time + timedelta(days=1)).strftime("%Y-%m-%d")
# 새벽 1시 강제 초기화는 사용하지 않습니다.
SHEET_NAME = "보안검색_데이터_공유"
# 각 화면은 아래의 공유 상태 확인기로 갱신됩니다.
@st.cache_resource(show_spinner=False)
def get_gspread_client():
    creds_dict = dict(st.secrets["gcp"])
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)
@st.cache_resource(show_spinner=False)
def get_spreadsheet():
    client = get_gspread_client()
    return client.open(SHEET_NAME)
@st.cache_data(ttl=300, max_entries=1, show_spinner=False)
def load_file_list():
    try:
        spreadsheet = get_spreadsheet()
        sheet = spreadsheet.worksheet("file_list")
        data = sheet.get_all_values()
        if len(data) > 1:
            df = pd.DataFrame(data[1:], columns=data[0])
            if '조회일자' not in df.columns: df['조회일자'] = today_date_str
            return df
    except: pass
    return pd.DataFrame()
@st.cache_data(ttl=300, max_entries=1, show_spinner=False)
def load_pax_data():
    try:
        spreadsheet = get_spreadsheet()
        sheet = spreadsheet.worksheet("pax_data")
        data = sheet.get_all_values()
        if len(data) > 1:
            df = pd.DataFrame(data[1:], columns=data[0])
            if '조회일자' not in df.columns: df['조회일자'] = today_date_str
            
            rename_map = {}
            for col in df.columns:
                c_upper = str(col).strip().upper()
                if c_upper in ['FLT', '편명', 'FLIGHT']: rename_map[col] = '편명'
                elif c_upper in ['ROUTE', '출발지', 'DEST']: rename_map[col] = '출발지'
                elif c_upper in ['ICN/O BKG', 'BKG', '승객수', 'PAX', 'T/S']: 
                    if '승객수' not in df.columns and '승객수' not in rename_map.values():
                        rename_map[col] = '승객수'
            if rename_map:
                df = df.rename(columns=rename_map)
            return df
    except: pass
    return pd.DataFrame()
# 서버 전체가 함께 사용하는 게이트 수집기입니다.
# 이 작업 스레드에서는 st.* 함수를 호출하지 않습니다.
GATE_REFRESH_SECONDS = 180
SCREEN_CHECK_SECONDS = 2
GATE_ENGINE_VERSION = "central-gates-2026-09-11-v2-supplement"
GATE_COLUMNS = ["편명", "시간", "게이트", "출발지", "출구"]
class GateFetchError(Exception):
    """화면에 표시할 수 있는 비밀키 없는 오류입니다."""
def parse_gate_xml(xml_text):
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        raise GateFetchError("공항에서 받은 응답 형식을 읽지 못했습니다.") from None
    for node in root.iter():
        node.tag = node.tag.rsplit("}", 1)[-1].lower()
    code = (root.findtext(".//resultcode") or "").strip()
    message = (root.findtext(".//resultmsg") or "").strip().upper()
    if code:
        success = code in {"0", "00", "000", "0000"}
    else:
        success = "NORMAL SERVICE" in message or message in {"OK", "SUCCESS", "정상"}
    if not success:
        safe_code = code if re.fullmatch(r"[A-Za-z0-9_-]{1,32}", code) else "확인 불가"
        raise GateFetchError(f"공항 응답 코드: {safe_code}")
    rows = []
    for item in root.findall(".//item"):
        fields = {child.tag: (child.text or "").strip() for child in item}
        def pick(*names):
            return next((fields[name.lower()] for name in names if fields.get(name.lower())), "")
        flight = pick("flightId", "fid").upper().replace("DAL", "DL").replace("KAL", "KE").replace("AAR", "OZ")
        flight = clean_flight_no(flight)
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
        rows.append({"편명": flight, "시간": formatted_time,
                     "게이트": pick("gateNumber", "fstandPosition"),
                     "출발지": pick("airportCode", "airport"),
                     "출구": pick("exitNumber")})
    return pd.DataFrame(rows, columns=GATE_COLUMNS).drop_duplicates().reset_index(drop=True)
def valid_gate_numbers(data):
    numbers = pd.to_numeric(data["게이트"], errors="coerce")
    return numbers.where((numbers > 0) & (numbers < float("inf")) & (numbers % 1 == 0))
def supplement_missing_gates(primary, secondary):
    """같은 조회일·편명의 누락 게이트만 보충하며 1차의 유효한 게이트는 유지합니다."""
    result = primary.copy(deep=True)
    missing = valid_gate_numbers(result).isna()
    candidates = secondary[["편명", "게이트"]].copy()
    candidates["게이트"] = valid_gate_numbers(candidates)
    candidates = candidates.dropna(subset=["게이트"]).drop_duplicates()
    # 동일 편명에 서로 다른 게이트가 있으면 임의로 하나를 고르지 않습니다.
    candidates = candidates[~candidates["편명"].duplicated(keep=False)]
    lookup = candidates.set_index("편명")["게이트"]
    replacement = result["편명"].map(lookup)
    fill = missing & replacement.notna()
    result.loc[fill, "게이트"] = replacement.loc[fill].astype(int).astype(str)
    return result, int(fill.sum())
def fetch_gate_payload(api_key, search_date_str):
    """조회 한 회차. 1차 실패·빈 목록·누락 게이트가 있을 때 2차를 확인합니다."""
    common = {"serviceKey": api_key, "type": "xml", "numOfRows": 1800, "pageNo": 1}
    endpoints = [
        ("1차 API", "https://apis.data.go.kr/B551177/StatusOfPassengerFlightsDeOdp/getPassengerArrivalsDeOdp",
         {"searchday": search_date_str, "from_time": "0000", "to_time": "2400"}),
        ("2차 API", "https://apis.data.go.kr/B551177/statusOfAllFltDeOdp/getFltArrivalsDeOdp",
         {"searchdtCode": "S", "searchDate": search_date_str, "searchFrom": "0000", "searchTo": "2359", "passengerOrCargo": "P"}),
    ]
    errors = []
    primary_payload = None
    for source, url, params in endpoints:
        for attempt in range(2):
            response = None
            retryable = True
            try:
                response = requests.get(url, params={**common, **params},
                    headers={"User-Agent": "Mozilla/5.0", "Accept": "application/xml"}, timeout=(5, 12))
                if response.status_code != 200:
                    retryable = response.status_code == 429 or response.status_code >= 500
                    raise GateFetchError(f"HTTP {response.status_code}")
                data = parse_gate_xml(response.text)
                if not data.empty:
                    received_at = datetime.now(KST)
                    if source == "1차 API":
                        primary_payload = {"data": data, "fetched_at": received_at, "source": source}
                        if valid_gate_numbers(data).notna().all():
                            return primary_payload
                        # 항공편마다 요청하지 않고 2차 전체 목록을 한 회차만 조회합니다.
                        break
                    if primary_payload is not None:
                        combined, filled_count = supplement_missing_gates(primary_payload["data"], data)
                        if filled_count:
                            return {"data": combined, "fetched_at": primary_payload["fetched_at"],
                                    "source": f"1차 API + 2차 게이트 보충 {filled_count}건"}
                        return primary_payload
                    return {"data": data, "fetched_at": received_at, "source": source}
                errors.append(f"{source}: 표시 대상 항공편 자료가 비어 있습니다.")
                break
            except requests.RequestException:
                problem = "연결이 지연되거나 끊겼습니다."
            except GateFetchError as exc:
                problem = str(exc)
            finally:
                if response is not None:
                    response.close()
            if attempt == 0 and retryable:
                time.sleep(1)
            else:
                errors.append(f"{source}: {problem}")
                break
    # 보충 API가 실패해도 이번에 정상 수신한 1차 자료를 사용합니다.
    if primary_payload is not None:
        return primary_payload
    # 빈 응답으로 마지막 정상 게이트를 덮어쓰지 않습니다.
    raise GateFetchError(" / ".join(errors) or "공항 자료를 받지 못했습니다.")
class CentralGateHub:
    """한 서버 프로세스에 하나. 날짜별 저장소 + 수집 작업 한 개 + 잠금."""
    def __init__(self, api_key, fetcher, refresh_seconds=180, idle_seconds=90,
                 clock=None, wall_clock=None):
        self._api_key = api_key
        self._fetcher = fetcher
        self._refresh_seconds = refresh_seconds
        self._idle_seconds = idle_seconds
        self._clock = clock or time.monotonic
        self._wall_clock = wall_clock or (lambda: datetime.now(KST))
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread = None
        self._closed = False
        self._entries = {}
        self._last_access = self._clock()
    def _read(self, search_date_str, include_data):
        if not re.fullmatch(r"\d{8}", search_date_str):
            raise ValueError("올바른 조회 날짜가 필요합니다.")
        with self._lock:
            if self._closed:
                raise RuntimeError("수집기가 종료되었습니다.")
            self._last_access = self._clock()
            valid_dates = {(self._wall_clock() + timedelta(days=i)).strftime("%Y%m%d") for i in (0, 1)}
            for old_date in list(self._entries):
                if old_date not in valid_dates and not self._entries[old_date]["updating"]:
                    del self._entries[old_date]
            if search_date_str not in valid_dates:
                raise ValueError("오늘 또는 내일 자료만 조회할 수 있습니다.")
            if search_date_str not in self._entries:
                self._entries[search_date_str] = {
                    "data": pd.DataFrame(columns=GATE_COLUMNS), "fetched_at": None,
                    "checked_at": None, "source": "", "error": "", "updating": False,
                    "next_due": self._clock(), "next_check_at": None, "version": 0,
                }
                self._wake.set()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="t2-central-gate", daemon=True)
                self._thread.start()
            entry = self._entries[search_date_str]
            result = {key: value for key, value in entry.items() if key not in {"data", "next_due"}}
            # 재접속 직후에도 오래된 표를 최신인 것처럼 보이지 않게 합니다.
            result["updating"] = entry["updating"] or entry["next_due"] <= self._clock()
            if include_data:
                result["data"] = entry["data"].copy(deep=True)
            return result
    def snapshot(self, search_date_str):
        return self._read(search_date_str, include_data=True)
    def status(self, search_date_str):
        # 화면 확인에는 큰 표를 복사하지 않고 상태 번호만 읽습니다.
        return self._read(search_date_str, include_data=False)
    def _run(self):
        while True:
            selected = None
            with self._lock:
                now = self._clock()
                if self._closed or now - self._last_access >= self._idle_seconds:
                    self._thread = None
                    return
                valid_dates = {(self._wall_clock() + timedelta(days=i)).strftime("%Y%m%d") for i in (0, 1)}
                candidates = [(date, entry) for date, entry in self._entries.items() if date in valid_dates]
                if candidates:
                    date, entry = min(candidates, key=lambda pair: pair[1]["next_due"])
                    if entry["next_due"] <= now:
                        entry["updating"] = True
                        entry["version"] += 1
                        selected = (date, entry)
                timeout = min(30.0, max(0.01, self._idle_seconds - (now - self._last_access)))
                if selected is None and candidates:
                    timeout = min(timeout, max(0.01, min(entry["next_due"] for _, entry in candidates) - now))
            if selected is None:
                self._wake.wait(timeout)
                self._wake.clear()
                continue
            date, entry = selected
            payload = None
            try:
                payload = self._fetcher(self._api_key, date)
                if payload["data"].empty:
                    raise GateFetchError("표시 대상 항공편 자료가 비어 있습니다.")
                # 처리와 검증까지 끝난 새 표만 한 번에 공개합니다.
                payload = {"data": payload["data"].copy(deep=True),
                           "fetched_at": payload["fetched_at"], "source": payload["source"]}
                problem = ""
            except GateFetchError as exc:
                payload = None
                problem = str(exc)
            except Exception as exc:
                payload = None
                problem = f"게이트 자료 처리 중 오류가 발생했습니다. ({type(exc).__name__})"
            with self._lock:
                if self._closed:
                    self._thread = None
                    return
                if payload is not None:
                    entry.update(payload)
                entry["checked_at"] = self._wall_clock()
                entry["next_due"] = self._clock() + self._refresh_seconds
                entry["next_check_at"] = entry["checked_at"] + timedelta(seconds=self._refresh_seconds)
                entry["error"] = problem
                entry["updating"] = False
                entry["version"] += 1
    def close(self):
        # 검사 종료나 명시적 서버 종료용입니다. 화면 버튼에서는 호출하지 않습니다.
        with self._lock:
            self._closed = True
            worker = self._thread
        self._wake.set()
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=1)
@st.cache_resource(show_spinner=False)
def get_central_gate_hub(api_key, engine_version):
    return CentralGateHub(api_key, fetch_gate_payload, refresh_seconds=GATE_REFRESH_SECONDS)
def configure_gate_refresh(hub, refresh_seconds):
    """메모리에 남은 수집기도 백업을 유지한 채 새 주기로 전환합니다."""
    with hub._lock:
        old_seconds = hub._refresh_seconds
        if old_seconds == refresh_seconds:
            return
        hub._refresh_seconds = refresh_seconds
        now = hub._clock()
        wall_now = hub._wall_clock()
        for entry in hub._entries.values():
            if entry["checked_at"] is not None and not entry["updating"]:
                entry["next_due"] = max(now, entry["next_due"] + refresh_seconds - old_seconds)
                entry["next_check_at"] = wall_now + timedelta(seconds=entry["next_due"] - now)
                entry["version"] += 1
    hub._wake.set()


def install_shared_screen_updates(hub, search_date_str, shown_version):
    rendered_minute = datetime.now(KST).strftime("%Y%m%d%H%M")
    # 운영 버전 Streamlit 1.62.0의 자동 갱신 기능을 사용합니다.
    @st.fragment(run_every=SCREEN_CHECK_SECONDS)
    def screen_clock():
        # 분/날짜가 바뀌면 기존 시간 표시와 필터도 갱신합니다.
        if datetime.now(KST).strftime("%Y%m%d%H%M") != rendered_minute:
            st.rerun()
        if hub is not None and hub.status(search_date_str)["version"] != shown_version:
            st.rerun()
    screen_clock()

if "toast_msg" in st.session_state:
    st.toast(st.session_state["toast_msg"], icon="✅")
    del st.session_state["toast_msg"]
st.markdown("""
    <style>
    .main .block-container { padding-top: 0px !important; padding-bottom: 0px !important; margin-top: -15px !important; }
    div[data-testid="stVerticalBlock"] { gap: 0px !important; }
    .element-container { margin-bottom: 0px !important; }
    iframe { margin-bottom: 0px !important; min-height: 45px !important; }
    section[data-testid="stSidebar"] div[data-testid="stSidebarUserContent"] { padding-top: 0rem !important; margin-top: -2.5rem !important; }
    
    .file-box { background-color:#f0f7ff; padding:15px; border-radius:5px; margin-bottom:15px; border: 1px solid #3b82f6; display: block; overflow: visible; }
    .file-item { font-size:13px; margin: 0 0 6px 10px !important; line-height: 1.5 !important; color: #1f2937; }
    
    .merged-table { width: 100%; border-collapse: collapse; text-align: center; margin-bottom: 0px !important; }
    .merged-table tr { border: none !important; } 
    .merged-table th { background-color: #f8f9fa !important; border: 1px solid #dee2e6 !important; padding: 4px; font-weight: bold; }
    .merged-table td { border: 1px solid #dee2e6 !important; padding: 3px; vertical-align: middle; font-weight: bold !important; }
    .sum-cell { font-weight: bold; color: #1E3A8A; }
    
    .total-banner { background-color: #f0f7ff !important; padding: 4px 8px !important; border-radius: 8px; text-align: center; border: 1px solid #3b82f6; margin-bottom: 2px; margin-top: 2px; }
    .carrier-banner { background-color: #ffffff !important; padding: 4px; border-radius: 8px; text-align: center; border: 1px solid #3b82f6; margin-bottom: 4px; display: flex; justify-content: center; gap: 20px; }
    .carrier-item { font-size: 14px; font-weight: bold; }
    .print-row { display: flex; flex-direction: row; gap: 15px; width: 100%; }
    .print-col { flex: 1; min-width: 0; }
    
    @media print {
        .no-print, header, footer, [data-testid="stSidebar"], [data-testid="stHeader"], [data-testid="stToolbar"], iframe, .icon-container { display: none !important; }
        html, body { height: auto !important; min-height: auto !important; width: 1024px !important; min-width: 1024px !important; padding: 0 !important; margin: 0 !important; }
        .appview-container, .main, .block-container, .element-container { padding: 0 !important; margin: 0 !important; width: 1024px !important; max-width: 1024px !important; }
        div[data-testid="stVerticalBlock"] { gap: 0 !important; }
        body { zoom: 75%; }
        .print-row { display: flex !important; flex-direction: row !important; flex-wrap: nowrap !important; width: 100% !important; justify-content: space-between !important; }
        .print-col { flex: 1 1 48% !important; width: 48% !important; }
        table { page-break-inside: auto; margin-bottom: 0px !important; width: 100% !important; }
        tr { page-break-inside: avoid; page-break-after: auto; }
        thead { display: table-header-group; }
        @page { size: A4; margin-top: 12mm !important; margin-bottom: 12mm !important; margin-left: 10mm !important; margin-right: 10mm !important; }
        @page :first { margin-top: 0mm !important; }
    }
    </style>
""", unsafe_allow_html=True)
def clean_flight_no(val):
    if pd.isna(val): return ""
    val = str(val).strip().replace(" ", "").upper()
    match = re.match(r'([A-Z]+)(\d+)', val)
    if match: return f"{match.group(1)}{int(match.group(2)):03d}"
    return val
IATA_CITY_MAP = {
    "LIS": "리스본", "HFE": "허페이", "KUH": "쿠시로", "KIX": "오사카/간사이", "NRT": "나리타", "HKG": "홍콩", 
    "TSN": "톈진", "CTS": "삿포로", "MFM": "마카오", "AKL": "오클랜드", "UKB": "고베", "KOJ": "가고시마",
    "DLC": "다롄", "LHR": "런던", "BUD": "부다페스트", "CDG": "파리", "PEK": "베이징", "NGO": "나고야", 
    "YNZ": "옌청", "PVG": "상하이/푸동", "CGQ": "창춘", "KIJ": "니가타", "LAX": "로스앤젤레스", "HND": "하네다",
    "JFK": "뉴욕", "ATL": "애틀랜타", "DTW": "디트로이트", "SEA": "시애틀", "SFO": "샌프란시스코", "FRA": "프랑크푸르트", 
    "FCO": "로마", "BKK": "방콕", "SGN": "호치민", "HAN": "하노이", "MNL": "마닐라", "CEB": "세부",
    "SIN": "싱가포르", "SYD": "시드니", "BNE": "브리즈번", "TPE": "타이베이", "CAN": "광저우", "TAO": "칭다오", 
    "FUK": "후쿠오카", "OKA": "오키나와", "MSP": "미니애폴리스", "DFW": "댈러스", "ORD": "시카고", "YVR": "밴쿠버",
    "YYZ": "토론토", "AMS": "암스테르담", "IST": "이스탄불", "DXB": "두바이", "CJU": "제주", "PUS": "부산", 
    "HNL": "호놀룰루", "BOS": "보스턴", "IAD": "워싱턴DC", "LAS": "라스베이거스", "MUC": "뮌헨", "PRG": "프라하",
    "ZRH": "취리히", "VIE": "빈", "MAD": "마드리드", "BCN": "바르셀로나", "MXP": "밀라노", "DEL": "델리", 
    "BOM": "뭄바이", "CGK": "자카르타", "DPS": "발리", "PNH": "프놈펜", "REP": "씨엠립", "VTE": "비엔티안",
    "DAD": "다낭", "CXR": "나트랑", "PQC": "푸꾸옥", "HKT": "푸켓", "CNX": "치앙마이", "RGN": "양곤", 
    "KUL": "쿠알라룸푸르", "BKI": "코타키나발루", "PEN": "페낭", "GUM": "괌", "SPN": "사이판", "ROR": "팔라우", 
    "UBN": "울란바토르", "KTI": "떼조", "TAE": "대구", "SHE": "심양", "HRB": "하얼빈", "SZX": "선전", "SLC": "솔트레이크시티",
    "NGS": "나가사키", "YNJ": "옌지", "TAS": "타슈켄트", "ALA": "알마티", "TFU": "청두", "KMQ": "고마츠",
    "HGH": "항저우", "NKG": "난징", "XIY": "시안", "FOC": "푸저우", "CGO": "정저우", "CKG": "충칭",
    "CSX": "장사", "KMG": "쿤밍", "DYG": "장가계", "KTM": "카트만두", "CRK": "클라크필드", "SDJ": "센다이",
    "OKJ": "오카야마", "AOJ": "아오모리", "WUH": "우한", "XMN": "샤먼", "KMI": "미야자키",  "KMJ": "구마모토", "OSL": "오슬로",
    "ARN": "스톡홀름", "MRS": "마르세유",
}
def format_route(val):
    val = str(val).strip().upper()
    if val in IATA_CITY_MAP: return f"{IATA_CITY_MAP[val]}({val})"
    match = re.search(r'^(.*?)\s*\((.*?)\)$', val)
    if match:
        part1, part2 = match.group(1).strip(), match.group(2).strip().upper()
        if re.match(r'^[A-Z]{3}$', part2):
            city = IATA_CITY_MAP.get(part2, part1) if not part1 or re.match(r'^[a-zA-Z/]+$', part1) else part1
            return f"{city}({part2})" if city else f"({part2})"
    if '/' in val: val = val.split('/')[0].strip()
    val_upper = val.upper()
    if re.match(r'^[A-Z]{3}$', val_upper):
        city = IATA_CITY_MAP.get(val_upper, "")
        return f"{city}({val_upper})" if city else val_upper
    return val
def generate_table_html(df, title, count, color, opt_airline, opt_peak, opt_incoming, font_size, target_date, now_kst):
    display_title = f"{title} ({count:,}명)"
    html_parts = [f"<div class='print-col'><h3 style='text-align:center; color:{color}; font-size:16px; margin-top:2px; margin-bottom:5px;'>{display_title}</h3>"]
    
    if df.empty: 
        html_parts.append("<div style='text-align:center; padding:20px; border:1px solid #ddd;'>데이터 없음</div></div>")
        return "".join(html_parts)
    
    df = df.sort_values('시간').reset_index(drop=True)
    
    html_parts.append("""
    <style>
    .icon-container { position: absolute; right: 2px; width: 28px; height: 16px; border-bottom: 1.5px solid #333333; overflow: hidden; }
    .plane-landing { position: absolute; bottom: 0.5px; color: #333333; animation: landing-anim 2.5s ease-in-out infinite; }
    @keyframes landing-anim { 0% { transform: translate(-15px, -12px) rotate(25deg); } 35% { transform: translate(1px, 0px) rotate(0deg); } 70% { transform: translate(12px, 0px) rotate(0deg); } 100% { transform: translate(27px, 0px) rotate(0deg); } }
    .plane-landed { position: absolute; bottom: 0.5px; left: 50%; transform: translateX(-50%); color: #333333; }
    .pax-cell-container { position: relative; display: flex; align-items: center; justify-content: center; width: 100%; min-height: 20px; padding-right: 40px; }
    @media print { .icon-container { display: none !important; } }
    </style>
    """)
    
    html_parts.append(f'<table class="merged-table" style="font-size: {font_size}px !important;"><thead><tr>')
    html_parts.append(f'<th style="width:14%; font-size:{font_size}px !important;">시간</th><th style="width:17%; font-size:{font_size}px !important;">편명</th><th style="font-size:{font_size}px !important;">출발지</th><th style="width:14%; font-size:{font_size}px !important;">게이트</th><th style="width:15%; font-size:{font_size}px !important;">승객</th><th style="width:12%; font-size:{font_size}px !important;">합계</th></tr></thead><tbody>')
    
    df['hour_val'] = df['시간'].astype(str).str.extract(r'^(\d{1,2})').fillna(0).astype(int)
    hour_counts = df['hour_val'].value_counts().sort_index()
    hour_sums = df.groupby('hour_val')['p_val'].sum()
    processed_hours = set()
    
    records = df.to_dict('records')
    for row in records:
        current_h = row['hour_val']
        flt = str(row['편명']).upper()
        row_style_css, text_style = "", ""
        
        is_past_20_mins, is_blinking, is_landing, is_landed = False, False, False, False
        
        try:
            time_parts = str(row['시간']).split(':')
            if len(time_parts) == 2:
                flight_dt = target_date.replace(hour=int(time_parts[0]), minute=int(time_parts[1]), second=0, microsecond=0)
                diff_mins = (now_kst - flight_dt).total_seconds() / 60.0
                if diff_mins >= 20: is_past_20_mins = True  
                elif 0 <= diff_mins < 10: is_blinking = True; is_landing = True       
                elif 10 <= diff_mins < 20: is_landed = True        
        except: pass
            
        if is_past_20_mins:
            text_style = " text-decoration: line-through; text-decoration-color: black; color: #6B7280;"
            row_style_css = "background-color: #F9FAFB;" 
        elif opt_incoming and is_blinking: row_style_css = "background-color: #FFFF00;"
        else:
            if opt_airline:
                if flt.startswith("DL"): row_style_css = "background-color: #E3F2FD;" 
                elif flt.startswith("OZ"): row_style_css = "background-color: #FDF4F7;" 
            elif opt_peak:
                if current_h in [16, 17, 18]: row_style_css = ["background-color: #F4FAFD;", "background-color: #FFFDF0;", "background-color: #FFF5F8;"][current_h-16] 
            else: row_style_css = "background-color: #ffffff;"
                
        td_style = f' style="{row_style_css} font-size: {font_size}px !important; font-weight: bold !important;{text_style}"'
        
        시간_val, 편명_val, 출발지_val, 게이트_val = html.escape(str(row["시간"])), html.escape(str(row["편명"])), html.escape(str(row.get("출발지", ""))), html.escape(str(row["게이트"]))
        pax_text = str(row.get("p_display", ""))
        pax_content = html.escape(pax_text)
        
        if pax_text and (is_landing or is_landed):
            plane_svg = '<svg viewBox="0 0 24 24" width="16" height="15" fill="currentColor"><path d="M22,12 c0,1.1 -0.9,2 -2,2 H15 l-4,5 h-2 l2.5,-5 H6 l-2.5,2.5 H2 l1.5,-3.5 C3.2,12.7 3.2,11.3 3.5,11 L2,7.5 h1.5 l2.5,2.5 h5.5 l-2.5,-5 h2 l4,5 h5 c1.1,0 2,0.9 2,2 z" /></svg>'
            icon_div = f'<div class="icon-container"><div class="{"plane-landing" if is_landing else "plane-landed"}">{plane_svg}</div></div>'
            pax_content = f'<div class="pax-cell-container"><span>{html.escape(pax_text)}</span> {icon_div}</div>'
        html_parts.append(f'<tr><td{td_style}>{시간_val}</td><td{td_style}>{편명_val}</td><td{td_style}>{출발지_val}</td><td{td_style}>{게이트_val}</td><td{td_style}>{pax_content}</td>')
        
        if current_h not in processed_hours:
            html_parts.append(f'<td rowspan="{hour_counts[current_h]}" class="sum-cell" style="background-color: #ffffff !important; font-size: {font_size + 1}px !important; font-weight: bold !important;"><div style="position: relative; z-index: 10;">{hour_sums[current_h]:,}</div></td>')
            processed_hours.add(current_h)
        html_parts.append('</tr>')
        
    html_parts.append('</tbody></table></div>')
    return "".join(html_parts)
with st.sidebar:
    st.markdown("<h3 style='margin: -10px 0px 8px 0px !important; padding: 0px !important; font-size: 19px; font-weight: bold; color: #1E3A8A;'>🔄 게이트 수신 상태</h3>", unsafe_allow_html=True)
    
    gate_time_placeholder = st.empty()
    st.caption(f"💡 게이트는 서버에서 날짜별로 약 {GATE_REFRESH_SECONDS // 60}분마다 갱신합니다.")
    st.caption("새로 받은 게이트는 연결된 화면에 자동 반영됩니다.")
    st.divider()
    file_list_placeholder = st.container()
    st.divider()
    today_ui_str = f"오늘 ({now_kst_time.strftime('%y')}년 {now_kst_time.month}월 {now_kst_time.day}일)"
    tomorrow_ui_str = f"내일 ({(now_kst_time + timedelta(days=1)).strftime('%y')}년 {(now_kst_time + timedelta(days=1)).month}월 {(now_kst_time + timedelta(days=1)).day}일)"
    
    date_option = st.radio("📅 확인할 게이트 날짜 선택", [today_ui_str, tomorrow_ui_str], index=0)
    
    target_date = (now_kst_time + timedelta(days=1)) if "내일" in date_option else now_kst_time
    target_date_str = target_date.strftime("%Y-%m-%d")
        
    display_date_str = target_date.strftime("%Y년 %m월 %d일")
    api_target_date_str = target_date.strftime("%Y%m%d")
    
    st.divider()
    
    vis_option = st.radio("🎨 시각화 옵션", ["✈ 항공사별 색상 표시 (DL, OZ)", "⏰ 첨두시간 색상 표시 (16~18시)", "곧 들어오는 비행기 표시 (형광색)", "적용 안 함"], index=2)
    opt_airline = (vis_option == "✈ 항공사별 색상 표시 (DL, OZ)")
    opt_peak = (vis_option == "⏰ 첨두시간 색상 표시 (16~18시)")
    opt_incoming = (vis_option == "곧 들어오는 비행기 표시 (형광색)")
    
    current_hour = now_kst_time.hour
    default_start_hour = max(0, current_hour - 1) if "오늘" in date_option else 0
    time_range = st.slider("조회 시간대 (시)", 0, 24, (default_start_hour, 24))
    base_font_size = st.slider("🔠 표 글자 조절 (px)", min_value=10, max_value=17, value=14, step=1)
    
    st.divider()
    st.header("🛠️ 시스템 복구")
    if st.button("🔌 승객 자료 연결 다시 시도", use_container_width=True, type="secondary"):
        load_pax_data.clear()
        load_file_list.clear()
        get_spreadsheet.clear()
        get_gspread_client.clear()
        st.session_state["toast_msg"] = "승객 자료 연결을 다시 시도합니다."
        st.rerun()
# 게이트 조회는 중앙 수집기만 수행합니다. 새 접속도 같은 백업을 읽습니다.
gate_hub = None
gate_status = {"data": pd.DataFrame(columns=GATE_COLUMNS), "fetched_at": None,
               "checked_at": None, "source": "", "error": "", "updating": False,
               "next_check_at": None, "version": -1}
try:
    gate_api_key = unquote(str(st.secrets["api"]["service_key"]).strip())
    if not gate_api_key:
        raise ValueError("empty key")
    gate_hub = get_central_gate_hub(gate_api_key, GATE_ENGINE_VERSION)
    configure_gate_refresh(gate_hub, GATE_REFRESH_SECONDS)
    gate_status = gate_hub.snapshot(api_target_date_str)
except Exception:
    gate_status["error"] = "공항 연결 설정을 확인하지 못했습니다. 사이트의 기존 연결키 설정을 확인해 주세요."
df_g = gate_status["data"]
with st.spinner("⏳ 승객 자료를 확인하는 중입니다..."):
    full_pax_df = load_pax_data()
    full_files_df = load_file_list()
fetched_at = gate_status["fetched_at"]
if fetched_at is not None:
    gate_time_placeholder.caption(f"게이트 정상 수신: {fetched_at:%Y-%m-%d %H:%M:%S}")
    st.caption(f"게이트 정상 수신: {fetched_at:%Y-%m-%d %H:%M:%S} · 약 {GATE_REFRESH_SECONDS // 60}분마다 갱신")
else:
    gate_time_placeholder.caption("게이트 자료: 첫 수신 대기 중")
if gate_status["updating"]:
    if df_g.empty:
        st.info("⏳ 공항에서 첫 게이트 자료를 받고 있습니다. 받는 즉시 자동으로 표시합니다.")
    elif gate_status["error"]:
        st.warning(f"⚠️ 게이트 연결 재시도 중 · 마지막 정상 수신 {fetched_at:%Y-%m-%d %H:%M:%S}. 현재 게이트와 다를 수 있습니다.")
    else:
        st.info(f"🔄 새 게이트를 확인 중입니다. 현재 표는 {fetched_at:%H:%M:%S} 수신 자료입니다.")
elif gate_status["error"]:
    if not df_g.empty:
        st.warning(f"⚠️ 게이트 갱신 실패 · 마지막 정상 수신 {fetched_at:%Y-%m-%d %H:%M:%S}. 현재 게이트와 다를 수 있습니다.")
    else:
        st.warning("선택한 날짜의 게이트 자료를 아직 받지 못했습니다. 내일 자료는 아직 제공되지 않았을 수도 있습니다.")
    with st.expander("게이트 연결 상태"):
        st.write(gate_status["error"])
        if gate_status["next_check_at"] is not None:
            st.caption(f"다음 자동 조회 예정: {gate_status['next_check_at']:%H:%M:%S}")
elif df_g.empty:
    st.info("⏳ 게이트 자료를 준비 중입니다. 잠시 후 자동으로 표시합니다.")
if not full_pax_df.empty: saved_pax_df = full_pax_df[full_pax_df['조회일자'] == target_date_str]
else: saved_pax_df = pd.DataFrame()
if not full_files_df.empty: saved_files = full_files_df[full_files_df['조회일자'] == target_date_str]['파일명'].tolist()
else: saved_files = []
with file_list_placeholder:
    if not saved_pax_df.empty:
        with st.expander("✅ 현재 공유중인 승객 데이터 목록", expanded=True):
            if saved_files:
                for fname in saved_files: st.markdown(f"<p class='file-item'>• {html.escape(str(fname))}</p>", unsafe_allow_html=True)
            else: st.markdown("<p class='file-item'>• 데이터 적용 완료</p>", unsafe_allow_html=True)
st.markdown(f"""
    <style>
    .merged-table, .merged-table th, .merged-table td {{ font-size: {base_font_size}px !important; font-weight: bold !important; }}
    .sum-cell {{ font-size: {base_font_size + 1}px !important; font-weight: bold !important; }}
    </style>
""", unsafe_allow_html=True)
p_all = [saved_pax_df] if not saved_pax_df.empty else []
if not p_all or df_g.empty:
    st.markdown("<h2 style='text-align: center;'>✈ T2 보안검색 환승부 잡지 (실시간 연동) ✈</h2>", unsafe_allow_html=True)
    st.markdown("<br>", unsafe_allow_html=True)
    
    if not p_all:
        st.warning("📂 **[승객 데이터 누락]** 아직 구글 시트에 공유된 승객수 엑셀 파일이 없습니다. [데이터 업로드] 사이트에서 해당 날짜의 엑셀 파일을 먼저 저장해 주세요.")
else:
    df_p = pd.concat(p_all)
    if '편명' not in df_p.columns:
        st.sidebar.error("🚨 [구글 시트 오류] 시트 상단에 '편명' 컬럼이 없거나 이름이 잘못되었습니다.")
        df_p['편명'] = ""
        
    df_p = df_p.drop_duplicates(['편명'])
    final = pd.merge(df_g, df_p, on='편명', how='inner', suffixes=('_api', '_pax'))
    
    if '출발지_pax' in final.columns:
        cond_empty = final['출발지_pax'].isna() | (final['출발지_pax'].astype(str).str.strip() == '')
        final['출발지'] = np.where(cond_empty, final['출발지_api'], final['출발지_pax'])
    else: final['출발지'] = final['출발지_api']
        
    if '출발지' in final.columns:
        final['출발지'] = final['출발지'].apply(format_route)
        final = final[~final['출발지'].astype(str).str.contains('PUS|김해|부산', case=False, na=False)]
    
    if not final.empty:
        if '승객수' not in final.columns:
            final['승객수'] = 0
            
        final['p_val'] = pd.to_numeric(final['승객수'], errors='coerce').fillna(0).astype(int)
        
        def format_pax_display(val):
            if pd.isna(val) or str(val).strip() == '': return ""
            try: return f"{int(float(str(val).replace(',', '').strip())):,}"
            except: return ""
                
        final['p_display'] = final['승객수'].apply(format_pax_display)
        final['hour'] = final['시간'].astype(str).str.extract(r'^(\d{1,2})').fillna(0).astype(int)
        final = final[(final['hour'] >= time_range[0]) & (final['hour'] <= time_range[1])]
        
        # ⭐ [스마트 슬라이더 연동 40분 삭제 로직] 
        if time_range[0] >= default_start_hour:
            def calc_diff_mins(t_str):
                try:
                    time_parts = str(t_str).split(':')
                    if len(time_parts) == 2:
                        flight_dt = target_date.replace(hour=int(time_parts[0]), minute=int(time_parts[1]), second=0, microsecond=0)
                        return (now_kst_time - flight_dt).total_seconds() / 60.0
                except:
                    pass
                return 0.0
            final['diff_mins'] = final['시간'].apply(calc_diff_mins)
            final = final[final['diff_mins'] < 40]
        
    if not final.empty:
        if '출구' not in final.columns: final['출구'] = ""
        final['g_num'] = pd.to_numeric(final['게이트'], errors='coerce').fillna(0)
        
        cond_gnum_valid = final['g_num'] > 0
        cond_west_gate = cond_gnum_valid & (final['g_num'] <= 250)
        cond_exit_A = final['출구'].astype(str).str.strip().str.upper() == 'A'
        
        final['구역'] = np.where(cond_gnum_valid, np.where(cond_west_gate, '서편', '동편'), np.where(cond_exit_A, '서편', '동편'))
        final['게이트'] = np.where(cond_gnum_valid, final['g_num'].astype(int).astype(str), '-')
        
        total_p = final['p_val'].sum()
        def c_sum(c): return final[final['편명'].str.startswith(c, na=False)]['p_val'].sum()
        ke_s, oz_s, dl_s = c_sum('KE'), c_sum('OZ'), c_sum('DL')
        
        st.iframe(
            """
            <style>
            body { margin: 0; padding: 0; overflow: hidden; display: flex; gap: 10px; }
            .custom-btn { background-color: white; border: 1px solid #dcdcdc; color: #31333f; padding: 6px 15px; font-size: 14px; border-radius: 6px; cursor: pointer; font-family: sans-serif; box-shadow: 0px 1px 3px rgba(0,0,0,0.1); }
            .custom-btn:hover { border-color: #ff4b4b; color: #ff4b4b; }
            </style>
            <button class="custom-btn" onclick="window.parent.print()">📄 PDF 저장</button>
            <button class="custom-btn" onclick="takePic()" id="pic-btn">📸 전체 사진으로 저장</button>
            <script>
            var parentWin = window.parent; var parentDoc = parentWin.document;
            function takePic() {
                var btn = document.getElementById('pic-btn'); btn.innerText = "⏳ 캡처 중... 잠시만요!";
                try {
                    if (!parentWin.html2canvas) {
                        var script = parentDoc.createElement('script'); script.src = "https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js";
                        script.onload = function() { doCap(parentWin, parentDoc, btn); }; script.onerror = function() { alert("⚠ 오류"); btn.innerText = "📸 캡처"; };
                        parentDoc.head.appendChild(script);
                    } else { doCap(parentWin, parentDoc, btn); }
                } catch(e) { btn.innerText = "📸 캡처"; }
            }
            function doCap(win, doc, btn) {
                var target = doc.querySelector('.block-container') || doc.querySelector('.main');
                var hides = doc.querySelectorAll('[data-testid="stSidebar"], header, iframe, .icon-container');
                var appView = doc.querySelector('.appview-container') || doc.querySelector('[data-testid="stAppViewContainer"]');
                var mainView = doc.querySelector('.main');
                var oldAppOverflow = appView ? appView.style.overflow : ''; var oldAppHeight = appView ? appView.style.height : '';
                var oldMainOverflow = mainView ? mainView.style.overflow : ''; var oldMainHeight = mainView ? mainView.style.height : '';
                if(appView) { appView.style.overflow = 'visible'; appView.style.height = 'auto'; }
                if(mainView) { mainView.style.overflow = 'visible'; mainView.style.height = 'auto'; }
                target.style.paddingTop = '10px'; target.style.marginTop = '0px'; target.style.width = '1100px'; target.style.maxWidth = '1100px';
                hides.forEach(function(e){ e.dataset.old = e.style.display; e.style.display = 'none'; });
                setTimeout(function() {
                    win.html2canvas(target, { scale: 6, useCORS: true, backgroundColor: '#ffffff' }).then(function(canvas) {
                        var link = doc.createElement('a'); link.download = '잡지.png'; link.href = canvas.toDataURL('image/png'); link.click();
                    }).finally(function() {
                        if(appView) { appView.style.overflow = oldAppOverflow; appView.style.height = oldAppHeight; }
                        if(mainView) { mainView.style.overflow = oldMainOverflow; mainView.style.height = oldMainHeight; }
                        target.style.paddingTop = ''; target.style.marginTop = ''; target.style.width = ''; target.style.maxWidth = '';
                        hides.forEach(function(e){ e.style.display = e.dataset.old || ''; }); btn.innerText = "📸 전체 사진으로 저장";
                    });
                }, 800);
            }
            function doScrollLogic() {
                var scrollContainer = parentDoc.querySelector('.main') || parentWin;
                var savedScroll = parentWin.sessionStorage.getItem('stScrollPos');
                if (savedScroll && scrollContainer.scrollTo) { scrollContainer.scrollTo(0, parseInt(savedScroll)); }
            }
            setTimeout(doScrollLogic, 100); setTimeout(doScrollLogic, 300); setTimeout(doScrollLogic, 600); setTimeout(doScrollLogic, 1000);
            setInterval(function() {
                var scrollContainer = parentDoc.querySelector('.main') || parentWin;
                var scrollTop = scrollContainer.scrollTop || parentWin.scrollY || 0;
                if(scrollTop > 0) { parentWin.sessionStorage.setItem('stScrollPos', scrollTop); }
            }, 500);
            </script>
            """, height=45
        )
        
        st.markdown(f"""
            <div class="total-banner" style="position: relative;">
                <div style='margin:0; color:#1E3A8A; font-size: 18px; font-weight: bold;'>📊 총 승객수: {total_p:,}명</div>
                <div style="position: absolute; right: 15px; top: 50%; transform: translateY(-50%); font-weight: bold; color: #1E3A8A; font-size: 16px;">{display_date_str}</div>
            </div>
            <div class="carrier-banner">
                <span class="carrier-item">KE: <span style="color:#1E3A8A;">{ke_s:,}</span>명</span>
                <span class="carrier-item">OZ: <span style="color:#1E3A8A;">{oz_s:,}</span>명</span>
                <span class="carrier-item">DL: <span style="color:#1E3A8A;">{dl_s:,}</span>명</span>
            </div>
            <hr style="margin: 2px 0 10px 0; border: 0; border-top: 1px solid #e5e7eb;">
        """, unsafe_allow_html=True)
        
        west_p = final[final['구역'] == '서편']['p_val'].sum()
        east_p = final[final['구역'] == '동편']['p_val'].sum()
        
        w_html = generate_table_html(final[final['구역'] == '서편'], " 서편", west_p, "#DC2626", opt_airline, opt_peak, opt_incoming, base_font_size, target_date, now_kst_time)
        e_html = generate_table_html(final[final['구역'] == '동편'], " 동편", east_p, "#2563EB", opt_airline, opt_peak, opt_incoming, base_font_size, target_date, now_kst_time)
        
        st.markdown(f'<div class="print-row">{e_html}{w_html}</div>', unsafe_allow_html=True)
    if final.empty:
        st.info("선택한 날짜·시간대에 표시할 항공편이 없습니다. 편명 일치 여부와 조회 시간대를 확인해 주세요.")
# 표를 그린 뒤에 연결합니다. 서버의 상태 번호가 바뀔 때 화면을 다시 그립니다.
install_shared_screen_updates(gate_hub, api_target_date_str, gate_status["version"])
