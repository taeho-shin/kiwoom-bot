import os
import requests
import json
import time
import threading
import queue
from flask import Flask, request, jsonify
from datetime import datetime
from collections import deque

app = Flask(__name__)

# --- [1. 환경변수 및 설정] ---
APP_KEY = os.environ.get("APP_KEY")
APP_SECRET = os.environ.get("APP_SECRET")
ACCOUNT_NO = os.environ.get("ACCOUNT_NO", "81185095") 
BASE_URL = "https://mockapi.kiwoom.com"

# [매수 설정] 1회 진입 목표 금액 (원)
TARGET_BUY_AMOUNT = 1000000 

# [큐 설정] 주문을 대기시킬 큐 생성
order_queue = queue.Queue()

# [로그 설정] 최근 50개 로그 저장
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


        print("\n🔍 잔고 조회 API 요청 중...")
        url = f"{self.base_url}/api/dostk/acnt"
        
        headers = self.headers.copy()
        headers.update({
            "authorization": f"Bearer {self.access_token}",
            "api-id": "kt00018",
        })

        json = {
            "dmst_stex_tp": "KRX",
            "qry_tp": "4"
        }

        try:
            res = requests.post(url, headers=headers, json=json)
            
            if res.status_code == 200:
                data = res.json()
                balance = data.get('acnt_evlt_remn_indv_tot', [])
                
                # 보유 종목 리스트에서 해당 티커 찾기
                for stock in balance:
                    # pdno(종목코드)에 ticker가 포함되는지 확인
                    if ticker in stock.get('stk_nm', ''):
                        qty = int(stock.get('rmnd_qty', 0))
                        add_log(f"🧐 [잔고 확인] {stock.get('stk_nm')}({ticker}) 보유량: {qty}주")
                        return qty
                
                # add_log(f"🧐 [잔고 확인] {ticker} 보유 없음 (0주)")
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
            api_id = "kt10000"; tr_type_nm = "매수"
        else:
            api_id = "kt10001"; tr_type_nm = "매도"

        url = f"{self.base_url}/api/dostk/ordr"
        headers = self.headers.copy()
        
        headers.update({
            "authorization": f"Bearer {ACCESS_TOKEN}",
            "api-id": api_id
        })

        ord_prc = int(float(price))
        if ord_prc == 0:
            print("지정가 = 0원 -> 시장가 매수")
            trde_tp = "3"
        else:
            trde_tp = "0"

        json = {
            "dmst_stex_tp": "KRX",
            "stk_cd": ticker,
            "ord_qty": str(qty),
            "ord_uv": str(ord_prc),
            "trde_tp": trde_tp,
        }

        try:
            add_log(f"🚀 [{tr_type_nm} 전송] {ticker} | {qty}주 | {ord_prc}원")
            res = requests.post(url, headers=headers, json=json)
            
            if res.status_code == 200:
                result = res.json()
                ord_no = result.get('ord_no', "XXXXX")
                rt_cd = result.get('return_code', "XXXXX")
                msg = result.get('return_msg', "XXXXX")

                # 성공 (0)
                if str(rt_cd) == 0:
                    add_log(f"✅ [주문 체결 성공] 주문번호:{ord_no} | {msg}")
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

# --- [3. 백그라운드 워커 (Queue Processor)] ---
def worker():
    """큐에서 작업을 하나씩 꺼내 순차적으로 실행하는 작업자"""
    add_log("👷 백그라운드 워커가 시작되었습니다.")
    while True:
        try:
            # 큐에서 데이터 꺼내기 (데이터가 없으면 대기)
            data = order_queue.get()
            
            ticker = data.get("ticker")
            action_raw = data.get("action", "")
            price = float(data.get("price", 0))

            add_log(f"⚙️ [처리 시작] {ticker} | {action_raw}")

            # --- [매수 로직] ---
            if "BUY" in action_raw:
                if price > 0:
                    buy_qty = int(TARGET_BUY_AMOUNT / price)
                    if buy_qty < 1: buy_qty = 1
                    kiwoom.send_order("buy", ticker, price, buy_qty)
                else:
                    add_log("⚠️ 가격 정보 오류로 매수 불가")

            # --- [매도 로직] ---
            elif any(k in action_raw for k in ["Profit", "Stop", "Exit"]):
                current_qty = kiwoom.get_stock_balance(ticker)
                
                # 잔고 조회 API 호출 후 잠시 대기 (안정성 확보)
                time.sleep(0.2) 

                if current_qty > 0:
                    if "Final Exit" in action_raw:
                        sell_qty = current_qty
                        add_log(f"👋 [전량 청산] {current_qty}주 매도")
                    else:
                        sell_qty = int(current_qty / 3)
                        if sell_qty < 1: sell_qty = 1
                        add_log(f"✂️ [분할 청산] {sell_qty}주 매도")
                    
                    kiwoom.send_order("sell", ticker, 0, sell_qty)
                else:
                    add_log(f"🚫 [매도 불가] 잔고 없음")
            
            # --- [처리 완료 후 휴식] ---
            # API 레이트 리밋 보호를 위해 작업 간 0.5초 딜레이
            time.sleep(0.5) 
            
            # 큐 작업 완료 처리
            order_queue.task_done()

        except Exception as e:
            add_log(f"❌ [워커 오류] {e}")

# 스레드 시작 (서버 켜질 때 같이 실행됨)
threading.Thread(target=worker, daemon=True).start()


# --- [4. 웹 서버 라우팅] ---
@app.route('/')
def index():
    html = """
    <html><head><title>Kiwoom Bot Logs</title>
    <meta http-equiv="refresh" content="3">
    <style>body{background:#1e1e1e;color:#0f0;padding:20px;font-family:monospace;}
    .log{border-bottom:1px solid #333;padding:5px;}</style></head><body>
    <h2>Kiwoom Trading Bot (Queue System Active)</h2><div id="logs">
    """
    for log in server_logs:
        html += f"<div class='log'>{log}</div>"
    return html + "</div></body></html>"

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        raw_data = request.get_data(as_text=True)
        if not raw_data: return jsonify({"status": "no data"}), 400

        # 데이터 파싱
        if "||" in raw_data:
            json_str = raw_data.split("||")[1]
            data = json.loads(json_str)
        else:
            try:
                data = json.loads(raw_data)
            except:
                return jsonify({"status": "error"}), 400

        # 테스트용 변환
        if data.get("ticker") in ["NVDA", "TSLA", "AAPL", "QQQ", "SPY"]:
            data["ticker"] = "005930"
            if data.get("price", 0) > 100000: data["price"] = 60000

        # [핵심 변경] 여기서 직접 주문하지 않고 큐에 넣기만 함!
        order_queue.put(data)
        
        # 큐 사이즈 확인용 로그
        q_size = order_queue.qsize()
        add_log(f"📥 [큐 적재] 대기열: {q_size}개 | {data.get('ticker')} - {data.get('action')}")

        return jsonify({"status": "queued", "message": "Order added to queue"}), 200

    except Exception as e:
        add_log(f"❌ [Webhook Error] {e}")
        return jsonify({"status": "error"}), 500

if __name__ == '__main__':
    kiwoom.get_token()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)