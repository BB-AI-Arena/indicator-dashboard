from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import mean, median
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from sqlalchemy import delete, desc, text
from sqlalchemy.orm import Session

from .auth import etrade_auth
from .config import config_manager
from .db import SessionLocal
from .history import get_candles_from_sql, now_iso as history_now_iso
from .money_flow import build_money_flow
from .news_catalyst import build_news_catalyst_impact
from .indicators import apply_indicators
from .models import (
    TradeReviewAccount,
    TradeReviewAnalysisCache,
    TradeReviewAuditLog,
    TradeReviewFill,
    TradeReviewSelection,
    TradeReviewSyncRun,
    TradeReviewTrade,
)
from .providers.base import ProviderError
from .providers.option_filters import parse_expiration_date, spread_pct
from .providers.rate_limiter import call_with_rate_limit, record_provider_error


ET_TZ = ZoneInfo("America/New_York")
ANALYSIS_VERSION = 1
DEFAULT_LOOKBACK_DAYS = 730
DEFAULT_INCREMENTAL_OVERLAP_DAYS = 7
DEFAULT_REVIEW_MODEL = "gpt-5.6"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
BAD_QUOTE_TYPES = {"CLOSING", "DELAYED", "SANDBOX"}
ALLOWED_SELECTION_MODES = {"EXPLICIT", "ALL"}
ALLOWED_TRADE_STATUSES = {"COMPLETE", "UNRESOLVED", "PENDING"}
ALLOWED_FILL_STATUSES = {"MATCHED", "UNRESOLVED", "IGNORED"}
ALLOWED_FILL_ACTIONS = {
    "buy to open",
    "sell to open",
    "buy to close",
    "sell to close",
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def normalize_symbol(symbol: str | None) -> str:
    return str(symbol or "").strip().upper()


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None:
            return default
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return default
        return number
    except Exception:
        return default


def _safe_int(value: Any, default: int | None = None) -> int | None:
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def _json_dumps(value: Any) -> str:
    try:
        return json.dumps(value, default=str, sort_keys=True)
    except Exception:
        return "{}"


def _json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _to_et(value: str | datetime | None) -> str | None:
    parsed = _parse_iso(value.isoformat() if isinstance(value, datetime) else value)
    if not parsed:
        return None
    return parsed.astimezone(ET_TZ).isoformat()


def _mask_account_number(raw: str | None) -> str:
    text = _safe_text(raw)
    if len(text) <= 4:
        return text
    return text[-4:]


def _account_ref(account_id_key: str | None) -> str:
    return f"acct_{_sha1(_safe_text(account_id_key))[:16]}"


def _first_value(source: dict[str, Any] | None, *keys: str) -> Any:
    if not isinstance(source, dict):
        return None
    for key in keys:
        for candidate in (key, key.lower(), key.capitalize()):
            if candidate in source and source.get(candidate) not in (None, ""):
                return source.get(candidate)
    return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _audit(db: Session, username: str, action: str, resource_type: str | None = None, resource_id: str | None = None, detail: str | None = None) -> None:
    db.add(
        TradeReviewAuditLog(
            username=username,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            detail=detail,
            created_at=now_iso(),
        )
    )
    db.commit()


def ensure_trade_review_schema(db: Session) -> None:
    try:
        table_exists = db.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='trade_review_trades'")
        ).first()
        if not table_exists:
            return
        columns = {
            str(row["name"] or "")
            for row in db.execute(text("PRAGMA table_info(trade_review_trades)")).mappings().all()
        }
    except Exception:
        return
    if "dte_at_entry" not in columns:
        db.execute(text("ALTER TABLE trade_review_trades ADD COLUMN dte_at_entry INTEGER"))
        db.commit()


def _openai_model() -> str:
    ai_cfg = config_manager.get("ai", default={}) or {}
    advisory_cfg = config_manager.get("advisory", default={}) or {}
    return (
        os.getenv("OPENAI_MODEL_TRADE_REVIEW")
        or os.getenv("OPENAI_MODEL_REVIEW")
        or os.getenv("OPENAI_ADVISORY_MODEL")
        or str(ai_cfg.get("review_model") or "").strip()
        or str(advisory_cfg.get("model") or "").strip()
        or DEFAULT_REVIEW_MODEL
    )


def _openai_timeout() -> int:
    ai_cfg = config_manager.get("ai", default={}) or {}
    return int(os.getenv("OPENAI_TRADE_REVIEW_TIMEOUT_SECONDS", ai_cfg.get("review_timeout_seconds", ai_cfg.get("timeout_seconds", 20)) or 20))


def _etrade_request_json(endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    if not etrade_auth.enabled():
        raise ProviderError("E*TRADE is disabled in config", provider="etrade")
    if not etrade_auth.configured():
        raise ProviderError("E*TRADE credentials are missing", provider="etrade")
    if not etrade_auth.is_connected():
        raise ProviderError("E*TRADE authorization expired. Reconnect in Settings.", provider="etrade")

    session = etrade_auth.signed_session()
    url = f"{etrade_auth.base_url()}{endpoint}"
    timeout = int(config_manager.get("etrade", "request_timeout_seconds", default=8) or 8)
    response = session.get(
        url,
        params=params or {},
        headers={"Accept": "application/json"},
        timeout=timeout,
    )

    if response.status_code in {401, 403}:
        raise ProviderError("E*TRADE authorization expired. Reconnect in Settings.", provider="etrade")
    if response.status_code == 429:
        raise ProviderError("E*TRADE rate limited", rate_limited=True, provider="etrade")
    if response.status_code >= 400:
        raise ProviderError(f"E*TRADE API error {response.status_code}", provider="etrade")

    try:
        return response.json()
    except Exception as exc:
        preview = (response.text or "")[:200].replace("\n", " ")
        raise ProviderError(f"E*TRADE returned malformed JSON: {exc}. body={preview}", provider="etrade") from exc


def _call_etrade(endpoint: str, params: dict[str, Any] | None = None, symbol: str | None = None) -> dict[str, Any]:
    return call_with_rate_limit("etrade", symbol, endpoint, _etrade_request_json, endpoint, params)


def _extract_account_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    root = payload.get("AccountListResponse") or payload.get("accountListResponse") or payload
    candidates = [
        root.get("Accounts") if isinstance(root, dict) else None,
        root.get("accounts") if isinstance(root, dict) else None,
        root.get("Account") if isinstance(root, dict) else None,
        root.get("account") if isinstance(root, dict) else None,
    ]
    for candidate in candidates:
        if isinstance(candidate, dict):
            for key in ("Account", "account", "accounts", "Accounts"):
                inner = candidate.get(key)
                if inner is not None:
                    return [item for item in _as_list(inner) if isinstance(item, dict)]
            return [candidate]
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    return []


def _normalize_account(raw: dict[str, Any]) -> dict[str, Any]:
    account_id_key = _safe_text(_first_value(raw, "accountIdKey", "account_id_key"))
    account_number = _safe_text(
        _first_value(
            raw,
            "accountId",
            "account_id",
            "accountNumber",
            "account_number",
            "accountNo",
            "account_no",
        )
    )
    account_desc = _safe_text(_first_value(raw, "accountDesc", "accountDescription", "accountName", "displayName", "nickname"))
    account_type = _safe_text(_first_value(raw, "accountType", "account_type", "type"))
    account_mode = _safe_text(_first_value(raw, "accountMode", "account_mode", "mode"))
    institution_type = _safe_text(_first_value(raw, "institutionType", "institution_type"))
    if not account_number:
        account_number = account_id_key
    return {
        "account_ref": _account_ref(account_id_key),
        "account_id_key": account_id_key or None,
        "account_mask": _mask_account_number(account_number or account_id_key),
        "account_desc": account_desc or None,
        "account_name": account_desc or None,
        "account_type": account_type or None,
        "account_mode": account_mode or None,
        "institution_type": institution_type or None,
    }


def _extract_collection(payload: dict[str, Any], paths: list[tuple[str, ...]]) -> list[dict[str, Any]]:
    for path in paths:
        node: Any = payload
        ok = True
        for key in path:
            if not isinstance(node, dict) or key not in node:
                ok = False
                break
            node = node[key]
        if not ok:
            continue
        if isinstance(node, dict):
            for key in ("Transaction", "Transactions", "Order", "Orders", "Item", "Items", "Account", "Accounts"):
                nested = node.get(key)
                if isinstance(nested, dict):
                    return [item for item in _as_list(nested.get("item") or nested.get("Item") or nested.get("transaction") or nested.get("Transaction") or nested.get("order") or nested.get("Order") or nested)]
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]
            return [node]
        if isinstance(node, list):
            return [item for item in node if isinstance(item, dict)]
    return []


def _extract_marker(payload: dict[str, Any]) -> str | None:
    for key in ("marker", "Marker", "nextMarker", "next_marker", "pageMarker", "page_marker"):
        value = payload.get(key)
        if value not in (None, ""):
            return _safe_text(value)
    for outer in ("TransactionList", "transactionList", "OrderList", "orderList", "Transactions", "transactions", "Orders", "orders"):
        node = payload.get(outer)
        if isinstance(node, dict):
            for key in ("marker", "Marker", "nextMarker", "next_marker"):
                value = node.get(key)
                if value not in (None, ""):
                    return _safe_text(value)
    return None


