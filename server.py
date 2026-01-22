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
MAX_BUY_RANK = 7         # [NEW] 동시 매수 최대 종목 수
BUFFER_SECONDS = 5       # [NEW] 랭킹 산정을 위해 기다리는 시간 (초)

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
            "authorization": f"Bearer {ACCESS_TOKEN}",
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

# --- [3. 실행 로직 분리 (Execute Functions)] ---

def execute_buy(data):
    """매수 주문 집행 함수"""
    ticker = data.get("ticker")
    price = float(data.get("price", 0))
    score = data.get("score", 0) # 점수 확인
    
    if price > 0:
        buy_qty = int(TARGET_BUY_AMOUNT / price)
        if buy_qty < 1: buy_qty = 1
        
        add_log(f"🏆 [순위권 매수] {ticker} (점수: {score}) -> {buy_qty}주 주문")
        kiwoom.send_order("buy", ticker, price, buy_qty)
    else:
        add_log(f"⚠️ 가격 정보 오류로 매수 스킵: {ticker}")

def execute_sell(data):
    """매도 주문 집행 함수"""
    ticker = data.get("ticker")
    action_raw = data.get("action", "")
    
    # 1. 잔고 조회
    current_qty = kiwoom.get_stock_balance(ticker)
    time.sleep(0.2) # API 안정성 대기

    if current_qty > 0:
        sell_qty = 0
        log_msg = ""
        
        # 2. 청산 전략에 따른 수량 계산
        if "Profit Target 1" in action_raw:
            sell_qty = int(current_qty / 2) # 50%
            if sell_qty < 1: sell_qty = 1
            log_msg = "💰 TP 1 (50%)"
            
        elif any(k in action_raw for k in ["Profit Target 2", "Final Exit", "Final Stop Loss", "Exit Breakeven"]):
            sell_qty = current_qty          # 전량
            log_msg = "👋 전량 청산"
            
        elif any(k in action_raw for k in ["Stop Loss 1", "Stop Loss 2"]):
            sell_qty = int(current_qty * 0.3) # 30%
            if sell_qty < 1: sell_qty = 1
            log_msg = "📉 부분 손절 (30%)"
        
        else:
            # 기타 안전장치 (기본 1/3)
            sell_qty = int(current_qty / 3)
            if sell_qty < 1: sell_qty = 1
            log_msg = "✂️ 일반 분할 청산"

        add_log(f"{log_msg} {ticker} | {sell_qty}주 매도 실행")
        kiwoom.send_order("sell", ticker, 0, sell_qty)
    else:
        add_log(f"🚫 [매도 불가] {ticker} 보유 잔고 없음")

# --- [4. 스마트 워커 (Smart Worker)] ---

