"""
黄金价格监控 HTTP 服务
- GET  /               → 看板 HTML 页面
- GET  /api/current    → 三个品种最新实时价
- GET  /api/history    → 日线历史数据（从 CSV 读取）
- GET  /api/info       → 服务器信息
- POST /api/refresh    → 拉取最新价 + 更新日线 CSV
"""

import akshare as ak
import json
import os
import socket
import pandas as pd
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ============ 配置 ============
PORT = int(os.environ.get("PORT", 8765))
CSV_DIR = "./history_data"

# ============ 数据抓取 ============

def fetch_realtime_gc():
    """COMEX 国际黄金实时价"""
    df = ak.futures_foreign_commodity_realtime(symbol="GC")
    row = df.iloc[0]
    return {
        "symbol": "GC",
        "name": "COMEX黄金",
        "type": "国际现货黄金",
        "price": round(float(row.iloc[1]), 2),
        "change": round(float(row.iloc[3]), 2),
        "change_percent": round(float(row.iloc[4]), 2),
        "unit": "美元/盎司",
    }

def fetch_realtime_au():
    """沪金期货主力实时价"""
    df = ak.futures_zh_realtime(symbol="黄金")
    row = df.iloc[0]
    price = float(row["trade"])
    presettlement = float(row["presettlement"])
    change = price - presettlement
    change_pct = float(row["changepercent"]) * 100
    return {
        "symbol": "AU",
        "name": "沪金主力",
        "type": "国内沪金期货",
        "price": round(price, 2),
        "change": round(change, 2),
        "change_percent": round(change_pct, 2),
        "unit": "元/克",
    }

def fetch_realtime_etf():
    """黄金 ETF 实时价"""
    df = ak.fund_etf_spot_em()
    row = df[df["代码"] == "518880"].iloc[0]
    return {
        "symbol": "518880",
        "name": "华安黄金ETF",
        "type": "黄金ETF",
        "price": float(row["最新价"]),
        "change": float(row["涨跌额"]),
        "change_percent": float(row["涨跌幅"]),
        "unit": "元/份",
    }

def fetch_all_realtime():
    """拉取全部实时价格"""
    result = []
    for fetcher in [fetch_realtime_gc, fetch_realtime_au, fetch_realtime_etf]:
        try:
            result.append(fetcher())
        except Exception as e:
            print(f"[ERROR] {fetcher.__name__}: {e}")
    return result

def fetch_and_save_history(symbol):
    """拉取日线数据并覆盖写入 CSV"""
    os.makedirs(CSV_DIR, exist_ok=True)
    filepath = os.path.join(CSV_DIR, f"history_{symbol}.csv")

    if symbol == "GC":
        df = ak.futures_foreign_hist(symbol="XAU")
        df = df.rename(columns={
            "date": "date", "open": "open", "high": "high",
            "low": "low", "close": "close", "volume": "volume"
        })
        df = df[["date", "open", "high", "low", "close", "volume"]]

    elif symbol == "AU":
        df = ak.futures_main_sina(symbol="AU0")
        df = df.rename(columns={
            "日期": "date", "开盘价": "open", "最高价": "high",
            "最低价": "low", "收盘价": "close", "成交量": "volume"
        })
        df = df[["date", "open", "high", "low", "close", "volume"]]

    elif symbol == "518880":
        df = ak.fund_etf_hist_sina(symbol="sh518880")
        df = df.rename(columns={
            "date": "date", "open": "open", "high": "high",
            "low": "low", "close": "close", "volume": "volume"
        })
        df = df[["date", "open", "high", "low", "close", "volume"]]

    else:
        return

    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df.to_csv(filepath, index=False, encoding="utf-8-sig")
    print(f"[SAVED] {filepath}  ({len(df)} 条)")

def load_history_csv(symbol, days=None):
    """从 CSV 读取日线数据，可选过滤最近 N 天"""
    filepath = os.path.join(CSV_DIR, f"history_{symbol}.csv")
    if not os.path.exists(filepath):
        return []
    df = pd.read_csv(filepath)
    df["date"] = df["date"].astype(str)
    if days:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        df = df[df["date"] >= cutoff]
    return df.to_dict(orient="records")

# ============ HTTP 服务 ============

HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gold_dashboard.html")

class GoldHandler(BaseHTTPRequestHandler):

    def _send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, code=200):
        try:
            with open(HTML_PATH, "rb") as f:
                content = f.read()
        except FileNotFoundError:
            self.send_error(404, "Dashboard HTML not found")
            return
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(content))
        self.end_headers()
        self.wfile.write(content)

    def _send_file(self, path, content_type):
        """发送静态文件"""
        filepath = os.path.join(os.path.dirname(os.path.abspath(__file__)), path.lstrip("/"))
        try:
            with open(filepath, "rb") as f:
                content = f.read()
        except FileNotFoundError:
            self.send_error(404, "File not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(content))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/" or parsed.path == "/index.html":
            self._send_html()

        # PWA 静态文件
        elif parsed.path == "/manifest.json":
            self._send_file(parsed.path, "application/json")
        elif parsed.path == "/sw.js":
            self._send_file(parsed.path, "application/javascript")
        elif parsed.path in ["/icon-192.png", "/icon-512.png"]:
            self._send_file(parsed.path, "image/png")

        elif parsed.path == "/api/current":
            data = fetch_all_realtime()
            self._send_json({
                "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "data": data,
            })

        elif parsed.path == "/api/history":
            qs = parse_qs(parsed.query)
            symbol = qs.get("symbol", ["GC"])[0]
            days = int(qs.get("days", [60])[0])
            records = load_history_csv(symbol, days=days)
            self._send_json({
                "symbol": symbol,
                "days": days,
                "count": len(records),
                "data": records,
            })

        elif parsed.path == "/api/info":
            self._send_json({
                "local_ip": get_local_ip(),
                "port": PORT,
            })

        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        if self.path == "/api/refresh":
            try:
                latest = fetch_all_realtime()
                update_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                for sym in ["GC", "AU", "518880"]:
                    try:
                        fetch_and_save_history(sym)
                    except Exception as e:
                        print(f"[ERROR] history {sym}: {e}")
                self._send_json({
                    "success": True,
                    "update_time": update_time,
                    "latest": latest,
                })
            except Exception as e:
                self._send_json({"success": False, "error": str(e)}, 500)
        else:
            self.send_error(404, "Not Found")

    def log_message(self, format, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]}")


def get_local_ip():
    """获取本机局域网 IP"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main():
    os.makedirs(CSV_DIR, exist_ok=True)
    local_ip = get_local_ip()
    print("=" * 50)
    print("  黄金价格监控 HTTP 服务")
    print(f"  本机访问: http://localhost:{PORT}")
    print(f"  手机访问: http://{local_ip}:{PORT}")
    print("=" * 50)

    print("[INIT] 检查日线数据...")
    for sym in ["GC", "AU", "518880"]:
        csv_path = os.path.join(CSV_DIR, f"history_{sym}.csv")
        if not os.path.exists(csv_path):
            try:
                fetch_and_save_history(sym)
            except Exception as e:
                print(f"[WARN] 初始化 {sym} 日线失败: {e}")
        else:
            print(f"  {sym}: 已有缓存，跳过")

    print(f"[READY] 服务已启动")
    server = HTTPServer(("0.0.0.0", PORT), GoldHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[EXIT] 服务已停止")
        server.shutdown()


if __name__ == "__main__":
    main()