def _paginate_records(
    endpoint: str,
    *,
    params: dict[str, Any],
    symbol: str | None,
    collection_paths: list[tuple[str, ...]],
    max_pages: int = 100,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    all_records: list[dict[str, Any]] = []
    raw_pages: list[dict[str, Any]] = []
    marker: str | None = None
    page = 0
    while page < max_pages:
        page_params = dict(params)
        if marker:
            page_params["marker"] = marker
        payload = _call_etrade(endpoint, params=page_params, symbol=symbol)
        raw_pages.append(payload)
        records = _extract_collection(payload, collection_paths)
        all_records.extend(records)
        next_marker = _extract_marker(payload)
        page += 1
        if not next_marker or next_marker == marker:
            break
        marker = next_marker
    return all_records, raw_pages


OPTION_SYMBOL_RE = re.compile(r"^(?P<root>[A-Z0-9.\-]+?)(?P<date>\d{6})(?P<cp>[CP])(?P<strike>\d{8})$")


def parse_option_symbol(symbol: str | None) -> dict[str, Any]:
    text = _safe_text(symbol).replace(" ", "").upper()
    result = {
        "underlying_symbol": None,
        "occ_symbol": text or None,
        "option_symbol": _safe_text(symbol) or None,
        "call_put": None,
        "strike": None,
        "expiration": None,
    }
    if not text:
        return result
    match = OPTION_SYMBOL_RE.match(text)
    if not match:
        return result
    root = _safe_text(match.group("root")).replace(".", ".")
    try:
        expiration = datetime.strptime(match.group("date"), "%y%m%d").date().isoformat()
    except Exception:
        expiration = None
    try:
        strike = int(match.group("strike")) / 1000.0
    except Exception:
        strike = None
    result.update(
        {
            "underlying_symbol": root or None,
            "call_put": "CALL" if match.group("cp") == "C" else "PUT",
            "strike": strike,
            "expiration": expiration,
        }
    )
    return result


def _infer_action(record: dict[str, Any], *, close_event: bool = False) -> str | None:
    text = " ".join(
        _safe_text(value).upper()
        for value in (
            _first_value(record, "orderAction", "order_action", "buySell", "buy_sell", "transactionType", "transaction_type", "description", "transactionDescription"),
        )
        if _safe_text(value)
    )
    if "BUY TO OPEN" in text or "BTO" in text:
        return "buy to open"
    if "SELL TO OPEN" in text or "STO" in text:
        return "sell to open"
    if "BUY TO CLOSE" in text or "BTC" in text:
        return "buy to close"
    if "SELL TO CLOSE" in text or "STC" in text:
        return "sell to close"
    if "EXERCISE" in text or "ASSIGN" in text or "EXPIR" in text or close_event:
        if "BUY" in text and "SELL" not in text:
            return "buy to close"
        if "SELL" in text and "BUY" not in text:
            return "sell to close"
        return "buy to close" if close_event else "sell to close"
    if "BUY" in text and "SELL" not in text:
        return "buy to open" if not close_event else "buy to close"
    if "SELL" in text and "BUY" not in text:
        return "sell to open" if not close_event else "sell to close"
    return None


def _extract_fill_timestamp(record: dict[str, Any]) -> datetime | None:
    for key in (
        "executionTimestamp",
        "execution_timestamp",
        "transactionDate",
        "transaction_date",
        "orderDate",
        "order_date",
        "placedTime",
        "placed_time",
        "createdTime",
        "created_time",
        "filledTime",
        "filled_time",
        "time",
        "timestamp",
    ):
        parsed = _parse_iso(_safe_text(_first_value(record, key)))
        if parsed:
            return parsed
    return None


def _extract_fill_price(record: dict[str, Any], *fallback_keys: str) -> float | None:
    for key in (
        "fillPrice",
        "fill_price",
        "executionPrice",
        "execution_price",
        "averageExecutionPrice",
        "average_execution_price",
        "averagePrice",
        "average_price",
        "price",
        "netPrice",
        "net_price",
        "lastPrice",
        "last_price",
    ) + fallback_keys:
        value = _safe_float(_first_value(record, key))
        if value is not None:
            return value
    return None


def _extract_quantity(record: dict[str, Any]) -> int | None:
    for key in ("quantity", "qty", "filledQuantity", "filled_quantity", "executedQuantity", "executed_quantity", "positionQuantity", "position_quantity"):
        value = _safe_int(_first_value(record, key))
        if value is not None:
            return abs(value)
    return None


def _extract_commission_and_fees(record: dict[str, Any]) -> tuple[float | None, float | None]:
    commission = _safe_float(_first_value(record, "commission", "commissionAmount", "commission_amount", "brokerCommission", "broker_commission"))
    fees = _safe_float(_first_value(record, "fees", "fee", "otherFees", "other_fees", "regFee", "reg_fee"))
    return commission, fees


def _extract_symbol_fields(record: dict[str, Any]) -> dict[str, Any]:
    symbol_text = _safe_text(
        _first_value(
            record,
            "optionSymbol",
            "option_symbol",
            "osiKey",
            "osi_key",
            "symbolDescription",
            "displaySymbol",
            "display_symbol",
            "symbol",
        )
    )
    parsed = parse_option_symbol(symbol_text)
    if not parsed.get("underlying_symbol"):
        parsed["underlying_symbol"] = _safe_text(_first_value(record, "underlyingSymbol", "underlying_symbol", "rootSymbol", "root_symbol")).upper() or None
    if not parsed.get("option_symbol"):
        parsed["option_symbol"] = symbol_text or None
    if not parsed.get("occ_symbol"):
        parsed["occ_symbol"] = symbol_text.replace(" ", "").upper() if symbol_text else None
    return parsed


def _extract_quote_fields(record: dict[str, Any]) -> tuple[float | None, float | None, float | None, float | None, str | None]:
    bid = _safe_float(_first_value(record, "bid", "bidPrice", "bestBid", "best_bid"))
    ask = _safe_float(_first_value(record, "ask", "askPrice", "bestAsk", "best_ask"))
    midpoint = None
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        midpoint = round((bid + ask) / 2.0, 4)
    spread = None
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        spread = round(spread_pct(bid, ask), 4)
    quote_type = _safe_text(_first_value(record, "quoteType", "quote_type", "quoteStatus", "quote_status")).upper() or None
    return bid, ask, midpoint, spread, quote_type


def _normalize_fill(
    *,
    account: dict[str, Any],
    source_type: str,
    record: dict[str, Any],
    record_id: str | None = None,
    parent_order_id: str | None = None,
    execution_id: str | None = None,
    close_event: bool = False,
    raw_payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    symbol_fields = _extract_symbol_fields(record)
    timestamp_utc = _extract_fill_timestamp(record)
    if timestamp_utc is None:
        return None

    quantity = _extract_quantity(record)
    if quantity is None or quantity <= 0:
        return None

    action = _infer_action(record, close_event=close_event)
    if action not in ALLOWED_FILL_ACTIONS:
        action = "buy to close" if close_event else "buy to open"

    fill_price = _extract_fill_price(record)
    commission, fees = _extract_commission_and_fees(record)
    if commission is None:
        commission = 0.0
    if fees is None:
        fees = 0.0

    net_cash_effect = _safe_float(_first_value(record, "netCashEffect", "net_cash_effect", "cashEffect", "cash_effect", "amount", "transactionAmount", "transaction_amount"))
    if net_cash_effect is None and fill_price is not None:
        sign = 1 if action.startswith("sell") else -1
        net_cash_effect = round(sign * fill_price * quantity * 100.0 - commission - fees, 4)

    bid, ask, midpoint, spread, quote_type = _extract_quote_fields(record)
    quote_source = _safe_text(_first_value(record, "quoteSource", "quote_source", "source", "provider")) or None
    data_status = "observed"
    confidence = "HIGH" if execution_id or timestamp_utc else "MEDIUM"
    source_record_id = _safe_text(record_id) or _safe_text(_first_value(record, "transactionId", "transaction_id", "orderId", "order_id", "id", "orderNumber", "order_number")) or None
    source_hash_source = {
        "account_ref": account["account_ref"],
        "source_type": source_type,
        "source_record_id": source_record_id,
        "execution_id": execution_id,
        "timestamp": timestamp_utc.isoformat(),
        "action": action,
        "quantity": quantity,
        "fill_price": fill_price,
        "occ_symbol": symbol_fields.get("occ_symbol"),
        "parent_order_id": parent_order_id,
    }
    source_hash = _sha1(_json_dumps(source_hash_source))
    return {
        "account_ref": account["account_ref"],
        "account_id_key": account["account_id_key"],
        "account_mask": account["account_mask"],
        "source_type": source_type,
        "source_record_id": source_record_id,
        "order_id": _safe_text(parent_order_id or _first_value(record, "orderId", "order_id", "orderNumber", "order_number")) or None,
        "execution_id": _safe_text(execution_id or _first_value(record, "executionId", "execution_id", "fillId", "fill_id")) or None,
        "parent_order_id": _safe_text(parent_order_id or _first_value(record, "parentOrderId", "parent_order_id")) or None,
        "execution_timestamp_utc": timestamp_utc.isoformat(),
        "execution_timestamp_et": timestamp_utc.astimezone(ET_TZ).isoformat(),
        "underlying_symbol": symbol_fields.get("underlying_symbol"),
        "occ_symbol": symbol_fields.get("occ_symbol"),
        "option_symbol": symbol_fields.get("option_symbol"),
        "call_put": symbol_fields.get("call_put"),
        "strike": symbol_fields.get("strike"),
        "expiration": symbol_fields.get("expiration"),
        "dte_at_entry": None,
        "action": action,
        "quantity": quantity,
        "fill_price": fill_price,
        "commission": commission,
        "fees": fees,
        "net_cash_effect": net_cash_effect,
        "bid": bid,
        "ask": ask,
        "midpoint": midpoint,
        "spread_pct": spread,
        "underlying_price": _safe_float(_first_value(record, "underlyingPrice", "underlying_price", "underlyingLastPrice", "underlying_last_price")),
        "quote_source": quote_source,
        "data_status": data_status,
        "confidence_level": confidence,
        "match_status": "UNRESOLVED",
        "raw_payload_json": _json_dumps(raw_payload or record),
        "source_hash": source_hash,
    }


def _extract_leg_records(record: dict[str, Any]) -> list[dict[str, Any]]:
    for key in (
        "executions",
        "Executions",
        "execution",
        "Execution",
        "legs",
        "Legs",
        "orderLegs",
        "OrderLegs",
        "legsDetail",
        "LegsDetail",
        "instruments",
        "Instruments",
        "instrument",
        "Instrument",
    ):
        value = _first_value(record, key)
        if isinstance(value, dict):
            for nested_key in ("Execution", "execution", "Leg", "leg", "Instrument", "instrument", "Item", "item"):
                nested = value.get(nested_key)
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]
                if isinstance(nested, dict):
                    return [nested]
            return [value]
        if isinstance(value, list) and value:
            return [item for item in value if isinstance(item, dict)]
    return [record]


def _record_to_fills(account: dict[str, Any], source_type: str, record: dict[str, Any]) -> list[dict[str, Any]]:
    order_id = _safe_text(_first_value(record, "orderId", "order_id", "orderNumber", "order_number")) or None
    parent_order_id = _safe_text(_first_value(record, "parentOrderId", "parent_order_id")) or order_id
    record_id = _safe_text(_first_value(record, "transactionId", "transaction_id", "id", "orderNumber", "order_number")) or None
    close_event = any(
        marker in " ".join(
            _safe_text(_first_value(record, key)).upper()
            for key in ("transactionType", "transaction_type", "orderAction", "order_action", "description", "transactionDescription")
        )
        for marker in ("CLOSE", "ASSIGN", "EXERCISE", "EXPIR", "CANCEL")
    )
    statuses = " ".join(
        _safe_text(_first_value(record, key)).upper()
        for key in ("orderStatus", "order_status", "status", "transactionStatus", "transaction_status")
    )
    if "CANCEL" in statuses and not close_event:
        return []

    fills: list[dict[str, Any]] = []
    legs = _extract_leg_records(record)
    if len(legs) == 1 and legs[0] is record:
        fill = _normalize_fill(
            account=account,
            source_type=source_type,
            record=record,
            record_id=record_id,
            parent_order_id=parent_order_id,
            execution_id=_safe_text(_first_value(record, "executionId", "execution_id")) or None,
            close_event=close_event,
            raw_payload=record,
        )
        if fill:
            fills.append(fill)
        return fills

    for index, leg in enumerate(legs):
        merged = dict(record)
        merged.update(leg)
        fill = _normalize_fill(
            account=account,
            source_type=source_type,
            record=merged,
            record_id=f"{record_id or order_id or source_type}:{index}",
            parent_order_id=parent_order_id,
            execution_id=_safe_text(_first_value(leg, "executionId", "execution_id", "fillId", "fill_id")) or _safe_text(_first_value(record, "executionId", "execution_id")) or None,
            close_event=close_event,
            raw_payload={"record": record, "leg": leg},
        )
        if fill:
            fills.append(fill)
    return fills


def _upsert_fill(db: Session, fill: dict[str, Any]) -> bool:
    existing = db.query(TradeReviewFill).filter(TradeReviewFill.source_hash == fill["source_hash"]).first()
    if existing:
        return False
    db.add(TradeReviewFill(**fill, created_at=now_iso(), updated_at=now_iso()))
    return True


def refresh_accounts(db: Session, username: str) -> list[dict[str, Any]]:
    payload = _call_etrade("/v1/accounts/list.json", symbol="accounts")
    accounts = [_normalize_account(item) for item in _extract_account_items(payload)]
    active_refs = set()
    for account in accounts:
        active_refs.add(account["account_ref"])
        existing = db.query(TradeReviewAccount).filter(TradeReviewAccount.account_ref == account["account_ref"]).first()
        if existing:
            existing.account_id_key = account["account_id_key"]
            existing.account_mask = account["account_mask"]
            existing.account_desc = account["account_desc"]
            existing.account_name = account["account_name"]
            existing.account_type = account["account_type"]
            existing.account_mode = account["account_mode"]
            existing.institution_type = account["institution_type"]
            existing.updated_at = now_iso()
        else:
            db.add(
                TradeReviewAccount(
                    account_ref=account["account_ref"],
                    account_id_key=account["account_id_key"],
                    account_mask=account["account_mask"],
                    account_desc=account["account_desc"],
                    account_name=account["account_name"],
                    account_type=account["account_type"],
                    account_mode=account["account_mode"],
                    institution_type=account["institution_type"],
                    imported_at=now_iso(),
                    updated_at=now_iso(),
                )
            )

    # Mark stale accounts but keep them for historical records.
    db.commit()
    _audit(db, username, "refresh_accounts", "account_selection", None, f"Refreshed {len(accounts)} E*TRADE accounts")
    return accounts


def _preferred_account(db: Session) -> TradeReviewAccount | None:
    suffix = str(config_manager.get("trade_review", "preferred_account_mask_suffix", default="") or "").strip()
    if not suffix:
        return None
    rows = db.query(TradeReviewAccount).all()
    normalized_suffix = "".join(character for character in suffix if character.isdigit()) or suffix
    for account in rows:
        mask = _safe_text(account.account_mask)
        digits = "".join(character for character in mask if character.isdigit())
        if digits.endswith(normalized_suffix) or mask.endswith(suffix):
            return account
    return None


def reviewable_accounts(db: Session) -> list[TradeReviewAccount]:
    preferred = _preferred_account(db)
    if preferred and bool(config_manager.get("trade_review", "restrict_to_preferred_account", default=False)):
        return [preferred]
    return db.query(TradeReviewAccount).order_by(TradeReviewAccount.account_mask.asc()).all()


def get_selection(db: Session, username: str) -> dict[str, Any]:
    row = db.query(TradeReviewSelection).filter(TradeReviewSelection.username == username).first()
    if not row:
        preferred = _preferred_account(db)
        if preferred:
            return {"selection_mode": "EXPLICIT", "selected_account_refs": [preferred.account_ref], "selection_source": "configured_preferred_account"}
        return {"selection_mode": "EXPLICIT", "selected_account_refs": []}
    selection = {
        "selection_mode": row.selection_mode,
        "selected_account_refs": _json_loads(row.selected_account_refs, []),
    }
    preferred = _preferred_account(db)
    if preferred and bool(config_manager.get("trade_review", "restrict_to_preferred_account", default=False)):
        selection = {"selection_mode": "EXPLICIT", "selected_account_refs": [preferred.account_ref], "selection_source": "configured_preferred_account"}
    return selection


def set_selection(db: Session, username: str, selection_mode: str, account_refs: list[str]) -> dict[str, Any]:
    mode = str(selection_mode or "EXPLICIT").strip().upper()
    if mode not in ALLOWED_SELECTION_MODES:
        mode = "EXPLICIT"
    refs = [str(ref).strip() for ref in (account_refs or []) if str(ref).strip()]
    preferred = _preferred_account(db)
    if preferred and bool(config_manager.get("trade_review", "restrict_to_preferred_account", default=False)):
        mode = "EXPLICIT"
        refs = [preferred.account_ref]
    if mode == "EXPLICIT" and not refs:
        raise ValueError("Select at least one account before importing data.")
    row = db.query(TradeReviewSelection).filter(TradeReviewSelection.username == username).first()
    if row:
        row.selection_mode = mode
        row.selected_account_refs = _json_dumps(refs)
        row.updated_at = now_iso()
    else:
        db.add(
            TradeReviewSelection(
                username=username,
                selection_mode=mode,
                selected_account_refs=_json_dumps(refs),
                updated_at=now_iso(),
                created_at=now_iso(),
            )
        )
    db.commit()
    _audit(db, username, "update_selection", "account_selection", None, f"mode={mode} refs={len(refs)}")
    return get_selection(db, username)


def _resolve_accounts_for_user(db: Session, username: str) -> tuple[list[TradeReviewAccount], dict[str, Any]]:
    selection = get_selection(db, username)
    mode = str(selection.get("selection_mode") or "EXPLICIT").upper()
    selected_refs = list(selection.get("selected_account_refs") or [])
    accounts = reviewable_accounts(db)
    if mode == "ALL":
        chosen = accounts
    else:
        chosen = [account for account in accounts if account.account_ref in selected_refs]
    return chosen, selection


def _default_range_for_account(account: TradeReviewAccount, payload: dict[str, Any]) -> tuple[str, str]:
    to_value = str(payload.get("to_date") or now_utc().date().isoformat())
    fixed_lookback = int(config_manager.get("trade_review", "fixed_lookback_days", default=0) or 0)
    if fixed_lookback > 0:
        return (now_utc().date() - timedelta(days=fixed_lookback)).isoformat(), to_value
    from_value = str(payload.get("from_date") or "").strip()
    if from_value:
        return from_value, to_value
    lookback = int(config_manager.get("trade_review", "default_lookback_days", default=DEFAULT_LOOKBACK_DAYS) or DEFAULT_LOOKBACK_DAYS)
    overlap = int(config_manager.get("trade_review", "incremental_overlap_days", default=DEFAULT_INCREMENTAL_OVERLAP_DAYS) or DEFAULT_INCREMENTAL_OVERLAP_DAYS)
    if account.last_successful_sync_at:
        parsed = _parse_iso(account.last_successful_sync_at)
        if parsed:
            start = (parsed - timedelta(days=max(1, overlap))).date().isoformat()
            return start, to_value
    return (now_utc().date() - timedelta(days=max(1, lookback))).isoformat(), to_value


def _trade_group_key(account_ref: str, fill: TradeReviewFill, sequence: int) -> str:
    contract = fill.occ_symbol or "|".join(
        _safe_text(value)
        for value in (
            fill.underlying_symbol,
            fill.call_put,
            fill.strike,
            fill.expiration,
        )
    )
    return f"{account_ref}:{contract}:{sequence}"


def _trade_source_hash(trade_key: str, open_fill_ids: list[int], close_fill_ids: list[int], realized_pnl: float | None, status: str) -> str:
    return _sha1(
        _json_dumps(
            {
                "trade_key": trade_key,
                "open_fill_ids": open_fill_ids,
                "close_fill_ids": close_fill_ids,
                "realized_pnl": realized_pnl,
                "status": status,
                "analysis_version": ANALYSIS_VERSION,
            }
        )
    )


def _compute_trade_direction(open_fills: list[TradeReviewFill]) -> str | None:
    if not open_fills:
        return None
    first = str(open_fills[0].action or "").lower()
    if first.startswith("buy"):
        return "LONG"
    if first.startswith("sell"):
        return "SHORT"
    return None


def _compute_setup_type(fills: list[TradeReviewFill]) -> str | None:
    tags = []
    if len(fills) > 2:
        tags.append("MULTI_LEG")
    if any("assign" in (f.raw_payload_json or "").lower() for f in fills):
        tags.append("ASSIGNMENT")
    if any("exercise" in (f.raw_payload_json or "").lower() for f in fills):
        tags.append("EXERCISE")
    if any("roll" in (f.raw_payload_json or "").lower() for f in fills):
        tags.append("ROLL")
    if not tags and len(fills) == 1:
        return "SINGLE_LEG"
    return tags[0] if tags else "SCALED"


def _fill_qty_from_action(fill: TradeReviewFill) -> int:
    return int(fill.quantity or 0)


def _weighted_average(entries: list[tuple[float, int]]) -> float | None:
    total_qty = sum(qty for _, qty in entries if qty)
    if total_qty <= 0:
        return None
    total = sum(price * qty for price, qty in entries if qty)
    return round(total / total_qty, 4)


def _market_day_label(ts: str | None) -> str | None:
    parsed = _parse_iso(ts)
    if not parsed:
        return None
    return parsed.astimezone(ET_TZ).strftime("%A")


def _bucket_dte(dte: int | None) -> str:
    if dte is None:
        return "UNKNOWN"
    if dte <= 0:
        return "EXPIRED"
    if dte <= 2:
        return "0-2"
    if dte <= 7:
        return "3-7"
    if dte <= 14:
        return "8-14"
    if dte <= 30:
        return "15-30"
    if dte <= 60:
        return "31-60"
    return "60+"


def _bucket_delta(delta: float | None) -> str:
    if delta is None:
        return "UNKNOWN"
    abs_delta = abs(delta)
    if abs_delta < 0.2:
        return "<0.20"
    if abs_delta < 0.35:
        return "0.20-0.35"
    if abs_delta < 0.55:
        return "0.35-0.55"
    if abs_delta < 0.75:
        return "0.55-0.75"
    return "0.75+"


def _bucket_spread(spread: float | None) -> str:
    if spread is None:
        return "UNKNOWN"
    if spread <= 1:
        return "<=1%"
    if spread <= 3:
        return "1-3%"
    if spread <= 5:
        return "3-5%"
    if spread <= 10:
        return "5-10%"
    return "10%+"


def _bucket_volume(volume: int | None) -> str:
    if volume is None:
        return "UNKNOWN"
    if volume < 50:
        return "<50"
    if volume < 100:
        return "50-99"
    if volume < 250:
        return "100-249"
    if volume < 1000:
        return "250-999"
    return "1000+"


def _calculate_data_confidence(trade: dict[str, Any]) -> tuple[str, float]:
    keys = [
        "opening_timestamp_utc",
        "closing_timestamp_utc",
        "average_entry_price",
        "average_exit_price",
        "realized_pnl",
        "return_on_premium",
        "total_fees",
        "holding_seconds",
        "underlying_symbol",
        "occ_symbol",
        "call_put",
        "strike",
        "expiration",
        "dte_at_entry",
        "market_context_json",
        "grade_breakdown_json",
    ]
    score = sum(1 for key in keys if trade.get(key) not in (None, "", []))
    ratio = score / len(keys)
    if ratio >= 0.8:
        return "HIGH", round(ratio * 100.0, 2)
    if ratio >= 0.5:
        return "MEDIUM", round(ratio * 100.0, 2)
    return "LOW", round(ratio * 100.0, 2)


def _grade_from_score(score: float) -> str:
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    if score >= 40:
        return "D"
    return "F"


def _overall_grade_from_breakdown(breakdown: dict[str, Any]) -> str:
    weights = {"A": 95, "B": 80, "C": 65, "D": 50, "F": 25}
    grades = [str(value).upper() for value in breakdown.values() if str(value).upper() in weights]
    if not grades:
        return "F"
    score = sum(weights[grade] for grade in grades) / len(grades)
    return _grade_from_score(score)


def _trade_grade_breakdown(trade: dict[str, Any], market: dict[str, Any] | None = None) -> dict[str, Any]:
    market = market or {}
    spread = _safe_float(trade.get("entry_spread_pct"))
    dte = _safe_int(trade.get("dte_at_entry"))
    volume = _safe_int(market.get("volume"))
    delta = _safe_float(market.get("delta"))
    return {
        "setup_quality": _grade_from_score(90 if trade.get("call_put") and trade.get("direction") == ("LONG" if trade.get("call_put") == "CALL" else "SHORT") else 45),
        "entry_timing": _grade_from_score(85 if (market.get("entry_above_vwap") is True or market.get("entry_below_vwap") is True) else 50),
        "confirmation": _grade_from_score(80 if market.get("vwap") is not None else 45),
        "contract_selection": _grade_from_score(90 if trade.get("call_put") and trade.get("call_put") in {"CALL", "PUT"} else 35),
        "liquidity": _grade_from_score(90 if (spread is not None and spread <= 5 and (volume or 0) >= 100) else 40),
        "dte_selection": _grade_from_score(85 if dte is not None and 7 <= dte <= 60 else 45),
        "position_sizing": _grade_from_score(80 if (trade.get("total_quantity") or 0) <= 3 else 50),
        "reward_to_risk": _grade_from_score(90 if (trade.get("realized_pnl") or 0) > 0 and (trade.get("return_on_premium") or 0) > 0 else 45),
        "exit_discipline": _grade_from_score(85 if trade.get("holding_seconds") is not None else 40),
        "adherence_to_strategy": _grade_from_score(85 if trade.get("direction") in {"LONG", "SHORT"} else 40),
        "data_confidence": _calculate_data_confidence(trade)[0],
    }


def _trade_explanatory_text(trade: dict[str, Any], breakdown: dict[str, Any]) -> dict[str, Any]:
    pnl = _safe_float(trade.get("realized_pnl"), 0.0) or 0.0
    fees = _safe_float(trade.get("total_fees"), 0.0) or 0.0
    spread = _safe_float(trade.get("entry_spread_pct"))
    dte = _safe_int(trade.get("dte_at_entry"))
    call_put = _safe_text(trade.get("call_put")).upper()
    direction = _safe_text(trade.get("direction")).upper()
    data_confidence, _ = _calculate_data_confidence(trade)
    went_well = []
    went_poorly = []
    if pnl > 0:
        went_well.append("The trade finished green after fees.")
    else:
        went_poorly.append("The trade lost money after fees.")
    if spread is not None and spread <= 3:
        went_well.append("Entry liquidity was acceptable.")
    elif spread is not None and spread > 5:
        went_poorly.append(f"The entry spread was {spread:.2f}%, which is too wide.")
    if dte is not None and 7 <= dte <= 60:
        went_well.append("DTE was in a workable range.")
    elif dte is not None and dte <= 7:
        went_poorly.append("The trade was taken too close to expiration.")
    if call_put in {"CALL", "PUT"} and direction in {"LONG", "SHORT"}:
        went_well.append(f"The contract direction matched the {direction.lower()} bias.")
    else:
        went_poorly.append("The contract direction was not cleanly aligned.")
    hard_truth = (
        f"{trade.get('underlying_symbol') or 'The trade'} was an {'open' if trade.get('status') == 'UNRESOLVED' else 'actual'} option trade. "
        f"After fees of {fees:.2f}, the result was {pnl:.2f}. "
        "The grade is driven by liquidity, timing, and whether the setup was supported by the available data."
    )
    should_skip = bool(spread is not None and spread > 5) or bool(dte is not None and dte <= 7 and pnl <= 0)
    lesson = "Protect entries with better liquidity, cleaner timing, and a wider DTE buffer."
    if pnl > 0 and spread is not None and spread <= 3:
        lesson = "Good liquidity helped; keep insisting on that standard."
    if pnl <= 0 and spread is not None and spread > 5:
        lesson = "Wide spreads are a tax on edge and should usually be skipped."
    return {
        "what_went_well": went_well or ["Data was not strong enough to credit a clean win."] ,
        "what_went_poorly": went_poorly or ["No strong failure signal was available from the stored data."],
        "hard_truth": hard_truth,
        "should_have_been_skipped": should_skip,
        "lesson": lesson,
        "data_confidence_label": data_confidence,
        "missing_data": [],
    }


def _trade_mfe_mae(candles: pd.DataFrame, trade: TradeReviewTrade) -> tuple[float | None, float | None, str]:
    if candles.empty or not trade.opening_timestamp_utc:
        return None, None, "unavailable"
    entry = _safe_float(trade.average_entry_price)
    if entry is None or entry <= 0:
        return None, None, "unavailable"
    direction = _safe_text(trade.direction).upper()
    best = None
    worst = None
    try:
        for _, row in candles.iterrows():
            high = _safe_float(row.get("high"))
            low = _safe_float(row.get("low"))
            if high is None or low is None:
                continue
            if direction == "SHORT":
                fav = max(0.0, entry - low)
                adverse = max(0.0, high - entry)
            else:
                fav = max(0.0, high - entry)
                adverse = max(0.0, entry - low)
            best = fav if best is None else max(best, fav)
            worst = adverse if worst is None else max(worst, adverse)
    except Exception:
        return None, None, "unavailable"
    return round(best or 0.0, 4), round(worst or 0.0, 4), "observed" if best is not None else "unavailable"


def _fetch_chart_context(symbol: str, start_ts: str | None, end_ts: str | None, *, refresh: bool = False) -> dict[str, Any]:
    normalized = normalize_symbol(symbol)
    if not normalized or not start_ts:
        return {
            "symbol": normalized,
            "candles": [],
            "indicators": [],
            "latest": {},
            "warnings": ["Historical candle data is unavailable."],
        }

    start = _parse_iso(start_ts)
    end = _parse_iso(end_ts) if end_ts else None
    if not start:
        return {
            "symbol": normalized,
            "candles": [],
            "indicators": [],
            "latest": {},
            "warnings": ["Historical candle data is unavailable."],
        }

    end = end or (start + timedelta(days=10))
    interval = "15m"
    local = get_candles_from_sql(normalized, interval, start=start - timedelta(days=5), end=end + timedelta(days=2))
    if local.empty:
        fallback_interval = "5m"
        fallback = get_candles_from_sql(normalized, fallback_interval, start=start - timedelta(days=5), end=end + timedelta(days=2))
        if not fallback.empty:
            interval = fallback_interval
            local = fallback
    if local.empty and refresh:
        # Best-effort only. Do not fail the trade review UI if the provider does not have the range.
        try:
            from .data_provider import fetch_candles

            period = "60d"
            local = fetch_candles(normalized, interval=interval, period=period, refresh=True, historical=True, prefer_stored=True)
        except Exception:
            local = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    if local.empty:
        return {
            "symbol": normalized,
            "interval": interval,
            "candles": [],
            "indicators": [],
            "latest": {},
            "warnings": ["Historical candle data is unavailable."],
        }

    cfg = {
        "ema_fast": 9,
        "ema_slow": 21,
        "ema_trend": 50,
        "rsi_period": 14,
        "atr_period": 14,
        "bollinger_period": 20,
        "bollinger_std": 2,
        "volume_avg_period": 20,
        "macd_fast": 12,
        "macd_slow": 26,
        "macd_signal": 9,
    }
    enriched = apply_indicators(local, cfg)
    enriched["ema_200"] = enriched["close"].ewm(span=200, adjust=False).mean()
    candles: list[dict[str, Any]] = []
    indicators: list[dict[str, Any]] = []
    for idx, row in enriched.iterrows():
        ts = int(idx.timestamp()) if hasattr(idx, "timestamp") else int(idx)
        candles.append(
            {
                "time": ts,
                "open": _safe_float(row.get("open"), 0.0),
                "high": _safe_float(row.get("high"), 0.0),
                "low": _safe_float(row.get("low"), 0.0),
                "close": _safe_float(row.get("close"), 0.0),
                "volume": _safe_float(row.get("volume"), 0.0),
            }
        )
        indicators.append(
            {
                "time": ts,
                "ema_9": _safe_float(row.get("ema_fast")),
                "ema_21": _safe_float(row.get("ema_slow")),
                "ema_50": _safe_float(row.get("ema_trend")),
                "ema_200": _safe_float(row.get("ema_200")),
                "vwap": _safe_float(row.get("vwap")),
                "rsi": _safe_float(row.get("rsi")),
                "macd_line": _safe_float(row.get("macd_line")),
                "macd_signal": _safe_float(row.get("macd_signal")),
                "macd_hist": _safe_float(row.get("macd_hist")),
                "atr": _safe_float(row.get("atr")),
                "volume_avg": _safe_float(row.get("volume_avg")),
            }
        )
    latest = indicators[-1] if indicators else {}
    return {
        "symbol": normalized,
        "interval": interval,
        "provider": local.attrs.get("provider") or "sqlite",
        "source": local.attrs.get("source") or "sqlite",
        "timestamp": local.attrs.get("timestamp") or local.attrs.get("last_updated"),
        "last_updated": local.attrs.get("timestamp") or local.attrs.get("last_updated"),
        "candles": candles,
        "indicators": indicators,
        "latest": latest,
        "line_indicators": [
            {"key": "ema_9", "label": "EMA 9", "color": "#16c784"},
            {"key": "ema_21", "label": "EMA 21", "color": "#f0b90b"},
            {"key": "ema_50", "label": "EMA 50", "color": "#ef4444"},
            {"key": "ema_200", "label": "EMA 200", "color": "#a78bfa"},
            {"key": "vwap", "label": "VWAP", "color": "#60a5fa"},
        ],
        "warnings": [],
    }


def _nearest_candle(candles: list[dict[str, Any]], timestamp_utc: str | None) -> dict[str, Any] | None:
    parsed = _parse_iso(timestamp_utc)
    if not parsed or not candles:
        return None
    target = int(parsed.timestamp())
    best = None
    best_delta = None
    for row in candles:
        ts = _safe_int(row.get("time"))
        if ts is None:
            continue
        delta = abs(ts - target)
        if best is None or delta < best_delta:
            best = row
            best_delta = delta
    return best


def _trade_context_for_fill(fill: TradeReviewFill, candle_pack: dict[str, Any], trade_side: str | None = None) -> dict[str, Any]:
    candles = candle_pack.get("candles") or []
    indicators = candle_pack.get("indicators") or []
    nearest = _nearest_candle(candles, fill.execution_timestamp_utc)
    nearest_indicator = _nearest_candle(indicators, fill.execution_timestamp_utc)
    if not nearest and not nearest_indicator:
        return {
            "value": None,
            "source": "unavailable",
            "timestamp": fill.execution_timestamp_utc,
            "data_status": "unavailable",
            "confidence": "LOW",
        }
    candle = nearest or {}
    indicator = nearest_indicator or {}
    entry_price = _safe_float(candle.get("close"), _safe_float(fill.underlying_price))
    vwap = _safe_float(indicator.get("vwap"))
    entry_above_vwap = None
    if entry_price is not None and vwap is not None:
        entry_above_vwap = entry_price >= vwap
    data = {
        "underlying_candle": {
            "open": _safe_float(candle.get("open")),
            "high": _safe_float(candle.get("high")),
            "low": _safe_float(candle.get("low")),
            "close": _safe_float(candle.get("close")),
            "volume": _safe_float(candle.get("volume")),
        },
        "vwap": vwap,
        "ema_9": _safe_float(indicator.get("ema_9")),
        "ema_21": _safe_float(indicator.get("ema_21")),
        "ema_50": _safe_float(indicator.get("ema_50")),
        "ema_200": _safe_float(indicator.get("ema_200")),
        "rsi": _safe_float(indicator.get("rsi")),
        "macd_line": _safe_float(indicator.get("macd_line")),
        "macd_signal": _safe_float(indicator.get("macd_signal")),
        "macd_hist": _safe_float(indicator.get("macd_hist")),
        "atr": _safe_float(indicator.get("atr")),
        "volume_avg": _safe_float(indicator.get("volume_avg")),
        "entry_above_vwap": entry_above_vwap,
        "entry_below_vwap": None if entry_above_vwap is None else not entry_above_vwap,
    }
    money_flow = build_money_flow(
        symbol=fill.underlying_symbol or "",
        side=trade_side,
        candles=candles,
        indicator_data={"candles": candles, "indicators": indicators, "latest": nearest_indicator or indicators[-1] if indicators else {}},
        market_session={"session_state": "HISTORICAL", "actionable_live_quotes": False},
        quote_timestamp=fill.execution_timestamp_utc,
        current_price=entry_price,
    )
    return {
        "value": data,
        "money_flow": money_flow,
        "source": candle_pack.get("source") or candle_pack.get("provider") or "sqlite",
        "timestamp": candle_pack.get("timestamp") or candle_pack.get("last_updated"),
        "data_status": "observed" if nearest else "reconstructed",
        "confidence": "HIGH" if nearest else "MEDIUM",
    }


def _rebuild_trades_for_account(db: Session, account_ref: str) -> dict[str, Any]:
    account = db.query(TradeReviewAccount).filter(TradeReviewAccount.account_ref == account_ref).first()
    if not account:
        return {"trades_reconstructed": 0, "unresolved_fills": 0, "rows_deleted": 0}

    fills = (
        db.query(TradeReviewFill)
        .filter(TradeReviewFill.account_ref == account_ref)
        .order_by(TradeReviewFill.occ_symbol.asc(), TradeReviewFill.execution_timestamp_utc.asc(), TradeReviewFill.id.asc())
        .all()
    )
    if not fills:
        return {"trades_reconstructed": 0, "unresolved_fills": 0, "rows_deleted": 0}

    existing_trade_ids = [row.id for row in db.query(TradeReviewTrade).filter(TradeReviewTrade.account_ref == account_ref).all()]
    if existing_trade_ids:
        db.query(TradeReviewAnalysisCache).filter(TradeReviewAnalysisCache.trade_id.in_(existing_trade_ids)).delete(synchronize_session=False)
        db.query(TradeReviewTrade).filter(TradeReviewTrade.account_ref == account_ref).delete(synchronize_session=False)
        db.commit()
        for obj in list(db.identity_map.values()):
            if isinstance(obj, (TradeReviewTrade, TradeReviewAnalysisCache)):
                db.expunge(obj)

    grouped: dict[str, list[TradeReviewFill]] = defaultdict(list)
    unresolved: list[TradeReviewFill] = []
    for fill in fills:
        key = fill.occ_symbol or "|".join(
            _safe_text(value)
            for value in (fill.underlying_symbol, fill.call_put, fill.strike, fill.expiration)
        )
        if not key.strip("|"):
            unresolved.append(fill)
            continue
        grouped[key].append(fill)

    trade_rows: list[dict[str, Any]] = []
    unresolved_count = len(unresolved)
    for contract_key, contract_fills in grouped.items():
        sequence = 1
        open_lots: list[dict[str, Any]] = []
        open_fill_ids: list[int] = []
        close_fill_ids: list[int] = []
        open_entries: list[tuple[float, int]] = []
        close_entries: list[tuple[float, int]] = []
        total_fees = 0.0
        net_cash = 0.0
        open_timestamp = None
        close_timestamp = None
        direction = None
        fill_tags: list[str] = []
        current_key = _trade_group_key(account_ref, contract_fills[0], sequence)

        def flush_trade(status: str = "COMPLETE") -> None:
            nonlocal sequence, open_lots, open_fill_ids, close_fill_ids, open_entries, close_entries, total_fees, net_cash, open_timestamp, close_timestamp, direction, current_key, fill_tags
            if not open_fill_ids and not close_fill_ids:
                return
            open_qty = sum(qty for _, qty in open_entries)
            close_qty = sum(qty for _, qty in close_entries)
            matched_qty = min(open_qty, close_qty) if status == "COMPLETE" else max(open_qty, close_qty)
            avg_entry = _weighted_average(open_entries)
            avg_exit = _weighted_average(close_entries)
            realized = None
            return_on_premium = None
            if status == "COMPLETE" and avg_entry is not None and avg_exit is not None and matched_qty > 0:
                sign = 1 if direction == "LONG" else -1
                realized = round(((avg_exit - avg_entry) * sign * matched_qty * 100.0) - total_fees, 4)
                entry_premium = abs(avg_entry * matched_qty * 100.0)
                if entry_premium:
                    return_on_premium = round((realized / entry_premium) * 100.0, 4)
            else:
                realized = None

            if open_timestamp and close_timestamp:
                holding_seconds = int((_parse_iso(close_timestamp) - _parse_iso(open_timestamp)).total_seconds()) if _parse_iso(close_timestamp) and _parse_iso(open_timestamp) else None
            else:
                holding_seconds = None

            trade = {
                "trade_key": current_key,
                "account_ref": account_ref,
                "account_id_key": account.account_id_key,
                "account_mask": account.account_mask,
                "underlying_symbol": contract_fills[0].underlying_symbol,
                "occ_symbol": contract_fills[0].occ_symbol,
                "option_symbol": contract_fills[0].option_symbol,
                "call_put": contract_fills[0].call_put,
                "strike": contract_fills[0].strike,
                "expiration": contract_fills[0].expiration,
                "dte_at_entry": next((fill.dte_at_entry for fill in contract_fills if fill.dte_at_entry is not None), None),
                "direction": direction,
                "setup_type": _compute_setup_type(contract_fills + unresolved),
                "total_quantity": matched_qty if matched_qty else open_qty,
                "open_fill_ids_json": _json_dumps(open_fill_ids),
                "close_fill_ids_json": _json_dumps(close_fill_ids),
                "opening_timestamp_utc": open_timestamp,
                "closing_timestamp_utc": close_timestamp,
                "opening_timestamp_et": _to_et(open_timestamp),
                "closing_timestamp_et": _to_et(close_timestamp),
                "average_entry_price": round(avg_entry, 4) if avg_entry is not None else None,
                "average_exit_price": round(avg_exit, 4) if avg_exit is not None else None,
                "realized_pnl": realized,
                "return_on_premium": return_on_premium,
                "total_fees": round(total_fees, 4),
                "holding_seconds": holding_seconds,
                "maximum_capital_at_risk": round(abs(avg_entry * matched_qty * 100.0), 4) if status == "COMPLETE" and avg_entry is not None and matched_qty else None,
                "expiration_outcome": (
                    "EXPIRED"
                    if status == "COMPLETE"
                    and contract_fills[0].expiration
                    and parse_expiration_date(contract_fills[0].expiration)
                    and close_timestamp
                    and _parse_iso(close_timestamp)
                    and _parse_iso(close_timestamp).date() >= parse_expiration_date(contract_fills[0].expiration)
                    else None
                ),
                "assignment_outcome": "ASSIGNED" if any("assign" in (fill.raw_payload_json or "").lower() for fill in contract_fills) else None,
                "exercise_outcome": "EXERCISED" if any("exercise" in (fill.raw_payload_json or "").lower() for fill in contract_fills) else None,
                "confidence_level": "HIGH" if all(fill.confidence_level == "HIGH" for fill in contract_fills) else "MEDIUM",
                "status": status,
                "grade": None,
                "grade_breakdown_json": None,
                "what_went_well": None,
                "what_went_poorly": None,
                "hard_truth": None,
                "should_have_been_skipped": False,
                "better_entry": None,
                "better_invalidation": None,
                "better_stop_plan": None,
                "better_contract_profile": None,
                "better_exit_plan": None,
                "lesson": None,
                "admin_notes": None,
                "missing_data_json": None,
                "pattern_tags_json": _json_dumps(sorted(set(fill_tags))),
                "market_context_json": None,
                "analysis_status": "PENDING" if status == "COMPLETE" else "UNRESOLVED",
                "analysis_version": ANALYSIS_VERSION,
                "data_version": _sha1(_json_dumps({"fills": open_fill_ids + close_fill_ids, "status": status, "direction": direction, "pnl": realized, "account_ref": account_ref})),
                "reviewed": False,
                "reviewed_at": None,
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }
            trade_rows.append(trade)

            sequence += 1
            open_lots = []
            open_fill_ids = []
            close_fill_ids = []
            open_entries = []
            close_entries = []
            total_fees = 0.0
            net_cash = 0.0
            open_timestamp = None
            close_timestamp = None
            direction = None
            current_key = _trade_group_key(account_ref, contract_fills[0], sequence)
            fill_tags = []

        for fill in contract_fills:
            qty = _safe_int(fill.quantity, 0) or 0
            if qty <= 0:
                continue
            total_fees += (fill.commission or 0.0) + (fill.fees or 0.0)
            if fill.action in {"buy to open", "sell to open"}:
                if direction is None:
                    direction = "LONG" if fill.action == "buy to open" else "SHORT"
                if not open_timestamp:
                    open_timestamp = fill.execution_timestamp_utc
                open_fill_ids.append(fill.id)
                open_lots.append(
                    {
                        "fill_id": fill.id,
                        "qty": qty,
                        "remaining": qty,
                        "entry_price": float(fill.fill_price or 0.0),
                        "action": fill.action,
                    }
                )
                if fill.fill_price is not None:
                    open_entries.append((float(fill.fill_price), qty))
                net_cash += float(fill.net_cash_effect or 0.0)
                fill.match_status = "MATCHED"
                continue

            if fill.action in {"buy to close", "sell to close"}:
                if not open_lots:
                    unresolved_count += 1
                    fill.match_status = "UNRESOLVED"
                    if fill.fill_price is not None:
                        close_entries.append((float(fill.fill_price), qty))
                    close_fill_ids.append(fill.id)
                    continue

                if not close_timestamp:
                    close_timestamp = fill.execution_timestamp_utc
                close_fill_ids.append(fill.id)
                if fill.fill_price is not None:
                    close_entries.append((float(fill.fill_price), qty))
                net_cash += float(fill.net_cash_effect or 0.0)
                remaining = qty
                while remaining > 0 and open_lots:
                    lot = open_lots[0]
                    matched = min(remaining, int(lot["remaining"]))
                    lot["remaining"] -= matched
                    remaining -= matched
                    if lot["remaining"] <= 0:
                        open_lots.pop(0)
                fill.match_status = "MATCHED"
                if not open_lots:
                    flush_trade("COMPLETE")
                continue

            fill.match_status = "UNRESOLVED"
            unresolved_count += 1

        if open_lots or open_fill_ids or close_fill_ids:
            flush_trade("UNRESOLVED" if open_lots else "COMPLETE")

    # Persist trade rows.
    created_count = 0
    for trade in trade_rows:
        existing = db.query(TradeReviewTrade).filter(TradeReviewTrade.trade_key == trade["trade_key"]).first()
        if existing:
            for key, value in trade.items():
                setattr(existing, key, value)
        else:
            db.add(TradeReviewTrade(**trade))
            created_count += 1
    db.commit()

    persisted_trades = (
        db.query(TradeReviewTrade)
        .filter(TradeReviewTrade.account_ref == account_ref)
        .order_by(TradeReviewTrade.opening_timestamp_utc.asc(), TradeReviewTrade.id.asc())
        .all()
    )
    for trade in persisted_trades:
        if trade.status != "COMPLETE":
            trade.analysis_status = "UNRESOLVED"
            trade.grade = trade.grade or "F"
            trade.updated_at = now_iso()
            continue
        market_context = _collect_market_context(db, trade, refresh=False)
        grade_breakdown = _trade_grade_breakdown(trade.__dict__, market_context)
        explanation = _trade_explanatory_text(trade.__dict__, grade_breakdown)
        pattern_tags = _trade_pattern_tags(trade.__dict__, market_context)
        data_confidence_label, data_confidence_score = _calculate_data_confidence(trade.__dict__)
        overall = _overall_grade_from_breakdown(grade_breakdown)
        trade.market_context_json = _json_dumps(market_context)
        trade.grade_breakdown_json = _json_dumps(grade_breakdown)
        trade.pattern_tags_json = _json_dumps(pattern_tags)
        trade.what_went_well = _json_dumps(explanation["what_went_well"])
        trade.what_went_poorly = _json_dumps(explanation["what_went_poorly"])
        trade.hard_truth = explanation["hard_truth"]
        trade.should_have_been_skipped = bool(explanation["should_have_been_skipped"])
        trade.better_entry = "Use a cleaner trigger and better liquidity before entering."
        trade.better_invalidation = "Define the invalidation before entry and honor it."
        trade.better_stop_plan = "Use a hard exit when the setup fails."
        trade.better_contract_profile = "Prefer tighter spreads, adequate volume, and a better DTE buffer."
        trade.better_exit_plan = "Take profits on the first target instead of waiting for the whole move."
        trade.lesson = explanation["lesson"]
        trade.missing_data_json = _json_dumps(explanation["missing_data"])
        trade.grade = overall
        trade.data_confidence_label = data_confidence_label
        trade.data_confidence_score = data_confidence_score
        trade.analysis_status = "PENDING"
        trade.updated_at = now_iso()
    db.commit()

    return {
        "trades_reconstructed": len(trade_rows),
        "trades_created": created_count,
        "unresolved_fills": unresolved_count,
        "rows_deleted": len(existing_trade_ids),
    }


def _collect_market_context(db: Session, trade: TradeReviewTrade, *, refresh: bool = False) -> dict[str, Any]:
    candle_pack = _fetch_chart_context(trade.underlying_symbol or "", trade.opening_timestamp_utc, trade.closing_timestamp_utc, refresh=refresh)
    open_fill_ids = _json_loads(trade.open_fill_ids_json, [])
    close_fill_ids = _json_loads(trade.close_fill_ids_json, [])
    fills = []
    for fill_id in list(open_fill_ids) + list(close_fill_ids):
        fill = db.query(TradeReviewFill).filter(TradeReviewFill.id == fill_id).first()
        if fill:
            fills.append(fill)
    context = {
        "entry": None,
        "exit": None,
        "entry_fields": {},
        "exit_fields": {},
        "candles": candle_pack,
    }
    if fills:
        first_open = next((fill for fill in fills if fill.id in open_fill_ids), None)
        last_close = next((fill for fill in reversed(fills) if fill.id in close_fill_ids), None)
        if first_open:
            context["entry"] = _trade_context_for_fill(first_open, candle_pack, trade.direction)
            context["entry_fields"] = context["entry"].get("value") or {}
        if last_close:
            context["exit"] = _trade_context_for_fill(last_close, candle_pack, trade.direction)
            context["exit_fields"] = context["exit"].get("value") or {}
    return context


def _trade_mfe_mae_series(trade: TradeReviewTrade, candle_pack: dict[str, Any]) -> dict[str, Any]:
    candles = candle_pack.get("candles") or []
    if not candles or not trade.average_entry_price:
        return {"mfe": None, "mae": None, "source": "unavailable"}
    entry_price = float(trade.average_entry_price or 0.0)
    direction = _safe_text(trade.direction).upper()
    mfe = 0.0
    mae = 0.0
    for row in candles:
        high = _safe_float(row.get("high"))
        low = _safe_float(row.get("low"))
        if high is None or low is None:
            continue
        if direction == "SHORT":
            mfe = max(mfe, max(0.0, entry_price - low))
            mae = max(mae, max(0.0, high - entry_price))
        else:
            mfe = max(mfe, max(0.0, high - entry_price))
            mae = max(mae, max(0.0, entry_price - low))
    return {"mfe": round(mfe, 4), "mae": round(mae, 4), "source": "observed" if mfe or mae else "unavailable"}


def _trade_levels(trade: TradeReviewTrade, candle_pack: dict[str, Any]) -> dict[str, Any]:
    candles = candle_pack.get("candles") or []
    if not candles or not trade.opening_timestamp_utc or not trade.average_entry_price:
        return {
            "support": None,
            "resistance": None,
            "stop": None,
            "target_1": None,
            "target_2": None,
            "stretch_target": None,
            "source": "unavailable",
        }
    entry_ts = _parse_iso(trade.opening_timestamp_utc)
    if not entry_ts:
        return {
            "support": None,
            "resistance": None,
            "stop": None,
            "target_1": None,
            "target_2": None,
            "stretch_target": None,
            "source": "unavailable",
        }
    entry_time = int(entry_ts.timestamp())
    eligible = [row for row in candles if _safe_int(row.get("time")) is not None and _safe_int(row.get("time")) <= entry_time]
    if not eligible:
        eligible = candles[:]
    lookback = eligible[-20:] if len(eligible) > 20 else eligible
    highs = [_safe_float(row.get("high")) for row in lookback if _safe_float(row.get("high")) is not None]
    lows = [_safe_float(row.get("low")) for row in lookback if _safe_float(row.get("low")) is not None]
    if not highs or not lows:
        return {
            "support": None,
            "resistance": None,
            "stop": None,
            "target_1": None,
            "target_2": None,
            "stretch_target": None,
            "source": "unavailable",
        }
    support = min(lows)
    resistance = max(highs)
    entry_price = float(trade.average_entry_price or 0.0)
    direction = _safe_text(trade.direction).upper()
    risk = max(abs(entry_price - support), abs(resistance - entry_price), entry_price * 0.01)
    if direction == "SHORT":
        stop = resistance
        target_1 = entry_price - risk
        target_2 = entry_price - (risk * 1.5)
        stretch = entry_price - (risk * 2.0)
    else:
        stop = support
        target_1 = entry_price + risk
        target_2 = entry_price + (risk * 1.5)
        stretch = entry_price + (risk * 2.0)
    return {
        "support": round(float(support), 4),
        "resistance": round(float(resistance), 4),
        "stop": round(float(stop), 4),
        "target_1": round(float(target_1), 4),
        "target_2": round(float(target_2), 4),
        "stretch_target": round(float(stretch), 4),
        "source": "observed",
    }


def _trade_pattern_tags(trade: dict[str, Any], market_context: dict[str, Any] | None = None) -> list[str]:
    market_context = market_context or {}
    tags: list[str] = []
    spread = _safe_float(trade.get("entry_spread_pct"))
    dte = _safe_int(trade.get("dte_at_entry"))
    entry_block = market_context.get("entry") or {}
    entry_value = entry_block.get("value") or {}
    entry_flow = entry_block.get("money_flow") or {}
    volume = _safe_int((entry_value.get("underlying_candle") or {}).get("volume"))
    entry_above_vwap = entry_value.get("entry_above_vwap")
    pnl = _safe_float(trade.get("realized_pnl"), 0.0) or 0.0
    holding_seconds = _safe_int(trade.get("holding_seconds"))

    if spread is not None and spread > 5:
        tags.append("WIDE_SPREAD")
    if dte is not None and dte <= 7:
        tags.append("LOW_DTE")
    if volume is not None and volume < 100:
        tags.append("LOW_VOLUME")
    if entry_above_vwap is False and trade.get("direction") == "LONG":
        tags.append("ENTRY_BELOW_VWAP")
    if entry_above_vwap is True and trade.get("direction") == "SHORT":
        tags.append("ENTRY_ABOVE_VWAP")
    if entry_flow.get("alignment") == "aligned":
        tags.append("FLOW_ALIGNED")
    if entry_flow.get("alignment") == "conflicted":
        tags.append("FLOW_CONFLICTED")
    if entry_flow.get("classification") in {"STRONG ACCUMULATION", "MODERATE ACCUMULATION"} and trade.get("direction") == "LONG":
        tags.append("ACCUMULATION_CONFIRMED")
    if entry_flow.get("classification") in {"STRONG DISTRIBUTION", "MODERATE DISTRIBUTION"} and trade.get("direction") == "SHORT":
        tags.append("DISTRIBUTION_CONFIRMED")
    if holding_seconds is not None and holding_seconds > 0 and holding_seconds > 3 * 86400:
        tags.append("HELD_TOO_LONG")
    if pnl < 0 and spread is not None and spread > 5:
        tags.append("LOSS_FROM_SPREAD")
    if pnl < 0 and dte is not None and dte <= 7:
        tags.append("LOSS_FROM_LOW_DTE")
    if pnl < 0 and holding_seconds is not None and holding_seconds > 0 and holding_seconds > 86400:
        tags.append("LOSS_FROM_HOLDING")
    if pnl < 0 and trade.get("total_quantity") and int(trade.get("total_quantity")) >= 3:
        tags.append("OVERSIZED")
    if trade.get("setup_type") == "ROLL":
        tags.append("ROLL")
    if trade.get("open_fill_ids_json") and len(_json_loads(trade.get("open_fill_ids_json"), [])) > 1:
        tags.append("SCALED_IN")
    if trade.get("close_fill_ids_json") and len(_json_loads(trade.get("close_fill_ids_json"), [])) > 1:
        tags.append("SCALED_OUT")
    return sorted(set(tags))


def _sync_account(db: Session, run: TradeReviewSyncRun, account: TradeReviewAccount, payload: dict[str, Any]) -> dict[str, Any]:
    from_date, to_date = _default_range_for_account(account, payload)
    run.current_account_ref = account.account_ref
    run.current_stage = "refreshing"
    run.current_message = f"Refreshing {account.account_mask}"
    db.commit()

    transactions_imported = 0
    orders_imported = 0
    fills_imported = 0
    errors = 0
    last_error = None

    try:
        tx_records, _ = _paginate_records(
            f"/v1/accounts/{account.account_id_key}/transactions.json",
            params={"startDate": from_date, "endDate": to_date, "count": 100},
            symbol=account.account_id_key,
            collection_paths=[
                ("TransactionList", "Transaction"),
                ("transactionList", "Transaction"),
                ("TransactionList", "transaction"),
                ("transactions",),
                ("Transaction",),
            ],
        )
        transactions_imported = len(tx_records)
        run.current_stage = "transactions"
        run.current_message = f"Imported {transactions_imported} transactions for {account.account_mask}"
        db.commit()
        for index, record in enumerate(tx_records):
            fills = _record_to_fills(account.__dict__, "transaction", record)
            for fill in fills:
                if _upsert_fill(db, fill):
                    fills_imported += 1
        db.commit()
    except Exception as exc:
        errors += 1
        last_error = str(exc)
        record_provider_error("etrade", account.account_id_key, "/v1/accounts/{accountIdKey}/transactions.json", exc)

    try:
        order_records, _ = _paginate_records(
            f"/v1/accounts/{account.account_id_key}/orders.json",
            params={"fromDate": from_date, "toDate": to_date, "count": 100},
            symbol=account.account_id_key,
            collection_paths=[
                ("OrderList", "Order"),
                ("orderList", "Order"),
                ("orders",),
                ("Orders",),
                ("Order",),
            ],
        )
        orders_imported = len(order_records)
        run.current_stage = "orders"
        run.current_message = f"Imported {orders_imported} orders for {account.account_mask}"
        db.commit()
        for index, record in enumerate(order_records):
            fills = _record_to_fills(account.__dict__, "order", record)
            for fill in fills:
                if _upsert_fill(db, fill):
                    fills_imported += 1
        db.commit()
    except Exception as exc:
        errors += 1
        last_error = str(exc)
        record_provider_error("etrade", account.account_id_key, "/v1/accounts/{accountIdKey}/orders.json", exc)

    run.transactions_imported += transactions_imported
    run.orders_imported += orders_imported
    run.fills_imported += fills_imported
    run.errors_count += errors
    if last_error:
        run.last_error = last_error
        account.last_sync_status = "FAILED"
        account.last_error_message = last_error
    else:
        account.last_sync_status = "COMPLETE"
        account.last_error_message = None
        account.last_successful_sync_at = now_iso()
        if not account.oldest_available_history_at:
            account.oldest_available_history_at = from_date
        else:
            parsed_oldest = _parse_iso(account.oldest_available_history_at)
            parsed_new = _parse_iso(from_date + "T00:00:00Z")
            if parsed_oldest and parsed_new and parsed_new < parsed_oldest:
                account.oldest_available_history_at = from_date

    db.commit()
    trade_result = _rebuild_trades_for_account(db, account.account_ref)
    run.trades_reconstructed += trade_result["trades_reconstructed"]
    run.unresolved_fills += trade_result["unresolved_fills"]
    run.current_stage = "rebuild_trades"
    run.current_message = f"Rebuilt {trade_result['trades_reconstructed']} trades for {account.account_mask}"
    db.commit()
    return {
        "from_date": from_date,
        "to_date": to_date,
        "transactions_imported": transactions_imported,
        "orders_imported": orders_imported,
        "fills_imported": fills_imported,
        "errors": errors,
        "last_error": last_error,
        "trades_reconstructed": trade_result["trades_reconstructed"],
        "unresolved_fills": trade_result["unresolved_fills"],
    }


def run_sync(db: Session, run_id: int, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = payload or {}
    run = db.query(TradeReviewSyncRun).filter(TradeReviewSyncRun.id == run_id).first()
    if not run:
        raise ValueError("Sync run not found")
    if run.status == "CANCELLED":
        return {"status": "CANCELLED", "run_id": run_id}

    run.status = "RUNNING"
    run.started_at = run.started_at or now_iso()
    run.updated_at = now_iso()
    db.commit()

    try:
        username = run.username or _safe_text(payload.get("username")) or "admin"
        if bool(payload.get("refresh_accounts", True)):
            refresh_accounts(db, username)
        selected, selection = _resolve_accounts_for_user(db, username)
        if not selected:
            raise ValueError("Select one or more accounts before importing history.")

        run.accounts_total = len(selected)
        run.selection_mode = selection.get("selection_mode") or run.selection_mode
        run.selected_account_refs = _json_dumps(selection.get("selected_account_refs") or [])
        db.commit()

        _audit(db, username, "start_sync", "sync_run", str(run.id), f"accounts={len(selected)}")
        for index, account in enumerate(selected, start=1):
            if run.status == "CANCELLED":
                break
            run.accounts_completed = index - 1
            run.current_account_ref = account.account_ref
            run.current_stage = "account_sync"
            run.current_message = f"Syncing {account.account_mask} ({index}/{len(selected)})"
            db.commit()
            result = _sync_account(db, run, account, payload)
            if result["last_error"]:
                run.accounts_failed += 1
            else:
                run.accounts_completed += 1
            db.commit()

        if run.status != "CANCELLED":
            run.status = "COMPLETE" if run.accounts_failed == 0 else "COMPLETE_WITH_ERRORS"
            run.finished_at = now_iso()
            run.current_stage = "done"
            run.current_message = "Trade review sync complete"
            run.message = (
                f"Imported {run.fills_imported} fills and reconstructed {run.trades_reconstructed} trades"
                + (f" with {run.errors_count} error(s)" if run.errors_count else "")
            )
        db.commit()
        return serialize_sync_run(db, run)
    except Exception as exc:
        run.status = "FAILED"
        run.last_error = str(exc)
        run.finished_at = now_iso()
        run.current_stage = "failed"
        run.current_message = str(exc)
        db.commit()
        _audit(db, run.username or "admin", "sync_failed", "sync_run", str(run.id), str(exc))
        raise


def serialize_account_row(db: Session, account: TradeReviewAccount, username: str) -> dict[str, Any]:
    trade_count = db.query(TradeReviewTrade).filter(TradeReviewTrade.account_ref == account.account_ref).count()
    fill_count = db.query(TradeReviewFill).filter(TradeReviewFill.account_ref == account.account_ref).count()
    selected = False
    selection = get_selection(db, username)
    if selection.get("selection_mode") == "ALL":
        selected = True
    else:
        selected = account.account_ref in set(selection.get("selected_account_refs") or [])
    return {
        "account_ref": account.account_ref,
        "account_mask": account.account_mask,
        "account_desc": account.account_desc,
        "account_name": account.account_name,
        "account_type": account.account_type,
        "account_mode": account.account_mode,
        "institution_type": account.institution_type,
        "selected": selected,
        "last_sync_status": account.last_sync_status,
        "last_error_message": account.last_error_message,
        "last_successful_sync_at": account.last_successful_sync_at,
        "oldest_available_history_at": account.oldest_available_history_at,
        "trade_count": trade_count,
        "fill_count": fill_count,
    }


def serialize_sync_run(db: Session, run: TradeReviewSyncRun | None) -> dict[str, Any] | None:
    if not run:
        return None
    return {
        "id": run.id,
        "username": run.username,
        "selection_mode": run.selection_mode,
        "selected_account_refs": _json_loads(run.selected_account_refs, []),
        "status": run.status,
        "from_date": run.from_date,
        "to_date": run.to_date,
        "accounts_total": run.accounts_total,
        "accounts_completed": run.accounts_completed,
        "accounts_failed": run.accounts_failed,
        "transactions_imported": run.transactions_imported,
        "orders_imported": run.orders_imported,
        "fills_imported": run.fills_imported,
        "trades_reconstructed": run.trades_reconstructed,
        "unresolved_fills": run.unresolved_fills,
        "errors_count": run.errors_count,
        "current_account_ref": run.current_account_ref,
        "current_stage": run.current_stage,
        "current_message": run.current_message,
        "last_error": run.last_error,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "message": run.message,
    }


def get_active_sync_run(db: Session) -> TradeReviewSyncRun | None:
    return (
        db.query(TradeReviewSyncRun)
        .filter(TradeReviewSyncRun.status.in_(["PENDING", "RUNNING", "FAILED"]))
        .order_by(desc(TradeReviewSyncRun.started_at))
        .first()
    )


def cancel_active_sync(db: Session, username: str) -> dict[str, Any]:
    run = get_active_sync_run(db)
    if not run:
        return {"ok": True, "cancelled": False, "message": "No active trade review sync"}
    run.status = "CANCELLED"
    run.finished_at = now_iso()
    run.current_stage = "cancelled"
    run.current_message = "Trade review sync cancelled"
    db.commit()
    _audit(db, username, "cancel_sync", "sync_run", str(run.id), "Cancelled active sync")
    return {"ok": True, "cancelled": True, "run_id": run.id}


def pause_stale_sync_runs(db: Session) -> int:
    stale_runs = db.query(TradeReviewSyncRun).filter(TradeReviewSyncRun.status == "RUNNING").all()
    for run in stale_runs:
        run.status = "FAILED"
        run.finished_at = now_iso()
        run.current_stage = "paused"
        run.current_message = "Trade review sync paused because the backend restarted. Resume to continue."
        run.last_error = "Backend restart interrupted the sync"
    if stale_runs:
        db.commit()
    return len(stale_runs)


def _trade_rows_query(db: Session, filters: dict[str, Any]) -> list[TradeReviewTrade]:
    query = db.query(TradeReviewTrade)
    account_refs = filters.get("account_refs") or []
    if account_refs:
        query = query.filter(TradeReviewTrade.account_ref.in_(account_refs))
    if filters.get("ticker"):
        query = query.filter(TradeReviewTrade.underlying_symbol == normalize_symbol(filters["ticker"]))
    if filters.get("call_put"):
        query = query.filter(TradeReviewTrade.call_put == str(filters["call_put"]).upper())
    if filters.get("grade"):
        query = query.filter(TradeReviewTrade.grade == str(filters["grade"]).upper())
    if filters.get("setup_type"):
        query = query.filter(TradeReviewTrade.setup_type == str(filters["setup_type"]).upper())
    if filters.get("reviewed") in {True, False}:
        query = query.filter(TradeReviewTrade.reviewed.is_(bool(filters["reviewed"])))
    if filters.get("from_date"):
        query = query.filter(TradeReviewTrade.closing_timestamp_utc >= f"{filters['from_date']}T00:00:00")
    if filters.get("to_date"):
        query = query.filter(TradeReviewTrade.closing_timestamp_utc <= f"{filters['to_date']}T23:59:59")
    if filters.get("winner_loser") == "WINNER":
        query = query.filter(TradeReviewTrade.realized_pnl > 0)
    elif filters.get("winner_loser") == "LOSER":
        query = query.filter(TradeReviewTrade.realized_pnl < 0)
    rows = query.order_by(desc(TradeReviewTrade.closing_timestamp_utc), desc(TradeReviewTrade.opening_timestamp_utc)).all()
    if filters.get("market_regime"):
        regime = str(filters["market_regime"]).upper()
        rows = [row for row in rows if _trade_row_to_payload(row).get("market_regime") == regime]
    if filters.get("dte_bucket"):
        bucket = str(filters["dte_bucket"]).upper()
        rows = [trade for trade in rows if _bucket_dte(trade.dte_at_entry) == bucket]
    return rows


def _trade_row_to_payload(trade: TradeReviewTrade) -> dict[str, Any]:
    grade_breakdown = _json_loads(trade.grade_breakdown_json, {}) if trade.grade_breakdown_json else {}
    pattern_tags = _json_loads(trade.pattern_tags_json, [])
    missing_data = _json_loads(trade.missing_data_json, [])
    market_context = _json_loads(trade.market_context_json, {}) if trade.market_context_json else {}
    if not pattern_tags:
        pattern_tags = _trade_pattern_tags(trade.__dict__, market_context)
    entry_value = (market_context.get("entry") or {}).get("value") or {}
    entry_candle = entry_value.get("underlying_candle") or {}
    market_regime = None
    close = _safe_float(entry_candle.get("close"))
    vwap = _safe_float(entry_value.get("vwap"))
    ema_9 = _safe_float(entry_value.get("ema_9"))
    ema_21 = _safe_float(entry_value.get("ema_21"))
    if close is not None and vwap is not None and ema_9 is not None and ema_21 is not None:
        if close > vwap and ema_9 > ema_21:
            market_regime = "BULLISH"
        elif close < vwap and ema_9 < ema_21:
            market_regime = "BEARISH"
        else:
            market_regime = "SIDEWAYS"
    data_confidence_label, data_confidence_score = _calculate_data_confidence(trade.__dict__)
    return {
        "id": trade.id,
        "trade_key": trade.trade_key,
        "account_ref": trade.account_ref,
        "account_mask": trade.account_mask,
        "underlying_symbol": trade.underlying_symbol,
        "occ_symbol": trade.occ_symbol,
        "option_symbol": trade.option_symbol,
        "call_put": trade.call_put,
        "direction": trade.direction,
        "quantity": trade.total_quantity,
        "dte": trade.dte_at_entry,
        "delta": entry_value.get("delta"),
        "entry_price": trade.average_entry_price,
        "exit_price": trade.average_exit_price,
        "entry_spread": entry_value.get("spread_pct") if market_context else None,
        "holding_time": trade.holding_seconds,
        "pnl": trade.realized_pnl,
        "return_pct": trade.return_on_premium,
        "grade": trade.grade or "F",
        "setup": trade.setup_type,
        "primary_mistake": next(iter(pattern_tags), None),
        "analysis_status": trade.analysis_status,
        "reviewed": bool(trade.reviewed),
        "reviewed_at": trade.reviewed_at,
        "opening_timestamp_utc": trade.opening_timestamp_utc,
        "closing_timestamp_utc": trade.closing_timestamp_utc,
        "market_regime": market_regime or market_context.get("regime"),
        "data_confidence_label": data_confidence_label,
        "data_confidence_score": data_confidence_score,
        "pattern_tags": pattern_tags,
        "missing_data": missing_data,
        "grade_breakdown": grade_breakdown,
    }


def summarize_trades(db: Session, filters: dict[str, Any]) -> dict[str, Any]:
    rows = _trade_rows_query(db, filters)
    dte_bucket = filters.get("dte_bucket")
    if dte_bucket:
        rows = [trade for trade in rows if _bucket_dte(trade.dte_at_entry) == str(dte_bucket).upper()]
    completed = [trade for trade in rows if trade.status == "COMPLETE" and trade.realized_pnl is not None]
    unresolved = [trade for trade in rows if trade.status != "COMPLETE"]
    pnls = [float(trade.realized_pnl or 0.0) for trade in completed]
    returns = [float(trade.return_on_premium or 0.0) for trade in completed if trade.return_on_premium is not None]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p < 0]
    win_rate = round((len(winners) / len(pnls)) * 100.0, 2) if pnls else None
    avg_winner = round(mean(winners), 2) if winners else None
    avg_loser = round(mean(losers), 2) if losers else None
    win_loss_ratio = round(avg_winner / abs(avg_loser), 2) if avg_winner is not None and avg_loser not in (None, 0) else None
    profit_factor = round(sum(winners) / abs(sum(losers)), 2) if winners and losers else None
    expectancy = round(mean(pnls), 2) if pnls else None
    median_return = round(median(returns), 2) if returns else None

    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in pnls:
        cumulative += pnl
        peak = max(peak, cumulative)
        max_drawdown = min(max_drawdown, cumulative - peak)

    largest_win = round(max(pnls), 2) if pnls else None
    largest_loss = round(min(pnls), 2) if pnls else None
    consecutive_wins = 0
    consecutive_losses = 0
    current_wins = 0
    current_losses = 0
    for pnl in pnls:
        if pnl > 0:
            current_wins += 1
            current_losses = 0
        elif pnl < 0:
            current_losses += 1
            current_wins = 0
        else:
            current_wins = 0
            current_losses = 0
        consecutive_wins = max(consecutive_wins, current_wins)
        consecutive_losses = max(consecutive_losses, current_losses)

    holding_values = [trade.holding_seconds for trade in completed if trade.holding_seconds is not None]
    summary = {
        "total_trades": len(rows),
        "completed_trades": len(completed),
        "unresolved_trades": len(unresolved),
        "net_pnl": round(sum(pnls), 2) if pnls else 0.0,
        "win_rate": win_rate,
        "loss_rate": round(100.0 - win_rate, 2) if win_rate is not None else None,
        "average_winner": avg_winner,
        "average_loser": avg_loser,
        "win_loss_ratio": win_loss_ratio,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "median_trade_return": median_return,
        "max_drawdown": round(max_drawdown, 2),
        "largest_win": largest_win,
        "largest_loss": largest_loss,
        "consecutive_wins": consecutive_wins,
        "consecutive_losses": consecutive_losses,
        "average_holding_seconds": round(mean(holding_values), 2) if holding_values else None,
        "median_holding_seconds": round(median(holding_values), 2) if holding_values else None,
        "data_coverage": round((len(completed) / len(rows)) * 100.0, 2) if rows else 0.0,
        "data_confidence_score": round(mean([_calculate_data_confidence(trade.__dict__)[1] for trade in completed]), 2) if completed else 0.0,
    }
    return summary


def _group_pnl(rows: list[TradeReviewTrade], key_fn) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0})
    for trade in rows:
        pnl = float(trade.realized_pnl or 0.0)
        key = key_fn(trade)
        if key is None:
            continue
        bucket = buckets[key]
        bucket["pnl"] += pnl
        bucket["trades"] += 1
        bucket["wins"] += 1 if pnl > 0 else 0
        bucket["losses"] += 1 if pnl < 0 else 0
    return [
        {
            "bucket": bucket,
            "pnl": round(values["pnl"], 2),
            "trades": values["trades"],
            "win_rate": round((values["wins"] / values["trades"]) * 100.0, 2) if values["trades"] else None,
        }
        for bucket, values in sorted(buckets.items(), key=lambda item: item[1]["pnl"], reverse=True)
    ]


