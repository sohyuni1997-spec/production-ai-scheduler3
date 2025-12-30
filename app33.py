
import streamlit as st
import pandas as pd
from supabase import create_client, Client
import requests
import re
from datetime import datetime, timedelta
import json

# 1. Supabase 설정
URL = "https://qipphcdzlmqidhrjnjtt.supabase.co"
KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InFpcHBoY2R6bG1xaWRocmpuanR0Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjY5NTIwMTIsImV4cCI6MjA4MjUyODAxMn0.AsuvjVGCLUJF_IPvQevYASaM6uRF2C6F-CjwC3eCNVk"

try:
    supabase: Client = create_client(URL, KEY)
except Exception as e:
    st.error(f"❌ Supabase 연결 실패: {e}")
    st.stop()

# 2. 고정 설정
CAPA_INFO = {"조립1": 3300, "조립2": 3700, "조립3": 3600}
CAPA_90_PERCENT = {"조립1": 2970, "조립2": 3330, "조립3": 3240}

WEEKDAY_RULES = {
    "조립2": {
        "월요일": ["FAN", "MOTOR"],
        "화요일": ["FLANGE", "MOTOR"],
        "수요일": ["FAN", "MOTOR"],
        "목요일": ["FLANGE", "MOTOR"],
        "금요일": ["FAN", "MOTOR"],
    }
}

FEW_SHOT_EXAMPLES = """
## 📚 참고할 성공 사례

### 사례 1: 2025년 10월 15일 조립2 CAPA 초과
**해결**: 조립2 → 조립1로 500개 이동, 달성률 98.5%

### 사례 2: 2025년 11월 8일 요일규칙 위반
**해결**: FAN 품목을 목요일 → 수요일로 이동, 달성률 99.2%
"""

# --- 데이터 로드 ---
@st.cache_data(ttl=600)
def fetch_data(target_date=None):
    try:
        hist_res = supabase.table("production_issue_analysis_8_11")\
            .select("최종_이슈분류, 품목명, 라인, 날짜, 누적달성률")\
            .execute()
        hist_df = pd.DataFrame(hist_res.data)

        if target_date:
            dt = datetime.strptime(target_date, '%Y-%m-%d')
            start_date = (dt - timedelta(days=2)).strftime('%Y-%m-%d')
            end_date = (dt + timedelta(days=2)).strftime('%Y-%m-%d')
            
            plan_res = supabase.table("production_plan_2026_01")\
                .select("*")\
                .gte("plan_date", start_date)\
                .lte("plan_date", end_date)\
                .execute()
            plan_df = pd.DataFrame(plan_res.data)
            
            if not plan_df.empty:
                plan_df = analyze_plan_issues(plan_df)
        else:
            plan_df = pd.DataFrame()

        return hist_df, plan_df
    except Exception as e:
        st.error(f"❌ 데이터 로드 실패: {e}")
        return pd.DataFrame(), pd.DataFrame()

# --- 사전 이슈 탐지 ---
def analyze_plan_issues(df):
    if df.empty:
        return df
    
    issues = []
    
    for date, group in df.groupby('plan_date'):
        dt = datetime.strptime(date, '%Y-%m-%d')
        weekday = dt.strftime('%A')
        weekday_kr = {'Monday': '월요일', 'Tuesday': '화요일', 'Wednesday': '수요일',
                      'Thursday': '목요일', 'Friday': '금요일', 'Saturday': '토요일', 'Sunday': '일요일'}.get(weekday, weekday)
        
        for line in group['line'].unique():
            line_data = group[group['line'] == line]
            total_qty = line_data['qty_0차'].sum() if 'qty_0차' in line_data.columns else 0
            
            if total_qty > CAPA_90_PERCENT.get(line, 9999):
                issues.append({
                    'date': date,
                    'line': line,
                    'issue_type': 'CAPA_초과',
                    'current_qty': int(total_qty),
                    'max_qty': CAPA_90_PERCENT[line],
                    'over_qty': int(total_qty - CAPA_90_PERCENT[line])
                })
            
            if line == '조립2' and weekday_kr in WEEKDAY_RULES['조립2']:
                allowed_products = WEEKDAY_RULES['조립2'][weekday_kr]
                for _, row in line_data.iterrows():
                    product = str(row.get('product_name', ''))
                    is_allowed = any(allowed in product.upper() for allowed in allowed_products)
                    if not is_allowed:
                        issues.append({
                            'date': date,
                            'line': line,
                            'issue_type': '요일규칙_위반',
                            'weekday': weekday_kr,
                            'product': product,
                            'allowed': allowed_products,
                            'qty': int(row.get('qty_0차', 0))
                        })
            
            if line == '조립2' and len(line_data) > 5:
                issues.append({
                    'date': date,
                    'line': line,
                    'issue_type': '품목수_초과',
                    'current_count': len(line_data),
                    'max_count': 5,
                    'products': list(line_data['product_name'].values)
                })
    
    df['detected_issues'] = json.dumps(issues, ensure_ascii=False) if issues else '[]'
    return df

