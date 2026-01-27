import os
import requests
import json
import time
import threading
import queue
from flask import Flask, request, jsonify
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import deque
import math

app = Flask(__name__)

# ==========================================
# [1] 환경변수 및 전역 설정
# ==========================================
app_key = os.environ.get("APP_KEY", "WEyClVdBvdo2e1QE8xuKSBbMTEbihZaM7v192j0DMko")
app_secret = os.environ.get("APP_SECRET", "a8E-GslMXGkFNptImpzTU1DUQ6s6cCfpDD_gSNuyL4Y")
BASE_URL = "https://mockapi.kiwoom.com"

# --- 트레이딩 설정 ---
TARGET_BUY_AMOUNT = 1000000  # 1회 매수 시도 금액 (원)
MAX_BUY_RANK = 7             # 동시 매수 허용 최대 종목 수 (랭킹 상위 N개)
BUFFER_SECONDS = 10          # 매수 신호 수집 및 랭킹 산정을 위한 대기 시간 (초)
SCORE_THRESHOLD = 70         # 매수 최소 기준 점수

# --- 시스템 설정 ---
order_queue = queue.Queue()  # 웹훅 수신 데이터 -> 워커 전달용 FIFO 큐
server_logs = deque() # 웹 대시보드 표시용 로그 (최신 50개 유지)

# ==========================================
# [2] 헬퍼 함수
# ==========================================
def add_log(message):
    """
    시스템 로그를 생성하여 콘솔 출력 및 메모리에 저장합니다.
    - Console: 실시간 디버깅용
    - server_logs: 웹 페이지(/) 조회용
    """
    time_str = datetime.now(ZoneInfo("Asia/Seoul")).strftime('%Y-%m-%d %H:%M:%S')
    log_entry = f"[{time_str}] {message}"
    print(log_entry) 
    server_logs.appendleft(log_entry)