def build_pattern_analysis(db: Session, filters: dict[str, Any]) -> dict[str, Any]:
    rows = [trade for trade in _trade_rows_query(db, filters) if trade.status == "COMPLETE" and trade.realized_pnl is not None]
    dte_bucket = filters.get("dte_bucket")
    if dte_bucket:
        rows = [trade for trade in rows if _bucket_dte(trade.dte_at_entry) == str(dte_bucket).upper()]
    tagged_rows: list[tuple[TradeReviewTrade, list[str]]] = []
    flow_rows: list[dict[str, Any]] = []
    for trade in rows:
        market_context = _json_loads(trade.market_context_json, {}) if trade.market_context_json else {}
        tags = _trade_pattern_tags(trade.__dict__, market_context)
        tagged_rows.append((trade, tags))
        entry_flow = (market_context.get("entry") or {}).get("money_flow") or {}
        entry_value = (market_context.get("entry") or {}).get("value") or {}
        flow_rows.append(
            {
                "pnl": float(trade.realized_pnl or 0.0),
                "direction": _safe_text(trade.direction).upper(),
                "alignment": _safe_text(entry_flow.get("alignment")).lower(),
                "classification": _safe_text(entry_flow.get("classification")).upper(),
                "above_vwap": entry_value.get("entry_above_vwap"),
                "relative_strength": _safe_float((entry_flow.get("relative_strength") or {}).get("relative_strength_pct")),
                "options_alignment_score": _safe_float((entry_flow.get("options_alignment") or {}).get("alignment_score")),
                "options_bias": _safe_text((entry_flow.get("options_alignment") or {}).get("bias")).upper(),
            }
        )

    tag_pnl: dict[str, float] = defaultdict(float)
    tag_counts: dict[str, int] = defaultdict(int)
    for trade, tags in tagged_rows:
        pnl = float(trade.realized_pnl or 0.0)
        for tag in tags:
            tag_pnl[tag] += pnl
            tag_counts[tag] += 1

    strengths = [
        {
            "name": tag,
            "trades": tag_counts[tag],
            "estimated_dollars": round(tag_pnl[tag], 2),
            "average_pnl": round(tag_pnl[tag] / tag_counts[tag], 2) if tag_counts[tag] else None,
        }
        for tag in sorted(tag_counts, key=lambda k: tag_pnl[k], reverse=True)
        if tag_pnl[tag] > 0
    ][:5]
    mistakes = [
        {
            "name": tag,
            "trades": tag_counts[tag],
            "estimated_dollars_lost": round(abs(tag_pnl[tag]), 2),
            "average_pnl": round(tag_pnl[tag] / tag_counts[tag], 2) if tag_counts[tag] else None,
        }
        for tag in sorted(tag_counts, key=lambda k: tag_pnl[k])
        if tag_pnl[tag] < 0
    ][:5]

    best_edge = None
    worst_edge = None
    pnl_by_ticker = _group_pnl(rows, lambda t: t.underlying_symbol or "UNKNOWN")
    pnl_by_setup = _group_pnl(rows, lambda t: t.setup_type or "UNKNOWN")
    pnl_by_dte = _group_pnl(rows, lambda t: _bucket_dte(t.dte_at_entry))
    pnl_by_delta = _group_pnl(rows, lambda t: _bucket_delta(_safe_float(_json_loads(t.market_context_json, {}).get("entry", {}).get("value", {}).get("delta"))))
    pnl_by_weekday = _group_pnl(rows, lambda t: _market_day_label(t.opening_timestamp_utc))
    pnl_by_spread = _group_pnl(rows, lambda t: _bucket_spread(_safe_float(_json_loads(t.market_context_json, {}).get("entry", {}).get("value", {}).get("spread_pct"))))
    pnl_by_volume = _group_pnl(rows, lambda t: _bucket_volume(_safe_int(_json_loads(t.market_context_json, {}).get("entry", {}).get("value", {}).get("underlying_candle", {}).get("volume"))))

    if pnl_by_ticker:
        best_edge = pnl_by_ticker[0]["bucket"]
        worst_edge = pnl_by_ticker[-1]["bucket"]
    strongest_mistake = mistakes[0] if mistakes else None
    strongest_edge = strengths[0] if strengths else None
    strongest_repeatable_edge = best_edge if best_edge else "UNKNOWN"
    most_damaging_repeated_mistake = strongest_mistake["name"] if strongest_mistake else "UNKNOWN"
    dollars_lost_top_mistake = strongest_mistake["estimated_dollars_lost"] if strongest_mistake else 0.0

    def _subset(rows: list[dict[str, Any]], predicate) -> list[dict[str, Any]]:
        return [row for row in rows if predicate(row)]

    def _win_rate(rows: list[dict[str, Any]]) -> float | None:
        if not rows:
            return None
        winners = [row for row in rows if row["pnl"] > 0]
        return round((len(winners) / len(rows)) * 100.0, 2)

    def _avg_pnl(rows: list[dict[str, Any]]) -> float | None:
        if not rows:
            return None
        return round(sum(row["pnl"] for row in rows) / len(rows), 2)

    aligned_rows = _subset(flow_rows, lambda row: row["alignment"] == "aligned")
    conflicted_rows = _subset(flow_rows, lambda row: row["alignment"] == "conflicted")
    above_vwap_rows = _subset(flow_rows, lambda row: row["above_vwap"] is True)
    below_vwap_rows = _subset(flow_rows, lambda row: row["above_vwap"] is False)
    options_confirmed_rows = _subset(flow_rows, lambda row: (row["options_alignment_score"] or 0) > 0)
    options_conflicted_rows = _subset(flow_rows, lambda row: (row["options_alignment_score"] or 0) < 0)

    return {
        "top_strengths": strengths,
        "top_mistakes": mistakes,
        "money_flow_stats": {
            "win_rate_when_aligned_with_money_flow": _win_rate(aligned_rows),
            "win_rate_when_against_money_flow": _win_rate(conflicted_rows),
            "average_pnl_when_aligned_with_money_flow": _avg_pnl(aligned_rows),
            "average_pnl_when_against_money_flow": _avg_pnl(conflicted_rows),
            "performance_above_vwap": _avg_pnl(above_vwap_rows),
            "performance_below_vwap": _avg_pnl(below_vwap_rows),
            "performance_when_options_positioning_confirmed": _avg_pnl(options_confirmed_rows),
            "performance_when_options_positioning_conflicted": _avg_pnl(options_conflicted_rows),
            "sample_size": len(flow_rows),
        },
        "best_trading_conditions": {
            "ticker": best_edge,
            "setup": strongest_edge["name"] if strongest_edge else None,
            "basis": "best performing bucket(s) in the imported trade history",
        },
        "worst_trading_conditions": {
            "ticker": worst_edge,
            "mistake": strongest_mistake["name"] if strongest_mistake else None,
            "basis": "worst performing bucket(s) in the imported trade history",
        },
        "best_contract_profile": {
            "dte_bucket": pnl_by_dte[0]["bucket"] if pnl_by_dte else None,
            "delta_bucket": pnl_by_delta[0]["bucket"] if pnl_by_delta else None,
            "basis": "highest average realized PnL by contract bucket",
        },
        "worst_contract_profile": {
            "spread_bucket": pnl_by_spread[-1]["bucket"] if pnl_by_spread else None,
            "volume_bucket": pnl_by_volume[-1]["bucket"] if pnl_by_volume else None,
            "basis": "lowest average realized PnL by contract bucket",
        },
        "rules_that_would_have_prevented_losses": [
            "Skip wide spreads above 5% unless the edge is exceptional.",
            "Avoid trades with less than 7 DTE unless the setup is unusually strong.",
            "Do not average down into a losing thesis.",
            "Use confirmed direction and better liquidity before entry.",
        ],
        "estimated_dollars_lost_by_top_mistake": round(dollars_lost_top_mistake, 2),
        "strongest_repeatable_edge": strongest_repeatable_edge,
        "most_damaging_repeated_mistake": most_damaging_repeated_mistake,
        "pnl_by_ticker": pnl_by_ticker[:10],
        "pnl_by_setup": pnl_by_setup[:10],
        "pnl_by_dte_bucket": pnl_by_dte[:10],
        "pnl_by_delta_bucket": pnl_by_delta[:10],
        "pnl_by_weekday": pnl_by_weekday[:10],
        "pnl_by_spread_bucket": pnl_by_spread[:10],
        "pnl_by_volume_bucket": pnl_by_volume[:10],
    }


