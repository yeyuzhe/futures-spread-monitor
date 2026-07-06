# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import re
import shutil
import sys
import time
import traceback
import zipfile
from dataclasses import dataclass, asdict
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
import xml.etree.ElementTree as ET

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_XLSX = BASE_DIR / "00-可转抛价差监测表.xlsx"
CONFIG_PATH = BASE_DIR / "rqdata_config.local.json"
QUOTE_JSON = BASE_DIR / "quotes.json"
QUOTE_CSV = BASE_DIR / "quotes.csv"
CACHE_DIR = BASE_DIR / "data"
CACHE_PATH = CACHE_DIR / "latest_quotes.json"
DIST_DIR = BASE_DIR / "dist"
STATIC_DATA_PATH = DIST_DIR / "data" / "latest_monitor.json"
MONTHS = list(range(1, 13))
MONTH_NAMES = {i: f"{i}月" for i in MONTHS}
SECTOR_MAP = {
    "SC": "油品",
    "FU": "油品",
    "BC": "有色",
    "CU": "有色",
    "AL": "有色",
    "ZN": "有色",
    "PB": "有色",
    "NI": "有色",
    "AG": "有色",
    "AU": "有色",
    "RB": "黑色",
    "HC": "黑色",
    "RU": "化工",
    "EG": "化工",
    "PG": "化工",
    "M": "农产品",
    "A": "农产品",
    "C": "农产品",
    "CS": "农产品",
    "CF": "农产品",
    "SR": "农产品",
    "CJ": "农产品",
}
COLS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def excel_date(serial: float | int | None) -> str | None:
    if serial is None:
        return None
    return (dt.date(1899, 12, 30) + dt.timedelta(days=int(serial))).isoformat()


def cycle_delivery_dates(
    near_serial: float | int | None,
    far_serial: float | int | None,
    today: dt.date | None = None,
) -> tuple[str | None, str | None]:
    if near_serial is None or far_serial is None:
        return excel_date(near_serial), excel_date(far_serial)
    today = today or dt.date.today()
    near_base = dt.date(1899, 12, 30) + dt.timedelta(days=int(near_serial))
    far_base = dt.date(1899, 12, 30) + dt.timedelta(days=int(far_serial))
    near = dt.date(today.year, near_base.month, near_base.day)
    if near < today:
        near = dt.date(today.year + 1, near_base.month, near_base.day)
    far = dt.date(near.year, far_base.month, far_base.day)
    if far <= near:
        far = dt.date(far.year + 1, far_base.month, far_base.day)
    return near.isoformat(), far.isoformat()


