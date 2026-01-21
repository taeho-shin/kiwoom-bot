import os
import requests
import json
import math
from flask import Flask, request, jsonify
from datetime import datetime
from collections import deque

app = Flask(__name__)

# --- [1. 환경변수 및 설정] ---
# Render 환경변수에서 가져오거나, 없으면 기본값 사용
APP_KEY = os.environ.get("APP_KEY")
APP_SECRET = os.environ.get("APP_SECRET")
# 잔고 조회를 위해 계좌번호가 필수입니다. (환경변수 설정 권장)
ACCOUNT_NO = os.environ.get("ACCOUNT_NO", "81185095") 
BASE_URL = "https://mockapi.kiwoom.com"

# [매수 설정] 1회 진입 목표 금액 (원)
# 예: 100만원 설정 시, 삼성전자(5만)는 20주, 하이닉스(10만)는 10주 매수
TARGET_BUY_AMOUNT = 1000000 

# [로그 설정] 최근 50개 로그를 저장할 리스트
server_logs = deque(maxlen=50)

# 전역 변수
ACCESS_TOKEN = None

# --- [2. 헬퍼 함수: 로그 기록] ---
def add_log(message):
    """시스템 로그를 메모리에 저장하고 콘솔에도 출력"""
    time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_entry = f"[{time_str}] {message}"
    print(log_entry) # 콘솔 출력 (Render Logs)
    server_logs.appendleft(log_entry) # 웹 표시용 리스트에 추가 (최신순)