def build_improvement_plan(summary: dict[str, Any], patterns: dict[str, Any]) -> dict[str, Any]:
    avg_loss = abs(_safe_float(summary.get("average_loser"), 0.0) or 0.0)
    max_risk = round(max(avg_loss, abs(_safe_float(summary.get("largest_loss"), 0.0) or 0.0)), 2)
    return {
        "maximum_risk_per_trade": max_risk,
        "maximum_daily_loss": round(max_risk * 2.0, 2),
        "minimum_reward_to_risk": round(max(1.5, _safe_float(summary.get("win_loss_ratio"), 1.5) or 1.5), 2),
        "preferred_dte_range": "7-45 days",
        "preferred_delta_range": "0.35-0.55",
        "maximum_acceptable_bid_ask_spread": "5%",
        "minimum_volume_and_open_interest": "Volume 100+, open interest 100+",
        "confirmation_requirements": [
            "Wait for a 5-minute candle close through the trigger level.",
            "Require volume to hold above recent average.",
            "Do not enter if the quote is stale or the contract is not liquid.",
        ],
        "rules_against_averaging_down": [
            "Do not add to a losing trade without a fresh setup.",
            "If the original invalidation breaks, exit instead of scaling in.",
        ],
        "maximum_simultaneous_positions": 3,
        "pre_trade_checklist": [
            "Chart direction and contract direction match.",
            "Spread is within tolerance.",
            "Historical edge is at least slightly positive.",
            "Entry trigger and invalidation are defined.",
        ],
        "post_trade_review_checklist": [
            "Record whether you followed the trigger.",
            "Record whether the exit followed the plan.",
            "Note whether spread, DTE, or timing damaged the trade.",
        ],
        "basis": "Derived from the imported trade history, the top repeated mistakes, and the observed loss distribution.",
    }


