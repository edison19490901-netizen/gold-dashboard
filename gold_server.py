"""
黄金价格监控 HTTP 服务
- GET  /               → 看板 HTML 页面
- GET  /api/current    → 三个品种最新实时价
- GET  /api/history    → 日线历史数据（从 CSV 读取）
- GET  /api/intraday   → 当日分时数据（分钟级）
- GET  /api/info       → 服务器信息
- POST /api/refresh    → 拉取最新价 + 后台更新日线 CSV
"""

import akshare as ak
import json
import os
import socket
import time
import threading
import pandas as pd
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============ 配置 ============
PORT = int(os.environ.get("PORT", 8765))
CSV_DIR = "./history_data"
HISTORY_CACHE_HOURS = 6  # 历史数据缓存时效（小时），超时才重新拉取

# ============ 实时行情（akshare） ============

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
        "price": round(float(row["最新价"]), 3),
        "change": round(float(row["涨跌额"]), 3),
        "change_percent": round(float(row["涨跌幅"]), 3),
        "unit": "元/份",
    }


def fetch_all_realtime():
    """拉取全部实时价格（并发）"""
    result = []
    fetchers = [fetch_realtime_gc, fetch_realtime_au, fetch_realtime_etf]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(f): f for f in fetchers}
        for future in as_completed(futures):
            f = futures[future]
            t0 = time.time()
            try:
                data = future.result()
                result.append(data)
                print(f"  [OK] {f.__name__}: {data['symbol']} = {data['price']} ({time.time()-t0:.1f}s)")
            except Exception as e:
                print(f"  [ERROR] {f.__name__}: {e} ({time.time()-t0:.1f}s)")
    return result


# ============ 分时数据（当日分钟级） ============

def _fmt_time(t):
    """将各种时间格式统一为 HH:MM"""
    s = str(t).strip()
    if not s:
        return ""
    # 已经是 HH:MM 或 HH:MM:SS
    if ":" in s and len(s) <= 8:
        return s[:5]
    # datetime 格式 2026-07-15 09:30:00
    if " " in s:
        time_part = s.split(" ")[-1]
        return time_part[:5]
    # 纯数字 HHMMSS 格式
    if s.isdigit() and len(s) >= 4:
        if len(s) == 6:
            return s[:2] + ":" + s[2:4]
        if len(s) == 4:
            return s[:2] + ":" + s[2:]
    return s[:5] if len(s) >= 5 else s


def fetch_intraday_au():
    """沪金期货当日分时数据"""
    for symbol in ["AU0", "AU2508"]:
        try:
            df = ak.futures_zh_minute_sina(symbol=symbol, period="1")
            if df is not None and not df.empty:
                break
        except Exception as e:
            print(f"  [DEBUG] AU intraday {symbol}: {e}")
            continue
    else:
        return []

    # 列名：datetime, open, high, low, close, volume, hold
    today_str = datetime.now().strftime("%Y-%m-%d")
    records = []
    for _, row in df.iterrows():
        try:
            dt = str(row["datetime"])
            # 只取当日数据
            if not dt.startswith(today_str):
                continue
            t = _fmt_time(dt)
            p = float(row["close"])
            if t and p > 0:
                records.append({"time": t, "price": round(p, 2)})
        except (ValueError, TypeError, KeyError):
            continue
    return records


def fetch_intraday_etf():
    """黄金 ETF 当日分时数据（新浪 API，绕过 eastmoney 代理问题）"""
    try:
        data = _sina_intraday("sh518880", scale=5)  # 新浪只支持5分钟线，1分钟返回null
    except Exception as e:
        print(f"  [DEBUG] ETF intraday sina: {e}")
        return []

    today_str = datetime.now().strftime("%Y-%m-%d")
    records = []
    for item in data:
        try:
            raw_time = str(item.get("day", ""))
            if not raw_time.startswith(today_str):
                continue
            p = float(item.get("close", 0))
            t = _fmt_time(raw_time)
            if t and p > 0:
                records.append({"time": t, "price": round(p, 3)})
        except (ValueError, TypeError):
            continue
    return records