class KiwoomAPI:
    def __init__(self):
        self.base_url = BASE_URL
        self.headers = {"Content-Type": "application/json;charset=UTF-8"}

    def get_token(self):
        global ACCESS_TOKEN
        url = f"{self.base_url}/oauth2/token"
        headers = self.headers.copy()
        data = {
            "grant_type": "client_credentials",
            "appkey": APP_KEY,
            "secretkey": APP_SECRET
        }
        try:
            if not APP_KEY or not APP_SECRET:
                add_log("❌ [설정 오류] APP_KEY 또는 APP_SECRET 환경변수 누락")
                return False

            res = requests.post(url, headers=headers, data=json.dumps(data))
            if res.status_code == 200:
                resp = res.json()
                ACCESS_TOKEN = resp.get("token") or resp.get("access_token")
                add_log(f"✅ [인증 성공] 토큰 발급 완료")
                return True
            else:
                add_log(f"❌ [인증 실패] {res.text}")
                return False
        except Exception as e:
            add_log(f"❌ [연결 오류] {e}")
            return False

    def get_stock_balance(self, ticker):
        """특정 종목의 보유 수량 확인 (잔고 조회 API)"""
        global ACCESS_TOKEN
        if not ACCESS_TOKEN: self.get_token()

        url = f"{self.base_url}/api/dostk/inqr/bal"
        headers = self.headers.copy()
        headers.update({
            "authorization": f"Bearer {ACCESS_TOKEN}",
            "appkey": APP_KEY,
            "appsecret": APP_SECRET,
            "tr_id": "vt00018", # 잔고조회용 TR ID
            "custtype": "P"
        })

        params = {
            "cano": ACCOUNT_NO,
            "acnt_prdt_cd": "01",
            "ovrs_excg_cd": "KRX",
            "tr_cont": "N",
            "ctx_area_fk": "",
            "ctx_area_nk": ""
        }

        try:
            res = requests.get(url, headers=headers, params=params)
            
            if res.status_code == 200:
                data = res.json()
                output2 = data.get('output2', [])
                
                # 보유 종목 리스트에서 해당 티커 찾기
                for stock in output2:
                    # pdno(종목코드)에 ticker가 포함되는지 확인
                    if ticker in stock.get('pdno', ''):
                        qty = int(stock.get('hldg_qty', 0))
                        add_log(f"🧐 [잔고 확인] {stock.get('prdt_name')}({ticker}) 보유량: {qty}주")
                        return qty
                
                add_log(f"🧐 [잔고 확인] {ticker} 보유 없음 (0주)")
                return 0
            else:
                add_log(f"❌ [잔고 조회 실패] {res.text}")
                return 0
        except Exception as e:
            add_log(f"❌ [시스템 오류] 잔고 조회 중: {e}")
            return 0

    def send_order(self, trade_type, ticker, price, qty, retry=True):
        """
        주문 전송 함수
        - retry: 토큰 만료/오류 시 1회 재시도 여부
        """
        global ACCESS_TOKEN
        if not ACCESS_TOKEN: 
            if not self.get_token(): return {"status": "fail"}

        if trade_type == "buy":
            api_id = "kt10000"; tr_type_nm = "매수"; tr_id = "vt00001"
        else:
            api_id = "kt10001"; tr_type_nm = "매도"; tr_id = "vt00002"

        url = f"{self.base_url}/api/dostk/ordr"
        headers = self.headers.copy()
        
        # [수정 1] Authorization 헤더의 키를 표준(대문자 A)으로 변경 (호환성 향상)
        headers.update({
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "appkey": APP_KEY,
            "appsecret": APP_SECRET,
            "tr_id": tr_id,
            "custtype": "P",
            "api-id": api_id 
        })

        ord_prc = int(float(price))
        trde_tp = "03" if ord_prc == 0 else "00"
        str_price = "0" if trde_tp == "03" else str(ord_prc)

        data = {
            "dmst_stex_tp": "KRX", "stk_cd": ticker, "ord_qty": str(qty),
            "ord_uv": str_price, "trde_tp": trde_tp, "cond_uv": "0",
            "cano": ACCOUNT_NO,
            "acnt_prdt_cd": "01"
        }

        try:
            add_log(f"🚀 [{tr_type_nm} 전송] {ticker} | {qty}주 | {str_price}원")
            res = requests.post(url, headers=headers, data=json.dumps(data))
            
            if res.status_code == 200:
                result = res.json()
                rt_cd = result.get('return_code') or result.get('rt_cd')
                msg = result.get('return_msg') or result.get('msg1')

                # 성공 (0)
                if str(rt_cd) == "0":
                    add_log(f"✅ [체결 성공] 주문번호:{result.get('ord_no')} | {msg}")
                    return {"status": "success", "data": result}
                
                # [수정 2] 실패했지만 토큰 관련 에러(8005 등)라면 재시도
                # 8005: 유효하지 않은 토큰, 8001: 인증 실패 등
                elif retry and (str(rt_cd) == "8005" or "Token" in str(msg)):
                    add_log(f"🔄 [토큰 만료 감지] {msg} -> 재발급 후 재시도")
                    ACCESS_TOKEN = None # 기존 토큰 폐기
                    if self.get_token():
                        # 재귀 호출 시 retry=False로 하여 무한 루프 방지
                        return self.send_order(trade_type, ticker, price, qty, retry=False)
                
                else:
                    add_log(f"❌ [주문 거절] 코드:{rt_cd} | {msg}")
                    return {"status": "fail", "data": result}
            else:
                # HTTP 401 등 통신 레벨의 에러 처리
                add_log(f"❌ [통신 실패] Status: {res.status_code} | {res.text}")
                if res.status_code == 401 and retry: 
                    add_log("🔄 [HTTP 401] 토큰 재발급 후 재시도...")
                    if self.get_token(): 
                        return self.send_order(trade_type, ticker, price, qty, retry=False)
                return {"status": "fail", "data": res.text}

        except Exception as e:
            add_log(f"❌ [실행 오류] {e}")
            return {"status": "error", "msg": str(e)}

kiwoom = KiwoomAPI()

# --- [3. 웹 서버 라우팅] ---

