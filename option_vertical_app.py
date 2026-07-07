from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "option_monitor_config.local.json"
CONFIG_EXAMPLE_PATH = BASE_DIR / "option_monitor_config.example.json"
SHARED_RQDATA_CONFIG_PATH = BASE_DIR / "rqdata_config.local.json"
STATIC_DIR = BASE_DIR / "option_vertical_web"
DIST_DIR = BASE_DIR / "dist"
STATIC_EXPORT_DIR = DIST_DIR / "option-vertical-spread"
STATIC_CONFIG_PATH = STATIC_EXPORT_DIR / "config.json"
STATIC_DATA_PATH = STATIC_EXPORT_DIR / "opportunities.json"

DEFAULT_OPTION_BOARDS = {
    "黑色": ["I", "RB", "HC", "J", "JM", "SF", "SM"],
    "有色/贵金属": ["CU", "AL", "ZN", "PB", "NI", "SN", "AU", "AG", "AO", "LC", "SI"],
    "能源化工": ["RU", "BR", "FU", "BU", "SC", "LU", "NR", "TA", "MA", "PF", "PX", "SA", "UR", "FG", "L", "V", "PP", "EG", "EB", "PG", "SH"],
    "农产品": ["M", "Y", "P", "A", "B", "C", "CS", "JD", "LH", "SR", "CF", "OI", "RM", "AP", "CJ", "PK"],
    "金融": ["IF", "IH", "IC", "IM", "T", "TF", "TS", "TL"],
}


def _today() -> dt.date:
    return dt.date.today()


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _first_number(value: Any) -> float | None:
    if isinstance(value, (list, tuple)) and value:
        return _as_float(value[0])
    return _as_float(value)


def _parse_date(value: Any) -> dt.date | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            candidate = text[:8] if fmt == "%Y%m%d" else text[:10]
            return dt.datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    return None


def _parse_datetime(value: Any) -> dt.datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time())
    text = str(value).strip().replace("T", " ")
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            size = 26 if "%f" in fmt else 19
            return dt.datetime.strptime(text[:size], fmt)
        except ValueError:
            continue
    return None


def _field(obj: Any, names: Iterable[str]) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        lower_map = {str(key).lower(): value for key, value in obj.items()}
        for name in names:
            if name in obj:
                return obj[name]
            value = lower_map.get(name.lower())
            if value is not None:
                return value
        return None
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    inner_data = getattr(obj, "_data", None)
    if isinstance(inner_data, dict):
        return _field(inner_data, names)
    if hasattr(obj, "to_dict"):
        try:
            return _field(obj.to_dict(), names)
        except Exception:
            return None
    return None


def _row_id(row: Any) -> str | None:
    value = _field(row, ("order_book_id", "id", "contract", "symbol"))
    return str(value).strip().upper() if value else None


def _rows_from_table(data: Any) -> list[Any]:
    if data is None:
        return []
    if hasattr(data, "to_dict"):
        try:
            return list(data.to_dict("records"))
        except Exception:
            pass
    if isinstance(data, dict):
        if all(isinstance(value, dict) for value in data.values()):
            return list(data.values())
        return [data]
    if isinstance(data, (list, tuple, set)):
        return list(data)
    return [data]


def _future_core(order_book_id: str) -> str:
    text = order_book_id.upper().split(".")[0].replace("_", "")
    match = re.search(r"([A-Z]+[0-9]{3,4})", text)
    return match.group(1) if match else text


def _parse_option_code(order_book_id: str, future_core: str | None = None) -> tuple[str | None, str | None, float | None]:
    text = order_book_id.upper().split(".")[0].replace("_", "")
    if future_core:
        pattern = rf"({re.escape(future_core)})([CP])([0-9]+(?:\.[0-9]+)?)"
        match = re.search(pattern, text)
        if match:
            return match.group(1), match.group(2), _as_float(match.group(3))
    match = re.search(r"([A-Z]+[0-9]{3,4})([CP])([0-9]+(?:\.[0-9]+)?)", text)
    if match:
        return match.group(1), match.group(2), _as_float(match.group(3))
    return None, None, None


@dataclass
class MonitorConfig:
    underlying_symbol: str
    option_boards: dict[str, list[str]]
    poll_seconds: int
    min_profit: float
    min_volume: float
    default_margin_rate: float | None
    market: str
    rqdata_username: str | None
    rqdata_password: str | None
    rqdata_api_key: str | None
    rqdata_uri: str | None
    rqdata_addr: tuple[str, int]