def build_overview(db: Session, username: str, filters: dict[str, Any]) -> dict[str, Any]:
    selection = get_selection(db, username)
    accounts = reviewable_accounts(db)
    sync_run = serialize_sync_run(db, get_active_sync_run(db))
    selected_accounts, _ = _resolve_accounts_for_user(db, username)
    account_refs = [account.account_ref for account in selected_accounts]
    if not account_refs and selection.get("selection_mode") != "ALL":
        account_refs = ["__no_selection__"]
    if filters.get("account_ref"):
        account_refs = [str(filters["account_ref"])]
    if filters.get("account_refs"):
        account_refs = [str(ref).strip() for ref in filters["account_refs"] if str(ref).strip()]
    filters = dict(filters)
    filters["account_refs"] = account_refs
    summary = summarize_trades(db, filters)
    patterns = build_pattern_analysis(db, filters)
    improvement_plan = build_improvement_plan(summary, patterns)
    trade_rows = _trade_rows_query(db, filters)
    if filters.get("market_regime"):
        regime = str(filters["market_regime"]).upper()
        trade_rows = [trade for trade in trade_rows if _trade_row_to_payload(trade).get("market_regime") == regime]
    dte_bucket = filters.get("dte_bucket")
    if dte_bucket:
        trade_rows = [trade for trade in trade_rows if _bucket_dte(trade.dte_at_entry) == str(dte_bucket).upper()]
    if filters.get("limit") is not None:
        trade_rows = trade_rows[: int(filters["limit"])]

    unresolved_fills = (
        db.query(TradeReviewFill)
        .filter(TradeReviewFill.account_ref.in_(account_refs or [row.account_ref for row in accounts]))
        .filter(TradeReviewFill.match_status == "UNRESOLVED")
        .order_by(desc(TradeReviewFill.execution_timestamp_utc))
        .limit(20)
        .all()
    )
    unresolved_payload = [
        {
            "id": fill.id,
            "account_ref": fill.account_ref,
            "account_mask": fill.account_mask,
            "order_id": fill.order_id,
            "execution_id": fill.execution_id,
            "symbol": fill.occ_symbol or fill.option_symbol,
            "underlying_symbol": fill.underlying_symbol,
            "action": fill.action,
            "quantity": fill.quantity,
            "fill_price": fill.fill_price,
            "timestamp": fill.execution_timestamp_utc,
            "source_type": fill.source_type,
            "match_status": fill.match_status,
        }
        for fill in unresolved_fills
    ]
    return {
        "selection": selection,
        "accounts": [serialize_account_row(db, account, username) for account in accounts],
        "sync": serialize_sync_run(db, get_active_sync_run(db)),
        "summary": summary,
        "patterns": patterns,
        "improvement_plan": improvement_plan,
        "trades": [_trade_row_to_payload(trade) for trade in trade_rows],
        "trade_count": len(trade_rows),
        "unresolved_fills": unresolved_payload,
        "filters": filters,
        "data_version": _sha1(_json_dumps({"selection": selection, "filters": filters, "summary": summary, "trade_count": len(trade_rows)})),
    }


