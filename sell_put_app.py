from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, urlparse


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "sell_put_monitor_config.local.json"
CONFIG_EXAMPLE_PATH = BASE_DIR / "sell_put_monitor_config.example.json"
SHARED_RQDATA_CONFIG_PATH = BASE_DIR / "rqdata_config.local.json"
STATIC_DIR = BASE_DIR / "sell_put_web"
DIST_DIR = BASE_DIR / "dist"
STATIC_EXPORT_DIR = DIST_DIR / "sell-put-monitor"
STATIC_CONFIG_PATH = STATIC_EXPORT_DIR / "config.json"

DEFAULT_OPTION_BOARDS = {
    "黑色": ["I", "RB", "HC", "J", "JM", "SF", "SM", "ZC"],
    "有色/贵金属": ["CU", "AL", "ZN", "PB", "NI", "SN", "AU", "AG", "AO", "LC", "SI"],
    "能源化工": ["RU", "BR", "FU", "BU", "SC", "LU", "NR", "TA", "MA", "PF", "PX", "SA", "UR", "FG", "L", "V", "PP", "EG", "EB", "PG", "SH"],
    "农产品": ["M", "Y", "P", "A", "B", "C", "CS", "JD", "LH", "SR", "CF", "OI", "RM", "AP", "CJ", "PK"],
    "金融": ["IF", "IH", "IC", "IM", "T", "TF", "TS", "TL"],
}
NON_FUTURE_OPTION_SYMBOLS = {"HO", "IO", "MO"}
STOCK_INDEX_FUTURES = {"IF", "IH", "IC", "IM"}
INDEX_FUTURE_OPTION_SYMBOL = {"IF": "IO", "IH": "HO", "IM": "MO"}
INDEX_CONTRACT_LIMIT = 4
PROGRESS_LOCK = threading.Lock()
PROGRESS_STATE: dict[str, Any] = {
    "running": False,
    "completed": 0,
    "total": 0,
    "percent": 0,
    "message": "等待加载",
    "updated_at": None,
}
DEFAULT_SYMBOL_NAMES = {
    "I": "铁矿石",
    "RB": "螺纹钢",
    "HC": "热卷",
    "J": "焦炭",
    "JM": "焦煤",
    "SF": "硅铁",
    "SM": "锰硅",
    "ZC": "动力煤",
    "CU": "铜",
    "AL": "铝",
    "ZN": "锌",
    "PB": "铅",
    "NI": "镍",
    "SN": "锡",
    "AU": "黄金",
    "AG": "白银",
    "AO": "氧化铝",
    "LC": "碳酸锂",
    "SI": "工业硅",
    "RU": "橡胶",
    "BR": "丁二烯橡胶",
    "FU": "燃油",
    "BU": "沥青",
    "SC": "原油",
    "LU": "低硫燃油",
    "NR": "20号胶",
    "TA": "PTA",
    "MA": "甲醇",
    "PF": "短纤",
    "PX": "对二甲苯",
    "SA": "纯碱",
    "UR": "尿素",
    "FG": "玻璃",
    "L": "塑料",
    "V": "PVC",
    "PP": "聚丙烯",
    "EG": "乙二醇",
    "EB": "苯乙烯",
    "PG": "液化气",
    "SH": "烧碱",
    "M": "豆粕",
    "Y": "豆油",
    "P": "棕榈油",
    "A": "豆一",
    "B": "豆二",
    "C": "玉米",
    "CS": "玉米淀粉",
    "JD": "鸡蛋",
    "LH": "生猪",
    "SR": "白糖",
    "CF": "棉花",
    "OI": "菜油",
    "RM": "菜粕",
    "AP": "苹果",
    "CJ": "红枣",
    "PK": "花生",
    "IF": "沪深300",
    "IH": "上证50",
    "IC": "中证500",
    "IM": "中证1000",
    "T": "10年国债",
    "TF": "5年国债",
    "TS": "2年国债",
    "TL": "30年国债",
}


def _today() -> dt.date:
    return dt.date.today()


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
            return dt.datetime.strptime(text[:10] if fmt != "%Y%m%d" else text[:8], fmt).date()
        except ValueError:
            continue
    return None


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


def _field(obj: Any, names: Iterable[str]) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        lower_map = {str(k).lower(): v for k, v in obj.items()}
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
    return str(value).strip() if value else None


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


def _normalize_option_side(option_side: str | None) -> str:
    return "call" if str(option_side or "").strip().lower() == "call" else "put"


def _option_side_label(option_side: str) -> str:
    return "Call" if option_side == "call" else "Put"


def _option_type_matches(option_type: str, parsed_type: str | None, option_side: str) -> bool:
    expected = "C" if option_side == "call" else "P"
    labels = {"C", "CALL", "看涨", "认购"} if option_side == "call" else {"P", "PUT", "看跌", "认沽"}
    return option_type in labels or parsed_type == expected


def _extreme_option_sort_key(item: dict[str, Any], option_side: str) -> tuple[float, dt.date]:
    strike = _as_float(item.get("strike")) or 0.0
    if option_side == "call":
        strike = -strike
    return strike, item.get("maturity_date") or dt.date.max


def _index_option_prefix(option_lookup: str | None, future_core: str) -> str | None:
    lookup = (option_lookup or "").upper()
    if lookup not in INDEX_FUTURE_OPTION_SYMBOL.values():
        return None
    match = re.search(r"([0-9]{3,4})", future_core)
    return f"{lookup}{match.group(1)}" if match else None


def _set_progress(completed: int, total: int, message: str, running: bool = True) -> None:
    percent = int((completed / total) * 100) if total else 0
    with PROGRESS_LOCK:
        PROGRESS_STATE.update(
            {
                "running": running,
                "completed": completed,
                "total": total,
                "percent": max(0, min(100, percent)),
                "message": message,
                "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
            }
        )


def _get_progress() -> dict[str, Any]:
    with PROGRESS_LOCK:
        return dict(PROGRESS_STATE)


def _rows_from_table(data: Any) -> list[Any]:
    if data is None:
        return []
    if hasattr(data, "to_dict"):
        try:
            return list(data.to_dict("records"))
        except Exception:
            pass
    if isinstance(data, dict):
        if all(isinstance(v, dict) for v in data.values()):
            return list(data.values())
        return [data]
    if isinstance(data, (list, tuple, set)):
        return list(data)
    return [data]


