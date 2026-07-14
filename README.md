# 黄金价格实时看板

实时监控 COMEX 黄金、沪金期货、黄金 ETF 价格，展示日线 K 线走势图。

## 功能

- 三个品种实时价格卡片（国际金 / 沪金 / 黄金 ETF）
- 日线 K 线图（支持 1月 / 3月 / 6月 周期切换）
- 日线数据本地 CSV 持久化存储
- 手机扫码访问（同一 WiFi）
- 一键更新数据按钮
- 深色主题响应式布局

## 快速开始

```bash
pip install -r requirements.txt
python gold_server.py
```

浏览器打开 `http://localhost:8765`

## 手机访问

同一 WiFi 下扫描看板上的二维码，或浏览器打开 `http://<电脑IP>:8765`

## 云部署

参考 `手机部署指南.md` 将项目部署到 Render.com（免费），获得 HTTPS 公网地址，任何网络都能访问。

## 项目结构

```
├── gold_server.py          # HTTP 服务（数据抓取 + API）
├── gold_dashboard.html     # 前端看板页面
├── requirements.txt        # Python 依赖
├── 启动看板.bat            # Windows 一键启动
├── 手机部署指南.md         # Render 云部署教程
├── icon-192.png/512.png    # 应用图标
└── history_data/           # 日线数据 CSV（本地自动生成）
```

## 数据来源

通过 [akshare](https://github.com/akfamily/akshare) 抓取：
- COMEX 黄金（新浪财经）
- 沪金期货主力（新浪财经）
- 华安黄金 ETF 518880（新浪财经）
