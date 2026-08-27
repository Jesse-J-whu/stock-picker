"""
周线爆发选股策略
================
基于布林带扩张 + 放量 + MACD金叉零轴上方的周线级别选股策略。

策略逻辑：
1. BOLL_COND: 布林带上轨上升、中轨上升、下轨下降（布林带开口扩张）
2. VOL_COND: 26周内存在成交量比大于4倍的周（放量）
3. MACD_COND: 3周内存在MACD在零轴上方金叉
"""

import numpy as np
import pandas as pd
import json
import os
import sys
import re
import requests
from datetime import datetime, timedelta
from jinja2 import Template
import traceback
import time


# ============================================================
# HTTP 请求基础设施
# ============================================================

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# 腾讯接口仍会为部分退市代码返回多年前的最后一根 K 线。不能把这种
# 历史快照当作当天行情，否则技术指标会产生看似有效的假信号。
MAX_KLINE_STALENESS_DAYS = 14
MIN_DATA_COVERAGE_RATIO = 0.80


def has_recent_kline(df, max_staleness_days=MAX_KLINE_STALENESS_DAYS):
    """确认 K 线末日期足够新，排除退市或长期停止更新的代码。"""
    if df.empty or 'date' not in df:
        return False
    try:
        last_date = pd.to_datetime(df['date'].iloc[-1]).date()
    except (TypeError, ValueError):
        return False
    return last_date >= (datetime.now().date() - timedelta(days=max_staleness_days))


# ============================================================
# 数据获取层 - 腾讯财经接口（国内外均可访问，无频率限制）
# ============================================================