def _analysis_prompt(trade: dict[str, Any], overview: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "trade": trade,
        "summary": overview.get("summary") or {},
        "patterns": overview.get("patterns") or {},
        "improvement_plan": overview.get("improvement_plan") or {},
        "detail": detail,
    }


def _analysis_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "headline": {"type": "string"},
            "what_went_well": {"type": "array", "items": {"type": "string"}},
            "what_went_poorly": {"type": "array", "items": {"type": "string"}},
            "hard_truth": {"type": "string"},
            "single_most_important_lesson": {"type": "string"},
            "specific_rules": {"type": "array", "items": {"type": "string"}},
            "metrics_used": {"type": "array", "items": {"type": "string"}},
            "next_steps": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["headline", "what_went_well", "what_went_poorly", "hard_truth", "single_most_important_lesson", "specific_rules", "metrics_used", "next_steps"],
    }


def _trade_analysis_existing_cache(db: Session, trade: TradeReviewTrade) -> TradeReviewAnalysisCache | None:
    return (
        db.query(TradeReviewAnalysisCache)
        .filter(TradeReviewAnalysisCache.trade_id == trade.id)
        .filter(TradeReviewAnalysisCache.analysis_version == ANALYSIS_VERSION)
        .filter(TradeReviewAnalysisCache.data_version == trade.data_version)
        .first()
    )