@app.route('/')
def index():
    """루트 경로 접속 시 로그 출력"""
    html = """
    <html>
    <head>
        <title>Kiwoom Bot Logs</title>
        <meta http-equiv="refresh" content="5"> <style>
            body { font-family: monospace; background-color: #1e1e1e; color: #00ff00; padding: 20px; }
            h1 { color: #ffffff; border-bottom: 1px solid #555; padding-bottom: 10px; }
            .log-entry { margin-bottom: 5px; border-bottom: 1px solid #333; padding: 5px 0; }
        </style>
    </head>
    <body>
        <h1>Kiwoom Auto-Trading Bot Status</h1>
        <div id="logs">
    """
    
    # 저장된 로그를 HTML로 변환
    if not server_logs:
        html += "<div class='log-entry'>대기 중... 아직 수신된 신호가 없습니다.</div>"
    else:
        for log in server_logs:
            html += f"<div class='log-entry'>{log}</div>"
            
    html += """
        </div>
    </body>
    </html>
    """
    return html

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        # 1. 텍스트/JSON 구분 처리
        raw_data = request.get_data(as_text=True)
        if not raw_data: return jsonify({"status": "no data"}), 400

        if "||" in raw_data:
            json_str = raw_data.split("||")[1]
            data = json.loads(json_str)
        else:
            try:
                data = json.loads(raw_data)
            except:
                return jsonify({"status": "error"}), 400

        if data:
            ticker = data.get("ticker")
            action_raw = data.get("action", "")
            price = float(data.get("price", 0))
            
            # 테스트용 변환 (해외주식 -> 삼성전자)
            if ticker in ["NVDA", "TSLA", "AAPL", "QQQ", "SPY"]:
                add_log(f"⚠️ [TEST] 해외주식({ticker}) 감지 -> 삼성전자(005930)로 변환")
                ticker = "005930"
                if price > 100000: price = 60000 # 가격도 임의 조정

            add_log(f"📩 [신호 수신] {ticker} | {action_raw} | 현재가: {price}")

            # === [매수 로직: 금액 기준] ===
            if "BUY" in action_raw:
                if price > 0:
                    # 목표금액 / 현재가 (소수점 버림)
                    buy_qty = int(TARGET_BUY_AMOUNT / price)
                    if buy_qty < 1: buy_qty = 1 # 최소 1주
                    
                    add_log(f"🧮 [매수 계산] {TARGET_BUY_AMOUNT}원 / {price}원 = {buy_qty}주")
                    kiwoom.send_order("buy", ticker, price, buy_qty)
                else:
                    add_log("⚠️ 가격 정보(0) 오류로 매수 불가")

            # === [매도 로직: 잔고 기준 분할] ===
            elif any(k in action_raw for k in ["Profit", "Stop", "Exit"]):
                # 1. 잔고 확인
                current_qty = kiwoom.get_stock_balance(ticker)
                
                if current_qty > 0:
                    sell_qty = 0
                    
                    # 2-A. 완전 청산 (Final Exit)
                    if "Final Exit" in action_raw:
                        sell_qty = current_qty
                        add_log(f"👋 [전량 청산] 보유 {current_qty}주 전량 매도")
                    
                    # 2-B. 분할 청산 (1/3)
                    else:
                        sell_qty = int(current_qty / 3)
                        if sell_qty < 1: sell_qty = 1 # 최소 1주 매도
                        add_log(f"✂️ [분할 청산] 보유 {current_qty}주 중 {sell_qty}주(33%) 매도")
                    
                    # 3. 매도 주문
                    kiwoom.send_order("sell", ticker, 0, sell_qty) # 시장가(0)
                else:
                    add_log(f"🚫 [매도 불가] {ticker} 보유 잔고 없음 (0주)")

            return jsonify({"status": "success"}), 200

    except Exception as e:
        add_log(f"❌ [Critical Error] {e}")
        return jsonify({"status": "error", "msg": str(e)}), 500

if __name__ == '__main__':
    # 최초 실행 시 토큰 발급 시도
    kiwoom.get_token()
    # Render 환경변수 PORT 사용
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)