# ==========================================
# [3] 키움 증권 API 클래스
# ==========================================
class KiwoomAPI():
    """
    키움증권(또는 모의투자) REST API와의 통신을 전담하는 클래스입니다.
    토큰 발급, 잔고 조회, 주문 전송 등의 기능을 수행합니다.
    """
    def __init__(self, app_key, app_secret):
        """API 초기화 및 최초 인증 토큰 발급"""
        self.app_key = app_key
        self.app_secret = app_secret
        self.base_url = "https://mockapi.kiwoom.com"
        
        # 기본 헤더 설정 (토큰 발급 전)
        self.headers = {"Content-Type": "application/json;charset=UTF-8"}
        
        # 초기 토큰 발급 시도
        self.access_token = self.get_token()
        if self.access_token:
            self.headers.update({"authorization": f"Bearer {self.access_token}"})

    def get_token(self):
        """
        OAuth2 Client Credentials 방식으로 접근 토큰을 발급받습니다.
        :return: token (str) or False
        """
        url = f"{self.base_url}/oauth2/token"
        headers = self.headers.copy() # 인증 전 헤더 사용
        data = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "secretkey": self.app_secret
        }
        try:
            if not self.app_key or not self.app_secret:
                add_log("❌ [설정 오류] API Key가 누락되었습니다.")
                return False

            res = requests.post(url, headers=headers, data=json.dumps(data))
            if res.status_code == 200:
                resp = res.json()
                token = resp.get("token") or resp.get("access_token")
                add_log(f"✅ [인증 성공] 토큰이 발급되었습니다.")
                return token
            else:
                add_log(f"❌ [인증 실패] {res.text}")
                return False
        except Exception as e:
            add_log(f"❌ [연결 오류] 토큰 발급 중 예외 발생: {e}")
            return False
        
    def get_stock_name_from_ticker(self, ticker):
        """
        종목 코드(Ticker)를 입력받아 종목명(Name)을 조회합니다.
        :return: stock_name (str)
        """
        url = f"{self.base_url}/api/dostk/stkinfo"
        headers = self.headers.copy()
        headers.update({"api-id": "ka10001"})
        payload = {"stk_cd": ticker}

        try:
            res = requests.post(url, headers=headers, json=payload)
            if res.status_code == 200:
                data = res.json()
                return data.get("stk_nm", "XXXXX")
            else:
                add_log(f"❌ [종목명 조회 실패] {res.text}")
                return "Unknown"
        except Exception as e:
            add_log(f"❌ [시스템 오류] 종목명 조회 중: {e}")
            return "Error"

    def get_stock_balance(self, ticker):
        """
        특정 종목의 현재 보유 수량과 종목명을 확인합니다.
        :param ticker: 종목 코드
        :return: (종목명, 보유수량) 튜플
        """
        # print("\n🔍 잔고 조회 API 요청 중...")
        url = f"{self.base_url}/api/dostk/acnt"
        headers = self.headers.copy()
        headers.update({"api-id": "kt00018"})
        payload = {
            "dmst_stex_tp": "KRX",
            "qry_tp": "1"
        }

        try:
            res = requests.post(url, headers=headers, json=payload)
            if res.status_code == 200:
                data = res.json()
                # 잔고 리스트 가져오기
                balance = data.get('acnt_evlt_remn_indv_tot', [])
                
                for stock in balance:
                    if ticker in stock.get('stk_cd', ''):
                        name = self.get_stock_name_from_ticker(ticker)
                        qty = int(stock.get('rmnd_qty', 0))
                        add_log(f"🧐 [잔고 확인] {name}({ticker}) | 보유량: {qty}주")
                        return name, qty
                
                # 보유 종목이 없는 경우
                return 0, 0
            else:
                add_log(f"❌ [잔고 조회 실패] {res.text}")
                return 0, 0
        except Exception as e:
            add_log(f"❌ [시스템 오류] 잔고 조회 중: {e}")
            return 0, 0
    
    def get_withdrawable_amount(self, ticker, price):
        """
        해당 종목을 지정가에 매수할 때, '주문 가능 현금'과 '최대 주문 가능 수량'을 조회합니다.
        :param ticker: 종목 코드
        :param price: 매수 희망 단가
        :return: (주문가능금액, 주문가능수량)
        """
        url = f"{self.base_url}/api/dostk/acnt"
        headers = self.headers.copy()
        headers.update({"api-id": "kt00011"})

        payload = {
            "stk_cd": ticker,
            # "uv": str(price), # API 요청 시 문자열 변환 필수
        }

        try:
            res = requests.post(url, headers=headers, json=payload)
            if res.status_code == 200:
                data = res.json()
                cash = int(data.get("min_ord_alow_amt", 100))          # 주문 가능 현금
                avail_qty = int(data.get("min_ord_alowq", 100))
                # avail_qty = math.floor(cash / price)
                return cash, avail_qty
            return 0, 0 # 실패 시 0 반환
        except Exception as e:
            add_log(f"❌ [시스템 오류] 가능 금액 조회: {e}")
            return 0, 0

    def send_order(self, trade_type, ticker, price, qty, stop=0, retry=True):
        """
        실제 매수/매도 주문을 API로 전송합니다.
        
        :param trade_type: "buy" or "sell"
        :param retry: 토큰 만료 에러(8005) 발생 시 재귀적으로 1회 재시도 여부
        :return: API 응답 결과 (Dict)
        """
        if trade_type == "buy":
            api_id = "kt10000"; tr_type_nm = "매수"
        else:
            api_id = "kt10001"; tr_type_nm = "매도"

        url = f"{self.base_url}/api/dostk/ordr"
        headers = self.headers.copy()
        headers.update({"api-id": api_id})

        ord_prc = int(float(price))
        
        # 주문 유형 결정 (지정가/시장가/스탑로스 등)
        if ord_prc == 0:
            trde_tp = "3" # 시장가
            add_log("market order")
        else:
            trde_tp = "0" if stop != 0 else "00" # 지정가 (API 문서에 따라 코드 확인 필요)

        # JSON 페이로드 구성
        payload = {
            "dmst_stex_tp": "KRX",
            "stk_cd": ticker,
            "ord_qty": str(qty),
            "ord_uv": str(ord_prc),
            "trde_tp": trde_tp,
        }

        try:
            name = self.get_stock_name_from_ticker(ticker)
            time.sleep(0.5) # API 과부하 방지 딜레이
            
            add_log(f"🚀 [{tr_type_nm} 전송] {ticker}({name}) | {qty}주 | {ord_prc}원")
            res = requests.post(url, headers=headers, json=payload)
            
            if res.status_code == 200:
                result = res.json()
                rt_cd = result.get('return_code', "XXXXX")
                msg = result.get('return_msg', "")

                # 1. 정상 체결 (Return Code: 0)
                if str(rt_cd) == "0":
                    add_log(f"✅ [주문 접수 완료] 주문번호:{result.get('ord_no')} | {msg}")
                    return {"status": "success", "data": result}
                
                # 2. 토큰 만료 에러 감지 및 재시도 로직
                elif retry and (str(rt_cd) == "8005" or "Token" in str(msg)):
                    add_log(f"🔄 [토큰 만료] 재발급 후 주문을 재시도합니다.")
                    
                    # 새 토큰 발급
                    new_token = self.get_token()
                    if new_token:
                        self.access_token = new_token
                        self.headers["authorization"] = f"Bearer {new_token}"
                        # 재귀 호출 (retry=False로 무한 루프 방지)
                        return self.send_order(trade_type, ticker, price, qty, stop, retry=False)
                
                else:
                    add_log(f"❌ [주문 거절] 코드:{rt_cd} | {msg}")
                    return {"status": "fail", "data": result}
            else:
                add_log(f"❌ [HTTP 에러] {res.status_code} | {res.text}")
                return {"status": "fail", "data": res.text}

        except Exception as e:
            add_log(f"❌ [실행 오류] {e}")
            return {"status": "error", "msg": str(e)}