@dataclass
class MonitorConfig:
    enabled: bool
    contracts: list[str]
    poll_seconds: int
    annual_days: int
    default_margin_rate: float | None
    per_contract_margin_rate: dict[str, float]
    rqdata_username: str | None
    rqdata_password: str | None
    rqdata_api_key: str | None
    rqdata_uri: str | None
    rqdata_addr: tuple[str, int]
    market: str
    option_overrides: dict[str, str]
    future_types: list[str]
    lookback_trading_days: int
    min_avg_notional: float | None
    fallback_to_sina: bool
    option_boards: dict[str, list[str]]
    default_selection_mode: str


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

    contracts = raw.get("contracts") or []
    contracts = [str(item).strip().upper() for item in contracts if str(item).strip()]

    default_margin_rate = _as_float(raw.get("default_margin_rate"))
    per_margin_raw = raw.get("per_contract_margin_rate") or {}
    per_contract_margin_rate = {
        str(key).upper(): rate
        for key, value in per_margin_raw.items()
        if (rate := _as_float(value)) is not None and rate > 0
    }

    overrides_raw = raw.get("option_overrides") or {}
    option_overrides = {
        str(key).strip().upper(): str(value).strip().upper()
        for key, value in overrides_raw.items()
        if str(key).strip() and str(value).strip()
    }
    boards_raw = raw.get("option_boards") or DEFAULT_OPTION_BOARDS
    option_boards = {
        str(board): [str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()]
        for board, symbols in boards_raw.items()
    }
    selection_mode = str(raw.get("default_selection_mode") or "dominant").strip()
    if selection_mode not in {"dominant", "option_volume"}:
        selection_mode = "dominant"

    return MonitorConfig(
        enabled=bool(raw.get("enabled", True)),
        contracts=contracts,
        poll_seconds=max(2, int(raw.get("poll_seconds", 5))),
        annual_days=max(1, int(raw.get("annual_days", 365))),
        default_margin_rate=default_margin_rate if default_margin_rate and default_margin_rate > 0 else None,
        per_contract_margin_rate=per_contract_margin_rate,
        rqdata_username=str(username).strip() if username else None,
        rqdata_password=str(password).strip() if password else None,
        rqdata_api_key=str(api_key).strip() if api_key else None,
        rqdata_uri=str(uri).strip() if uri else None,
        rqdata_addr=addr,
        market=str(os.getenv("RQDATA_MARKET") or raw.get("market") or rqdata_config.get("market") or shared_rqdata.get("market") or "cn").strip(),
        option_overrides=option_overrides,
        future_types=[str(item) for item in raw.get("future_types", ["Future"])],
        lookback_trading_days=max(1, int(raw.get("lookback_trading_days", 20))),
        min_avg_notional=_as_float(raw.get("min_avg_notional")) or 1_000_000_000.0,
        fallback_to_sina=bool(raw.get("fallback_to_sina", False)),
        option_boards=option_boards,
        default_selection_mode=selection_mode,
    )