# --- RAG: 유사 사례 검색 ---
def retrieve_similar_cases(history_df, current_issues):
    if history_df.empty or not current_issues:
        return "유사 사례 없음"
    
    issue_types = set()
    for issue in current_issues:
        if issue['issue_type'] == 'CAPA_초과':
            issue_types.add('CAPA')
        elif issue['issue_type'] == '요일규칙_위반':
            issue_types.add('요일')
        elif issue['issue_type'] == '품목수_초과':
            issue_types.add('품목')
    
    similar_cases = []
    for issue_type in issue_types:
        matched = history_df[history_df['최종_이슈분류'].str.contains(issue_type, na=False, case=False)]
        if not matched.empty:
            top_cases = matched.nlargest(3, '누적달성률') if '누적달성률' in matched.columns else matched.head(3)
            similar_cases.append(f"\n### {issue_type} 관련 과거 사례")
            for idx, row in top_cases.iterrows():
                similar_cases.append(f"- 날짜: {row.get('날짜', 'N/A')}, 품목: {row.get('품목명', 'N/A')}, "
                                   f"라인: {row.get('라인', 'N/A')}, 달성률: {row.get('누적달성률', 'N/A')}%")
    
    return "\n".join(similar_cases) if similar_cases else "유사 사례 없음"

# --- AI 답변 검증 ---
def validate_ai_response(response, current_df):
    if current_df.empty:
        return True, [], "✅ 검증할 데이터 없음"
    
    warnings = []
    details = []
    
    mentioned_dates = set()
    dates_pattern1 = re.findall(r'202[56]-\d{2}-\d{2}', response)
    mentioned_dates.update(dates_pattern1)
    
    dates_pattern2 = re.findall(r'(\d{1,2})/(\d{1,2})', response)
    for m, d in dates_pattern2:
        mentioned_dates.add(f"2026-{int(m):02d}-{int(d):02d}")
    
    dates_pattern3 = re.findall(r'(\d{1,2})월\s*(\d{1,2})일', response)
    for m, d in dates_pattern3:
        mentioned_dates.add(f"2026-{int(m):02d}-{int(d):02d}")
    
    actual_dates = set(current_df['plan_date'].unique())
    invalid_dates = mentioned_dates - actual_dates
    
    if invalid_dates:
        warnings.append({
            'type': 'DATE_MISMATCH',
            'severity': 'HIGH',
            'message': f"존재하지 않는 날짜: {', '.join(sorted(invalid_dates))}"
        })
        details.append(f"❌ **날짜 오류**: {', '.join(sorted(invalid_dates))}")
    else:
        details.append(f"✅ **날짜 검증**: 통과 ({len(mentioned_dates)}개)")
    
    mentioned_qtys = re.findall(r'\b([1-9]\d{2,})\b', response)
    mentioned_qtys = [int(q) for q in mentioned_qtys]
    
    actual_qtys = set()
    if 'qty_0차' in current_df.columns:
        actual_qtys = set(current_df['qty_0차'].dropna().astype(int))
    
    suspicious_qtys = [q for q in mentioned_qtys if q not in actual_qtys and q > 100]
    
    if len(suspicious_qtys) > 3:
        warnings.append({
            'type': 'QUANTITY_SUSPICIOUS',
            'severity': 'MEDIUM',
            'message': f"의심 수량 {len(suspicious_qtys)}개"
        })
        details.append(f"⚠️ **수량 의심**: 일부 불일치")
    else:
        details.append(f"✅ **수량 검증**: 통과")
    
    after_qtys = re.findall(r'변경\s*후\s*수량[:\s]+(\d+)', response)
    after_qtys = [int(q) for q in after_qtys]
    
    capa_violations = []
    for qty in after_qtys:
        for line, max_capa in CAPA_90_PERCENT.items():
            if qty > max_capa:
                capa_violations.append(f"{line} 초과: {qty} > {max_capa}")
    
    if capa_violations:
        warnings.append({
            'type': 'CAPA_VIOLATION',
            'severity': 'CRITICAL',
            'message': f"CAPA 위반: {capa_violations[0]}"
        })
        details.append(f"🚨 **CAPA 위반**: {capa_violations[0]}")
    else:
        details.append(f"✅ **CAPA 검증**: 통과")
    
    critical_warnings = [w for w in warnings if w['severity'] == 'CRITICAL']
    high_warnings = [w for w in warnings if w['severity'] == 'HIGH']
    is_valid = len(critical_warnings) == 0 and len(high_warnings) <= 1
    
    validation_report = "\n".join(details)
    if warnings:
        validation_report += "\n\n### ⚠️ 경고\n"
        for w in warnings:
            severity_icon = {'CRITICAL': '🚨', 'HIGH': '❌', 'MEDIUM': '⚠️'}.get(w['severity'], '⚠️')
            validation_report += f"{severity_icon} {w['message']}\n"
    
    return is_valid, warnings, validation_report

