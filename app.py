import html
import streamlit as st
import pandas as pd
import numpy as np
import gspread
from google.oauth2.service_account import Credentials
import re
import io
import requests
import time
from datetime import datetime, timedelta, timezone

st.set_page_config(page_title="T2 보안검색 환승부 잡지", layout="wide", initial_sidebar_state="collapsed")

# KST 시간 세팅
KST = timezone(timedelta(hours=9))
now_kst_time = datetime.now(KST)
today_date_str = now_kst_time.strftime("%Y-%m-%d")
tomorrow_date_str = (now_kst_time + timedelta(days=1)).strftime("%Y-%m-%d")

if "last_updated" not in st.session_state:
    st.session_state["last_updated"] = now_kst_time.strftime("%Y-%m-%d %H:%M:%S")

# ⭐ 하얀화면 1차 방어: 마지막 정상 게이트 데이터를 기억해둘 공간
if "last_valid_gate_df" not in st.session_state:
    st.session_state["last_valid_gate_df"] = pd.DataFrame()

# 새벽 1시 자동 캐시 초기화 엔진 (구글 시트 삭제 아님! 메모리만 비워줌)
if "last_auto_clear" not in st.session_state:
    st.session_state["last_auto_clear"] = None

if now_kst_time.hour == 1 and st.session_state["last_auto_clear"] != today_date_str:
    try:
        get_gspread_client.clear()
        get_spreadsheet.clear()
        load_file_list.clear()
        load_pax_data.clear()
        fetch_realtime_gate_info.clear()
        st.session_state["last_valid_gate_df"] = pd.DataFrame() # 백업 초기화
    except Exception:
        pass
    st.session_state["last_auto_clear"] = today_date_str

SHEET_NAME = "보안검색_데이터_공유"

st.components.v1.html(
    """
    <script>
    var parentWin = window.parent;
    var parentDoc = parentWin.document;

    function force5MinRefresh() {
        var btns = parentDoc.querySelectorAll('button');
        var clicked = false;
        btns.forEach(function(b) {
            if (b.innerText.includes("업데이트하기") || b.innerText.includes("실시간 업데이트")) {
                b.click();
                clicked = true;
            }
        });
        if (!clicked) { parentWin.location.reload(); }
    }
    setInterval(force5MinRefresh, 300000);
    </script>
    """,
    height=0, width=0
)

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

@st.cache_data(ttl=1800, max_entries=1, show_spinner=False)
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

@st.cache_data(ttl=21600, max_entries=1, show_spinner=False)
def load_pax_data():
    try:
        spreadsheet = get_spreadsheet()
        sheet = spreadsheet.worksheet("pax_data")
        data = sheet.get_all_values()
        if len(data) > 1:
            df = pd.DataFrame(data[1:], columns=data[0])
            if '조회일자' not in df.columns: df['조회일자'] = today_date_str
            return df
    except: pass
    return pd.DataFrame()