# 인스턴스 생성
kiwoom = KiwoomAPI(app_key=app_key, app_secret=app_secret)


# ==========================================
# [4] 주문 집행 로직 (Execution Logic)
# ==========================================
def execute_buy(data):
    """
    매수 시그널 처리: 
    - 가용 현금 확인 후 목표 금액(TARGET_BUY_AMOUNT)만큼 수량 계산
    - 잔고 부족 시 가능한 최대 수량으로 보정하여 주문
    """
    ticker = data.get("ticker")
    price = float(data.get("price", 0))
    score = data.get("score", 0)
    stop = data.get("stop", 0)

    # 잔고 및 종목명 조회
    cash, avail_qty = kiwoom.get_withdrawable_amount(ticker=ticker, price=price)
    add_log(f"현금: {cash} | 구매가능수량: {avail_qty}")
    
    if price > 0:
        # 목표 금액에 따른 수량 계산
        buy_qty = int(TARGET_BUY_AMOUNT / price)
        if buy_qty < 1: buy_qty = 1

        # 현금이 부족할 경우, 최대 가능 수량으로 조정
        if buy_qty > avail_qty:
            add_log(f"⚠️ [수량 조정] 목표:{buy_qty}주 -> 가능:{avail_qty}주 (잔고 부족)")
            buy_qty = avail_qty

        # 주문 전송
        if buy_qty > 0:
            add_log(f"🏆 [최종 진입] {ticker} (점수: {score}) -> {buy_qty}주")
            result = kiwoom.send_order(trade_type="buy", ticker=ticker, price=price, qty=buy_qty, stop=stop)
            status = result.get("status", "fail")
            _cash, _avail_qty = kiwoom.get_withdrawable_amount(ticker=ticker, price=price)
            # add_log(f"주문완료 후 현금: {_cash} | 구매가능수량: {_avail_qty}")
            return status

    else:
        add_log(f"⚠️ 가격 정보 오류({price})로 매수를 건너뜁니다: {ticker}")
        return "error"