# --- AI 분석 엔진 (수정) ---
def ask_professional_scheduler(question, current_df, history_df):
    api_url = "https://ai.potens.ai/api/chat"
    api_key = "qD2gfuVAkMJexDAcFb5GnEb1SZksTs7o"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}

    if not current_df.empty:
        # ⭐ 1. 날짜별 라인별 정확한 집계
        summary = current_df.groupby(['plan_date', 'line']).agg({
            'qty_0차': 'sum',
            'product_name': 'count'
        }).reset_index()
        summary.columns = ['plan_date', 'line', 'total_qty', 'product_count']
        
        # ⭐ 2. 상세 품목 리스트 (별도)
        product_details = current_df.groupby(['plan_date', 'line']).apply(
            lambda x: x[['product_name', 'qty_0차']].to_dict('records')
        ).reset_index()
        product_details.columns = ['plan_date', 'line', 'products']
        
        # ⭐ 3. 합치기
        summary = summary.merge(product_details, on=['plan_date', 'line'])
        
        # ⭐ 4. 명확한 텍스트 형식으로 변환
        data_text = ""
        for _, row in summary.iterrows():
            data_text += f"\n## {row['plan_date']} / {row['line']}\n"
            data_text += f"**총 계획 수량: {int(row['total_qty'])}개** (품목 수: {int(row['product_count'])}개)\n"
            data_text += f"**CAPA 90% 기준: {CAPA_90_PERCENT.get(row['line'], 'N/A')}개**\n"
            
            if row['total_qty'] > CAPA_90_PERCENT.get(row['line'], 99999):
                over = int(row['total_qty'] - CAPA_90_PERCENT.get(row['line'], 0))
                data_text += f"⚠️ **CAPA 초과: {over}개 초과**\n"
            
            data_text += "\n상세 품목:\n"
            for prod in row['products']:
                data_text += f"  - {prod['product_name']}: {int(prod['qty_0차'])}개\n"
        
        detected_issues_str = current_df['detected_issues'].iloc[0] if 'detected_issues' in current_df.columns else '[]'
        detected_issues = json.loads(detected_issues_str)
    else:
        data_text = "데이터 없음"
        detected_issues = []
    
    similar_cases = retrieve_similar_cases(history_df, detected_issues)
    
    system_rules = f"""
당신은 자동차 부품 조립라인의 '수석 생산 스케줄러'입니다.

## ⚠️ 절대 규칙
1. **아래 [현재 1월 계획 데이터]에 명시된 "총 계획 수량"을 그대로 사용하세요**
2. 숫자를 절대 임의로 계산하거나 추정하지 마세요
3. 제공된 숫자를 그대로 인용하세요

## 📊 현재 1월 계획 데이터 (정확한 집계)
{data_text}

## 🚨 사전 탐지 이슈
{json.dumps(detected_issues, ensure_ascii=False, indent=2)}

## 📚 유사 과거 사례
{similar_cases}

{FEW_SHOT_EXAMPLES}

## 📏 필수 규칙
1. CAPA 90%: 조립1={CAPA_90_PERCENT['조립1']}, 조립2={CAPA_90_PERCENT['조립2']}, 조립3={CAPA_90_PERCENT['조립3']}
2. 조립2 요일제: {json.dumps(WEEKDAY_RULES['조립2'], ensure_ascii=False)}
3. 조립2 품목: 하루 최대 5품목

## 📝 출력 형식

### 대안 1: [제목]

**🔧 조치사항**
- 날짜: [실제 날짜]
- 라인: [조립1/2/3]
- 현재 상황: **위 데이터의 "총 계획 수량"을 그대로 인용**
- 제안: [구체적 변경 내용]

**📊 근거**
- 규칙: [번호]
- 현재 수량: [위 데이터 직접 인용]
- 초과량: [위 데이터 직접 인용]

**✅ 장점 / ⚠️ 단점**

---
(대안 2, 3 동일)

⚠️ 주의: 숫자는 위 [현재 1월 계획 데이터]의 "총 계획 수량"을 정확히 그대로 사용하세요.
"""

    payload = {
        "prompt": f"{system_rules}\n\n## 사용자 요청\n{question}",
        "temperature": 0.1,
        "max_tokens": 2500
    }
    
    try:
        response = requests.post(api_url, headers=headers, json=payload, timeout=90)
        response.raise_for_status()
        ai_response = response.json().get('message', '응답 생성 오류')
        
        is_valid, warnings, validation_report = validate_ai_response(ai_response, current_df)
        
        if not is_valid:
            ai_response += f"\n\n---\n## 🔍 검증 결과\n{validation_report}"
        
        return ai_response, is_valid, warnings, validation_report
        
    except Exception as e:
        return f"❌ 오류: {str(e)}", False, [], ""

