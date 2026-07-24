import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
import re
import io 
import requests
from datetime import datetime, timedelta, timezone

# ⭐ 병렬 처리를 위한 라이브러리 추가
import concurrent.futures
import threading
from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx

# 1. 페이지 설정 (UI 렌더링이 가장 먼저 되도록 최상단 유지)
st.set_page_config(page_title="T2 보안검색 환승부 잡지", layout="wide")

# 앱 최초 실행 시 마지막 업데이트 시간 초기화
if "last_updated" not in st.session_state:
    st.session_state["last_updated"] = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S")

# ⭐ [구글 시트 연동 설정]
SHEET_NAME = "보안검색_데이터_공유" 

@st.cache_resource(show_spinner=False)
def get_gspread_client():
    creds_dict = dict(st.secrets["gcp"])
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)

@st.cache_resource(show_spinner=False)
def get_spreadsheet():
    client = get_gspread_client()
    return client.open(SHEET_NAME)

def save_to_sheet(df, sheet_name):
    try:
        spreadsheet = get_spreadsheet()
        try:
            sheet = spreadsheet.worksheet(sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            sheet = spreadsheet.add_worksheet(title=sheet_name, rows="1000", cols="20")
        sheet.clear()
        data_to_save = [df.columns.values.tolist()] + df.fillna("").astype(str).values.tolist()
        sheet.update(range_name="A1", values=data_to_save)
        load_from_sheet.clear() 
        return True
    except Exception as e:
        st.sidebar.error(f"⚠ 데이터 저장 실패: {e}")
        return False

def append_file_names(new_names):
    if not new_names: return
    try:
        spreadsheet = get_spreadsheet()
        try:
            sheet = spreadsheet.worksheet("file_list")
        except gspread.exceptions.WorksheetNotFound:
            sheet = spreadsheet.add_worksheet(title="file_list", rows="100", cols="1")
        existing_list = load_file_names()
        combined = list(set(existing_list + new_names))
        sheet.clear()
        df = pd.DataFrame(combined, columns=["파일명"])
        data_to_save = [df.columns.values.tolist()] + df.values.tolist()
        sheet.update(range_name="A1", values=data_to_save)
        load_file_names.clear() 
    except Exception as e:
        st.sidebar.error(f"⚠ 파일 목록 저장 실패: {e}")

# [최적화] 병렬 처리 시 스피너 충돌을 막기 위해 개별 show_spinner는 False로 설정
@st.cache_data(ttl=1800, show_spinner=False)
def load_file_names():
    try:
        spreadsheet = get_spreadsheet()
        sheet = spreadsheet.worksheet("file_list")
        data = sheet.get_all_values()
        if len(data) > 1:
            return [row[0] for row in data[1:] if row and row[0].strip() != ""]
    except gspread.exceptions.WorksheetNotFound:
        pass
    except Exception as e:
        st.sidebar.error(f"⚠ 파일 목록 불러오기 실패: {e}")
    return []

@st.cache_data(ttl=1800, show_spinner=False)
def load_from_sheet(sheet_name):
    try:
        spreadsheet = get_spreadsheet()
        sheet = spreadsheet.worksheet(sheet_name)
        data = sheet.get_all_values()
        if len(data) > 1:
            return pd.DataFrame(data[1:], columns=data[0])
    except gspread.exceptions.WorksheetNotFound:
        pass
    except Exception as e:
        st.sidebar.error(f"⚠ 데이터 불러오기 실패: {e}")
    return pd.DataFrame()

def clear_sheet(sheet_name):
    try:
        spreadsheet = get_spreadsheet()
        sheet = spreadsheet.worksheet(sheet_name)
        sheet.clear()
        load_from_sheet.clear() 
        load_file_names.clear()
    except gspread.exceptions.WorksheetNotFound:
        pass
    except Exception as e:
        st.sidebar.error(f"⚠ 데이터 비우기 실패: {e}")

# ⭐ [실시간 게이트 데이터 API 연동]
@st.cache_data(ttl=1800, show_spinner=False)
def fetch_realtime_gate_info(search_date_str):
    api_key = st.secrets["api"]["service_key"]
    url = "http://apis.data.go.kr/B551177/statusOfAllFltDeOdp/getFltArrivalsDeOdp"
    
    req_url = f"{url}?serviceKey={api_key}&searchdtCode=S&searchDate={search_date_str}&searchFrom=0000&searchTo=2359&passengerOrCargo=P&type=json&numOfRows=1800&pageNo=1"
    
    try:
        response = requests.get(req_url, timeout=15)
        if response.status_code != 200:
            return pd.DataFrame()
            
        data = response.json()
        items = []
        if 'response' in data and 'body' in data['response'] and 'items' in data['response']['body']:
            item_data = data['response']['body']['items']
            if isinstance(item_data, dict) and 'item' in item_data:
                item_data = item_data['item']
            elif not isinstance(item_data, list):
                item_data = [item_data]
                
            for item in item_data:
                flight_id = item.get('flightId', '').replace('DAL', 'DL').replace('KAL', 'KE').replace('AAR', 'OZ')
                
                raw_time = str(item.get('estimatedDatetime', '') or item.get('scheduleDatetime', ''))[-4:]
                formatted_time = f"{raw_time[:2]}:{raw_time[2:]}" if len(raw_time) == 4 else raw_time
                
                items.append({
                    '편명': clean_flight_no(flight_id),
                    '시간': formatted_time,
                    '게이트': item.get('gateNumber') or item.get('fstandPosition', ''),
                    '출발지': item.get('airportCode', '') or item.get('airport', ''),
                    '출구': item.get('exitNumber', '')
                })
        
        df = pd.DataFrame(items)
        
        if not df.empty:
            df = df[df['편명'].str.startswith(('KE', 'OZ', 'DL'), na=False)]
            
        return df
    except Exception as e:
        return pd.DataFrame()

if "toast_msg" in st.session_state:
    st.toast(st.session_state["toast_msg"], icon="✅")
    del st.session_state["toast_msg"]

# --- [디자인 CSS] ---
st.markdown("""
    <style>
    .main .block-container { padding-top: 0px !important; padding-bottom: 0px !important; margin-top: -15px !important; }
    div[data-testid="stVerticalBlock"] { gap: 0px !important; }
    .element-container { margin-bottom: 0px !important; }
    iframe { margin-bottom: 0px !important; min-height: 45px !important; }
    
    .file-box { background-color:#f0f7ff; padding:15px; border-radius:5px; margin-bottom:15px; border: 1px solid #3b82f6; display: block; overflow: visible; }
    .file-item { font-size:13px; margin: 0 0 6px 10px !important; line-height: 1.5 !important; color: #1f2937; font-weight: normal; word-break: break-all; }
    .file-box-title { font-size:14px; font-weight:bold; color:#1E3A8A; margin: 0 0 10px 0 !important; line-height: 1.4 !important; }
    
    .merged-table { width: 100%; border-collapse: collapse; text-align: center; font-family: sans-serif; margin-bottom: 0px !important; }
    .merged-table tr { border: none !important; } 
    .merged-table th { background-color: #f8f9fa !important; border: 1px solid #dee2e6 !important; padding: 4px; font-weight: bold; }
    .merged-table td { border: 1px solid #dee2e6 !important; padding: 3px; vertical-align: middle; font-weight: bold !important; }
    
    .sum-cell { font-weight: bold; color: #1E3A8A; vertical-align: middle !important; }
    
    .total-banner { background-color: #f0f7ff !important; padding: 4px 8px !important; border-radius: 8px; text-align: center; border: 1px solid #3b82f6; margin-bottom: 2px; margin-top: 2px; }
    .carrier-banner { background-color: #ffffff !important; padding: 4px; border-radius: 8px; text-align: center; border: 1px solid #3b82f6; margin-bottom: 4px; display: flex; justify-content: center; gap: 20px; flex-wrap: wrap; }
    .carrier-item { font-size: 14px; font-weight: bold; }
    .print-row { display: flex; flex-direction: row; gap: 15px; width: 100%; }
    .print-col { flex: 1; min-width: 0; margin-bottom: 0px !important; }
    
    @media print {
        .no-print, header, footer, [data-testid="stSidebar"], [data-testid="stHeader"], [data-testid="stToolbar"], iframe { display: none !important; }
        html, body { height: auto !important; min-height: auto !important; padding-bottom: 0 !important; margin-bottom: 0 !important; padding-top: 0 !important; }
        .appview-container, .main, .block-container, .element-container { padding-top: 0 !important; margin-top: 0 !important; padding-bottom: 0 !important; margin-bottom: 0 !important; }
        div[data-testid="stVerticalBlock"] { gap: 0 !important; }
        body { zoom: 75%; }
        .print-row { display: flex !important; flex-direction: row !important; }
        table { page-break-inside: auto; margin-bottom: 0px !important; }
        tr { page-break-inside: avoid; page-break-after: auto; }
        thead { display: table-header-group; }
        @page { size: A4; margin-top: 12mm !important; margin-bottom: 12mm !important; margin-left: 10mm !important; margin-right: 10mm !important; }
        @page :first { margin-top: 0mm !important; }
    }
    </style>
""", unsafe_allow_html=True)

# --- [도구함 (데이터 처리 로직)] ---
def clean_flight_no(val):
    if pd.isna(val): return ""
    val = str(val).strip().replace(" ", "").upper()
    match = re.match(r'([A-Z]+)(\d+)', val)
    if match: return f"{match.group(1)}{int(match.group(2)):03d}"
    return val

def smart_read(file):
    filename = file.name.lower()
    df = None
    try:
        if filename.endswith('.csv'):
            encodings = ['utf-8', 'cp949', 'euc-kr', 'utf-16', 'utf-8-sig']
            for enc in encodings:
                try:
                    file.seek(0)
                    df = pd.read_csv(file, encoding=enc)
                    break
                except: pass
        elif filename.endswith('.xls'):
            try:
                file.seek(0)
                df = pd.read_excel(file, engine='xlrd')
            except:
                try:
                    file.seek(0)
                    raw_data = file.read()
                    for enc in ['cp949', 'euc-kr', 'utf-8']:
                        try:
                            html_str = raw_data.decode(enc)
                            dfs = pd.read_html(io.StringIO(html_str))
                            if dfs: 
                                df = dfs[0]
                                break
                        except: pass
                except: pass
        else:
            file.seek(0)
            df = pd.read_excel(file, engine='openpyxl')
    except:
        try:
            file.seek(0)
            df = pd.read_excel(file)
        except: return None
        
    if df is None or df.empty: return None
    all_data = [df.columns.tolist()] + df.values.tolist()
    header_idx = -1
    for i, row in enumerate(all_data[:20]):
        row_str = "".join([str(x).upper() for x in row])
        if 'FLT' in row_str or '편명' in row_str or 'FLIGHT' in row_str:
            header_idx = i
            break
            
    if header_idx > 0:
        new_header = all_data[header_idx]
        new_data = all_data[header_idx+1:]
        df = pd.DataFrame(new_data, columns=new_header)
        
    df.columns = [str(c) if pd.notna(c) else f"Unnamed_{i}" for i, c in enumerate(df.columns)]
    return df

def parse_dl_pax(df):
    if df is None or df.empty: return None
    all_rows = [df.columns.tolist()] + df.values.tolist()
    pax_row_idx = -1
    pax_row_data = []
    header_row_data = []
    
    for i, row in enumerate(all_rows):
        for cell in row:
            if str(cell).replace(" ", "").strip() == '환승객':
                pax_row_idx = i
                pax_row_data = row
                break
        if pax_row_idx != -1: break
        
    if pax_row_idx != -1:
        header_row_data = all_rows[0]
        dl_data = []
        for col_idx, cell in enumerate(header_row_data):
            cell_str = str(cell)
            if 'DL' in cell_str.upper() and re.search(r'DL\s*\d+', cell_str, re.IGNORECASE):
                flt_no = re.search(r'(DL\s*\d+)', cell_str, re.IGNORECASE).group(1).replace(" ", "").upper()
                flt_no = clean_flight_no(flt_no) 
                
                if col_idx < len(pax_row_data):
                    pax_val = str(pax_row_data[col_idx]).replace(",", "").strip()
                    try:
                        pax_count = int(float(pax_val))
                        dl_data.append({'편명': flt_no, '승객수': pax_count})
                    except: pass
        if dl_data: return pd.DataFrame(dl_data)
    return None

def find_col(df, keywords):
    if df is None or df.empty: return None
    for col in df.columns:
        clean_col = str(col).replace(" ", "").replace("/", "").replace("_", "").replace(".", "").upper()
        for key in keywords:
            if key.upper() in clean_col: return col
    return None

IATA_CITY_MAP = {
    "LIS": "리스본", "HFE": "허페이", "KUH": "쿠시로", "KIX": "오사카", "NRT": "나리타", "HKG": "홍콩", 
    "TSN": "톈진", "CTS": "삿포로", "MFM": "마카오", "AKL": "오클랜드", "UKB": "고베", "KOJ": "가고시마",
    "DLC": "다롄", "LHR": "런던", "BUD": "부다페스트", "CDG": "파리", "PEK": "베이징", "NGO": "나고야", 
    "YNZ": "옌청", "PVG": "상하이/푸둥", "CGQ": "창춘", "KIJ": "니가타", "LAX": "로스앤젤레스", "HND": "하네다",
    "JFK": "뉴욕", "ATL": "애틀랜타", "DTW": "디트로이트", "SEA": "시애틀", "SFO": "샌프란시스코", "FRA": "프랑크푸르트", 
    "FCO": "로마", "BKK": "방콕", "SGN": "호치민", "HAN": "하노이", "MNL": "마닐라", "CEB": "세부",
    "SIN": "싱가포르", "SYD": "시드니", "BNE": "브리즈번", "TPE": "타이베이", "CAN": "광저우", "TAO": "칭다오", 
    "FUK": "후쿠오카", "OKA": "오키나와", "MSP": "미니애폴리스", "DFW": "댈러스", "ORD": "시카고", "YVR": "밴쿠버",
    "YYZ": "토론토", "AMS": "암스테르담", "IST": "이스탄불", "DXB": "두바이", "CJU": "제주", "PUS": "부산", 
    "HNL": "호놀룰루", "BOS": "보스턴", "IAD": "워싱턴DC", "LAS": "라스베이거스", "MUC": "뮌헨", "PRG": "프라하",
    "ZRH": "취리히", "VIE": "빈", "MAD": "마드리드", "BCN": "바르셀로나", "MXP": "밀라노", "DEL": "델리", 
    "BOM": "뭄바이", "CGK": "자카르타", "DPS": "발리", "PNH": "프놈펜", "REP": "씨엠립", "VTE": "비엔티안",
    "DAD": "다낭", "CXR": "나트랑", "PQC": "푸꾸옥", "HKT": "푸껫", "CNX": "치앙마이", "RGN": "양곤", 
    "KUL": "쿠알라룸푸르", "BKI": "코타키나발루", "PEN": "페낭", "GUM": "괌", "SPN": "사이판", "ROR": "팔라우", 
    "UBN": "울란바토르", "KTI": "떼조", "TAE": "대구", "SHE": "심양", "HRB": "하얼빈", "SZX": "선전", "SLC": "솔트레이크시티",
    "NGS": "나가사키", "YNJ": "옌지", "TAS": "타슈켄트", "ALA": "알마티", "TFU": "청두", "KMQ": "고마츠",
    "HGH": "항저우", "NKG": "난징", "XIY": "시안", "FOC": "푸저우", "CGO": "정저우", "CKG": "충칭",
    "CSX": "장사", "KMG": "쿤밍", "DYG": "장가계", "KTM": "카트만두", "CRK": "클라크필드", "SDJ": "센다이",
    "OKJ": "오카야마", "AOJ": "아오모리", "WUH": "우한", "XMN": "샤먼", "KMI": "미야자키",  "KMJ": "구마모토", "OSL": "오슬로",
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
    html = f"<div class='print-col'><h3 style='text-align:center; color:{color}; font-size:16px; margin-top:2px; margin-bottom:5px;'>{display_title}</h3>"
    if df.empty: return html + "<div style='text-align:center; padding:20px; border:1px solid #ddd;'>데이터 없음</div></div>"
    
    df = df.sort_values('시간').reset_index(drop=True)
    
    html += f'<table class="merged-table" style="font-size: {font_size}px !important;"><thead><tr>'
    html += f'<th style="width:14%; font-size:{font_size}px !important;">시간</th>'
    html += f'<th style="width:18%; font-size:{font_size}px !important;">편명</th>'
    html += f'<th style="font-size:{font_size}px !important;">출발지</th>'
    html += f'<th style="width:14%; font-size:{font_size}px !important;">게이트</th>'
    html += f'<th style="width:13%; font-size:{font_size}px !important;">승객</th>'
    html += f'<th style="width:13%; font-size:{font_size}px !important;">합계</th>'
    html += f'</tr></thead><tbody>'
    
    df['hour_val'] = df['시간'].astype(str).str.extract(r'^(\d{1,2})').fillna(0).astype(int)
    hour_counts = df['hour_val'].value_counts().sort_index()
    hour_sums = df.groupby('hour_val')['p_val'].sum()
    processed_hours = set()
    
    for i, row in df.iterrows():
        current_h, flt = row['hour_val'], str(row['편명']).upper()
        row_style_css, text_style = "", ""
        
        is_past_20_mins = False
        is_blinking = False
        
        try:
            time_parts = str(row['시간']).split(':')
            if len(time_parts) == 2:
                f_hour, f_min = int(time_parts[0]), int(time_parts[1])
                flight_dt = target_date.replace(hour=f_hour, minute=f_min, second=0, microsecond=0)
                
                if flight_dt <= now_kst - timedelta(minutes=20):
                    is_past_20_mins = True
                elif now_kst - timedelta(minutes=10) <= flight_dt <= now_kst + timedelta(minutes=10):
                    is_blinking = True
        except: pass
            
        if is_past_20_mins:
            text_style = " text-decoration: line-through; color: #6B7280;"
            row_style_css = "background-color: #F9FAFB;" 
        elif opt_incoming and is_blinking:
            row_style_css = "background-color: #EDE9FE;"
        else:
            if opt_airline:
                if flt.startswith("DL"): row_style_css = "background-color: #E3F2FD;" 
                elif flt.startswith("OZ"): row_style_css = "background-color: #FDF4F7;" 
            elif opt_peak:
                if current_h == 16: row_style_css = "background-color: #F4FAFD;" 
                elif current_h == 17: row_style_css = "background-color: #FFFDF0;" 
                elif current_h == 18: row_style_css = "background-color: #FFF5F8;" 
            else:
                row_style_css = "background-color: #ffffff;"
                
        td_style = f' style="{row_style_css} font-size: {font_size}px !important; font-weight: bold !important;{text_style}"'
        
        html += f'<tr><td{td_style}>{row["시간"]}</td><td{td_style}>{row["편명"]}</td><td{td_style}>{row.get("출발지", "")}</td><td{td_style}>{row["게이트"]}</td><td{td_style}>{row["p_display"]}</td>'
        
        if current_h not in processed_hours:
            sum_font = font_size + 1
            html += f'<td rowspan="{hour_counts[current_h]}" class="sum-cell" style="background-color: #ffffff !important; font-size: {sum_font}px !important; font-weight: bold !important;"><div style="position: relative; z-index: 10;">{hour_sums[current_h]:,}</div></td>'
            processed_hours.add(current_h)
        html += '</tr>'
    return html + '</tbody></table></div>'


# --- [사이드바 설정] ---
with st.sidebar:
    file_list_placeholder = st.container()
    st.divider()

    date_option = st.radio("📅 표시 날짜 선택", ["오늘", "내일 (+1일)"], index=0)
    
    KST = timezone(timedelta(hours=9))
    today_date = datetime.now(KST)
    target_date = today_date + timedelta(days=1) if date_option == "내일 (+1일)" else today_date
        
    display_date_str = target_date.strftime("%Y년 %m월 %d일")
    api_target_date_str = target_date.strftime("%Y%m%d")
    
    st.divider()
    
    # ⭐ 시각화 옵션 복원 (선택 가능)
    vis_option = st.radio("🎨 시각화 옵션", ["✈ 항공사별 색상 표시 (DL, OZ)", "⏰ 첨두시간 색상 표시 (16~18시)", "곧 들어오는 비행기 표시 (연보라색)", "적용 안 함"], index=2)
    opt_airline = (vis_option == "✈ 항공사별 색상 표시 (DL, OZ)")
    opt_peak = (vis_option == "⏰ 첨두시간 색상 표시 (16~18시)")
    opt_incoming = (vis_option == "곧 들어오는 비행기 표시 (연보라색)")
    
    time_range = st.slider("조회 시간대 (시)", 0, 24, (0, 24))
    base_font_size = st.slider("🔠 표 글자 조절 (px)", min_value=10, max_value=17, value=12, step=1)
    
    st.divider()

    st.header("🔄 실시간 업데이트")
    if st.button("🔄 업데이트하기", use_container_width=True):
        fetch_realtime_gate_info.clear()
        st.session_state["toast_msg"] = "게이트 정보를 최신 상태로 업데이트했습니다!"
        KST = timezone(timedelta(hours=9))
        st.session_state["last_updated"] = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
        st.rerun()
        
    if "last_updated" in st.session_state:
        st.caption(f"마지막 업데이트: {st.session_state['last_updated']}")
        st.caption("⚠️ 잦은 업데이트 시 트래픽 허용량 초과로 기능이 정지 될 수 있습니다.(자정 초기화)")

    st.divider()
    
    st.header("🛠️ 시스템 복구")
    st.caption("에러코드 발생 시 눌러주세요.")
    if st.button("🗑️ 전체 캐시 초기화", use_container_width=True, type="secondary"):
        fetch_realtime_gate_info.clear()
        load_from_sheet.clear()
        load_file_names.clear()
        get_spreadsheet.clear()
        get_gspread_client.clear()
        
        st.session_state["toast_msg"] = "모든 캐시를 비우고 시스템 연결을 초기화했습니다!"
        st.rerun()

ctx = get_script_run_ctx()

def thread_wrapper(func, *args):
    add_script_run_ctx(threading.current_thread(), ctx)
    return func(*args)

with st.spinner("⏳ 실시간 게이트 및 승객 데이터를 불러오는 중입니다..."):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future_api = executor.submit(thread_wrapper, fetch_realtime_gate_info, api_target_date_str)
        
    saved_pax_df = load_from_sheet("pax_data")
    saved_files = load_file_names()
    df_g = future_api.result()

with file_list_placeholder:
    if not saved_pax_df.empty:
        with st.expander("✅ 현재 공유중인 승객 데이터 목록", expanded=True):
            if saved_files:
                for fname in saved_files:
                    st.markdown(f"<p class='file-item'>• {fname}</p>", unsafe_allow_html=True)
            else:
                st.markdown("<p class='file-item'>• 데이터 적용 완료</p>", unsafe_allow_html=True)

st.markdown(f"""
    <style>
    .merged-table, .merged-table th, .merged-table td {{ font-size: {base_font_size}px !important; font-weight: bold !important; }}
    .sum-cell {{ font-size: {base_font_size + 1}px !important; font-weight: bold !important; }}
    </style>
""", unsafe_allow_html=True)

# --- [메인 로직] ---
p_all = []
if not saved_pax_df.empty:
    p_all.append(saved_pax_df)

if not p_all or df_g.empty:
    st.markdown("<h2 style='text-align: center;'>✈ T2 보안검색 환승부 잡지 (실시간 연동) ✈</h2>", unsafe_allow_html=True)
    with st.expander("💡 홈페이지 이용 방법 (필독)", expanded=True):
        st.markdown("""
        ### 🌐 데이터 공유 방식 안내
        * **자동 공유:** 서버에 연결된 데이터를 자동으로 불러옵니다.
        * **실시간 게이트 연동:** 게이트 정보는 실시간으로 도착편을 조회합니다.
        * **업데이트:** 게이트 정보가 변경되었을 수 있으니 언제든 사이드바의 **[🔄 업데이트하기]** 버튼을 눌러주세요.
        * **스크롤 유지:** 자동 갱신 시에도 보시던 화면 위치가 그대로 유지됩니다.
        """)
    if df_g.empty:
        st.info(f"🔄 {display_date_str}의 실시간 공항 API에서 게이트 데이터를 불러오는 중이거나 데이터가 없습니다.")
else:
    df_p = pd.concat(p_all).drop_duplicates('편명')
    final = pd.merge(df_g, df_p, on='편명', how='inner', suffixes=('_api', '_pax'))
    
    if '출발지_pax' in final.columns:
        final['출발지'] = final.apply(
            lambda row: row['출발지_api'] if pd.isna(row['출발지_pax']) or str(row['출발지_pax']).strip() == '' else row['출발지_pax'], 
            axis=1
        )
    else:
        final['출발지'] = final['출발지_api']
        
    if '출발지' in final.columns:
        final['출발지'] = final['출발지'].apply(format_route)
        final = final[~final['출발지'].astype(str).str.contains('PUS|김해|부산', case=False, na=False)]
    
    if not final.empty:
        final['p_val'] = pd.to_numeric(final['승객수'], errors='coerce').fillna(0).astype(int)
        
        def format_pax_display(val):
            if pd.isna(val) or str(val).strip() == '': return ""
            try:
                cleaned_val = str(val).replace(',', '').strip()
                if cleaned_val == '': return ""
                return f"{int(float(cleaned_val)):,}"
            except: return ""
                
        final['p_display'] = final['승객수'].apply(format_pax_display)
        
        final['hour'] = final['시간'].astype(str).str.extract(r'^(\d{1,2})').fillna(0).astype(int)
        final = final[(final['hour'] >= time_range[0]) & (final['hour'] <= time_range[1])]
        
        if '출구' not in final.columns: final['출구'] = ""
        final['g_num'] = pd.to_numeric(final['게이트'], errors='coerce').fillna(0)
        
        def get_zone(row):
            if row['g_num'] > 0:
                return '서편' if 0 < row['g_num'] <= 250 else '동편'
            else:
                exit_val = str(row.get('출구', '')).strip().upper()
                return '서편' if exit_val == 'A' else '동편'
        
        def get_gate_str(row):
            if row['g_num'] > 0:
                return str(int(row['g_num']))
            else:
                return '-'
        
        final['구역'] = final.apply(get_zone, axis=1)
        final['게이트'] = final.apply(get_gate_str, axis=1)
        
        total_p = final['p_val'].sum()
        def c_sum(c): return final[final['편명'].str.startswith(c, na=False)]['p_val'].sum()
        ke_s, oz_s, dl_s = c_sum('KE'), c_sum('OZ'), c_sum('DL')
        
        # ⭐⭐⭐ 5분 자동 새로고침 및 스크롤 기억 보강 적용 스크립트 ⭐⭐⭐
        st.components.v1.html(
            """
            <style>
            body { margin: 0; padding: 0; overflow: hidden; display: flex; gap: 10px; }
            .custom-btn {
                background-color: white; border: 1px solid #dcdcdc; color: #31333f;
                padding: 6px 15px; font-size: 14px; border-radius: 6px; cursor: pointer;
                font-family: sans-serif; box-shadow: 0px 1px 3px rgba(0,0,0,0.1);
            }
            .custom-btn:hover { border-color: #ff4b4b; color: #ff4b4b; }
            </style>
            <button class="custom-btn" onclick="window.parent.print()">📄 PDF 저장</button>
            <button class="custom-btn" onclick="takePic()" id="pic-btn">📸 전체 사진으로 저장</button>
            
            <script>
            var parentWin = window.parent;
            var parentDoc = parentWin.document;

            // ⭐ 30분마다 자동으로 새로고침하여 현재 시간(-10~+10분)을 실시간으로 반영
            setTimeout(function() {
                parentWin.location.reload();
            }, 1800000);

            function takePic() {
                var btn = document.getElementById('pic-btn');
                btn.innerText = "⏳ 캡처 중... 잠시만요!";
                try {
                    if (!parentWin.html2canvas) {
                        var script = parentDoc.createElement('script');
                        script.src = "https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js";
                        script.onload = function() { doCap(parentWin, parentDoc, btn); };
                        script.onerror = function() { 
                            alert("⚠ 라이브러리를 불러올 수 없습니다."); 
                            btn.innerText = "📸 전체 사진으로 저장"; 
                        };
                        parentDoc.head.appendChild(script);
                    } else {
                        doCap(parentWin, parentDoc, btn);
                    }
                } catch(e) {
                    alert("⚠ 브라우저 보안 설정으로 인해 캡처가 차단되었습니다.");
                    btn.innerText = "📸 전체 사진으로 저장";
                }
            }
            
            function doCap(win, doc, btn) {
                var target = doc.querySelector('.block-container') || doc.querySelector('.main');
                var hides = doc.querySelectorAll('[data-testid="stSidebar"], header, iframe');
                
                var appView = doc.querySelector('.appview-container') || doc.querySelector('[data-testid="stAppViewContainer"]');
                var mainView = doc.querySelector('.main');
                
                var oldAppOverflow = appView ? appView.style.overflow : '';
                var oldAppHeight = appView ? appView.style.height : '';
                var oldMainOverflow = mainView ? mainView.style.overflow : '';
                var oldMainHeight = mainView ? mainView.style.height : '';
                var oldTargetPaddingTop = target.style.paddingTop;
                var oldTargetMarginTop = target.style.marginTop;
                var oldTargetWidth = target.style.width;
                var oldTargetMaxWidth = target.style.maxWidth;
                if(appView) { appView.style.overflow = 'visible'; appView.style.height = 'auto'; }
                if(mainView) { mainView.style.overflow = 'visible'; mainView.style.height = 'auto'; }
                target.style.paddingTop = '10px';
                target.style.marginTop = '0px';
                target.style.width = '1100px'; 
                target.style.maxWidth = '1100px';
                hides.forEach(function(e){ e.dataset.old = e.style.display; e.style.display = 'none'; });
                
                setTimeout(function() {
                    win.html2canvas(target, { 
                        scale: 6, 
                        useCORS: true, 
                        backgroundColor: '#ffffff'
                    }).then(function(canvas) {
                        var link = doc.createElement('a');
                        link.download = '보안검색_잡지_전체.png';
                        link.href = canvas.toDataURL('image/png');
                        link.click();
                    }).catch(function(err) {
                        alert("사진 생성 중 오류가 발생했습니다.");
                    }).finally(function() {
                        if(appView) { appView.style.overflow = oldAppOverflow; appView.style.height = oldAppHeight; }
                        if(mainView) { mainView.style.overflow = oldMainOverflow; mainView.style.height = oldMainHeight; }
                        
                        target.style.paddingTop = oldTargetPaddingTop;
                        target.style.marginTop = oldTargetMarginTop;
                        target.style.width = oldTargetWidth;
                        target.style.maxWidth = oldTargetMaxWidth;
                        hides.forEach(function(e){ e.style.display = e.dataset.old || ''; });
                        btn.innerText = "📸 전체 사진으로 저장";
                    });
                }, 800);
            }

            function doScrollLogic() {
                var scrollContainer = parentDoc.querySelector('.main') || parentWin;
                var savedScroll = parentWin.sessionStorage.getItem('stScrollPos');
                if (savedScroll) {
                    if (scrollContainer.scrollTo) {
                        scrollContainer.scrollTo(0, parseInt(savedScroll));
                    }
                }
            }

            // ⭐ 스크롤 위치를 다중 타이머로 확실하게 복원 (업데이트 버튼 클릭 시 스크롤 유지)
            setTimeout(doScrollLogic, 100);
            setTimeout(doScrollLogic, 300);
            setTimeout(doScrollLogic, 600);
            setTimeout(doScrollLogic, 1000);

            setInterval(function() {
                var scrollContainer = parentDoc.querySelector('.main') || parentWin;
                var scrollTop = scrollContainer.scrollTop || parentWin.scrollY || 0;
                if(scrollTop > 0) {
                    parentWin.sessionStorage.setItem('stScrollPos', scrollTop);
                }
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
        
        w_html = generate_table_html(final[final['구역'] == '서편'], "⬅ 서편", west_p, "#DC2626", opt_airline, opt_peak, opt_incoming, base_font_size, target_date, today_date)
        e_html = generate_table_html(final[final['구역'] == '동편'], "➡ 동편", east_p, "#2563EB", opt_airline, opt_peak, opt_incoming, base_font_size, target_date, today_date)
        
        st.markdown(f'<div class="print-row">{e_html}{w_html}</div>', unsafe_allow_html=True)
    else:
        st.warning(f"⚠ 업로드한 승객 파일과 일치하는 {display_date_str} 실시간 도착편 정보가 없습니다.")