def execute_sell(data):
    """
    매도 시그널 처리:
    - 현재 보유 잔고 확인
    - 시그널 메시지(TP/SL 등)에 따라 분할 매도 비율 결정
    """
    ticker = data.get("ticker")
    action_raw = data.get("action", "") # 예: "Profit Target 1", "Stop Loss"
    stop = data.get("stop", 0)
    
    # 1. 잔고 조회
    name, current_qty = kiwoom.get_stock_balance(ticker)
    time.sleep(0.2) 

    if current_qty > 0:
        sell_qty = 0
        log_msg = ""
        
        # 2. 청산 전략에 따른 수량 계산
        if "Profit Target 1" in action_raw:
            sell_qty = int(current_qty / 2) # 50% 분할 익절
            if sell_qty < 1: sell_qty = 1
            log_msg = "💰 TP 1 (50%)"
            
        elif any(k in action_raw for k in ["Profit Target 2", "Final Exit", "Final Stop Loss"]):
            sell_qty = current_qty          # 전량 청산
            log_msg = "👋 전량 청산"
            
        elif "Stop Loss" in action_raw:
            sell_qty = int(current_qty * 0.3) # 30% 부분 손절 (예시)
            if sell_qty < 1: sell_qty = 1
            log_msg = "📉 부분 손절 (30%)"
        
        else:
            sell_qty = int(current_qty / 3) # 그 외 1/3 청산
            if sell_qty < 1: sell_qty = 1
            log_msg = "✂️ 일반 분할 청산"

        add_log(f"{log_msg} {ticker}({name}) -> {sell_qty}주 매도 실행")
        # 매도는 보통 지정가 혹은 시장가로 던짐 (여기서는 stop 가격 활용)
        kiwoom.send_order("sell", ticker, price=stop, stop=stop, qty=sell_qty)
    else:
        add_log(f"🚫 [매도 불가] {ticker} 보유 잔고가 없습니다.")


# ==========================================
# [5] 스마트 워커 (Background Worker)
# ==========================================
def worker():
    """
    백그라운드 스레드:
    1. 큐에서 트레이딩 시그널을 꺼냅니다.
    2. [매도]는 즉시 집행합니다 (우선순위 높음).
    3. [매수]는 일정 시간(BUFFER_SECONDS) 동안 모아서 점수(Score) 경쟁을 붙입니다.
    4. 상위 랭킹 종목만 선별하여 매수합니다.
    """
    add_log("👷 스마트 랭킹 워커가 시작되었습니다.")
    
    buy_buffer = []          # 매수 후보군 임시 저장소
    flush_deadline = None    # 랭킹 산정 마감 시간
    
    while True:
        try:
            # 1. 큐 데이터 폴링 (0.5초 대기)
            try:
                data = order_queue.get(timeout=0.5)
            except queue.Empty:
                data = None
            
            # 2. 데이터 수신 시 처리
            if data:
                action = data.get("action", "")
                country = data.get("country", "")

                # [A] 매도(청산) 신호 -> 즉시 실행
                if any(k in action for k in ["Profit", "Stop", "Exit"]):
                    add_log(f"⚡ [매도 급행] {data.get('ticker')} 즉시 처리를 시작합니다.")
                    if country != "US":
                        execute_sell(data)
                        time.sleep(1) 
                
                # [B] 매수 신호 -> 버퍼링 (경쟁 유도)
                elif "BUY" in action:
                    # 첫 매수 신호가 들어오면 타이머 시작
                    if not buy_buffer:
                        flush_deadline = time.time() + BUFFER_SECONDS
                        add_log(f"⏳ [매수 버퍼링 시작] {BUFFER_SECONDS}초 뒤 랭킹을 산정합니다.")
                    
                    buy_buffer.append(data)
                    add_log(f"📥 [후보 등록] {data.get('ticker')} (점수: {data.get('score', 0)})")
                
                # 작업 완료 표시
                order_queue.task_done()

            # 3. 버퍼 마감 시간 체크 및 일괄 실행
            if buy_buffer and flush_deadline and time.time() >= flush_deadline:
                add_log(f"⚖️ [랭킹 산정 시작] 후보: {len(buy_buffer)}개 / 선발: {MAX_BUY_RANK}개")
                
                # (1) 점수 기준 내림차순 정렬 (Score가 높은 순)
                buy_buffer_scored = [b for b in buy_buffer if b.get("score", 0) > SCORE_THRESHOLD]
                sorted_buys = sorted(buy_buffer_scored, key=lambda x: float(x.get("score", 0)), reverse=True)
                
                # (2) 상위 N개 선발
                final_targets = sorted_buys[:MAX_BUY_RANK]
                dropped_targets = sorted_buys[MAX_BUY_RANK:]
                
                # (3) 선발 종목 매수 집행
                if country != "US":
                    for target in final_targets:
                        execute_buy(target)
                        time.sleep(1) # 주문 간 텀을 두어 API 과부하 방지

                    # (4) 탈락 종목 로깅
                    if dropped_targets:
                        dropped_tickers = [d.get('ticker') for d in dropped_targets]
                        add_log(f"🗑️ [진입 탈락] 점수/순위 미달: {dropped_tickers}")
                
                # (5) 버퍼 초기화
                buy_buffer = []
                flush_deadline = None
                add_log("🏁 [사이클 종료] 다시 대기 상태로 전환합니다.")

        except Exception as e:
            add_log(f"❌ [워커 오류] 처리 중 예외 발생: {e}")
            time.sleep(1)

