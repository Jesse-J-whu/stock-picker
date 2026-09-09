"""Reject inconsistent generated output before committing or deploying."""
import json
import math
import re
from datetime import date
from pathlib import Path
import sys


def verify(data, html):
    def require(condition, message):
        if not condition:
            raise ValueError(message)
    require(data.get("adjustment") == "qfq", "Expected qfq")
    require(data.get("timezone") == "Asia/Shanghai", "Expected explicit Beijing timezone")
    day = data.get("trade_date", "")
    date.fromisoformat(day)
    require(day in html and "前复权" in html, "HTML date/adjustment mismatch")
    quality = data["data_quality"]
    universe = quality["universe"]
    require(isinstance(universe, int) and universe > 0, "Empty universe")
    require(quality["valid_histories"] == universe and quality["coverage"] == 1, "Incomplete data coverage")
    require(not quality.get("fetch_errors"), "Unresolved fetch errors")
    counts = quality["strategy_counts"]
    require(counts["errors"] == 0, "Calculation errors")
    require(counts["evaluated"] + counts["insufficient_history"] == universe, "Unaccounted stocks")
    stocks = data["stocks"]
    require(data["count"] == len(stocks), "Result count mismatch")
    require(len(stocks) <= counts["evaluated"], "Too many selected stocks")
    require(len({s["code"] for s in stocks}) == len(stocks), "Duplicate selected codes")
    for stock in stocks:
        code, name = stock["code"], stock["name"]
        require(bool(re.fullmatch(r"\d{6}", code)), "Invalid stock code")
        require(bool(name) and name != code, "Missing stock name")
        require(code in html and name in html, "Selected stock absent from HTML")
        require(all(math.isfinite(float(stock[k])) for k in ("price", "open", "high", "low", "change_pct")),
                "Invalid quote values")
        require(stock["low"] <= min(stock["open"], stock["price"]) <=
                max(stock["open"], stock["price"]) <= stock["high"], "Invalid displayed OHLC")
    return f"Verified {day}: {universe} histories, {counts['evaluated']} evaluated, {len(stocks)} selected"


if __name__ == "__main__":
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else "docs")
    print(verify(json.loads((folder / "data.json").read_text(encoding="utf-8")),
                 (folder / "index.html").read_text(encoding="utf-8")))