def build_or_get_trade_analysis(db: Session, trade: TradeReviewTrade, overview: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    cached = _trade_analysis_existing_cache(db, trade)
    if cached:
        return {
            "status": "ok",
            "cached": True,
            "model": cached.model,
            "analysis": _json_loads(cached.analysis_json, {}),
            "blocking_reason": None,
        }

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return {
            "status": "unavailable",
            "cached": False,
            "model": None,
            "analysis": None,
            "blocking_reason": "OpenAI API key is missing on the backend",
        }

    model = _openai_model()
    payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": (
                    "You are reviewing a completed options trade for an admin user. "
                    "Use only the provided facts. Do not invent prices, fills, Greeks, or market conditions. "
                    "Summarize what actually happened, what went well, what went poorly, and the hard truth. "
                    "Give direct coaching and concrete rules. Return JSON only."
                ),
            },
            {"role": "user", "content": _json_dumps(_analysis_prompt(_trade_row_to_payload(trade), overview, detail))},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "trade_review_analysis",
                "strict": True,
                "schema": _analysis_schema(),
            }
        },
    }

    def _post_openai() -> requests.Response:
        response = requests.post(
            OPENAI_RESPONSES_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=_openai_timeout(),
        )
        response.raise_for_status()
        return response

    try:
        response = call_with_rate_limit("openai", None, "trade_review_analysis", _post_openai)
        response_payload = response.json()
        text = response_payload.get("output_text")
        if not text:
            text = ""
            for item in response_payload.get("output") or []:
                for content in item.get("content") or []:
                    if isinstance(content.get("text"), str):
                        text = content["text"]
                        break
        analysis = json.loads(text or "{}")
        cache_row = TradeReviewAnalysisCache(
            trade_id=trade.id,
            analysis_version=ANALYSIS_VERSION,
            data_version=trade.data_version,
            model=model,
            analysis_json=_json_dumps(analysis),
            created_at=now_iso(),
        )
        db.add(cache_row)
        db.commit()
        return {
            "status": "ok",
            "cached": False,
            "model": model,
            "analysis": analysis,
            "blocking_reason": None,
        }
    except Exception as exc:
        return {
            "status": "unavailable",
            "cached": False,
            "model": model,
            "analysis": None,
            "blocking_reason": f"Trade coaching unavailable: {exc}",
        }