def parse_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        return float(value)
    text = str(value).strip()
    if not text or text.startswith("#"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def col_to_index(col: str) -> int:
    n = 0
    for ch in col:
        n = n * 26 + ord(ch) - 64
    return n


def index_to_col(index: int) -> str:
    s = ""
    while index:
        index, rem = divmod(index - 1, 26)
        s = chr(65 + rem) + s
    return s


def split_cell_ref(ref: str) -> tuple[int, int]:
    match = re.match(r"([A-Z]+)(\d+)", ref)
    if not match:
        raise ValueError(f"invalid cell ref: {ref}")
    return col_to_index(match.group(1)), int(match.group(2))


@dataclass
class FeeSpec:
    symbol: str
    vat_rate: float
    storage_fee: float
    commission_rate: float
    trade_fee: float
    delivery_fee: float
    storage_fee_gross: float
    stamp_tax: float


@dataclass
class QuotePoint:
    month: int
    month_name: str
    rtd_symbol: str | None
    rq_symbol: str | None
    price: float | None
    source: str
    maturity_date: str | None = None


@dataclass
class Opportunity:
    product: str
    sector: str
    near_month: int
    far_month: int
    pair: str
    near_symbol: str | None
    far_symbol: str | None
    near_price: float
    far_price: float
    spread: float
    cost_spread: float
    net_profit: float
    annualized_return: float
    hold_days: int
    capital_cost_rate: float
    storage_fee: float
    fee_total: float
    quote_source: str
    is_profitable: bool
    delivery_near: str | None
    delivery_far: str | None


class XlsxMonitorTemplate:
    def __init__(self, path: Path):
        self.path = path
        self.cells: dict[str, dict[str, Any]] = {}
        self.shared_strings: list[str] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        with zipfile.ZipFile(self.path) as zf:
            self.shared_strings = self._read_shared_strings(zf)
            sheet_path = self._first_sheet_path(zf)
            root = ET.fromstring(zf.read(sheet_path))
            for row in root.findall("a:sheetData/a:row", NS):
                for cell in row.findall("a:c", NS):
                    ref = cell.attrib["r"]
                    formula = cell.find("a:f", NS)
                    value_node = cell.find("a:v", NS)
                    inline_node = cell.find("a:is/a:t", NS)
                    value: Any = None
                    if inline_node is not None:
                        value = inline_node.text
                    elif value_node is not None:
                        value = value_node.text
                        if cell.attrib.get("t") == "s":
                            value = self.shared_strings[int(value)]
                    self.cells[ref] = {
                        "value": value,
                        "formula": formula.text if formula is not None else None,
                    }

    def _read_shared_strings(self, zf: zipfile.ZipFile) -> list[str]:
        if "xl/sharedStrings.xml" not in zf.namelist():
            return []
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        out = []
        for item in root.findall("a:si", NS):
            out.append("".join(t.text or "" for t in item.findall(".//a:t", NS)))
        return out

    def _first_sheet_path(self, zf: zipfile.ZipFile) -> str:
        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}
        first = workbook.find("a:sheets/a:sheet", NS)
        if first is None:
            raise ValueError("workbook has no sheets")
        rid = first.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
        return "xl/" + rel_map[rid].lstrip("/")

    def value(self, ref: str) -> Any:
        return self.cells.get(ref, {}).get("value")

    def number(self, ref: str) -> float | None:
        return parse_number(self.value(ref))

    def formula(self, ref: str) -> str | None:
        return self.cells.get(ref, {}).get("formula")

    def parse(self) -> dict[str, Any]:
        fees: dict[str, FeeSpec] = {}
        delivery: dict[str, dict[int, float]] = {}
        for row in range(2, 24):
            product = self.value(f"A{row}")
            if product:
                product = str(product).strip()
                fees[product] = FeeSpec(
                    symbol=product,
                    vat_rate=self.number(f"B{row}") or 0.0,
                    storage_fee=self.number(f"C{row}") or 0.0,
                    commission_rate=self.number(f"D{row}") or 0.0,
                    trade_fee=self.number(f"E{row}") or 0.0,
                    delivery_fee=self.number(f"F{row}") or 0.0,
                    storage_fee_gross=self.number(f"G{row}") or 0.0,
                    stamp_tax=self.number(f"H{row}") or 0.0,
                )

            delivery_product = self.value(f"N{row}")
            if delivery_product:
                delivery_product = str(delivery_product).strip()
                delivery[delivery_product] = {}
                for month, col_idx in zip(MONTHS, range(15, 27)):
                    serial = self.number(f"{index_to_col(col_idx)}{row}")
                    if serial is not None:
                        delivery[delivery_product][month] = serial

        quote_rows: dict[str, dict[int, dict[str, Any]]] = {}
        for row in range(26, 48):
            product = self.value(f"A{row}")
            if not product:
                continue
            product = str(product).strip()
            quote_rows[product] = {}
            for month, col_idx in zip(MONTHS, range(2, 14)):
                ref = f"{index_to_col(col_idx)}{row}"
                formula = self.formula(ref)
                rtd_symbol = extract_rtd_symbol(formula)
                cached_price = self.number(ref)
                if rtd_symbol or cached_price not in (None, 0):
                    quote_rows[product][month] = {
                        "rtd_symbol": rtd_symbol,
                        "cached_price": cached_price,
                    }
        return {"fees": fees, "delivery": delivery, "quote_rows": quote_rows}