class RqdataAdapter:
    def __init__(self, config: MonitorConfig):
        self.config = config
        self._rqdatac: Any = None
        self._initialized = False
        self._option_rows_cache: list[Any] = []
        self._option_rows_loaded_at = 0.0
        self._option_history_rows_cache: list[Any] = []
        self._option_volume_contracts_cache: dict[str, tuple[str, str | None, float | None]] | None = None
        self._future_rows_cache: list[Any] = []
        self._future_rows_loaded_at = 0.0
        self._lock = threading.Lock()

    def _connect(self) -> None:
        with self._lock:
            if self._initialized:
                return
            try:
                import rqdatac  # type: ignore
            except ImportError as exc:
                raise RuntimeError("未安装 rqdatac。请先运行 pip install rqdatac，或使用米筐提供的安装方式。") from exc

            if self.config.rqdata_uri:
                rqdatac.init(uri=self.config.rqdata_uri, lazy=False)
            elif self.config.rqdata_username and self.config.rqdata_password:
                rqdatac.init(self.config.rqdata_username, self.config.rqdata_password, addr=self.config.rqdata_addr, lazy=False)
            elif self.config.rqdata_api_key:
                rqdatac.init("license", self.config.rqdata_api_key, addr=self.config.rqdata_addr, lazy=False)
            else:
                if os.getenv("RQDATA_USE_LOCAL_AUTH") != "1":
                    raise RuntimeError(
                        "未配置 rqdata 认证。请设置 RQDATA_API_KEY，或在 monitor_config.json 中填写 api_key。"
                    )
                # Some rqdatac installations can read local auth settings. Try it
                # only when explicitly enabled.
                try:
                    rqdatac.init()
                except Exception as exc:
                    raise RuntimeError(
                        "未配置 rqdata 认证。请设置 RQDATA_API_KEY，或在 monitor_config.json 中填写 api_key。"
                    ) from exc
            self._rqdatac = rqdatac
            self._initialized = True

    def _instrument(self, order_book_id: str) -> Any:
        self._connect()
        try:
            return self._rqdatac.instruments(order_book_id)
        except Exception:
            return None

    def _current_snapshots(self, order_book_ids: list[str]) -> dict[str, Any]:
        self._connect()
        ids = [item for item in order_book_ids if item]
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
        today = _today()
        calls = [
            {"type": "Option", "date": today},
            {"type": "option", "date": today},
            {"type": "FutureOption", "date": today},
            {"type": "Option"},
        ]
        for kwargs in calls:
            try:
                result = self._rqdatac.all_instruments(**kwargs)
            except Exception:
                continue
            for row in _rows_from_table(result):
                order_book_id = _row_id(row)
                if not order_book_id:
                    continue
                key = order_book_id.upper()
                if key in seen:
                    continue
                seen.add(key)
                rows.append(row)
            if rows:
                break

        self._option_rows_cache = rows
        self._option_rows_loaded_at = now
        return rows

    def _option_history_universe(self) -> list[Any]:
        self._connect()
        if self._option_history_rows_cache:
            return self._option_history_rows_cache
        try:
            result = self._rqdatac.all_instruments(type="Option")
        except Exception:
            result = self._rqdatac.all_instruments(type="Option", date=_today())
        self._option_history_rows_cache = _rows_from_table(result)
        return self._option_history_rows_cache

    def _future_universe(self) -> list[Any]:
        self._connect()
        now = time.time()
        if self._future_rows_cache and now - self._future_rows_loaded_at < 600:
            return self._future_rows_cache
        rows: list[Any] = []
        today = _today()
        for kwargs in ({"type": "Future", "date": today}, {"type": "Future"}):
            try:
                rows = _rows_from_table(self._rqdatac.all_instruments(**kwargs))
            except Exception:
                continue
            if rows:
                break
        self._future_rows_cache = rows
        self._future_rows_loaded_at = now
        return rows

    def _active_future_contracts(self, underlying_symbol: str, limit: int = INDEX_CONTRACT_LIMIT) -> list[str]:
        self._connect()
        underlying_symbol = underlying_symbol.upper()
        today = _today()
        contracts: list[str] = []
        try:
            result = self._rqdatac.futures.get_contracts(underlying_symbol, date=today, market=self.config.market)
            contracts = [str(_row_id(row) or row).strip().upper() for row in _rows_from_table(result) if str(_row_id(row) or row).strip()]
        except Exception:
            contracts = []

        if not contracts:
            for row in self._future_universe():
                order_book_id = _row_id(row)
                if not order_book_id:
                    continue
                contract = order_book_id.upper()
                if not re.match(rf"^{re.escape(underlying_symbol)}[0-9]{{3,4}}", contract):
                    continue
                listed_date = _parse_date(_field(row, ("listed_date", "list_date", "start_date")))
                delisted_date = _parse_date(_field(row, ("de_listed_date", "delisted_date", "end_date", "maturity_date", "expire_date")))
                if listed_date and today < listed_date:
                    continue
                if delisted_date and today > delisted_date:
                    continue
                contracts.append(contract)

        seen: set[str] = set()
        unique = [contract for contract in contracts if contract and not (contract in seen or seen.add(contract))]
        unique.sort(key=lambda item: (re.search(r"([0-9]{3,4})", item).group(1) if re.search(r"([0-9]{3,4})", item) else item, item))
        if unique:
            return unique[:limit]
        return [self._dominant_contract(underlying_symbol)]

    def option_symbols_by_board(self) -> list[dict[str, Any]]:
        rows = self._option_universe()
        symbols: dict[str, str] = {}
        for row in rows:
            symbol = str(_field(row, ("underlying_symbol",)) or "").strip().upper()
            if not symbol or "." in symbol or not re.match(r"^[A-Z]+$", symbol):
                continue
            if symbol in NON_FUTURE_OPTION_SYMBOLS:
                continue
            product_name = DEFAULT_SYMBOL_NAMES.get(symbol, symbol)
            symbols.setdefault(symbol, product_name or symbol)

        assigned: set[str] = set()
        groups: list[dict[str, Any]] = []
        for board, board_symbols in self.config.option_boards.items():
            items = [
                {"symbol": symbol, "name": symbols.get(symbol, symbol)}
                for symbol in board_symbols
                if symbol in symbols or symbol in STOCK_INDEX_FUTURES
            ]
            if items:
                assigned.update(item["symbol"] for item in items)
                groups.append({"board": board, "items": items})

        other_items = [
            {"symbol": symbol, "name": name}
            for symbol, name in sorted(symbols.items())
            if symbol not in assigned
        ]
        if other_items:
            groups.append({"board": "其他", "items": other_items})
        return groups

    def quote_symbol(self, underlying_symbol: str, selection_mode: str = "dominant", option_side: str = "put") -> dict[str, Any]:
        option_side = _normalize_option_side(option_side)
        underlying_symbol = underlying_symbol.strip().upper()
        if selection_mode == "option_volume":
            lookup_symbol = INDEX_FUTURE_OPTION_SYMBOL.get(underlying_symbol, underlying_symbol)
            future_contract, selected_option, selected_volume = self._contract_from_option_volume(lookup_symbol)
            selected_source = "期权成交量最大"
        else:
            future_contract = self._dominant_contract(underlying_symbol)
            selected_option = None
            selected_volume = None
            mapped_option_symbol = INDEX_FUTURE_OPTION_SYMBOL.get(underlying_symbol)
            selected_source = f"期货主力 + {mapped_option_symbol}期权" if mapped_option_symbol else "期货主力"

        try:
            option_symbol = INDEX_FUTURE_OPTION_SYMBOL.get(underlying_symbol)
            row = self.quote_contract(future_contract, option_underlying_symbol=option_symbol, option_side=option_side)
        except RuntimeError:
            if underlying_symbol in STOCK_INDEX_FUTURES:
                row = self.quote_future_only(future_contract, option_side=option_side)
            elif selection_mode == "dominant":
                fallback_contract, fallback_option, fallback_volume = self._contract_from_option_volume(underlying_symbol)
                row = self.quote_contract(fallback_contract, option_side=option_side)
                selected_option = fallback_option
                selected_volume = fallback_volume
                selected_source = "主力无期权，改用期权成交量最大"
            else:
                raise
        row.update(
            {
                "underlying_symbol": underlying_symbol,
                "selection_mode": selection_mode,
                "selected_source": selected_source,
                "selected_option_contract": selected_option,
                "selected_option_volume": selected_volume,
            }
        )
        return row

    def quote_symbol_rows(self, underlying_symbol: str, selection_mode: str = "dominant", option_side: str = "put") -> list[dict[str, Any]]:
        underlying_symbol = underlying_symbol.strip().upper()
        option_side = _normalize_option_side(option_side)
        if underlying_symbol not in STOCK_INDEX_FUTURES:
            return [self.quote_symbol(underlying_symbol, selection_mode, option_side=option_side)]

        option_symbol = INDEX_FUTURE_OPTION_SYMBOL.get(underlying_symbol)
        rows: list[dict[str, Any]] = []
        for future_contract in self._active_future_contracts(underlying_symbol):
            try:
                row = self.quote_contract(future_contract, option_underlying_symbol=option_symbol, option_side=option_side)
            except RuntimeError:
                row = self.quote_future_only(future_contract, option_side=option_side)
            row.update(
                {
                    "underlying_symbol": underlying_symbol,
                    "selection_mode": "all_index_contracts",
                    "selected_source": f"股指全部合约 + {option_symbol}期权" if option_symbol else "股指全部合约",
                    "selected_option_contract": None,
                    "selected_option_volume": None,
                }
            )
            rows.append(row)
        return rows

    def quote_future_only(self, future_contract: str, option_side: str = "put") -> dict[str, Any]:
        option_side = _normalize_option_side(option_side)
        future_contract = future_contract.strip().upper()
        future_instrument = self._instrument(future_contract)
        snapshots = self._current_snapshots([future_contract])
        future_snapshot = snapshots.get(future_contract.upper())
        margin_rate = self._margin_rate(future_contract, future_instrument)
        leverage = 1 / margin_rate if margin_rate and margin_rate > 0 else None
        return {
            "future_contract": future_contract,
            "future_last": self._last_price(future_snapshot),
            "future_symbol": str(_field(future_instrument, ("symbol", "abbrev_symbol", "name")) or ""),
            "future_turnover": self._turnover(future_snapshot),
            "option_side": option_side,
            "option_side_label": _option_side_label(option_side),
            "option_contract": None,
            "option_last": None,
            "option_bid1": None,
            "option_bid1_volume": None,
            "put_contract": None,
            "put_last": None,
            "put_bid1": None,
            "put_bid1_volume": None,
            "call_contract": None,
            "call_last": None,
            "call_bid1": None,
            "call_bid1_volume": None,
            "yield_price": None,
            "yield_price_type": "买一价",
            "option_symbol": "",
            "option_source": "股指期货",
            "put_symbol": "",
            "put_source": "股指期货",
            "call_symbol": "",
            "call_source": "股指期货",
            "strike": None,
            "strike_gap_ratio": None,
            "maturity_date": None,
            "days_to_expiry": None,
            "contract_multiplier": _as_float(_field(future_instrument, ("contract_multiplier", "multiplier"))) or 1.0,
            "option_multiplier": None,
            "option_contracts_per_future": None,
            "premium_amount": None,
            "notional_amount": None,
            "margin_rate": margin_rate,
            "margin_amount": None,
            "future_leverage": leverage,
            "cash_yield": None,
            "annualized_cash_yield": None,
            "margin_yield": None,
            "annualized_margin_yield": None,
            "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        }

    def quote_boards(
        self,
        selection_mode: str = "dominant",
        option_side: str = "put",
        progress_cb: Callable[[str, str, str], None] | None = None,
    ) -> dict[str, Any]:
        option_side = _normalize_option_side(option_side)
        self._option_universe()
        groups = []
        errors = []
        for group in self.option_symbols_by_board():
            rows_by_symbol: dict[str, list[dict[str, Any]]] = {}
            for item in group["items"]:
                try:
                    item_rows = self.quote_symbol_rows(item["symbol"], selection_mode, option_side=option_side)
                    visible_rows = []
                    for row in item_rows:
                        row["underlying_name"] = item["name"]
                        if self._passes_turnover_filter(row):
                            visible_rows.append(row)
                        if progress_cb:
                            progress_cb(option_side, group["board"], row.get("future_contract") or item["symbol"])
                    if visible_rows:
                        rows_by_symbol[item["symbol"]] = visible_rows
                except Exception as exc:
                    errors.append(
                        {
                            "board": group["board"],
                            "underlying_symbol": item["symbol"],
                            "underlying_name": item["name"],
                            "message": str(exc),
                        }
                    )
                    if progress_cb:
                        progress_cb(option_side, group["board"], item["symbol"])
            rows = [
                row
                for item in group["items"]
                if item["symbol"] in rows_by_symbol
                for row in rows_by_symbol[item["symbol"]]
            ]
            if rows:
                groups.append({"board": group["board"], "rows": rows})
        return {"groups": groups, "errors": errors}

    def _passes_turnover_filter(self, row: dict[str, Any]) -> bool:
        threshold = self.config.min_avg_notional
        if not threshold or threshold <= 0:
            return True
        turnover = _as_float(row.get("future_turnover"))
        return turnover is not None and turnover >= threshold

    def quote_boards_sequential(self, selection_mode: str = "dominant", option_side: str = "put") -> dict[str, Any]:
        option_side = _normalize_option_side(option_side)
        groups = []
        errors = []
        for group in self.option_symbols_by_board():
            rows = []
            for item in group["items"]:
                try:
                    row = self.quote_symbol(item["symbol"], selection_mode, option_side=option_side)
                    row["underlying_name"] = item["name"]
                    rows.append(row)
                except Exception as exc:
                    errors.append(
                        {
                            "board": group["board"],
                            "underlying_symbol": item["symbol"],
                            "underlying_name": item["name"],
                            "message": str(exc),
                        }
                    )
            if rows:
                groups.append({"board": group["board"], "rows": rows})
        return {"groups": groups, "errors": errors}

    def _dominant_contract(self, underlying_symbol: str) -> str:
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
        rows = _rows_from_table(dominant)
        for row in rows:
            if isinstance(row, dict):
                value = next((v for v in row.values() if v), None)
            else:
                value = row
            if value:
                return str(value).strip().upper()
        raise RuntimeError(f"未找到 {underlying_symbol} 的主力合约")

    def _contract_from_option_volume(self, underlying_symbol: str) -> tuple[str, str | None, float | None]:
        self._connect()
        volume_map = self._option_volume_contract_map()
        if underlying_symbol in volume_map:
            return volume_map[underlying_symbol]
        raise RuntimeError(f"未找到 {underlying_symbol} 的期权成交量")

    def _option_volume_contract_map(self) -> dict[str, tuple[str, str | None, float | None]]:
        if self._option_volume_contracts_cache is not None:
            return self._option_volume_contracts_cache
        today = _today()
        option_rows: list[tuple[str, str, str | None]] = []
        for row in self._option_universe():
            symbol = str(_field(row, ("underlying_symbol",)) or "").strip().upper()
            if not symbol or "." in symbol or symbol in NON_FUTURE_OPTION_SYMBOLS:
                continue
            listed_date = _parse_date(_field(row, ("listed_date", "list_date", "start_date")))
            delisted_date = _parse_date(_field(row, ("de_listed_date", "delisted_date", "end_date", "maturity_date", "expire_date")))
            if listed_date and today < listed_date:
                continue
            if delisted_date and today > delisted_date:
                continue
            order_book_id = _row_id(row)
            if order_book_id:
                underlying = str(_field(row, ("underlying_order_book_id",)) or "").strip().upper()
                option_rows.append((symbol, order_book_id.upper(), underlying or None))
        snapshots: dict[str, Any] = {}
        option_ids = [item[1] for item in option_rows]
        for index in range(0, len(option_ids), 500):
            snapshots.update(self._current_snapshots(option_ids[index:index + 500]))

        best_by_symbol: dict[str, tuple[str, str | None, float | None]] = {}
        best_volume_by_symbol: dict[str, float] = {}
        for symbol, option_id, underlying in option_rows:
            snapshot = snapshots.get(option_id)
            volume = _as_float(_field(snapshot, ("volume",))) or 0.0
            if volume <= best_volume_by_symbol.get(symbol, -1.0):
                continue
            parsed_underlying = underlying
            if not parsed_underlying:
                parsed_underlying, _, _ = _parse_option_code(option_id)
            if not parsed_underlying:
                continue
            best_volume_by_symbol[symbol] = volume
            best_by_symbol[symbol] = (parsed_underlying, option_id, volume)
            for future_symbol, option_symbol in INDEX_FUTURE_OPTION_SYMBOL.items():
                if symbol == option_symbol:
                    best_by_symbol[future_symbol] = (self._dominant_contract(future_symbol), option_id, volume)

        self._option_volume_contracts_cache = best_by_symbol
        return best_by_symbol

    def _find_extreme_option(
        self,
        future_contract: str,
        option_underlying_symbol: str | None = None,
        option_side: str = "put",
    ) -> dict[str, Any]:
        option_side = _normalize_option_side(option_side)
        option_type = "C" if option_side == "call" else "P"
        option_name = "看涨" if option_side == "call" else "看跌"

        override = self.config.option_overrides.get(future_contract.upper()) if option_side == "put" else None
        if override:
            instrument = self._instrument(override)
            strike = _as_float(_field(instrument, ("strike_price", "exercise_price", "strike")))
            if strike is None:
                _, _, strike = _parse_option_code(override, _future_core(future_contract))
            return {"order_book_id": override, "instrument": instrument, "strike": strike, "source": "配置指定"}

        future_core = _future_core(future_contract)
        option_lookup = option_underlying_symbol or future_core
        index_option_prefix = _index_option_prefix(option_lookup, future_core)
        today = _today()
        candidates: list[dict[str, Any]] = []

        universe_match = self._extreme_option_from_universe(future_core, today, option_lookup, option_side)
        if universe_match:
            return universe_match

        try:
            option_ids = self._rqdatac.options.get_contracts(
                option_lookup,
                option_type=option_type,
                trading_date=today,
            )
        except Exception:
            option_ids = []

        for option_id in option_ids or []:
            if index_option_prefix and index_option_prefix not in str(option_id).upper():
                continue
            instrument = self._instrument(option_id)
            strike = _as_float(_field(instrument, ("strike_price", "exercise_price", "strike")))
            if strike is None:
                _, _, strike = _parse_option_code(option_id, future_core)
            if strike is None:
                continue
            maturity_date = _parse_date(
                _field(instrument, ("maturity_date", "expire_date", "expiration_date", "last_trade_date", "de_listed_date"))
            )
            candidates.append(
                {
                    "order_book_id": str(option_id).upper(),
                    "instrument": instrument,
                    "strike": strike,
                    "maturity_date": maturity_date,
                    "source": "options.get_contracts",
                }
            )

        if candidates:
            return sorted(candidates, key=lambda item: _extreme_option_sort_key(item, option_side))[0]

        for row in self._option_universe():
            option = self._option_candidate_from_row(row, future_core, today, option_side)
            if option:
                candidates.append(option)

        if not candidates:
            action = "最高执行价" if option_side == "call" else "最低执行价"
            hint = "可在 monitor_config.json 的 option_overrides 中手工指定。" if option_side == "put" else ""
            raise RuntimeError(f"未找到 {future_contract} 对应的{action}{option_name}期权。{hint}".strip())
        return sorted(candidates, key=lambda item: _extreme_option_sort_key(item, option_side))[0]

    def _extreme_option_from_universe(
        self,
        future_core: str,
        today: dt.date,
        option_lookup: str | None = None,
        option_side: str = "put",
    ) -> dict[str, Any] | None:
        candidates: list[dict[str, Any]] = []
        lookup = (option_lookup or future_core).upper()
        for row in self._option_universe():
            order_book_id = _row_id(row)
            if not order_book_id:
                continue
            parsed_underlying, parsed_type, parsed_strike = _parse_option_code(order_book_id, future_core)
            option_type = str(_field(row, ("option_type", "type", "contract_type", "call_or_put")) or "").upper()
            if not _option_type_matches(option_type, parsed_type, option_side):
                continue

            option_symbol = str(_field(row, ("underlying_symbol",)) or "").upper()
            underlying = str(_field(row, ("underlying_order_book_id", "underlying_symbol")) or "").upper()
            if lookup in INDEX_FUTURE_OPTION_SYMBOL.values():
                if option_symbol != lookup:
                    continue
                index_option_prefix = _index_option_prefix(lookup, future_core)
                if index_option_prefix and index_option_prefix not in order_book_id.upper():
                    continue
            else:
                underlying_core = _future_core(underlying) if underlying else parsed_underlying
                if underlying_core != future_core:
                    continue

            listed_date = _parse_date(_field(row, ("listed_date", "list_date", "start_date")))
            delisted_date = _parse_date(_field(row, ("de_listed_date", "delisted_date", "end_date", "maturity_date", "expire_date")))
            if listed_date and today < listed_date:
                continue
            if delisted_date and today > delisted_date:
                continue

            strike = _as_float(_field(row, ("strike_price", "exercise_price", "strike"))) or parsed_strike
            if strike is None:
                continue
            maturity_date = _parse_date(
                _field(row, ("maturity_date", "expire_date", "expiration_date", "last_trade_date", "de_listed_date"))
            )
            candidates.append(
                {
                    "order_book_id": order_book_id.upper(),
                    "instrument": row,
                    "strike": strike,
                    "maturity_date": maturity_date,
                    "source": "合约全集筛选",
                }
            )
        if not candidates:
            return None
        return sorted(candidates, key=lambda item: _extreme_option_sort_key(item, option_side))[0]

    def _option_candidate_from_row(
        self,
        row: Any,
        future_core: str,
        today: dt.date,
        option_side: str,
    ) -> dict[str, Any] | None:
        order_book_id = _row_id(row)
        if not order_book_id:
            return None

        parsed_underlying, parsed_type, parsed_strike = _parse_option_code(order_book_id, future_core)
        option_type = str(_field(row, ("option_type", "type", "contract_type", "call_or_put")) or "").upper()
        if not _option_type_matches(option_type, parsed_type, option_side):
            return None

        underlying = str(
            _field(
                row,
                (
                    "underlying_order_book_id",
                    "underlying_symbol",
                    "underlying",
                    "underlying_contract",
                    "underlying_code",
                ),
            )
            or ""
        ).upper()
        underlying_core = _future_core(underlying) if underlying else parsed_underlying
        if underlying_core != future_core and future_core not in order_book_id.upper():
            return None

        listed_date = _parse_date(_field(row, ("listed_date", "list_date", "start_date")))
        delisted_date = _parse_date(_field(row, ("de_listed_date", "delisted_date", "end_date", "maturity_date", "expire_date")))
        if listed_date and today < listed_date:
            return None
        if delisted_date and today > delisted_date:
            return None

        strike = _as_float(_field(row, ("strike_price", "exercise_price", "strike"))) or parsed_strike
        if strike is None:
            return None
        maturity_date = _parse_date(
            _field(row, ("maturity_date", "expire_date", "expiration_date", "last_trade_date", "de_listed_date"))
        )
        return {
            "order_book_id": order_book_id.upper(),
            "instrument": row,
            "strike": strike,
            "maturity_date": maturity_date,
            "source": "自动筛选",
        }

    def quote_contract(
        self,
        future_contract: str,
        option_underlying_symbol: str | None = None,
        option_side: str = "put",
    ) -> dict[str, Any]:
        option_side = _normalize_option_side(option_side)
        future_contract = future_contract.strip().upper()
        future_instrument = self._instrument(future_contract)
        selected_option = self._find_extreme_option(
            future_contract,
            option_underlying_symbol=option_underlying_symbol,
            option_side=option_side,
        )
        option_contract = selected_option["order_book_id"]

        snapshots = self._current_snapshots([future_contract, option_contract])
        future_snapshot = snapshots.get(future_contract.upper())
        option_snapshot = snapshots.get(option_contract.upper())

        future_last = self._last_price(future_snapshot)
        future_turnover = self._turnover(future_snapshot)
        option_last = self._last_price(option_snapshot)
        option_bid1 = self._bid_price(option_snapshot)
        option_bid1_volume = self._bid_volume(option_snapshot)
        quote_price = option_bid1
        strike = _as_float(selected_option.get("strike"))
        future_multiplier = _as_float(_field(future_instrument, ("contract_multiplier", "multiplier"))) or 1.0
        option_multiplier = _as_float(_field(selected_option.get("instrument"), ("contract_multiplier", "multiplier"))) or future_multiplier
        option_contracts_per_future = future_multiplier / option_multiplier if option_multiplier else 1.0
        multiplier = future_multiplier
        margin_rate = self._margin_rate(future_contract, future_instrument)
        maturity_date = (
            selected_option.get("maturity_date")
            or _parse_date(_field(selected_option.get("instrument"), ("maturity_date", "expire_date", "expiration_date", "last_trade_date", "de_listed_date")))
        )
        days_to_expiry = max(0, (maturity_date - _today()).days) if maturity_date else None

        cash_yield = None
        margin_yield = None
        annualized_cash_yield = None
        annualized_margin_yield = None
        premium_amount = None
        notional_amount = None
        margin_amount = None
        strike_gap_ratio = None
        if future_last and strike:
            if option_side == "call":
                strike_gap_ratio = (strike - future_last) / future_last
            else:
                strike_gap_ratio = (future_last - strike) / future_last
        if quote_price is not None and strike and strike > 0:
            premium_amount = quote_price * option_multiplier * option_contracts_per_future
            notional_amount = strike * multiplier
            cash_yield = premium_amount / notional_amount
            if days_to_expiry and days_to_expiry > 0:
                annualized_cash_yield = cash_yield * self.config.annual_days / days_to_expiry
            if margin_rate and margin_rate > 0:
                margin_amount = notional_amount * margin_rate
                margin_yield = premium_amount / margin_amount
                if days_to_expiry and days_to_expiry > 0:
                    annualized_margin_yield = margin_yield * self.config.annual_days / days_to_expiry

        leverage = 1 / margin_rate if margin_rate and margin_rate > 0 else None
        option_symbol = str(_field(selected_option.get("instrument"), ("symbol", "abbrev_symbol", "name")) or "")
        option_source = selected_option.get("source")

        row = {
            "future_contract": future_contract,
            "future_last": future_last,
            "future_symbol": str(_field(future_instrument, ("symbol", "abbrev_symbol", "name")) or ""),
            "future_turnover": future_turnover,
            "option_side": option_side,
            "option_side_label": _option_side_label(option_side),
            "option_contract": option_contract,
            "option_last": option_last,
            "option_bid1": option_bid1,
            "option_bid1_volume": option_bid1_volume,
            "yield_price": quote_price,
            "yield_price_type": "买一价",
            "option_symbol": option_symbol,
            "option_source": option_source,
            "strike": strike,
            "strike_gap_ratio": strike_gap_ratio,
            "maturity_date": maturity_date.isoformat() if maturity_date else None,
            "days_to_expiry": days_to_expiry,
            "contract_multiplier": multiplier,
            "option_multiplier": option_multiplier,
            "option_contracts_per_future": option_contracts_per_future,
            "premium_amount": premium_amount,
            "notional_amount": notional_amount,
            "margin_rate": margin_rate,
            "margin_amount": margin_amount,
            "future_leverage": leverage,
            "cash_yield": cash_yield,
            "annualized_cash_yield": annualized_cash_yield,
            "margin_yield": margin_yield,
            "annualized_margin_yield": annualized_margin_yield,
            "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        }
        if option_side == "call":
            row.update(
                {
                    "call_contract": option_contract,
                    "call_last": option_last,
                    "call_bid1": option_bid1,
                    "call_bid1_volume": option_bid1_volume,
                    "call_symbol": option_symbol,
                    "call_source": option_source,
                    "put_contract": None,
                    "put_last": None,
                    "put_bid1": None,
                    "put_bid1_volume": None,
                    "put_symbol": "",
                    "put_source": None,
                }
            )
        else:
            row.update(
                {
                    "put_contract": option_contract,
                    "put_last": option_last,
                    "put_bid1": option_bid1,
                    "put_bid1_volume": option_bid1_volume,
                    "put_symbol": option_symbol,
                    "put_source": option_source,
                    "call_contract": None,
                    "call_last": None,
                    "call_bid1": None,
                    "call_bid1_volume": None,
                    "call_symbol": "",
                    "call_source": None,
                }
            )
        return row

    def _margin_rate(self, future_contract: str, instrument: Any) -> float | None:
        try:
            margin_df = self._rqdatac.futures.get_commission_margin(
                [future_contract],
                fields=["short_margin_ratio"],
            )
            rows = _rows_from_table(margin_df)
            for row in rows:
                margin = _as_float(_field(row, ("short_margin_ratio",)))
                if margin and margin > 0:
                    return margin
        except Exception:
            pass
        configured = self.config.per_contract_margin_rate.get(future_contract.upper())
        if configured:
            return configured
        from_instrument = _as_float(
            _field(
                instrument,
                (
                    "margin_rate",
                    "long_margin_rate",
                    "short_margin_rate",
                    "maintenance_margin_rate",
                    "margin_ratio",
                ),
            )
        )
        if from_instrument and from_instrument > 0:
            return from_instrument
        return self.config.default_margin_rate

    @staticmethod
    def _last_price(snapshot: Any) -> float | None:
        return _as_float(
            _field(
                snapshot,
                (
                    "last",
                    "last_price",
                    "latest",
                    "latest_price",
                    "close",
                    "prev_close",
                    "last_trade_price",
                ),
            )
        )

    @staticmethod
    def _bid_price(snapshot: Any) -> float | None:
        return _first_number(_field(snapshot, ("bid", "bid_price", "b1", "bid1")))

    @staticmethod
    def _bid_volume(snapshot: Any) -> float | None:
        return _first_number(_field(snapshot, ("bid_vol", "bid_volume", "b1_v", "bid1_volume")))

    @staticmethod
    def _turnover(snapshot: Any) -> float | None:
        return _as_float(_field(snapshot, ("total_turnover", "turnover", "amount", "money")))

    def backtest_hourly_short_puts(
        self,
        start_date: dt.date,
        end_date: dt.date,
        threshold: float = 1.0,
        symbols: list[str] | None = None,
    ) -> dict[str, Any]:
        self._connect()
        symbols_filter = {item.upper() for item in symbols or [] if item}
        option_symbols_filter = {
            INDEX_FUTURE_OPTION_SYMBOL.get(symbol, symbol)
            for symbol in symbols_filter
        }
        candidates = self._backtest_candidates(start_date, end_date, option_symbols_filter)
        trades = []
        for candidate in candidates:
            trade = self._backtest_candidate(candidate, start_date, end_date, threshold)
            if trade:
                trades.append(trade)
        trades.sort(key=lambda item: (item["exit_date"], item["entry_time"], item["option_contract"]))

        cumulative_return = 0.0
        cumulative_profit = 0.0
        curve = []
        for trade in trades:
            cumulative_return += trade["return_on_margin"] or 0.0
            cumulative_profit += trade["profit_amount"] or 0.0
            curve.append(
                {
                    "date": trade["exit_date"],
                    "cumulative_return": cumulative_return,
                    "cumulative_profit": cumulative_profit,
                }
            )
        wins = [trade for trade in trades if (trade["profit_amount"] or 0) >= 0]
        return {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "threshold": threshold,
            "trade_count": len(trades),
            "win_count": len(wins),
            "win_rate": len(wins) / len(trades) if trades else None,
            "total_return_units": cumulative_return,
            "total_profit_amount": cumulative_profit,
            "trades": trades,
            "curve": curve,
        }

    def _backtest_candidates(self, start_date: dt.date, end_date: dt.date, symbols_filter: set[str]) -> list[Any]:
        grouped: dict[tuple[str, str], Any] = {}
        for row in self._option_history_universe():
            option_type = str(_field(row, ("option_type",)) or "").upper()
            if option_type != "P":
                continue
            symbol = str(_field(row, ("underlying_symbol",)) or "").upper()
            if not symbol or "." in symbol:
                continue
            if symbols_filter and symbol not in symbols_filter:
                continue
            if symbol in NON_FUTURE_OPTION_SYMBOLS or symbol in DEFAULT_SYMBOL_NAMES:
                pass
            else:
                continue
            maturity_date = _parse_date(_field(row, ("maturity_date", "de_listed_date")))
            listed_date = _parse_date(_field(row, ("listed_date",)))
            if not maturity_date or maturity_date < start_date or maturity_date > end_date:
                continue
            if listed_date and listed_date > end_date:
                continue
            strike = _as_float(_field(row, ("strike_price", "exercise_price", "strike")))
            if strike is None:
                continue
            underlying = str(_field(row, ("underlying_order_book_id", "underlying_symbol")) or symbol).upper()
            key = (underlying if symbol not in INDEX_FUTURE_OPTION_SYMBOL.values() else symbol, maturity_date.isoformat())
            old = grouped.get(key)
            if old is None or strike < (_as_float(_field(old, ("strike_price", "exercise_price", "strike"))) or float("inf")):
                grouped[key] = row
        return list(grouped.values())

    def _backtest_candidate(self, row: Any, start_date: dt.date, end_date: dt.date, threshold: float) -> dict[str, Any] | None:
        option_id = _row_id(row)
        if not option_id:
            return None
        option_id = option_id.upper()
        symbol = str(_field(row, ("underlying_symbol",)) or "").upper()
        maturity_date = _parse_date(_field(row, ("maturity_date", "de_listed_date")))
        listed_date = _parse_date(_field(row, ("listed_date",))) or start_date
        strike = _as_float(_field(row, ("strike_price", "exercise_price", "strike")))
        option_multiplier = _as_float(_field(row, ("contract_multiplier", "multiplier"))) or 1.0
        if not maturity_date or not strike:
            return None

        future_contract, underlying_for_settlement = self._backtest_underlying_contract(row, option_id, symbol)
        future_instrument = self._instrument(future_contract)
        future_multiplier = _as_float(_field(future_instrument, ("contract_multiplier", "multiplier"))) or option_multiplier
        option_contracts_per_future = future_multiplier / option_multiplier if option_multiplier else 1.0
        margin_rate = self._margin_rate(future_contract, future_instrument) or self.config.default_margin_rate
        if not margin_rate:
            return None

        price_start = max(start_date, listed_date)
        price_end = min(end_date, maturity_date)
        option_prices = self._hourly_close_rows(option_id, price_start, price_end)
        signal = None
        for item in option_prices:
            option_price = _as_float(item.get("close"))
            timestamp = item.get("datetime")
            if option_price is None or option_price <= 0 or not isinstance(timestamp, dt.datetime):
                continue
            days_to_expiry = (maturity_date - timestamp.date()).days
            if days_to_expiry <= 0:
                continue
            margin_yield = option_price / (strike * margin_rate)
            annualized_margin_yield = margin_yield * self.config.annual_days / days_to_expiry
            if annualized_margin_yield > threshold:
                signal = {
                    "entry_time": timestamp,
                    "entry_price": option_price,
                    "days_to_expiry": days_to_expiry,
                    "annualized_margin_yield": annualized_margin_yield,
                }
                break
        if not signal:
            return None

        expiry_price = self._daily_close(underlying_for_settlement, maturity_date)
        if expiry_price is None:
            return None
        premium_amount = signal["entry_price"] * option_multiplier * option_contracts_per_future
        exercise_loss = max(strike - expiry_price, 0) * option_multiplier * option_contracts_per_future
        profit_amount = premium_amount - exercise_loss
        margin_amount = strike * future_multiplier * margin_rate
        return_on_margin = profit_amount / margin_amount if margin_amount else None
        holding_days = max((maturity_date - signal["entry_time"].date()).days, 1)
        return {
            "symbol": self._backtest_display_symbol(symbol),
            "option_symbol": symbol,
            "future_contract": future_contract,
            "settlement_underlying": underlying_for_settlement,
            "option_contract": option_id,
            "entry_time": signal["entry_time"].isoformat(sep=" ", timespec="minutes"),
            "entry_price": signal["entry_price"],
            "strike": strike,
            "expiry_price": expiry_price,
            "exit_date": maturity_date.isoformat(),
            "days_to_expiry": signal["days_to_expiry"],
            "option_multiplier": option_multiplier,
            "future_multiplier": future_multiplier,
            "option_contracts_per_future": option_contracts_per_future,
            "margin_rate": margin_rate,
            "margin_amount": margin_amount,
            "premium_amount": premium_amount,
            "exercise_loss": exercise_loss,
            "profit_amount": profit_amount,
            "return_on_margin": return_on_margin,
            "annualized_margin_yield_at_entry": signal["annualized_margin_yield"],
            "annualized_realized_return": return_on_margin * self.config.annual_days / holding_days if return_on_margin is not None else None,
        }

    def _backtest_underlying_contract(self, row: Any, option_id: str, symbol: str) -> tuple[str, str]:
        underlying = str(_field(row, ("underlying_order_book_id",)) or "").upper()
        index_to_future = {"IO": "IF", "HO": "IH", "MO": "IM"}
        if symbol in index_to_future:
            match = re.search(r"([A-Z]+)([0-9]{4})[CP]", option_id)
            future_contract = f"{index_to_future[symbol]}{match.group(2)}" if match else self._dominant_contract(index_to_future[symbol])
            return future_contract, underlying
        return underlying, underlying

    @staticmethod
    def _backtest_display_symbol(option_symbol: str) -> str:
        return {"IO": "IF", "HO": "IH", "MO": "IM"}.get(option_symbol, option_symbol)

    def _hourly_close_rows(self, order_book_id: str, start_date: dt.date, end_date: dt.date) -> list[dict[str, Any]]:
        try:
            data = self._rqdatac.get_price(
                order_book_id,
                start_date=start_date,
                end_date=end_date,
                frequency="60m",
                fields=["close"],
                expect_df=True,
                market=self.config.market,
            )
        except Exception:
            return []
        rows = []
        if hasattr(data, "reset_index"):
            for record in data.reset_index().to_dict("records"):
                timestamp = record.get("datetime")
                if hasattr(timestamp, "to_pydatetime"):
                    timestamp = timestamp.to_pydatetime()
                rows.append({"datetime": timestamp, "close": record.get("close")})
        return rows

    def _daily_close(self, order_book_id: str, date: dt.date) -> float | None:
        try:
            data = self._rqdatac.get_price(
                order_book_id,
                start_date=date,
                end_date=date,
                frequency="1d",
                fields=["close"],
                expect_df=True,
                market=self.config.market,
            )
        except Exception:
            return None
        rows = _rows_from_table(data)
        for row in reversed(rows):
            value = _as_float(_field(row, ("close",)))
            if value is not None:
                return value
        return None


class MonitorHandler(SimpleHTTPRequestHandler):
    server_version = "OptionMonitor/1.1"

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        print("%s - %s" % (self.log_date_time_string(), format % args))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/board-quotes":
            self._handle_board_quotes(parsed.query)
            return
        if parsed.path == "/api/progress":
            self._send_json({"ok": True, **_get_progress()})
            return
        if parsed.path == "/api/quotes":
            self._handle_quotes(parsed.query)
            return
        if parsed.path == "/api/config":
            self._send_json(self._public_config())
            return
        if parsed.path in {"/", ""}:
            self.path = "/index.html"
        for header in ("If-Modified-Since", "If-None-Match"):
            if header in self.headers:
                del self.headers[header]
        super().do_GET()

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def _handle_quotes(self, query: str) -> None:
        config = load_config()
        adapter = RqdataAdapter(config)
        params = parse_qs(query)
        option_side = _normalize_option_side(params.get("side", ["put"])[0])
        contracts_param = params.get("contracts", [""])[0]
        contracts = [
            item.strip().upper()
            for item in re.split(r"[,，\s]+", contracts_param)
            if item.strip()
        ] or config.contracts

        rows = []
        errors = []
        for contract in contracts:
            try:
                rows.append(adapter.quote_contract(contract, option_side=option_side))
            except Exception as exc:
                errors.append({"future_contract": contract, "message": str(exc)})

        self._send_json(
            {
                "ok": not errors,
                "rows": rows,
                "errors": errors,
                "option_side": option_side,
                "poll_seconds": config.poll_seconds,
                "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
            }
        )

    def _handle_board_quotes(self, query: str) -> None:
        config = load_config()
        adapter = RqdataAdapter(config)
        params = parse_qs(query)
        mode = params.get("mode", [config.default_selection_mode])[0]
        side_param = str(params.get("side", ["put"])[0]).strip().lower()
        option_side = "all" if side_param == "all" else _normalize_option_side(side_param)
        if mode not in {"dominant", "option_volume"}:
            mode = config.default_selection_mode
        try:
            if option_side == "all":
                groups_for_progress = adapter.option_symbols_by_board()
                total = 0
                for group in groups_for_progress:
                    for item in group["items"]:
                        if item["symbol"] in STOCK_INDEX_FUTURES:
                            total += len(adapter._active_future_contracts(item["symbol"]))
                        else:
                            total += 1
                total *= 2
                completed = 0
                _set_progress(0, total, "开始加载行情", running=True)

                def progress_cb(side: str, board: str, symbol: str) -> None:
                    nonlocal completed
                    completed += 1
                    label = "Call" if side == "call" else "Put"
                    _set_progress(completed, total, f"{label} / {board} / {symbol}", running=True)

                sections = {}
                all_errors = []
                for side in ("put", "call"):
                    section = adapter.quote_boards(mode, option_side=side, progress_cb=progress_cb)
                    sections[side] = section
                    all_errors.extend({**error, "option_side": side} for error in section["errors"])
                _set_progress(total, total, "行情加载完成", running=False)
                self._send_json(
                    {
                        "ok": not all_errors,
                        "sections": sections,
                        "errors": all_errors,
                        "selection_mode": mode,
                        "option_side": option_side,
                        "poll_seconds": config.poll_seconds,
                        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
                    }
                )
                return

            payload = adapter.quote_boards(mode, option_side=option_side)
            self._send_json(
                {
                    "ok": not payload["errors"],
                    "groups": payload["groups"],
                    "errors": payload["errors"],
                    "selection_mode": mode,
                    "option_side": option_side,
                    "poll_seconds": config.poll_seconds,
                    "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
                }
            )
        except Exception as exc:
            _set_progress(0, 0, f"行情加载失败：{exc}", running=False)
            self._send_json(
                {
                    "ok": False,
                    "groups": [],
                    "errors": [{"message": str(exc)}],
                    "selection_mode": mode,
                    "option_side": option_side,
                    "poll_seconds": config.poll_seconds,
                    "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
                }
            )

    def _public_config(self) -> dict[str, Any]:
        config = load_config()
        return {
            "contracts": config.contracts,
            "poll_seconds": config.poll_seconds,
            "annual_days": config.annual_days,
            "default_margin_rate": config.default_margin_rate,
            "default_selection_mode": config.default_selection_mode,
            "option_boards": config.option_boards,
            "has_credentials": bool(config.rqdata_api_key or (config.rqdata_username and config.rqdata_password)),
        }

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def public_config(config: MonitorConfig | None = None) -> dict[str, Any]:
    config = config or load_config()
    return {
        "contracts": config.contracts,
        "poll_seconds": config.poll_seconds,
        "annual_days": config.annual_days,
        "default_margin_rate": config.default_margin_rate,
        "default_selection_mode": config.default_selection_mode,
        "option_boards": config.option_boards,
        "has_credentials": bool(config.rqdata_api_key or (config.rqdata_username and config.rqdata_password)),
        "mode": "static",
    }


def build_board_quotes(adapter: RqdataAdapter, config: MonitorConfig, mode: str) -> dict[str, Any]:
    if mode not in {"dominant", "option_volume"}:
        mode = config.default_selection_mode
    sections: dict[str, Any] = {}
    all_errors: list[dict[str, Any]] = []
    for side in ("put", "call"):
        section = adapter.quote_boards(mode, option_side=side)
        sections[side] = section
        all_errors.extend({**error, "option_side": side} for error in section.get("errors", []))
    return {
        "ok": not all_errors,
        "sections": sections,
        "errors": all_errors,
        "selection_mode": mode,
        "option_side": "all",
        "poll_seconds": config.poll_seconds,
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }


def export_static() -> dict[str, Any]:
    config = load_config()
    adapter = RqdataAdapter(config)
    STATIC_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    for filename in ("index.html", "main.js", "styles.css"):
        shutil.copy2(STATIC_DIR / filename, STATIC_EXPORT_DIR / filename)
    STATIC_CONFIG_PATH.write_text(json.dumps(public_config(config), ensure_ascii=False, indent=2), encoding="utf-8")

    payloads: dict[str, Any] = {}
    for mode in ("dominant", "option_volume"):
        payload = build_board_quotes(adapter, config, mode)
        payload["mode"] = "static"
        payload["generated_for"] = "github-pages"
        (STATIC_EXPORT_DIR / f"board-quotes-{mode}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        payloads[mode] = payload
    return payloads


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="期货最低执行价 sell put 监控")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8765")))
    parser.add_argument("--export-static", action="store_true", help="生成 GitHub Pages 静态页面到 dist/")
    parser.add_argument("--once", choices=["dominant", "option_volume"], help="只计算一次并输出 JSON")
    args = parser.parse_args()

    if args.once:
        config = load_config()
        print(json.dumps(build_board_quotes(RqdataAdapter(config), config, args.once), ensure_ascii=False, indent=2))
        return
    if args.export_static:
        payloads = export_static()
        summary = {}
        for mode, payload in payloads.items():
            summary[mode] = {
                "ok": payload.get("ok"),
                "put_rows": sum(len(group.get("rows", [])) for group in payload.get("sections", {}).get("put", {}).get("groups", [])),
                "call_rows": sum(len(group.get("rows", [])) for group in payload.get("sections", {}).get("call", {}).get("groups", [])),
                "errors": len(payload.get("errors") or []),
            }
        print(json.dumps({"dist": str(STATIC_EXPORT_DIR), "summary": summary}, ensure_ascii=False, indent=2))
        return

    server = ThreadingHTTPServer((args.host, args.port), MonitorHandler)
    print(f"期货 Put/Call 板块监控已启动: http://{args.host}:{args.port}")
    print("按 Ctrl+C 停止服务")
    server.serve_forever()


if __name__ == "__main__":
    main()