def build_trade_detail(db: Session, trade_id: int, *, refresh_context: bool = False, include_analysis: bool = True) -> dict[str, Any]:
    trade = db.query(TradeReviewTrade).filter(TradeReviewTrade.id == trade_id).first()
    if not trade:
        raise ValueError("Trade not found")

    fills = (
        db.query(TradeReviewFill)
        .filter(TradeReviewFill.account_ref == trade.account_ref)
        .filter(TradeReviewFill.id.in_(_json_loads(trade.open_fill_ids_json, []) + _json_loads(trade.close_fill_ids_json, [])))
        .order_by(TradeReviewFill.execution_timestamp_utc.asc())
        .all()
    )
    open_fills = [fill for fill in fills if fill.id in set(_json_loads(trade.open_fill_ids_json, []))]
    close_fills = [fill for fill in fills if fill.id in set(_json_loads(trade.close_fill_ids_json, []))]
    market_context = _collect_market_context(db, trade, refresh=refresh_context)
    candle_pack = market_context.get("candles") or {}
    news_catalyst = build_news_catalyst_impact(
        trade.underlying_symbol or "",
        market_session={"session_state": "HISTORICAL", "actionable_live_quotes": False, "session_note": "Historical trade review."},
        indicator_data=candle_pack,
        historical=True,
        direction=trade.direction,
        entry_ts=trade.opening_timestamp_utc,
        exit_ts=trade.closing_timestamp_utc,
        expiration=trade.expiration,
        context_type="historical_trade",
    )
    mfe_mae = _trade_mfe_mae_series(trade, candle_pack)
    levels = _trade_levels(trade, candle_pack)
    grade_breakdown = _trade_grade_breakdown(trade.__dict__, market_context)
    explanation = _trade_explanatory_text(trade.__dict__, grade_breakdown)
    pattern_tags = _trade_pattern_tags(trade.__dict__, market_context)
    trade_payload = _trade_row_to_payload(trade)
    trade_payload.update(
        {
            "grade_breakdown": grade_breakdown,
            "what_went_well": explanation["what_went_well"],
            "what_went_poorly": explanation["what_went_poorly"],
            "hard_truth": explanation["hard_truth"],
            "should_have_been_skipped": explanation["should_have_been_skipped"],
            "better_entry": "Use a cleaner trigger and better liquidity before entering.",
            "better_invalidation": "Define the invalidation before entry and honor it.",
            "better_stop_plan": "Use a hard exit when the setup fails.",
            "better_contract_profile": "Prefer tighter spreads, adequate volume, and a better DTE buffer.",
            "better_exit_plan": "Take profits on the first target instead of waiting for the whole move.",
            "lesson": explanation["lesson"],
            "missing_data": explanation["missing_data"],
            "pattern_tags": pattern_tags,
            "market_context": market_context,
            "money_flow": (market_context.get("entry") or {}).get("money_flow"),
            "news_catalyst": news_catalyst,
            "levels": levels,
            "mfe": mfe_mae["mfe"],
            "mae": mfe_mae["mae"],
            "mfe_mae_source": mfe_mae["source"],
            "fills": [
                {
                    "id": fill.id,
                    "source_type": fill.source_type,
                    "source_record_id": fill.source_record_id,
                    "order_id": fill.order_id,
                    "execution_id": fill.execution_id,
                    "execution_timestamp_utc": fill.execution_timestamp_utc,
                    "execution_timestamp_et": fill.execution_timestamp_et,
                    "action": fill.action,
                    "quantity": fill.quantity,
                    "fill_price": fill.fill_price,
                    "commission": fill.commission,
                    "fees": fill.fees,
                    "net_cash_effect": fill.net_cash_effect,
                    "bid": fill.bid,
                    "ask": fill.ask,
                    "midpoint": fill.midpoint,
                    "spread_pct": fill.spread_pct,
                    "data_status": fill.data_status,
                    "confidence_level": fill.confidence_level,
                    "match_status": fill.match_status,
                }
                for fill in fills
            ],
            "chart": {
                **candle_pack,
                "markers": [
                    {
                        "time": int(_parse_iso(trade.opening_timestamp_utc).timestamp()) if _parse_iso(trade.opening_timestamp_utc) else None,
                        "position": "belowBar" if _safe_text(trade.direction).upper() == "LONG" else "aboveBar",
                        "color": "#16c784" if _safe_text(trade.direction).upper() == "LONG" else "#ef4444",
                        "shape": "arrowUp" if _safe_text(trade.direction).upper() == "LONG" else "arrowDown",
                        "text": "Entry",
                    },
                    {
                        "time": int(_parse_iso(trade.closing_timestamp_utc).timestamp()) if _parse_iso(trade.closing_timestamp_utc) else None,
                        "position": "aboveBar" if _safe_text(trade.direction).upper() == "LONG" else "belowBar",
                        "color": "#ef4444" if _safe_text(trade.direction).upper() == "LONG" else "#16c784",
                        "shape": "arrowDown" if _safe_text(trade.direction).upper() == "LONG" else "arrowUp",
                        "text": "Exit",
                    },
                    *[marker for marker in news_catalyst.get("news_markers", []) if marker and marker.get("time")],
                ],
                "price_levels": [
                    {"price": levels.get("support"), "label": "Support", "color": "#38bdf8"},
                    {"price": levels.get("resistance"), "label": "Resistance", "color": "#f0b90b"},
                    {"price": levels.get("stop"), "label": "Stop", "color": "#ef4444"},
                    {"price": levels.get("target_1"), "label": "Target 1", "color": "#16c784"},
                    {"price": levels.get("target_2"), "label": "Target 2", "color": "#22c55e"},
                    {"price": levels.get("stretch_target"), "label": "Stretch", "color": "#a78bfa"},
                ],
            },
        }
    )
    analysis = None
    if include_analysis:
        overview = build_overview(db, "admin", {"account_refs": [trade.account_ref], "limit": 25})
        analysis = build_or_get_trade_analysis(db, trade, overview, trade_payload)
    return {
        "trade": trade_payload,
        "analysis": analysis,
        "market_context": market_context,
    }


def list_trade_filters(db: Session, username: str) -> dict[str, Any]:
    selected, _ = _resolve_accounts_for_user(db, username)
    account_refs = [account.account_ref for account in selected]
    rows = (
        db.query(TradeReviewTrade)
        .filter(TradeReviewTrade.account_ref.in_(account_refs or [row.account_ref for row in db.query(TradeReviewAccount).all()]))
        .all()
    )
    return {
        "tickers": sorted({row.underlying_symbol for row in rows if row.underlying_symbol}),
        "setup_types": sorted({row.setup_type for row in rows if row.setup_type}),
        "grades": sorted({row.grade for row in rows if row.grade}),
        "dte_buckets": sorted({_bucket_dte(row.dte_at_entry) for row in rows}),
    }