def load_config() -> MonitorConfig:
    config_path = CONFIG_PATH if CONFIG_PATH.exists() else CONFIG_EXAMPLE_PATH
    raw: dict[str, Any] = {}
    if config_path.exists():
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    shared_rqdata: dict[str, Any] = {}
    if SHARED_RQDATA_CONFIG_PATH.exists():
        shared_raw = json.loads(SHARED_RQDATA_CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(shared_raw, dict):
            shared_rqdata = shared_raw

    rqdata_config = raw.get("rqdata", {}) if isinstance(raw.get("rqdata"), dict) else {}
    datafeed_username = raw.get("datafeed.username")
    datafeed_password = raw.get("datafeed.password")
    username = os.getenv("RQDATA_USERNAME") or os.getenv("RQDATAC_USERNAME") or rqdata_config.get("username") or raw.get("username") or shared_rqdata.get("username")
    password = os.getenv("RQDATA_PASSWORD") or os.getenv("RQDATAC_PASSWORD") or rqdata_config.get("password") or raw.get("password") or shared_rqdata.get("password")
    api_key = os.getenv("RQDATA_API_KEY") or os.getenv("RQDATAC_API_KEY") or rqdata_config.get("api_key") or raw.get("api_key") or shared_rqdata.get("api_key")
    uri = os.getenv("RQDATA_URI") or os.getenv("RQDATAC_URI") or rqdata_config.get("uri") or raw.get("uri") or shared_rqdata.get("uri")
    env_host = os.getenv("RQDATA_ADDR_HOST") or os.getenv("RQDATAC_ADDR_HOST")
    env_port = os.getenv("RQDATA_ADDR_PORT") or os.getenv("RQDATAC_ADDR_PORT")
    addr_raw = [env_host, int(env_port or 16011)] if env_host else (rqdata_config.get("addr") or raw.get("addr") or shared_rqdata.get("addr") or ["rqdatad-pro.ricequant.com", 16011])
    if isinstance(addr_raw, str):
        if ":" in addr_raw:
            host, port_text = addr_raw.rsplit(":", 1)
            addr = (host, int(port_text))
        else:
            addr = (addr_raw, 16011)
    else:
        addr = (str(addr_raw[0]), int(addr_raw[1]))

    boards_raw = raw.get("option_boards") or DEFAULT_OPTION_BOARDS
    option_boards = {
        str(board): [str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()]
        for board, symbols in boards_raw.items()
    }

    return MonitorConfig(
        underlying_symbol=str(raw.get("underlying_symbol") or "AG").strip().upper(),
        option_boards=option_boards,
        poll_seconds=max(30, int(raw.get("poll_seconds", 300))),
        min_profit=max(0.0, _as_float(raw.get("min_profit")) or 0.0),
        min_volume=max(0.0, _as_float(raw.get("min_volume")) or 1.0),
        default_margin_rate=(_as_float(raw.get("default_margin_rate")) if _as_float(raw.get("default_margin_rate")) and _as_float(raw.get("default_margin_rate")) > 0 else None),
        market=str(os.getenv("RQDATA_MARKET") or raw.get("market") or rqdata_config.get("market") or shared_rqdata.get("market") or "cn").strip(),
        rqdata_username=str(username).strip() if username else None,
        rqdata_password=str(password).strip() if password else None,
        rqdata_api_key=str(api_key or datafeed_password).strip() if (api_key or datafeed_password) else None,
        rqdata_uri=str(uri).strip() if uri else None,
        rqdata_addr=addr,
    )


class RqdataAdapter:
    def __init__(self, config: MonitorConfig):
        self.config = config
        self._rqdatac: Any = None
        self._initialized = False
        self._option_rows_cache: list[Any] = []
        self._option_rows_loaded_at = 0.0
        self._margin_rate_cache: dict[str, float | None] = {}
        self._lock = threading.Lock()

    def _connect(self) -> None:
        with self._lock:
            if self._initialized:
                return
            try:
                import rqdatac  # type: ignore
            except ImportError as exc:
                raise RuntimeError("未安装 rqdatac。请先运行 pip install -r requirements.txt。") from exc

            if self.config.rqdata_uri:
                rqdatac.init(uri=self.config.rqdata_uri, lazy=False)
            elif self.config.rqdata_username and self.config.rqdata_password:
                rqdatac.init(self.config.rqdata_username, self.config.rqdata_password, addr=self.config.rqdata_addr, lazy=False)
            elif self.config.rqdata_api_key:
                rqdatac.init("license", self.config.rqdata_api_key, addr=self.config.rqdata_addr, lazy=False)
            else:
                if os.getenv("RQDATA_USE_LOCAL_AUTH") != "1":
                    raise RuntimeError("未配置 rqdata 认证。请设置 RQDATA_API_KEY，或在 monitor_config.json 写入 api_key。")
                rqdatac.init()
            self._rqdatac = rqdatac
            self._initialized = True

    def dominant_contract(self, underlying_symbol: str) -> str:
        self._connect()
        today = _today()
        dominant = self._rqdatac.futures.get_dominant(
            underlying_symbol,
            start_date=today,
            end_date=today,
            market=self.config.market,
        )
        if hasattr(dominant, "iloc") and len(dominant):
            return str(dominant.iloc[-1]).strip().upper()
        for row in _rows_from_table(dominant):
            value = next((v for v in row.values() if v), None) if isinstance(row, dict) else row
            if value:
                return str(value).strip().upper()
        raise RuntimeError(f"未找到 {underlying_symbol} 的主力合约")

    def _instrument(self, order_book_id: str) -> Any:
        self._connect()
        try:
            return self._rqdatac.instruments(order_book_id)
        except Exception:
            return None

    def _margin_rate(self, future_contract: str) -> float | None:
        future_contract = future_contract.strip().upper()
        if future_contract in self._margin_rate_cache:
            return self._margin_rate_cache[future_contract]
        margin_rate = None
        try:
            margin_df = self._rqdatac.futures.get_commission_margin(
                [future_contract],
                fields=["long_margin_ratio", "short_margin_ratio"],
            )
            rows = _rows_from_table(margin_df)
            for row in rows:
                margin_rate = _as_float(_field(row, ("long_margin_ratio",))) or _as_float(_field(row, ("short_margin_ratio",)))
                if margin_rate and margin_rate > 0:
                    break
        except Exception:
            margin_rate = None
        if not margin_rate:
            instrument = self._instrument(future_contract)
            margin_rate = _as_float(
                _field(
                    instrument,
                    ("margin_rate", "long_margin_rate", "short_margin_rate", "maintenance_margin_rate", "margin_ratio"),
                )
            )
        if not margin_rate or margin_rate <= 0:
            margin_rate = self.config.default_margin_rate
        self._margin_rate_cache[future_contract] = margin_rate if margin_rate and margin_rate > 0 else None
        return self._margin_rate_cache[future_contract]

    def _return_metrics(self, future_contract: str, maturity_date: str | None, profit_amount: float, market_value_amount: float | None) -> dict[str, float | int | None]:
        days_to_expiry = None
        parsed_maturity = _parse_date(maturity_date)
        if parsed_maturity:
            days_to_expiry = max((parsed_maturity - _today()).days, 0)
        expiry_return = profit_amount / market_value_amount if market_value_amount and market_value_amount > 0 else None
        annualized_expiry_return = (
            expiry_return * 365 / days_to_expiry
            if expiry_return is not None and days_to_expiry and days_to_expiry > 0
            else None
        )
        margin_rate = self._margin_rate(future_contract)
        return {
            "days_to_expiry": days_to_expiry,
            "margin_rate": margin_rate,
            "open_market_value_amount": market_value_amount,
            "expiry_return": expiry_return,
            "annualized_expiry_return": annualized_expiry_return,
            "annualized_leveraged_expiry_return": None,
        }

    @staticmethod
    def _open_market_value_amount(future_price: float | None, fallback_price: float | None, multiplier: float) -> float | None:
        price = future_price if future_price and future_price > 0 else fallback_price
        if price is None or price <= 0 or multiplier <= 0:
            return None
        return price * multiplier

    @staticmethod
    def _short_option_margin_amount(
        option_type: str,
        future_price: float | None,
        strike: float,
        option_price: float,
        multiplier: float,
        margin_rate: float | None,
    ) -> float | None:
        if future_price is None or future_price <= 0 or strike <= 0 or multiplier <= 0 or not margin_rate or margin_rate <= 0:
            return None
        if option_type == "C":
            out_of_money = max(strike - future_price, 0.0)
            floor_margin = 0.5 * future_price * margin_rate
        else:
            out_of_money = max(future_price - strike, 0.0)
            floor_margin = 0.5 * strike * margin_rate
        short_margin_per_unit = option_price + max(future_price * margin_rate - 0.5 * out_of_money, floor_margin)
        return short_margin_per_unit * multiplier

    @staticmethod
    def _annualized_margin_return(profit_per_lot: float, margin_amount: float | None, days_to_expiry: int | None) -> float | None:
        if not margin_amount or margin_amount <= 0 or not days_to_expiry or days_to_expiry <= 0:
            return None
        return profit_per_lot / margin_amount * 365 / days_to_expiry

    def _current_snapshots(self, order_book_ids: list[str]) -> dict[str, Any]:
        self._connect()
        ids = [item.strip().upper() for item in order_book_ids if item]
        if not ids:
            return {}
        try:
            snapshots = self._rqdatac.current_snapshot(ids, market=self.config.market)
        except Exception:
            snapshots = [self._rqdatac.current_snapshot(item, market=self.config.market) for item in ids]

        if isinstance(snapshots, dict):
            return {str(key).upper(): value for key, value in snapshots.items()}

        rows = _rows_from_table(snapshots)
        mapped: dict[str, Any] = {}
        for index, row in enumerate(rows):
            row_order_book_id = _row_id(row) or (ids[index] if index < len(ids) else None)
            if row_order_book_id:
                mapped[row_order_book_id.upper()] = row
        if not mapped and len(ids) == 1:
            mapped[ids[0].upper()] = snapshots
        return mapped

    def _option_universe(self) -> list[Any]:
        self._connect()
        now = time.time()
        if self._option_rows_cache and now - self._option_rows_loaded_at < 600:
            return self._option_rows_cache

        rows: list[Any] = []
        seen: set[str] = set()
        calls = [
            {"type": "Option", "date": _today()},
            {"type": "option", "date": _today()},
            {"type": "FutureOption", "date": _today()},
            {"type": "Option"},
        ]
        for kwargs in calls:
            try:
                result = self._rqdatac.all_instruments(**kwargs)
            except Exception:
                continue
            for row in _rows_from_table(result):
                order_book_id = _row_id(row)
                if not order_book_id or order_book_id in seen:
                    continue
                seen.add(order_book_id)
                rows.append(row)
            if rows:
                break

        self._option_rows_cache = rows
        self._option_rows_loaded_at = now
        return rows

    def active_options_for_future(self, future_contract: str, option_kind: str) -> list[dict[str, Any]]:
        today = _today()
        future_core = _future_core(future_contract)
        option_kind = option_kind.upper()
        options: list[dict[str, Any]] = []
        for row in self._option_universe():
            order_book_id = _row_id(row)
            if not order_book_id:
                continue
            parsed_underlying, parsed_type, parsed_strike = _parse_option_code(order_book_id, future_core)
            option_type = str(_field(row, ("option_type", "type", "contract_type", "call_or_put")) or "").upper()
            normalized_type = parsed_type or option_type[:1]
            if option_type == "PUT":
                normalized_type = "P"
            if option_type == "CALL":
                normalized_type = "C"
            if normalized_type != option_kind:
                continue

            underlying = str(_field(row, ("underlying_order_book_id", "underlying_symbol")) or "").strip().upper()
            underlying_core = _future_core(underlying) if underlying else parsed_underlying
            if underlying_core != future_core:
                continue

            listed_date = _parse_date(_field(row, ("listed_date", "list_date", "start_date")))
            maturity_date = _parse_date(
                _field(row, ("maturity_date", "expire_date", "expiration_date", "last_trade_date", "de_listed_date"))
            )
            if listed_date and today < listed_date:
                continue
            if maturity_date and today > maturity_date:
                continue

            strike = _as_float(_field(row, ("strike_price", "exercise_price", "strike"))) or parsed_strike
            if strike is None:
                continue
            options.append(
                {
                    "contract": order_book_id,
                    "option_type": option_kind,
                    "strike": strike,
                    "maturity_date": maturity_date.isoformat() if maturity_date else None,
                    "multiplier": _as_float(_field(row, ("contract_multiplier", "multiplier"))),
                    "name": str(_field(row, ("symbol", "abbrev_symbol", "name")) or ""),
                }
            )
        return sorted(options, key=lambda item: (item["strike"], item["contract"]))

    def scan_put_arbitrage(self, underlying_symbol: str | None = None) -> dict[str, Any]:
        symbol = (underlying_symbol or self.config.underlying_symbol).strip().upper()
        future_contract = self.dominant_contract(symbol)
        future_snapshot = self._current_snapshots([future_contract]).get(future_contract.upper())
        future_last = self._last_price(future_snapshot)
        puts = self.active_puts_for_future(future_contract)
        snapshots: dict[str, Any] = {}
        option_ids = [item["contract"] for item in puts]
        for index in range(0, len(option_ids), 400):
            snapshots.update(self._current_snapshots(option_ids[index:index + 400]))

        quote_rows = []
        for item in puts:
            snapshot = snapshots.get(item["contract"].upper())
            ask1 = self._ask_price(snapshot)
            bid1 = self._bid_price(snapshot)
            ask_volume = self._ask_volume(snapshot)
            bid_volume = self._bid_volume(snapshot)
            quote_rows.append(
                {
                    **item,
                    "bid1": bid1,
                    "ask1": ask1,
                    "bid_volume": bid_volume,
                    "ask_volume": ask_volume,
                    "last": self._last_price(snapshot),
                    "quote_time": self._quote_time(snapshot),
                }
            )

        candidates = self._find_put_inversions(quote_rows)
        opportunities = [self._verify_opportunity(item) for item in candidates]
        opportunities.sort(key=lambda item: (item["verified"] is not True, -item["profit_per_unit"], item["low_strike"]))

        verified = [item for item in opportunities if item["verified"]]
        return {
            "ok": True,
            "underlying_symbol": symbol,
            "future_contract": future_contract,
            "future_last": future_last,
            "put_count": len(quote_rows),
            "candidate_count": len(candidates),
            "verified_count": len(verified),
            "poll_seconds": self.config.poll_seconds,
            "min_profit": self.config.min_profit,
            "min_volume": self.config.min_volume,
            "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "puts": quote_rows,
            "opportunities": opportunities,
        }

    def _find_put_inversions(self, puts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        valid = [item for item in puts if item.get("bid1") is not None or item.get("ask1") is not None]
        valid.sort(key=lambda item: item["strike"])
        for low_index, low_put in enumerate(valid):
            low_bid = _as_float(low_put.get("bid1"))
            low_bid_volume = _as_float(low_put.get("bid_volume")) or 0.0
            if low_bid is None or low_bid <= 0 or low_bid_volume < self.config.min_volume:
                continue
            for high_put in valid[low_index + 1 :]:
                high_ask = _as_float(high_put.get("ask1"))
                high_ask_volume = _as_float(high_put.get("ask_volume")) or 0.0
                if high_ask is None or high_ask <= 0 or high_ask_volume < self.config.min_volume:
                    continue
                profit = low_bid - high_ask
                if profit <= self.config.min_profit:
                    continue
                candidates.append(self._build_opportunity(low_put, high_put, low_bid, high_ask, source="snapshot"))
        return candidates

    def _build_opportunity(
        self,
        low_put: dict[str, Any],
        high_put: dict[str, Any],
        low_bid: float,
        high_ask: float,
        source: str,
    ) -> dict[str, Any]:
        multiplier = high_put.get("multiplier") or low_put.get("multiplier") or 1.0
        width = high_put["strike"] - low_put["strike"]
        profit_per_unit = low_bid - high_ask
        return {
            "source": source,
            "low_contract": low_put["contract"],
            "low_strike": low_put["strike"],
            "low_bid1": low_bid,
            "low_bid_volume": low_put.get("bid_volume"),
            "high_contract": high_put["contract"],
            "high_strike": high_put["strike"],
            "high_ask1": high_ask,
            "high_ask_volume": high_put.get("ask_volume"),
            "strike_width": width,
            "profit_per_unit": profit_per_unit,
            "profit_per_lot": profit_per_unit * multiplier,
            "multiplier": multiplier,
            "maturity_date": high_put.get("maturity_date") or low_put.get("maturity_date"),
            "quote_time": high_put.get("quote_time") or low_put.get("quote_time"),
            "action": "买入高执行价 Put，卖出低执行价 Put",
            "verified": False,
            "verification_message": "等待 tick 复验",
        }

    def _verify_opportunity(self, opportunity: dict[str, Any]) -> dict[str, Any]:
        high_tick = self._latest_tick_quote(opportunity["high_contract"])
        low_tick = self._latest_tick_quote(opportunity["low_contract"])
        if not high_tick or not low_tick:
            opportunity.update(
                {
                    "verified": False,
                    "verification_message": "未取得两个合约的 tick 盘口",
                    "tick_high_ask1": high_tick.get("ask1") if high_tick else None,
                    "tick_low_bid1": low_tick.get("bid1") if low_tick else None,
                }
            )
            return opportunity

        high_ask = _as_float(high_tick.get("ask1"))
        low_bid = _as_float(low_tick.get("bid1"))
        if high_ask is None or low_bid is None or high_ask <= 0 or low_bid <= 0:
            opportunity.update(
                {
                    "verified": False,
                    "verification_message": "tick 盘口缺少有效买一或卖一",
                    "tick_high_ask1": high_ask,
                    "tick_low_bid1": low_bid,
                    "tick_high_time": high_tick.get("time"),
                    "tick_low_time": low_tick.get("time"),
                }
            )
            return opportunity

        profit = low_bid - high_ask
        verified = profit > self.config.min_profit
        multiplier = opportunity.get("multiplier") or 1.0
        opportunity.update(
            {
                "verified": verified,
                "verification_message": "tick 复验通过" if verified else "tick 复验后价差消失",
                "tick_high_ask1": high_ask,
                "tick_low_bid1": low_bid,
                "tick_profit_per_unit": profit,
                "tick_profit_per_lot": profit * multiplier,
                "tick_high_time": high_tick.get("time"),
                "tick_low_time": low_tick.get("time"),
            }
        )
        return opportunity

    def _quote_options(self, options: list[dict[str, Any]]) -> list[dict[str, Any]]:
        snapshots: dict[str, Any] = {}
        option_ids = [item["contract"] for item in options]
        for index in range(0, len(option_ids), 400):
            snapshots.update(self._current_snapshots(option_ids[index:index + 400]))

        rows = []
        for item in options:
            snapshot = snapshots.get(item["contract"].upper())
            rows.append(
                {
                    **item,
                    "bid1": self._bid_price(snapshot),
                    "ask1": self._ask_price(snapshot),
                    "bid_volume": self._bid_volume(snapshot),
                    "ask_volume": self._ask_volume(snapshot),
                    "last": self._last_price(snapshot),
                    "quote_time": self._quote_time(snapshot),
                }
            )
        return rows

    def scan_symbol_arbitrage(self, underlying_symbol: str) -> dict[str, Any]:
        symbol = underlying_symbol.strip().upper()
        future_contract = self.dominant_contract(symbol)
        future_snapshot = self._current_snapshots([future_contract]).get(future_contract.upper())
        future_last = self._last_price(future_snapshot)
        put_rows = self._quote_options(self.active_options_for_future(future_contract, "P"))
        call_rows = self._quote_options(self.active_options_for_future(future_contract, "C"))
        for row in put_rows + call_rows:
            row["underlying_symbol"] = symbol
            row["future_contract"] = future_contract

        put_opportunities = [self._verify_vertical_opportunity(item) for item in self._find_vertical_puts(symbol, future_contract, future_last, put_rows)]
        call_opportunities = [self._verify_vertical_opportunity(item) for item in self._find_vertical_calls(symbol, future_contract, future_last, call_rows)]
        for rows in (put_opportunities, call_opportunities):
            rows.sort(key=lambda item: (item["verified"] is not True, -item["profit_per_unit"], item["low_strike"]))

        opportunities = put_opportunities + call_opportunities
        verified = [item for item in opportunities if item["verified"]]
        return {
            "ok": True,
            "underlying_symbol": symbol,
            "future_contract": future_contract,
            "future_last": future_last,
            "put_count": len(put_rows),
            "call_count": len(call_rows),
            "candidate_count": len(opportunities),
            "put_candidate_count": len(put_opportunities),
            "call_candidate_count": len(call_opportunities),
            "verified_count": len(verified),
            "put_verified_count": len([item for item in put_opportunities if item["verified"]]),
            "call_verified_count": len([item for item in call_opportunities if item["verified"]]),
            "poll_seconds": self.config.poll_seconds,
            "min_profit": self.config.min_profit,
            "min_volume": self.config.min_volume,
            "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "puts": put_rows,
            "calls": call_rows,
            "put_opportunities": put_opportunities,
            "call_opportunities": call_opportunities,
            "opportunities": opportunities,
        }

    def scan_many_arbitrage(self, symbols: list[str]) -> dict[str, Any]:
        normalized = [item.strip().upper() for item in symbols if item.strip()]
        if not normalized:
            normalized = [self.config.underlying_symbol]

        results = []
        errors = []
        for symbol in normalized:
            try:
                results.append(self.scan_symbol_arbitrage(symbol))
            except Exception as exc:
                errors.append({"underlying_symbol": symbol, "message": str(exc)})

        puts = [row for result in results for row in result.get("puts", [])]
        calls = [row for result in results for row in result.get("calls", [])]
        put_opportunities = [row for result in results for row in result.get("put_opportunities", [])]
        call_opportunities = [row for result in results for row in result.get("call_opportunities", [])]
        opportunities = put_opportunities + call_opportunities
        return {
            "ok": bool(results),
            "symbols": normalized,
            "results": results,
            "errors": errors,
            "future_contract": " / ".join(item["future_contract"] for item in results[:3]) + (" ..." if len(results) > 3 else ""),
            "future_last": results[0]["future_last"] if len(results) == 1 else None,
            "put_count": len(puts),
            "call_count": len(calls),
            "candidate_count": len(opportunities),
            "put_candidate_count": len(put_opportunities),
            "call_candidate_count": len(call_opportunities),
            "verified_count": len([item for item in opportunities if item["verified"]]),
            "put_verified_count": len([item for item in put_opportunities if item["verified"]]),
            "call_verified_count": len([item for item in call_opportunities if item["verified"]]),
            "poll_seconds": self.config.poll_seconds,
            "min_profit": self.config.min_profit,
            "min_volume": self.config.min_volume,
            "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "puts": puts,
            "calls": calls,
            "put_opportunities": put_opportunities,
            "call_opportunities": call_opportunities,
            "opportunities": opportunities,
        }

    def _find_vertical_puts(self, symbol: str, future_contract: str, future_price: float | None, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidates = []
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(str(row.get("maturity_date") or ""), []).append(row)
        for group_rows in groups.values():
            valid = sorted(group_rows, key=lambda item: item["strike"])
            for low_index, low_option in enumerate(valid):
                sell_price = _as_float(low_option.get("bid1"))
                sell_volume = _as_float(low_option.get("bid_volume")) or 0.0
                if sell_price is None or sell_price <= 0 or sell_volume < self.config.min_volume:
                    continue
                for high_option in valid[low_index + 1 :]:
                    buy_price = _as_float(high_option.get("ask1"))
                    buy_volume = _as_float(high_option.get("ask_volume")) or 0.0
                    if buy_price is None or buy_price <= 0 or buy_volume < self.config.min_volume:
                        continue
                    if sell_price - buy_price > self.config.min_profit:
                        candidates.append(self._build_vertical_opportunity(symbol, future_contract, future_price, "P", low_option, high_option, buy_price, sell_price))
        return candidates

    def _find_vertical_calls(self, symbol: str, future_contract: str, future_price: float | None, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidates = []
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(str(row.get("maturity_date") or ""), []).append(row)
        for group_rows in groups.values():
            valid = sorted(group_rows, key=lambda item: item["strike"])
            for low_index, low_option in enumerate(valid):
                buy_price = _as_float(low_option.get("ask1"))
                buy_volume = _as_float(low_option.get("ask_volume")) or 0.0
                if buy_price is None or buy_price <= 0 or buy_volume < self.config.min_volume:
                    continue
                for high_option in valid[low_index + 1 :]:
                    sell_price = _as_float(high_option.get("bid1"))
                    sell_volume = _as_float(high_option.get("bid_volume")) or 0.0
                    if sell_price is None or sell_price <= 0 or sell_volume < self.config.min_volume:
                        continue
                    if sell_price - buy_price > self.config.min_profit:
                        candidates.append(self._build_vertical_opportunity(symbol, future_contract, future_price, "C", low_option, high_option, buy_price, sell_price))
        return candidates

    def _build_vertical_opportunity(
        self,
        symbol: str,
        future_contract: str,
        future_price: float | None,
        option_type: str,
        low_option: dict[str, Any],
        high_option: dict[str, Any],
        buy_price: float,
        sell_price: float,
    ) -> dict[str, Any]:
        multiplier = high_option.get("multiplier") or low_option.get("multiplier") or 1.0
        if option_type == "P":
            buy_option = high_option
            sell_option = low_option
            action = "买入高执行价 Put，卖出低执行价 Put"
        else:
            buy_option = low_option
            sell_option = high_option
            action = "买入低执行价 Call，卖出高执行价 Call"
        profit_per_unit = sell_price - buy_price
        strike_width = high_option["strike"] - low_option["strike"]
        maturity_date = high_option.get("maturity_date") or low_option.get("maturity_date")
        open_market_value_amount = self._open_market_value_amount(future_price, sell_option["strike"], multiplier)
        return_metrics = self._return_metrics(future_contract, maturity_date, profit_per_unit * multiplier, open_market_value_amount)
        margin_rate = return_metrics["margin_rate"]
        short_margin_amount = self._short_option_margin_amount(
            option_type,
            future_price,
            sell_option["strike"],
            sell_price,
            multiplier,
            margin_rate,
        )
        buy_cost_amount = buy_price * multiplier
        sell_premium_amount = sell_price * multiplier
        net_credit_amount = sell_premium_amount - buy_cost_amount
        open_margin_amount = short_margin_amount + buy_cost_amount - sell_premium_amount if short_margin_amount is not None else None
        annualized_margin_expiry_return = self._annualized_margin_return(
            profit_per_unit * multiplier,
            open_margin_amount,
            return_metrics["days_to_expiry"],
        )
        return {
            "underlying_symbol": symbol,
            "future_contract": future_contract,
            "future_price": future_price,
            "option_type": option_type,
            "action": action,
            "low_contract": low_option["contract"],
            "low_strike": low_option["strike"],
            "low_bid1": low_option.get("bid1"),
            "low_ask1": low_option.get("ask1"),
            "low_bid_volume": low_option.get("bid_volume"),
            "low_ask_volume": low_option.get("ask_volume"),
            "high_contract": high_option["contract"],
            "high_strike": high_option["strike"],
            "high_bid1": high_option.get("bid1"),
            "high_ask1": high_option.get("ask1"),
            "high_bid_volume": high_option.get("bid_volume"),
            "high_ask_volume": high_option.get("ask_volume"),
            "buy_contract": buy_option["contract"],
            "buy_strike": buy_option["strike"],
            "buy_price": buy_price,
            "sell_contract": sell_option["contract"],
            "sell_strike": sell_option["strike"],
            "sell_price": sell_price,
            "strike_width": strike_width,
            "profit_per_unit": profit_per_unit,
            "profit_per_lot": profit_per_unit * multiplier,
            "multiplier": multiplier,
            "buy_cost_amount": buy_cost_amount,
            "sell_premium_amount": sell_premium_amount,
            "net_credit_amount": net_credit_amount,
            "short_margin_amount": short_margin_amount,
            "open_margin_amount": open_margin_amount,
            "maturity_date": maturity_date,
            "quote_time": high_option.get("quote_time") or low_option.get("quote_time"),
            **return_metrics,
            "annualized_margin_expiry_return": annualized_margin_expiry_return,
            "verified": False,
            "verification_message": "等待 tick 复验",
        }

    def _verify_vertical_opportunity(self, opportunity: dict[str, Any]) -> dict[str, Any]:
        buy_tick = self._latest_tick_quote(opportunity["buy_contract"])
        sell_tick = self._latest_tick_quote(opportunity["sell_contract"])
        buy_ask = _as_float(buy_tick.get("ask1")) if buy_tick else None
        sell_bid = _as_float(sell_tick.get("bid1")) if sell_tick else None
        opportunity.update(
            {
                "tick_buy_ask1": buy_ask,
                "tick_sell_bid1": sell_bid,
                "tick_buy_time": buy_tick.get("time") if buy_tick else None,
                "tick_sell_time": sell_tick.get("time") if sell_tick else None,
            }
        )
        if buy_ask is None or sell_bid is None or buy_ask <= 0 or sell_bid <= 0:
            opportunity.update({"verified": False, "verification_message": "tick 盘口缺少有效买入卖一或卖出买一"})
            return opportunity
        profit = sell_bid - buy_ask
        verified = profit > self.config.min_profit
        multiplier = opportunity.get("multiplier") or 1.0
        tick_metrics = self._return_metrics(
            opportunity["future_contract"],
            opportunity.get("maturity_date"),
            profit * multiplier,
            _as_float(opportunity.get("open_market_value_amount")),
        )
        tick_short_margin_amount = self._short_option_margin_amount(
            opportunity["option_type"],
            _as_float(opportunity.get("future_price")),
            _as_float(opportunity.get("sell_strike")) or 0.0,
            sell_bid,
            opportunity.get("multiplier") or 1.0,
            tick_metrics["margin_rate"],
        )
        tick_buy_cost_amount = buy_ask * multiplier
        tick_sell_premium_amount = sell_bid * multiplier
        tick_open_margin_amount = (
            tick_short_margin_amount + tick_buy_cost_amount - tick_sell_premium_amount
            if tick_short_margin_amount is not None
            else None
        )
        tick_annualized_margin_expiry_return = self._annualized_margin_return(
            profit * multiplier,
            tick_open_margin_amount,
            tick_metrics["days_to_expiry"],
        )
        opportunity.update(
            {
                "verified": verified,
                "verification_message": "tick 复验通过" if verified else "tick 复验后价差消失",
                "tick_profit_per_unit": profit,
                "tick_profit_per_lot": profit * multiplier,
                "tick_expiry_return": tick_metrics["expiry_return"],
                "tick_annualized_expiry_return": tick_metrics["annualized_expiry_return"],
                "tick_annualized_leveraged_expiry_return": tick_metrics["annualized_leveraged_expiry_return"],
                "tick_short_margin_amount": tick_short_margin_amount,
                "tick_open_margin_amount": tick_open_margin_amount,
                "tick_annualized_margin_expiry_return": tick_annualized_margin_expiry_return,
            }
        )
        return opportunity

    def _latest_tick_quote(self, order_book_id: str) -> dict[str, Any] | None:
        self._connect()
        today = _today()
        try:
            data = self._rqdatac.get_price(
                order_book_id,
                start_date=today,
                end_date=today,
                frequency="tick",
                fields=["b1", "a1"],
                expect_df=True,
                market=self.config.market,
            )
        except Exception:
            return None

        rows = []
        if hasattr(data, "reset_index"):
            rows = data.reset_index().to_dict("records")
        else:
            rows = _rows_from_table(data)
        for record in reversed(rows):
            timestamp = record.get("datetime") or record.get("date") or record.get("time")
            if hasattr(timestamp, "to_pydatetime"):
                timestamp = timestamp.to_pydatetime()
            bid1 = _first_number(record.get("b1") or record.get("bid") or record.get("bid1"))
            ask1 = _first_number(record.get("a1") or record.get("ask") or record.get("ask1"))
            if bid1 is None and ask1 is None:
                continue
            return {
                "bid1": bid1,
                "ask1": ask1,
                "time": timestamp.isoformat(sep=" ", timespec="milliseconds")
                if isinstance(timestamp, dt.datetime)
                else str(timestamp or ""),
            }
        return None

    @staticmethod
    def _last_price(snapshot: Any) -> float | None:
        return _as_float(_field(snapshot, ("last", "last_price", "latest", "latest_price", "close", "prev_close")))

    @staticmethod
    def _bid_price(snapshot: Any) -> float | None:
        return _first_number(_field(snapshot, ("bid", "bid_price", "b1", "bid1")))

    @staticmethod
    def _ask_price(snapshot: Any) -> float | None:
        return _first_number(_field(snapshot, ("ask", "ask_price", "a1", "ask1")))

    @staticmethod
    def _bid_volume(snapshot: Any) -> float | None:
        return _first_number(_field(snapshot, ("bid_vol", "bid_volume", "b1_v", "bid1_volume")))

    @staticmethod
    def _ask_volume(snapshot: Any) -> float | None:
        return _first_number(_field(snapshot, ("ask_vol", "ask_volume", "a1_v", "ask1_volume")))

    @staticmethod
    def _quote_time(snapshot: Any) -> str | None:
        value = _field(snapshot, ("datetime", "time", "trading_time", "update_time"))
        parsed = _parse_datetime(value)
        if parsed:
            return parsed.isoformat(sep=" ", timespec="seconds")
        return str(value) if value else None


def all_option_symbols(config: MonitorConfig) -> list[str]:
    symbols: list[str] = []
    seen: set[str] = set()
    for group in config.option_boards.values():
        for symbol in group:
            normalized = str(symbol).strip().upper()
            if normalized and normalized not in seen:
                seen.add(normalized)
                symbols.append(normalized)
    return symbols or [config.underlying_symbol]


def public_config(config: MonitorConfig | None = None) -> dict[str, Any]:
    config = config or load_config()
    return {
        "underlying_symbol": config.underlying_symbol,
        "option_boards": config.option_boards,
        "poll_seconds": config.poll_seconds,
        "min_profit": config.min_profit,
        "min_volume": config.min_volume,
        "has_credentials": bool(config.rqdata_api_key or (config.rqdata_username and config.rqdata_password)),
        "mode": "static",
    }


def export_static() -> dict[str, Any]:
    config = load_config()
    adapter = RqdataAdapter(config)
    symbols = all_option_symbols(config)
    payload = adapter.scan_many_arbitrage(symbols)
    payload["mode"] = "static"
    payload["generated_for"] = "github-pages"

    STATIC_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    for filename in ("index.html", "main.js", "styles.css"):
        shutil.copy2(STATIC_DIR / filename, STATIC_EXPORT_DIR / filename)
    STATIC_CONFIG_PATH.write_text(json.dumps(public_config(config), ensure_ascii=False, indent=2), encoding="utf-8")
    STATIC_DATA_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


class MonitorHandler(SimpleHTTPRequestHandler):
    server_version = "PutSpreadArb/1.0"

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        print("%s - %s" % (self.log_date_time_string(), format % args))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/config":
            self._send_json(self._public_config())
            return
        if parsed.path == "/api/opportunities":
            self._handle_opportunities(parsed.query)
            return
        if parsed.path in {"/", ""}:
            self.path = "/index.html"
        super().do_GET()

    def _handle_opportunities(self, query: str) -> None:
        config = load_config()
        adapter = RqdataAdapter(config)
        params = parse_qs(query)
        symbols_param = params.get("symbols", params.get("symbol", [config.underlying_symbol]))[0].strip().upper()
        if symbols_param in {"", "ALL", "__ALL__"}:
            symbols = [symbol for group in config.option_boards.values() for symbol in group]
        else:
            symbols = [item.strip().upper() for item in re.split(r"[,，\s]+", symbols_param) if item.strip()]
        try:
            self._send_json(adapter.scan_many_arbitrage(symbols))
        except Exception as exc:
            self._send_json(
                {
                    "ok": False,
                    "message": str(exc),
                    "symbols": symbols,
                    "poll_seconds": config.poll_seconds,
                    "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
                    "puts": [],
                    "calls": [],
                    "put_opportunities": [],
                    "call_opportunities": [],
                    "opportunities": [],
                }
            )

    def _public_config(self) -> dict[str, Any]:
        return public_config()

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="期权垂直价差套利监控")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8768")))
    parser.add_argument("--once", action="store_true", help="只计算一次并输出 JSON")
    parser.add_argument("--export-static", action="store_true", help="生成 GitHub Pages 静态页面到 dist/")
    args = parser.parse_args()

    if args.once:
        config = load_config()
        print(json.dumps(RqdataAdapter(config).scan_many_arbitrage(all_option_symbols(config)), ensure_ascii=False, indent=2))
        return
    if args.export_static:
        payload = export_static()
        print(
            json.dumps(
                {
                    "dist": str(STATIC_EXPORT_DIR),
                    "data": str(STATIC_DATA_PATH),
                    "symbols": len(payload.get("symbols") or []),
                    "put_opportunities": payload.get("put_candidate_count"),
                    "call_opportunities": payload.get("call_candidate_count"),
                    "errors": len(payload.get("errors") or []),
                    "ok": payload.get("ok"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    server = ThreadingHTTPServer((args.host, args.port), MonitorHandler)
    print(f"期权垂直价差套利监控已启动: http://{args.host}:{args.port}")
    print("按 Ctrl+C 停止服务")
    server.serve_forever()


if __name__ == "__main__":
    main()