def start_worker_if_needed():
    """워커 스레드가 죽었는지 확인하고 필요 시 재시작"""
    is_alive = False
    for t in threading.enumerate():
        if t.name == "KiwoomWorker":
            is_alive = True
            break
            
    if not is_alive:
        add_log("🚑 워커 스레드가 발견되지 않아 재시작합니다.")
        t = threading.Thread(target=worker, name="KiwoomWorker", daemon=True)
        t.start()

# ==========================================
# [6] 웹 서버 라우팅 (Flask)
# ==========================================
@app.route('/')
def index():
    """로그 확인용 간단한 웹 페이지 렌더링"""
    html = """
    <html><head><title>Kiwoom Bot Status</title>
    <meta http-equiv="refresh" content="3">
    <style>
        body { background-color: #101010; color: #FFB000; padding: 20px; font-family: 'Consolas', monospace; }
        .log { border-bottom: 1px solid #333; padding: 6px; font-size: 14px; }
        h2 { border-bottom: 2px solid #FFB000; padding-bottom: 10px; }
    </style>
    </head><body>
    <h2>🚀 Kiwoom Smart Trading Bot</h2>
    <div id="logs">
    """
    for log in server_logs:
        html += f"<div class='log'>{log}</div>"
    return html + "</div></body></html>"

@app.route('/webhook', methods=['POST'])
def webhook():
    """
    TradingView 등의 외부 툴에서 보내는 웹훅을 수신합니다.
    데이터를 파싱하여 큐(Order Queue)에 넣는 역할만 수행합니다.
    """
    try:
        start_worker_if_needed() # 일꾼 생존 확인

        raw_data = request.get_data(as_text=True)
        if not raw_data: return jsonify({"status": "no data"}), 400

        # [JSON 파싱 보정] 줄바꿈 문자 등으로 인한 JSON 에러 방지
        raw_data = raw_data.replace('\n', ' ').replace('\r', '')
        
        data = None
        # 1. 표준 JSON 파싱 시도
        try:
            data = json.loads(raw_data)
        except json.JSONDecodeError:
            # 2. TradingView 경고 메시지 포맷 ('||' 구분자) 처리
            if "||" in raw_data:
                try:
                    parts = raw_data.split("||", 1)
                    json_str = parts[1]
                    data = json.loads(json_str)
                except Exception as e:
                    add_log(f"❌ [파싱 실패] Split 방식 실패: {e}")
                    return jsonify({"status": "error", "reason": "invalid split format"}), 400
            else:
                add_log(f"❌ [파싱 실패] JSON 형식이 아닙니다: {raw_data}")
                return jsonify({"status": "error", "reason": "invalid json"}), 400

        # 정상 파싱된 데이터를 큐에 삽입
        order_queue.put(data)
        
        q_size = order_queue.qsize()
        add_log(f"📥 [Webhook 수신] {data.get('ticker')} | {data.get('action')} (대기열: {q_size})")

        return jsonify({"status": "queued"}), 200

    except Exception as e:
        add_log(f"❌ [Webhook 오류] {e}")
        return jsonify({"status": "error"}), 500

# ==========================================
# [7] 메인 실행 블록
# ==========================================
if __name__ == '__main__':
    # API 및 잔고 조회 테스트 코드 (실행 시 주석 해제하여 사용)
    print(">>> 시스템 시작 및 API 테스트 수행")
    add_log("서버가 시작되었습니다. (http://127.0.0.1:5000)")
    app.run(port=5000)
    
    # 1. 잔고 조회 테스트
    balance = kiwoom.get_stock_balance(ticker="005930") # 삼성전자
    print(balance)
    
    # 2. 인출 가능 금액 테스트
    cash, avail_qty = kiwoom.get_withdrawable_amount(ticker="005930", price=70000)
    print(f"현금: {cash}, 가능수량: {avail_qty}")

    # Flask 서버 실행 (프로덕션 환경에서는 waitress 등을 권장)
    # app.run(host='0.0.0.0', port=5000)
    pass