def worker():
    """버퍼링 및 랭킹 시스템이 적용된 워커"""
    add_log("👷 백그라운드 워커(스마트 랭킹)가 시작되었습니다.")
    
    buy_buffer = []          # 매수 후보를 모아둘 바구니
    flush_deadline = None    # 바구니를 비워야 할 마감 시간
    
    while True:
        try:
            # 1. 큐에서 데이터 가져오기 (0.5초 타임아웃으로 주기적 버퍼 체크)
            try:
                data = order_queue.get(timeout=0.5)
            except queue.Empty:
                data = None
            
            # 2. 데이터 처리
            if data:
                action = data.get("action", "")
                
                # [A] 매도 신호: 즉시 처리 (우선순위 높음)
                if any(k in action for k in ["Profit", "Stop", "Exit"]):
                    add_log(f"⚡ [매도 급행] {data.get('ticker')} 즉시 처리")
                    execute_sell(data)
                    time.sleep(1) # 주문 간 쿨타임
                
                # [B] 매수 신호: 버퍼에 담기
                elif "BUY" in action:
                    # 버퍼가 비어있다면 타이머 시작 (첫 손님 입장 후 5초 카운트)
                    if not buy_buffer:
                        flush_deadline = time.time() + BUFFER_SECONDS
                        add_log(f"⏳ [매수 접수] 5초간 후보를 모읍니다... (현재 1번째)")
                    
                    buy_buffer.append(data)
                    add_log(f"📥 [후보 등록] {data.get('ticker')} (Score: {data.get('score', 0)})")
                
                order_queue.task_done()

            # 3. 버퍼 체크 및 일괄 처리
            # 버퍼에 내용이 있고, 마감 시간이 지났다면?
            if buy_buffer and flush_deadline and time.time() >= flush_deadline:
                add_log(f"⚖️ [랭킹 산정] 총 {len(buy_buffer)}개 후보 중 상위 {MAX_BUY_RANK}개 선발")
                
                # (1) 점수 기준 내림차순 정렬
                # score가 없으면 0점으로 처리
                sorted_buys = sorted(buy_buffer, key=lambda x: float(x.get("score", 0)), reverse=True)
                
                # (2) 상위 N개 선발 및 나머지 탈락
                final_targets = sorted_buys[:MAX_BUY_RANK]
                dropped_targets = sorted_buys[MAX_BUY_RANK:]
                
                # (3) 선발된 종목 매수 집행
                for target in final_targets:
                    execute_buy(target)
                    time.sleep(1) # 주문 폭주 방지 딜레이
                    
                # (4) 탈락 종목 로그
                if dropped_targets:
                    dropped_tickers = [d.get('ticker') for d in dropped_targets]
                    add_log(f"🗑️ [매수 제외] 순위 밖 {len(dropped_targets)}종목: {dropped_tickers}")
                
                # (5) 버퍼 초기화
                buy_buffer = []
                flush_deadline = None
                add_log("🏁 [배치 처리 완료] 대기 모드 전환")

        except Exception as e:
            add_log(f"❌ [워커 오류] {e}")
            time.sleep(1)

# 스레드 생존 확인 및 복구
def start_worker_if_needed():
    is_alive = False
    for t in threading.enumerate():
        if t.name == "KiwoomWorker":
            is_alive = True
            break
            
    if not is_alive:
        add_log("🚑 워커 스레드 복구 및 재시작")
        t = threading.Thread(target=worker, name="KiwoomWorker", daemon=True)
        t.start()

# 최초 실행 시 스레드 시작
# threading.Thread(target=worker, name="KiwoomWorker", daemon=True).start()

# --- [5. 웹 서버 라우팅] ---
@app.route('/')
def index():
    html = """
    <html><head><title>Kiwoom Bot Logs</title>
    <meta http-equiv="refresh" content="3">
    <style>
        /* 배경색 변경 (옵션) */
        body { background-color: #101010; color: #FFB000; padding: 20px; font-family: monospace; }
        
        /* 로그 구분선 색상도 살짝 맞춰주면 예쁩니다 */
        .log { border-bottom: 1px solid #333; padding: 5px; font-size: 14px; }
    </style>
    </head><body>
    <h2 style="color: #FFB000;">Kiwoom Smart Trading Bot</h2>
    <div id="logs">
    """
    for log in server_logs:
        html += f"<div class='log'>{log}</div>"
    return html + "</div></body></html>"

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        start_worker_if_needed() # 일꾼 생존 확인

        raw_data = request.get_data(as_text=True)
        add_log(raw_data)
        if not raw_data: return jsonify({"status": "no data"}), 400

        if "||" in raw_data:
            json_str = raw_data.split("||")[1]
            data = json.loads(json_str)
        else:
            try:
                data = json.loads(raw_data)
            except:
                return jsonify({"status": "error"}), 400

        # 해외주식 티커 변환 (테스트용)
        if data.get("ticker") in ["NVDA", "TSLA", "AAPL", "QQQ", "SPY"]:
            data["ticker"] = "005930"
            if data.get("price", 0) > 100000: data["price"] = 60000

        # 큐에 넣기 (처리는 워커가 함)
        order_queue.put(data)
        
        # 로그는 간략하게
        q_size = order_queue.qsize()
        # add_log(f"📥 [수신] {data.get('ticker')} (대기열: {q_size})")

        return jsonify({"status": "queued"}), 200

    except Exception as e:
        add_log(f"❌ [Webhook Error] {e}")
        return jsonify({"status": "error"}), 500

if __name__ == '__main__':
    kiwoom.get_token()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)