@st.cache_data(ttl=290, max_entries=1, show_spinner=False)
def fetch_realtime_gate_info(search_date_str):
    import xml.etree.ElementTree as ET
    try:
        api_key = str(st.secrets["api"]["service_key"]).strip()
        url = "https://apis.data.go.kr/B551177/statusOfAllFltDeOdp/getFltArrivalsDeOdp"
        req_url = f"{url}?serviceKey={api_key}&searchdtCode=S&searchDate={search_date_str}&searchFrom=0000&searchTo=2359&passengerOrCargo=P&type=xml&numOfRows=1800&pageNo=1"
        headers = {"User-Agent": "Mozilla/5.0"}
        
        response = None
        for attempt in range(2):
            try:
                response = requests.get(req_url, headers=headers, timeout=(3, 5))
                if response.status_code == 200: break
            except:
                if attempt == 1: return pd.DataFrame()
                time.sleep(1)
                
        if not response or response.status_code != 200: return pd.DataFrame()

        err_text = response.text
        if "NORMAL SERVICE" not in err_text: return pd.DataFrame()

        root = ET.fromstring(err_text)
        items = []
        for item in root.findall(".//item"):
            flight_id = (item.findtext("flightId") or item.findtext("fid") or "").replace('DAL', 'DL').replace('KAL', 'KE').replace('AAR', 'OZ')
            time_str = str(item.findtext("estimatedDatetime") or item.findtext("scheduleDatetime") or "")
            raw_time = time_str[-4:] if len(time_str) >= 4 else time_str
            formatted_time = f"{raw_time[:2]}:{raw_time[2:]}" if len(raw_time) == 4 else raw_time
            
            items.append({
                '편명': clean_flight_no(flight_id), '시간': formatted_time,
                '게이트': item.findtext("gateNumber") or item.findtext("fstandPosition") or "",
                '출발지': item.findtext("airportCode") or item.findtext("airport") or "",
                '출구': item.findtext("exitNumber") or ""
            })
        
        df = pd.DataFrame(items)
        if not df.empty: df = df[df['편명'].str.startswith(('KE', 'OZ', 'DL'), na=False)]
        return df
    except: return pd.DataFrame()

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
    "ARN": "스톡홀름",
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
    st.markdown("<h3 style='margin: -10px 0px -15px 0px !important; padding: 0px !important; font-size: 19px; font-weight: bold; color: #1E3A8A;'>🔄 실시간 업데이트</h3>", unsafe_allow_html=True)
    
    if st.button("🔄 업데이트하기", use_container_width=True):
        fetch_realtime_gate_info.clear()
        load_pax_data.clear()
        load_file_list.clear()
        st.session_state["toast_msg"] = "모든 정보를 최신 상태로 업데이트했습니다!"
        st.session_state["last_updated"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
        st.rerun()
        
    st.caption(f"마지막 업데이트: {st.session_state['last_updated']}")
    st.caption("💡 5분(300초)마다 자동으로 최신 게이트 정보를 갱신합니다!")

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
    base_font_size = st.slider("🔠 표 글자 조절 (px)", min_value=10, max_value=17, value=13, step=1)
    
    st.divider()
    st.header("🛠️ 시스템 복구")
    if st.button("🗑️ 전체 캐시 초기화", use_container_width=True, type="secondary"):
        fetch_realtime_gate_info.clear()
        load_pax_data.clear()
        load_file_list.clear()
        get_spreadsheet.clear()
        get_gspread_client.clear()
        st.session_state["last_valid_gate_df"] = pd.DataFrame()
        st.session_state["toast_msg"] = "모든 캐시를 비우고 시스템 연결을 초기화했습니다!"
        st.rerun()

with st.spinner("⏳ 실시간 게이트 및 승객 데이터를 불러오는 중입니다..."):
    df_g = fetch_realtime_gate_info(api_target_date_str)
    
    if df_g.empty:
        fetch_realtime_gate_info.clear() 
        if not st.session_state.get("last_valid_gate_df", pd.DataFrame()).empty:
            df_g = st.session_state["last_valid_gate_df"].copy()
            st.warning("⚠️ 현재 공항 서버 응답 지연으로 인해 마지막으로 수신된 정상 데이터를 표출 중입니다. (자동 복구 시도 중)")
    else:
        st.session_state["last_valid_gate_df"] = df_g.copy()

    full_pax_df = load_pax_data()
    full_files_df = load_file_list()

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
    
    if df_g.empty:
        st.error("🚨 **[공항 서버 응답 지연]** 실시간 게이트 정보를 받아오지 못했습니다. 공항 데이터 서버 점검 중이거나 응답이 지연되고 있으니 잠시 후 좌측의 `[🔄 업데이트하기]` 버튼을 눌러주세요.")
        
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
        
        # ⭐ [핵심 추가] 40분 경과한 비행기 모조리 삭제 로직 탑재!
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
        # 40분 미만인(아직 안 들어왔거나, 들어온 지 39분 이하) 비행기들만 살려둡니다.
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
        
        st.components.v1.html(
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
        
        w_html = generate_table_html(final[final['구역'] == '서편'], "⬅ 서편", west_p, "#DC2626", opt_airline, opt_peak, opt_incoming, base_font_size, target_date, now_kst_time)
        e_html = generate_table_html(final[final['구역'] == '동편'], "➡ 동편", east_p, "#2563EB", opt_airline, opt_peak, opt_incoming, base_font_size, target_date, now_kst_time)
        
        st.markdown(f'<div class="print-row">{e_html}{w_html}</div>', unsafe_allow_html=True)