def get_all_a_stocks():
    """通过腾讯实时行情接口批量探测有效A股代码"""
    print("[1/4] 获取A股股票列表...")

    # 生成所有可能的A股代码
    code_ranges = []
    # 深圳主板 000001-000999
    code_ranges += [f"sz{str(i).zfill(6)}" for i in range(1, 1000)]
    # 深圳中小板 002001-002999
    code_ranges += [f"sz{str(i).zfill(6)}" for i in range(2001, 3000)]
    # 深圳创业板 300001-301999
    code_ranges += [f"sz{str(i).zfill(6)}" for i in range(300001, 302000)]
    # 上海主板 600000-601999, 603000-603999, 605000-605999
    code_ranges += [f"sh{str(i).zfill(6)}" for i in range(600000, 602000)]
    code_ranges += [f"sh{str(i).zfill(6)}" for i in range(603000, 604000)]
    code_ranges += [f"sh{str(i).zfill(6)}" for i in range(605000, 606000)]
    # 上海科创板 688001-689999
    code_ranges += [f"sh{str(i).zfill(6)}" for i in range(688001, 690000)]

    all_stocks = []
    batch_size = 80  # 腾讯接口支持批量查询

    for i in range(0, len(code_ranges), batch_size):
        batch = code_ranges[i:i + batch_size]
        query = ','.join(batch)  # 用逗号分隔: sz000001,sz000002,...
        url = f"https://qt.gtimg.cn/q={query}"

        try:
            resp = SESSION.get(url, timeout=20)
            if resp.status_code != 200:
                continue

            text = resp.text
            # 按分号分割每条记录: v_sz000001="...";
            for entry in text.split(';'):
                entry = entry.strip()
                if not entry:
                    continue
                # 提取: v_sz000001="51~平安银行~000001~价格~..."
                match = re.search(r'v_(\w+)="(\d+)~(.+?)~(\d+)~([^~]*)~', entry)
                if not match:
                    continue

                name = match.group(3).strip()
                code = match.group(4)
                price_str = match.group(5)

                # 过滤无效股票
                if not name or not code or len(code) != 6:
                    continue
                # 过滤ST、退市、PT
                if 'ST' in name or '退' in name or 'PT' in name:
                    continue
                # 过滤价格为0的（已停牌/退市）
                try:
                    price = float(price_str)
                    if price <= 0:
                        continue
                except (ValueError, TypeError):
                    continue

                all_stocks.append({'代码': code, '名称': name})
        except Exception as e:
            continue

        if (i // batch_size) % 20 == 0 and i > 0:
            print(f"    已探测 {i}/{len(code_ranges)}，有效 {len(all_stocks)} 只...")
        time.sleep(0.05)

    df = pd.DataFrame(all_stocks)
    if df.empty:
        print("  股票列表获取失败!")
        return df

    df = df.drop_duplicates(subset='代码').reset_index(drop=True)
    print(f"  共 {len(df)} 只股票待筛选")
    return df


def get_weekly_data(stock_code, name=""):
    """通过腾讯财经接口获取周线数据"""
    if stock_code.startswith(('60', '68')):
        symbol = f"sh{stock_code}"
    else:
        symbol = f"sz{stock_code}"

    url = (
        f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
        f"_var=kline_weekqfq&param={symbol},week,,,120,qfq"
    )

    try:
        resp = SESSION.get(url, timeout=15)
        if resp.status_code != 200:
            return pd.DataFrame()

        text = resp.text.strip()
        # 去掉 "kline_weekqfq=" 前缀
        if '=' in text:
            text = text.split('=', 1)[1]

        data = json.loads(text)
        if data.get('code') != 0:
            return pd.DataFrame()

        stock_data = data.get('data', {})
        if not stock_data:
            return pd.DataFrame()

        # 获取第一个key的数据
        first_key = list(stock_data.keys())[0]
        klines = stock_data[first_key].get('qfqweek', [])

        if not klines or len(klines) < 30:
            return pd.DataFrame()

        rows = []
        for k in klines:
            # 格式: [日期, 开盘, 收盘, 最高, 最低, 成交量]
            if len(k) >= 6:
                try:
                    rows.append({
                        'date': k[0],
                        'open': float(k[1]),
                        'close': float(k[2]),
                        'high': float(k[3]),
                        'low': float(k[4]),
                        'vol': float(k[5]),
                    })
                except (ValueError, IndexError):
                    continue

        if len(rows) < 30:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        df = df.sort_values('date').reset_index(drop=True)
        if not has_recent_kline(df):
            return pd.DataFrame()
        return df
    except Exception:
        return pd.DataFrame()


def get_daily_data_for_display(stock_code):
    """通过腾讯实时行情获取最新数据用于页面展示"""
    if stock_code.startswith(('60', '68')):
        symbol = f"sh{stock_code}"
    else:
        symbol = f"sz{stock_code}"

    url = f"https://qt.gtimg.cn/q={symbol}"

    try:
        resp = SESSION.get(url, timeout=15)
        text = resp.text.strip()

        # 解析: v_sh600000="1~浦发银行~600000~9.85~9.89~..."
        match = re.search(r'"(.+)"', text)
        if not match:
            return {}

        parts = match.group(1).split('~')
        if len(parts) < 40:
            return {}

        price = float(parts[3])
        prev_close = float(parts[4])
        change_pct = (price - prev_close) / prev_close * 100 if prev_close > 0 else 0

        return {
            'price': price,
            'change_pct': round(change_pct, 2),
            'volume': float(parts[36]) if parts[36] else 0,
            'turnover': 0,
            'high': float(parts[33]) if parts[33] else price,
            'low': float(parts[34]) if parts[34] else price,
            'open': float(parts[5]) if parts[5] else price,
        }
    except Exception:
        return {}


# ============================================================
# 策略计算层
# ============================================================

def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def ma(series, period):
    return series.rolling(window=period).mean()

def std(series, period):
    return series.rolling(window=period).std(ddof=0)

def ref(series, n):
    return series.shift(n)

def cross(s1, s2):
    return (s1 > s2) & (s1.shift(1) <= s2.shift(1))

def exist(cond, n):
    return cond.rolling(window=n).max().astype(bool)


def apply_strategy(df):
    """
    对周线数据应用"周线爆发"策略
    df 需包含: close, vol 列
    返回布尔Series，True表示当前周满足选股条件
    """
    close = df['close']
    vol = df['vol']

    # --- 布林带条件 ---
    mid = ma(close, 20)
    upper = mid + 2 * std(close, 20)
    lower = mid - 2 * std(close, 20)

    boll_cond = (
        (upper > ref(upper, 1)) &
        (mid > ref(mid, 1)) &
        (lower < ref(lower, 1))
    )

    # --- 成交量条件 ---
    vol_ratio = vol / ref(vol, 1)
    vol_cond = exist(vol_ratio > 4, 26)

    # --- MACD条件 ---
    dif = ema(close, 12) - ema(close, 26)
    dea = ema(dif, 9)
    jc = cross(dif, dea)
    zero_cond = dea > 0
    macd_cond = exist(jc & zero_cond, 3)

    # --- 综合选股 ---
    xg = boll_cond & vol_cond & macd_cond
    return xg


# ============================================================
# 主流程
# ============================================================

def run_strategy():
    """运行完整选股流程"""
    print("=" * 60)
    print(f"  周线爆发选股策略 - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    stocks = get_all_a_stocks()
    if stocks.empty:
        print("无法获取股票列表，退出")
        return []

    selected = []
    total = len(stocks)
    failed = 0

    print(f"\n[2/4] 逐只计算策略信号（共 {total} 只）...")
    for idx, row in stocks.iterrows():
        code = row['代码']
        name = row['名称']

        if idx % 200 == 0:
            print(f"  进度: {idx}/{total} ({idx/total*100:.1f}%)")

        df = get_weekly_data(code, name)
        if df.empty:
            failed += 1
            continue

        try:
            signal = apply_strategy(df)
            if signal.iloc[-1]:
                selected.append({
                    'code': code,
                    'name': name,
                })
                print(f"  ★ 选中: {code} {name}")
        except Exception:
            failed += 1
            continue

        # 控制请求频率
        time.sleep(0.1)

    print(f"\n  策略计算完成: 成功 {total - failed}, 失败 {failed}")

    # 数据源失效时绝不能把“抓取失败”伪装成“今日 0 只”。抛出异常会让
    # Actions 失败并保留上一份已验证的页面结果。
    processed = total - failed
    min_processed = max(100, int(total * MIN_DATA_COVERAGE_RATIO))
    if processed < min_processed:
        raise RuntimeError(
            f"行情数据覆盖率不足：仅成功 {processed}/{total}，停止发布结果"
        )

    print(f"\n[3/4] 获取选中股票的最新行情...")
    for item in selected:
        daily = get_daily_data_for_display(item['code'])
        item.update(daily)
        time.sleep(0.1)

    print(f"\n  共选出 {len(selected)} 只股票")
    return selected


def generate_html(selected_stocks, output_path):
    """生成移动端适配的HTML展示页面"""
    print(f"\n[4/4] 生成展示页面...")

    template_str = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>周线爆发选股</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;
    background: #0a0e27;
    color: #e0e6ff;
    min-height: 100vh;
    padding-bottom: env(safe-area-inset-bottom);
}
.header {
    background: linear-gradient(135deg, #1a1f4e 0%, #0d1234 100%);
    padding: 20px 16px 16px;
    border-bottom: 1px solid rgba(100, 120, 255, 0.15);
    position: sticky;
    top: 0;
    z-index: 100;
    backdrop-filter: blur(20px);
}
.header h1 {
    font-size: 20px;
    font-weight: 700;
    background: linear-gradient(90deg, #6c8cff, #a78bfa);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    letter-spacing: 1px;
}
.header .meta {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-top: 8px;
    font-size: 12px;
    color: #7a85b3;
}
.header .count {
    background: rgba(100, 120, 255, 0.15);
    color: #8fa4ff;
    padding: 2px 10px;
    border-radius: 12px;
    font-weight: 600;
}
.strategy-tag {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    background: rgba(167, 139, 250, 0.12);
    color: #a78bfa;
    padding: 4px 10px;
    border-radius: 6px;
    font-size: 11px;
    margin-top: 10px;
}
.strategy-tag::before {
    content: '';
    width: 6px;
    height: 6px;
    background: #a78bfa;
    border-radius: 50%;
    animation: pulse 2s infinite;
}
@keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
}
.stock-list { padding: 12px; }
.stock-card {
    background: linear-gradient(135deg, rgba(26, 31, 78, 0.8) 0%, rgba(13, 18, 52, 0.9) 100%);
    border: 1px solid rgba(100, 120, 255, 0.1);
    border-radius: 14px;
    padding: 16px;
    margin-bottom: 10px;
    transition: all 0.2s;
    position: relative;
    overflow: hidden;
}
.stock-card::before {
    content: '';
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    height: 2px;
    background: linear-gradient(90deg, transparent, rgba(100, 120, 255, 0.3), transparent);
}
.stock-card:active {
    transform: scale(0.98);
    border-color: rgba(100, 120, 255, 0.3);
}
.card-top {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
}
.stock-name {
    font-size: 17px;
    font-weight: 700;
    color: #e8ecff;
}
.stock-code {
    font-size: 12px;
    color: #5a6599;
    margin-top: 2px;
    font-family: 'SF Mono', 'Fira Code', monospace;
}
.stock-price {
    text-align: right;
}
.price-value {
    font-size: 22px;
    font-weight: 700;
    font-family: 'SF Mono', 'DIN Alternate', monospace;
}
.price-change {
    font-size: 13px;
    font-weight: 600;
    margin-top: 2px;
}
.up { color: #f43f5e; }
.down { color: #10b981; }
.flat { color: #7a85b3; }
.card-bottom {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 8px;
    margin-top: 14px;
    padding-top: 12px;
    border-top: 1px solid rgba(100, 120, 255, 0.08);
}
.metric {
    text-align: center;
}
.metric-label {
    font-size: 10px;
    color: #5a6599;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}
.metric-value {
    font-size: 13px;
    color: #b0badf;
    margin-top: 2px;
    font-family: 'SF Mono', monospace;
}
.empty-state {
    text-align: center;
    padding: 60px 20px;
    color: #5a6599;
}
.empty-state .icon { font-size: 48px; margin-bottom: 16px; }
.empty-state p { font-size: 14px; line-height: 1.6; }
.footer {
    text-align: center;
    padding: 20px;
    font-size: 11px;
    color: #3d4570;
    border-top: 1px solid rgba(100, 120, 255, 0.06);
    margin-top: 10px;
}
.disclaimer {
    background: rgba(234, 179, 8, 0.06);
    border: 1px solid rgba(234, 179, 8, 0.15);
    border-radius: 10px;
    padding: 12px 14px;
    margin: 12px;
    font-size: 11px;
    color: #b8a44a;
    line-height: 1.5;
}
</style>
</head>
<body>
<div class="header">
    <h1>周线爆发选股</h1>
    <div class="meta">
        <span>{{ update_time }}</span>
        <span class="count">{{ stock_count }} 只</span>
    </div>
    <div class="strategy-tag">BOLL扩张 + 放量 + MACD零上金叉</div>
</div>

<div class="disclaimer">
    本页面仅为量化策略筛选结果展示，不构成任何投资建议。股市有风险，投资需谨慎。
</div>

<div class="stock-list">
{% if stocks %}
{% for s in stocks %}
<div class="stock-card">
    <div class="card-top">
        <div>
            <div class="stock-name">{{ s.name }}</div>
            <div class="stock-code">{{ s.code }}</div>
        </div>
        <div class="stock-price">
            {% if s.price %}
            <div class="price-value {% if s.change_pct > 0 %}up{% elif s.change_pct < 0 %}down{% else %}flat{% endif %}">
                {{ "%.2f"|format(s.price) }}
            </div>
            <div class="price-change {% if s.change_pct > 0 %}up{% elif s.change_pct < 0 %}down{% else %}flat{% endif %}">
                {% if s.change_pct > 0 %}+{% endif %}{{ "%.2f"|format(s.change_pct) }}%
            </div>
            {% else %}
            <div class="price-value flat">--</div>
            {% endif %}
        </div>
    </div>
    {% if s.price %}
    <div class="card-bottom">
        <div class="metric">
            <div class="metric-label">开盘</div>
            <div class="metric-value">{{ "%.2f"|format(s.open) }}</div>
        </div>
        <div class="metric">
            <div class="metric-label">最高</div>
            <div class="metric-value">{{ "%.2f"|format(s.high) }}</div>
        </div>
        <div class="metric">
            <div class="metric-label">最低</div>
            <div class="metric-value">{{ "%.2f"|format(s.low) }}</div>
        </div>
    </div>
    {% endif %}
</div>
{% endfor %}
{% else %}
<div class="empty-state">
    <div class="icon">📊</div>
    <p>今日暂无符合"周线爆发"策略的股票<br>策略会在每个交易日自动更新</p>
</div>
{% endif %}
</div>

<div class="footer">
    <p>策略自动运行 · 数据来源: 腾讯财经</p>
    <p style="margin-top:4px;">周线级别 · 每个交易日收盘后自动更新</p>
</div>
</body>
</html>"""

    template = Template(template_str)
    html = template.render(
        stocks=selected_stocks,
        stock_count=len(selected_stocks),
        update_time=datetime.now().strftime('%Y年%m月%d日 %H:%M 更新'),
    )

    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else '.', exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"  页面已生成: {output_path}")


def save_data_json(selected_stocks, output_path):
    """保存选股结果为JSON"""
    data = {
        'update_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'strategy': '周线爆发',
        'count': len(selected_stocks),
        'stocks': selected_stocks,
    }
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else '.', exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  数据已保存: {output_path}")


if __name__ == '__main__':
    output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'docs')
    os.makedirs(output_dir, exist_ok=True)

    results = run_strategy()

    html_path = os.path.join(output_dir, 'index.html')
    generate_html(results, html_path)

    json_path = os.path.join(output_dir, 'data.json')
    save_data_json(results, json_path)

    print(f"\n{'=' * 60}")
    print(f"  完成! 共选出 {len(results)} 只股票")
    print(f"  HTML: {html_path}")
    print(f"  JSON: {json_path}")
    print(f"{'=' * 60}")