def extract_rtd_symbol(formula: str | None) -> str | None:
    if not formula:
        return None
    match = re.search(r'RTD\([^,]+,\s*,\s*"([^"]+)"', formula, flags=re.I)
    return match.group(1).strip() if match else None


def load_config() -> dict[str, Any]:
    config: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        config.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    env_api_key = os.environ.get("RQDATA_API_KEY")
    env_username = os.environ.get("RQDATA_USERNAME")
    env_password = os.environ.get("RQDATA_PASSWORD")
    env_uri = os.environ.get("RQDATA_URI")
    env_addr_host = os.environ.get("RQDATA_ADDR_HOST")
    env_addr_port = os.environ.get("RQDATA_ADDR_PORT")
    env_market = os.environ.get("RQDATA_MARKET")
    if env_api_key:
        config["api_key"] = env_api_key
    if env_username:
        config["username"] = env_username
    if env_password:
        config["password"] = env_password
    if env_uri:
        config["uri"] = env_uri
    if env_addr_host and env_addr_port:
        config["addr"] = [env_addr_host, int(env_addr_port)]
    if env_market:
        config["market"] = env_market
    if any(config.get(key) for key in ("api_key", "username", "password", "uri")):
        config["enabled"] = True
    return config or {"enabled": False}


def load_file_quotes() -> dict[str, float]:
    quotes: dict[str, float] = {}
    for path in (QUOTE_JSON, CACHE_PATH):
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                rows = data.get("quotes", data) if isinstance(data, dict) else data
                if isinstance(rows, dict):
                    for key, value in rows.items():
                        price = parse_number(value.get("price") if isinstance(value, dict) else value)
                        if price is not None:
                            quotes[str(key)] = price
                elif isinstance(rows, list):
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        key = row.get("rtd_symbol") or row.get("symbol") or row.get("order_book_id")
                        price = parse_number(row.get("price") or row.get("last") or row.get("last_price"))
                        if key and price is not None:
                            quotes[str(key)] = price
            except Exception:
                pass
    if QUOTE_CSV.exists():
        try:
            with QUOTE_CSV.open("r", encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    key = row.get("rtd_symbol") or row.get("symbol") or row.get("order_book_id")
                    price = parse_number(row.get("price") or row.get("last") or row.get("last_price"))
                    if key and price is not None:
                        quotes[str(key)] = price
        except Exception:
            pass
    return quotes


def normalize_rq_symbol(rtd_symbol: str | None) -> str | None:
    if not rtd_symbol:
        return None
    return rtd_symbol.split(".", 1)[0].replace("01M", "1M")


def month_from_rtd_symbol(rtd_symbol: str | None) -> tuple[str, int] | None:
    if not rtd_symbol:
        return None
    core = rtd_symbol.split(".", 1)[0].upper()
    match = re.match(r"([A-Z]+)(\d{2})M$", core)
    if not match:
        return None
    return match.group(1), int(match.group(2))


class RqdataQuoteSource:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.market = config.get("market") or "cn"
        self.instrument_map: dict[tuple[str, int], str] = {}
        self.instrument_dates: dict[str, str] = {}
        self.errors: list[str] = []

    def enabled(self) -> bool:
        return bool(self.config.get("enabled"))

    def init(self) -> bool:
        if not self.enabled():
            return False
        try:
            import rqdatac

            uri = self.config.get("uri") or None
            username = self.config.get("username") or None
            password = self.config.get("password") or None
            api_key = self.config.get("api_key") or None
            addr = self.config.get("addr") or ("rqdatad-pro.ricequant.com", 16011)
            if uri:
                rqdatac.init(uri=uri, lazy=True, timeout=20, connect_timeout=8)
            else:
                if api_key and not username and not password:
                    username, password = "license", api_key
                rqdatac.init(username=username, password=password, addr=tuple(addr), lazy=True, timeout=20, connect_timeout=8)
            return True
        except Exception as exc:
            self.errors.append(f"rqdata init failed: {exc}")
            return False

    def load_instrument_map(self, rtd_symbols: list[str]) -> None:
        try:
            import pandas as pd
            import rqdatac

            pairs = {month_from_rtd_symbol(symbol) for symbol in rtd_symbols}
            pairs.discard(None)
            if not pairs:
                return
            today = pd.Timestamp(dt.date.today())
            df = rqdatac.all_instruments(type="Future", market=self.market)
            if df is None or len(df) == 0:
                return
            for col in ("listed_date", "de_listed_date", "maturity_date"):
                if col in df.columns:
                    df[col] = pd.to_datetime(df[col], errors="coerce")
            for underlying, month in sorted(pairs):
                sub = df[df.get("underlying_symbol").astype(str).str.upper() == underlying.upper()]
                if "maturity_date" in sub.columns:
                    sub = sub[sub["maturity_date"].dt.month == month]
                    sub = sub[sub["maturity_date"].isna() | (sub["maturity_date"] >= today)]
                    if "listed_date" in sub.columns:
                        sub = sub[sub["listed_date"].isna() | (sub["listed_date"] <= today)]
                    sub = sub.sort_values("maturity_date")
                if len(sub) > 0:
                    row = sub.iloc[0]
                    rq_id = str(row["order_book_id"])
                    self.instrument_map[(underlying.upper(), month)] = rq_id
                    maturity = row.get("maturity_date")
                    if maturity is not None and not pd.isna(maturity):
                        self.instrument_dates[rq_id] = maturity.date().isoformat()
        except Exception as exc:
            self.errors.append(f"instrument map failed: {exc}")

    def fetch(self, rtd_symbols: list[str]) -> tuple[dict[str, float], dict[str, str], dict[str, str]]:
        prices: dict[str, float] = {}
        symbol_map: dict[str, str] = {}
        if not self.init():
            return prices, symbol_map, {}
        self.load_instrument_map(rtd_symbols)
        rq_ids: list[str] = []
        rq_to_rtd: dict[str, str] = {}
        for rtd in rtd_symbols:
            pair = month_from_rtd_symbol(rtd)
            rq_id = self.instrument_map.get((pair[0].upper(), pair[1])) if pair else None
            if pair and not rq_id:
                continue
            rq_id = rq_id or normalize_rq_symbol(rtd)
            if not rq_id:
                continue
            symbol_map[rtd] = rq_id
            rq_to_rtd[rq_id] = rtd
            rq_ids.append(rq_id)
        if not rq_ids:
            return prices, symbol_map, {}
        try:
            import rqdatac

            snapshots = rqdatac.current_snapshot(rq_ids, market=self.market)
            if not isinstance(snapshots, list):
                snapshots = [snapshots]
            for fallback_rq_id, snap in zip(rq_ids, snapshots):
                rq_id = getattr(snap, "order_book_id", None) or getattr(snap, "_order_book_id", None) or fallback_rq_id
                price = extract_snapshot_price(snap)
                if price is not None and rq_id in rq_to_rtd:
                    prices[rq_to_rtd[rq_id]] = price
        except Exception as exc:
            self.errors.append(f"current_snapshot failed: {exc}")
        if prices:
            CACHE_DIR.mkdir(exist_ok=True)
            CACHE_PATH.write_text(
                json.dumps(
                    {
                        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
                        "quotes": {
                            rtd: {"price": price, "order_book_id": symbol_map.get(rtd)}
                            for rtd, price in sorted(prices.items())
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        return prices, symbol_map, {rtd: self.instrument_dates.get(rq_id) for rtd, rq_id in symbol_map.items() if self.instrument_dates.get(rq_id)}


def extract_snapshot_price(snapshot: Any) -> float | None:
    for name in ("last", "last_price", "price", "close", "prev_close", "open"):
        value = getattr(snapshot, name, None)
        price = parse_number(value)
        if price is not None and price > 0:
            return price
    if hasattr(snapshot, "__dict__"):
        for key, value in snapshot.__dict__.items():
            if key.lower() in {"last", "last_price", "price", "close"}:
                price = parse_number(value)
                if price is not None and price > 0:
                    return price
    return None


def build_monitor(source: str = "auto", funding_rate: float | None = None) -> dict[str, Any]:
    template = XlsxMonitorTemplate(TEMPLATE_XLSX)
    parsed = template.parse()
    file_quotes = load_file_quotes()
    rtd_symbols = sorted(
        {
            item["rtd_symbol"]
            for product in parsed["quote_rows"].values()
            for item in product.values()
            if item.get("rtd_symbol")
        }
    )
    rq_prices: dict[str, float] = {}
    rq_symbol_map: dict[str, str] = {}
    rq_maturity_map: dict[str, str] = {}
    rq_errors: list[str] = []
    if source in {"auto", "rqdata"}:
        rq = RqdataQuoteSource(load_config())
        rq_prices, rq_symbol_map, rq_maturity_map = rq.fetch(rtd_symbols)
        rq_errors = rq.errors

    quote_grid: dict[str, dict[int, QuotePoint]] = {}
    opportunities: list[Opportunity] = []
    capital_cost_rate = funding_rate if funding_rate is not None else (template.number("N49") or 0.03)

    for product, month_items in parsed["quote_rows"].items():
        quote_grid[product] = {}
        for month in MONTHS:
            item = month_items.get(month)
            if not item:
                continue
            rtd = item.get("rtd_symbol")
            price = None
            quote_source = "none"
            if source != "excel" and rtd and rtd in rq_prices:
                price, quote_source = rq_prices[rtd], "rqdata"
            elif source == "auto" and rtd and rtd in file_quotes:
                price, quote_source = file_quotes[rtd], "file"
            elif source != "rqdata":
                price, quote_source = item.get("cached_price"), "excel-cache"
            if price is None or price <= 0:
                continue
            quote_grid[product][month] = QuotePoint(
                month=month,
                month_name=MONTH_NAMES[month],
                rtd_symbol=rtd,
                rq_symbol=rq_symbol_map.get(rtd) if rtd else None,
                price=float(price),
                source=quote_source,
                maturity_date=rq_maturity_map.get(rtd) if rtd else None,
            )

        fees = parsed["fees"].get(product)
        delivery = parsed["delivery"].get(product, {})
        if not fees:
            continue
        valid_months = [
            m for m in MONTHS
            if m in quote_grid[product] and m in delivery and quote_grid[product][m].price is not None
        ]
        for near_m, far_m in zip(valid_months, valid_months[1:]):
            days = int(round(delivery[far_m] - delivery[near_m]))
            if days <= 0:
                continue
            near = quote_grid[product][near_m]
            far = quote_grid[product][far_m]
            if near.maturity_date and far.maturity_date and far.maturity_date <= near.maturity_date:
                continue
            near_price = near.price or 0.0
            far_price = far.price or 0.0
            spread = near_price - far_price
            storage_cost = fees.storage_fee * days
            commission = near_price * fees.commission_rate if fees.commission_rate else fees.trade_fee
            finance_and_tax = near_price * (days * capital_cost_rate / 365 + fees.stamp_tax)
            fee_before_vat = storage_cost + commission + fees.delivery_fee + finance_and_tax
            fee_total = fee_before_vat * (1 + fees.vat_rate)
            cost_spread = -fee_total
            net_profit = cost_spread - spread
            annualized_return = net_profit / near_price * 365 / days if near_price > 0 and days > 0 else 0.0
            delivery_near, delivery_far = cycle_delivery_dates(delivery.get(near_m), delivery.get(far_m))
            opportunities.append(
                Opportunity(
                    product=product,
                    sector=SECTOR_MAP.get(product, "其他"),
                    near_month=near_m,
                    far_month=far_m,
                    pair=f"{near_m}-{far_m}月",
                    near_symbol=near.rq_symbol or near.rtd_symbol,
                    far_symbol=far.rq_symbol or far.rtd_symbol,
                    near_price=near_price,
                    far_price=far_price,
                    spread=spread,
                    cost_spread=cost_spread,
                    net_profit=net_profit,
                    annualized_return=annualized_return,
                    hold_days=days,
                    capital_cost_rate=capital_cost_rate,
                    storage_fee=storage_cost,
                    fee_total=fee_total,
                    quote_source=near.source if near.source == far.source else f"{near.source}/{far.source}",
                    is_profitable=net_profit > 0,
                    delivery_near=delivery_near,
                    delivery_far=delivery_far,
                )
            )

    opportunities.sort(key=lambda x: x.annualized_return, reverse=True)
    profitable = [x for x in opportunities if x.is_profitable]
    return {
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "source_requested": source,
        "quote_status": {
            "rqdata_prices": len(rq_prices),
            "file_quotes": len(file_quotes),
            "rqdata_errors": rq_errors[-5:],
        },
        "capital_cost_rate": capital_cost_rate,
        "mode": "server",
        "summary": {
            "products": len(quote_grid),
            "spreads": len(opportunities),
            "profitable": len(profitable),
            "best_return": profitable[0].annualized_return if profitable else (opportunities[0].annualized_return if opportunities else None),
        },
        "quotes": {
            product: {str(month): asdict(point) for month, point in months.items()}
            for product, months in quote_grid.items()
        },
        "opportunities": [asdict(item) for item in opportunities],
    }


def export_static(source: str = "rqdata", funding_rate: float | None = None) -> dict[str, Any]:
    payload = build_monitor(source=source, funding_rate=funding_rate)
    payload["mode"] = "static"
    payload["generated_for"] = "github-pages"
    STATIC_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(BASE_DIR / "index.html", DIST_DIR / "index.html")
    STATIC_DATA_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


class MonitorHandler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/data":
            query = parse_qs(parsed.query)
            source = query.get("source", ["auto"])[0]
            funding = parse_number(query.get("funding_rate", [None])[0])
            try:
                payload = build_monitor(source=source, funding_rate=funding)
                self.send_json(payload)
            except Exception as exc:
                self.send_json(
                    {
                        "error": str(exc),
                        "traceback": traceback.format_exc(),
                        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
                    },
                    status=500,
                )
            return
        if parsed.path in {"/", "/index.html"}:
            self.path = "/index.html"
        return super().do_GET()

    def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main() -> None:
    parser = argparse.ArgumentParser(description="可转抛价差监控本地服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--once", action="store_true", help="只计算一次并输出 JSON")
    parser.add_argument("--export-static", action="store_true", help="生成 GitHub Pages 静态站点到 dist/")
    parser.add_argument("--source", choices=["auto", "rqdata", "excel"], default="auto")
    parser.add_argument("--funding-rate", type=float, default=None)
    args = parser.parse_args()
    if args.once:
        print(json.dumps(build_monitor(source=args.source, funding_rate=args.funding_rate), ensure_ascii=False, indent=2))
        return
    if args.export_static:
        payload = export_static(source=args.source, funding_rate=args.funding_rate)
        print(
            json.dumps(
                {
                    "dist": str(DIST_DIR),
                    "data": str(STATIC_DATA_PATH),
                    "summary": payload["summary"],
                    "quote_status": payload["quote_status"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    server = ThreadingHTTPServer((args.host, args.port), MonitorHandler)
    print(f"可转抛价差监控已启动: http://{args.host}:{args.port}/")
    print("按 Ctrl+C 停止服务。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
