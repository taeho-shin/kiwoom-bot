import os
import requests
import json
from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)

# --- [1. 환경변수에서 설정 가져오기] ---
# 코드를 공개된 곳(GitHub)에 올려도 안전하도록, 키 값은 서버 설정에서 가져옵니다.
APP_KEY = os.environ.get("APP_KEY")
APP_SECRET = os.environ.get("APP_SECRET")
# 기본값 설정 (혹시 설정 안됐을 때를 대비해 모의투자 URL 고정)
BASE_URL = "https://mockapi.kiwoom.com"

# 매수 수량
INITIAL_BUY_QTY = 3 
ACCESS_TOKEN = None

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
            "appkey": APP_KEY,      # 환경변수 사용
            "secretkey": APP_SECRET # 환경변수 사용
        }
        try:
            # 키 값이 없으면 로그 남기고 중단
            if not APP_KEY or not APP_SECRET:
                print("❌ [오류] 환경변수(APP_KEY, APP_SECRET)가 설정되지 않았습니다.")
                return False

            res = requests.post(url, headers=headers, data=json.dumps(data))
            if res.status_code == 200:
                resp = res.json()
                ACCESS_TOKEN = resp.get("token") or resp.get("access_token")
                print(f"✅ [인증 성공] 토큰 발급 완료")
                return True
            else:
                print(f"❌ [인증 실패] {res.text}")
                return False
        except Exception as e:
            print(f"❌ [연결 오류] {e}")
            return False

    def send_order(self, trade_type, ticker, price, qty):
        global ACCESS_TOKEN
        if not ACCESS_TOKEN:
            if not self.get_token(): return {"status": "fail", "msg": "Token Error"}

        if trade_type == "buy":
            api_id = "kt10000"; tr_type_nm = "매수"; tr_id = "vt00001"
        else:
            api_id = "kt10001"; tr_type_nm = "매도"; tr_id = "vt00002"

        url = f"{self.base_url}/api/dostk/ordr"
        headers = self.headers.copy()
        headers.update({
            "authorization": f"Bearer {ACCESS_TOKEN}",
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
            "ord_uv": str_price, "trde_tp": trde_tp, "cond_uv": "0"
        }

        try:
            print(f"🚀 [{tr_type_nm} 전송] {ticker} | {qty}주")
            res = requests.post(url, headers=headers, data=json.dumps(data))
            if res.status_code == 200:
                result = res.json()
                if result.get('return_code') == 0:
                    print(f"✅ [주문 성공] {result.get('ord_no')}")
                    return {"status": "success", "data": result}
                else:
                    print(f"❌ [거절] {result.get('return_msg')}")
                    return {"status": "fail", "data": result}
            else:
                if res.status_code == 401: 
                    if self.get_token(): return self.send_order(trade_type, ticker, price, qty)
                return {"status": "fail", "data": res.text}
        except Exception as e:
            print(f"❌ [오류] {e}")
            return {"status": "error", "msg": str(e)}

kiwoom = KiwoomAPI()

@app.route('/')
def index():
    return "Kiwoom Server is Running on Cloud!"

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.json
        if data:
            ticker = data.get("ticker")
            action_raw = data.get("action", "")
            price = data.get("price", 0)
            
            # 테스트용 변환
            if ticker in ["NVDA", "TSLA", "AAPL", "QQQ", "SPY"]:
                ticker = "005930"

            if "BUY" in action_raw:
                kiwoom.send_order("buy", ticker, price, INITIAL_BUY_QTY)
            elif any(k in action_raw for k in ["Profit", "Stop", "Exit"]):
                sell_qty = max(1, int(INITIAL_BUY_QTY / 3))
                kiwoom.send_order("sell", ticker, 0, sell_qty)
            
            return jsonify({"status": "success"}), 200
        else:
            return jsonify({"status": "no data"}), 400
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500

if __name__ == '__main__':
    # 클라우드 환경에서는 PORT 환경변수를 사용해야 함
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)