def _sina_intraday(code, scale=5):
    """通过新浪 API 获取分时数据（绕过代理问题）"""
    url = f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={code}&scale={scale}&ma=no&datalen=240"
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "http://finance.sina.com.cn",
    })
    with urlopen(req, timeout=5) as resp:
        body = resp.read().decode("utf-8")
    if not body or body.strip() in ("", "null"):
        return []
    result = json.loads(body)
    return result if isinstance(result, list) else []


def fetch_intraday_gc():
    """COMEX 黄金当日分时数据（新浪 API）"""
    try:
        data = _sina_intraday("hf_GC", scale=5)
    except Exception as e:
        print(f"  [DEBUG] GC intraday sina: {e}")
        return []

    records = []
    for item in data:
        try:
            p = float(item.get("close", 0))
            t = _fmt_time(item.get("day", ""))
            if t and p > 0:
                records.append({"time": t, "price": round(p, 2)})
        except (ValueError, TypeError):
            continue
    return records


def fetch_all_intraday():
    """并发拉取当日分时数据"""
    result = {}
    fetchers = {
        "GC": fetch_intraday_gc,
        "AU": fetch_intraday_au,
        "518880": fetch_intraday_etf,
    }
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(f): sym for sym, f in fetchers.items()}
        for future in as_completed(futures):
            sym = futures[future]
            try:
                records = future.result()
                if records:
                    result[sym] = records
                print(f"  [INTRADAY] {sym}: {len(records)} 条分时数据")
            except Exception as e:
                print(f"  [INTRADAY] {sym} 分时获取失败: {e}")
    return result


# ============ 日线历史 ============

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


def refresh_history_background():
    """后台线程：检查并更新过期历史数据。不阻塞前端响应。"""
    symbols = ["GC", "AU", "518880"]
    for sym in symbols:
        filepath = os.path.join(CSV_DIR, f"history_{sym}.csv")
        need_fetch = True
        if os.path.exists(filepath):
            mtime = os.path.getmtime(filepath)
            age_hours = (time.time() - mtime) / 3600
            if age_hours < HISTORY_CACHE_HOURS:
                need_fetch = False
        if need_fetch:
            try:
                t0 = time.time()
                fetch_and_save_history(sym)
                print(f"  [BG] {sym} 历史更新完成 ({time.time()-t0:.1f}s)")
            except Exception as e:
                print(f"  [BG] {sym} 历史更新失败: {e}")


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

        elif parsed.path == "/api/intraday":
            qs = parse_qs(parsed.query)
            symbol = qs.get("symbol", [None])[0]
            if symbol:
                # 单品种
                fetchers = {
                    "GC": fetch_intraday_gc,
                    "AU": fetch_intraday_au,
                    "518880": fetch_intraday_etf,
                }
                f = fetchers.get(symbol)
                records = f() if f else []
                self._send_json({"symbol": symbol, "count": len(records), "data": records})
            else:
                # 全品种
                data = fetch_all_intraday()
                total = sum(len(v) for v in data.values())
                self._send_json({"count": total, "data": data})

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
                t_total = time.time()
                update_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                symbols = ["GC", "AU", "518880"]

                # 并发拉取实时行情 + 分时数据
                with ThreadPoolExecutor(max_workers=6) as pool:
                    future_realtime = pool.submit(fetch_all_realtime)
                    future_intraday = pool.submit(fetch_all_intraday)

                    latest = future_realtime.result()
                    intraday = future_intraday.result()

                # 读取本地 CSV 缓存（毫秒级）
                history = {}
                for sym in symbols:
                    records = load_history_csv(sym)
                    if records:
                        history[sym] = records

                # 历史数据后台异步更新
                threading.Thread(target=refresh_history_background, daemon=True).start()

                print(f"  [TIMING] 总响应时间: {time.time()-t_total:.1f}s")
                self._send_json({
                    "success": True,
                    "update_time": update_time,
                    "latest": latest,
                    "history": history,
                    "intraday": intraday,
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