# --- 날짜 추출 ---
def extract_date(text):
    patterns = [r'(\d{1,2})/(\d{1,2})', r'(\d{1,2})월\s*(\d{1,2})일']
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            m, d = match.groups()
            return f"2026-{int(m):02d}-{int(d):02d}"
    return None

# --- 메인 UI ---
st.set_page_config(page_title="AI 수석 스케줄러", layout="wide")
st.title("👨‍✈️ 수석 스케줄러 AI 통합 전략 관제 (2026.01)")

with st.sidebar:
    st.header("⚙️ 생산 라인 CAPA")
    st.json(CAPA_INFO)
    st.subheader("📏 CAPA 90%")
    st.json(CAPA_90_PERCENT)
    st.subheader("📅 조립2 요일 규칙")
    st.json(WEEKDAY_RULES)
    if st.button("🔄 데이터 동기화"):
        st.cache_data.clear()
        st.rerun()

if "messages" not in st.session_state:
    st.session_state.messages = []

if "target_date" not in st.session_state:
    st.session_state.target_date = None

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]): 
        st.markdown(msg["content"])

if prompt := st.chat_input("이슈를 입력하세요 (예: 1/14 조립2 FAN 요일위반 해결해줘)"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"): 
        st.markdown(prompt)

    target_date = extract_date(prompt)
    st.session_state.target_date = target_date
    
    with st.spinner("🚀 분석 중..."):
        history_df, current_plan = fetch_data(target_date)
        answer, is_valid, warnings, validation_report = ask_professional_scheduler(prompt, current_plan, history_df)
        
        st.session_state.messages.append({"role": "assistant", "content": answer})
        
        with st.chat_message("assistant"):
            st.markdown(answer)
            
            if is_valid:
                st.success("✅ AI 답변 검증 통과")
            else:
                st.warning("⚠️ 일부 불일치 발견")
            
            with st.expander("🔍 상세 검증 결과"):
                st.markdown(validation_report)
            
            col1, col2 = st.columns(2)
            with col1:
                if not current_plan.empty:
                    with st.expander("📍 1월 계획 원본"):
                        display_df = current_plan.drop(columns=['detected_issues'], errors='ignore')
                        st.dataframe(display_df)
            
            with col2:
                if not history_df.empty:
                    with st.expander("📚 과거 이슈 Top 5"):
                        issue_summary = history_df['최종_이슈분류'].value_counts().head(5)
                        st.bar_chart(issue_summary)

with st.expander("🐛 디버그: 사전 탐지 이슈"):
    if st.session_state.target_date:
        _, debug_plan = fetch_data(st.session_state.target_date)
        if not debug_plan.empty and 'detected_issues' in debug_plan.columns:
            detected = json.loads(debug_plan['detected_issues'].iloc[0])
            st.json(detected)
    else:
        st.info("💡 날짜가 포함된 질문을 입력하면 디버그 정보가 표시됩니다.")
