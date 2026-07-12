from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy import inspect, text

from .config import config_manager
from .models import (
    PaperFill,
    PaperMigrationReview,
    PaperOrder,
    PaperPerformanceSnapshot,
    PaperPortfolio,
    PaperPortfolioEvent,
    PaperPosition,
    PaperPositionRiskState,
    PaperRecommendation,
    PaperRiskAuditEvent,
)
from .recommendation_performance import get_recommendation_performance, migrate_legacy_recommendations
from .risk_engine import evaluate_paper_portfolio


def ensure_paper_schema() -> None:
    """Add provenance columns to pre-existing risk tables without dropping data."""
    from .db import engine

    additions = {
        "paper_position_risk_states": {"paper_portfolio_id": "INTEGER"},
        "paper_risk_audit_events": {"paper_portfolio_id": "INTEGER"},
        "brokerage_accounts": {
            "account_equity": "FLOAT",
            "cash_balance": "FLOAT",
            "buying_power": "FLOAT",
        },
    }
    with engine.begin() as connection:
        tables = set(inspect(connection).get_table_names())
        for table, columns in additions.items():
            if table not in tables:
                continue
            existing = {column["name"] for column in inspect(connection).get_columns(table)}
            for name, definition in columns.items():
                if name not in existing:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
        return number if number == number and abs(number) != float("inf") else None
    except (TypeError, ValueError):
        return None


def ensure_default_portfolio(db: Session) -> PaperPortfolio:
    portfolio = db.query(PaperPortfolio).filter(PaperPortfolio.name == "Default Paper Challenge").first()
    if portfolio:
        return portfolio
    now = _now()
    portfolio = PaperPortfolio(
        name="Default Paper Challenge",
        starting_balance=100000.0,
        cash=100000.0,
        buying_power=100000.0,
        status="ACTIVE",
        created_at=now,
        updated_at=now,
    )
    db.add(portfolio)
    db.commit()
    return portfolio


def migrate_ambiguous_legacy_records(db: Session) -> int:
    """Keep old mixed risk rows out of both views and put them in review."""
    created = 0
    existing = {
        (row.source_table, row.source_record_id)
        for row in db.query(PaperMigrationReview).all()
    }
    for row in db.query(PaperRiskAuditEvent).all():
        key = ("paper_risk_audit_events", str(row.id))
        if key in existing:
            continue
        db.add(PaperMigrationReview(
            source_table=key[0],
            source_record_id=key[1],
            reason="Legacy risk event has no authoritative paper portfolio/source marker.",
            details_json=json.dumps({"position_id": row.position_id, "symbol": row.symbol, "event_type": row.event_type}),
        ))
        created += 1
    for row in db.query(PaperPositionRiskState).all():
        key = ("paper_position_risk_states", str(row.id))
        if key in existing:
            continue
        db.add(PaperMigrationReview(
            source_table=key[0],
            source_record_id=key[1],
            reason="Legacy paper risk state was previously evaluated from a mixed position source; provenance is ambiguous.",
            details_json=json.dumps({"position_id": row.position_id, "symbol": row.symbol}),
        ))
        created += 1
    if created:
        db.commit()
    return created


def _position_payload(row: PaperPosition) -> dict[str, Any]:
    payload = {}
    try:
        payload = json.loads(row.payload_json or "{}")
    except Exception:
        payload = {}
    return {
        **payload,
        "position_id": row.position_key,
        "paper_position_id": row.id,
        "paper_portfolio_id": row.paper_portfolio_id,
        "recommendation_id": row.recommendation_id,
        "symbol": row.symbol,
        "display_symbol": row.contract_symbol or row.symbol,
        "contract_symbol": row.contract_symbol,
        "direction": row.direction,
        "quantity": row.quantity,
        "entry_option_price": row.entry_price,
        "average_entry_price": row.entry_price,
        "bid": row.current_price,
        "ask": row.current_price,
        "last": row.current_price,
        "cost_basis": row.cost_basis,
        "market_value": row.market_value,
        "status": row.status,
        "model_version": row.model_version,
        "strategy_version": row.strategy_version,
        "simulated_fill_source": row.simulated_fill_source,
        "quote_timestamp": row.updated_at,
    }


def _serialize_order(row: PaperOrder) -> dict[str, Any]:
    return {
        "id": row.id,
        "paper_portfolio_id": row.paper_portfolio_id,
        "recommendation_id": row.recommendation_id,
        "order_id": row.order_id,
        "symbol": row.symbol,
        "contract_symbol": row.contract_symbol,
        "side": row.side,
        "quantity": row.quantity,
        "limit_price": row.limit_price,
        "status": row.status,
        "simulated_fill_source": row.simulated_fill_source,
        "model_version": row.model_version,
        "strategy_version": row.strategy_version,
        "created_at": row.created_at,
    }


def get_paper_portfolio(db: Session, market_session: dict[str, Any] | None = None) -> dict[str, Any]:
    portfolio = ensure_default_portfolio(db)
    migrate_legacy_recommendations(db)
    migrate_ambiguous_legacy_records(db)
    positions = db.query(PaperPosition).filter(PaperPosition.paper_portfolio_id == portfolio.id).filter(PaperPosition.status == "OPEN").all()
    payload_positions = [_position_payload(row) for row in positions]
    paper_risk = evaluate_paper_portfolio(payload_positions, market_session=market_session)
    performance = get_recommendation_performance(db)
    market_value = sum(_safe_float(row.market_value) or 0.0 for row in positions)
    equity = float(portfolio.cash or 0.0) + market_value
    unrealized = sum((_safe_float(row.market_value) or 0.0) - (_safe_float(row.cost_basis) or 0.0) for row in positions)
    portfolio.cash = float(portfolio.cash or 0.0)
    portfolio.buying_power = max(0.0, portfolio.cash)
    portfolio.updated_at = _now()
    db.add(PaperPerformanceSnapshot(
        paper_portfolio_id=portfolio.id,
        equity=equity,
        cash=portfolio.cash,
        realized_pnl=equity - float(portfolio.starting_balance or 100000.0) - unrealized,
        unrealized_pnl=unrealized,
        payload_json=json.dumps({"position_count": len(positions), "source": "paper_tables"}),
    ))
    db.commit()
    orders = db.query(PaperOrder).filter(PaperOrder.paper_portfolio_id == portfolio.id).order_by(PaperOrder.id.desc()).limit(100).all()
    fills = db.query(PaperFill).filter(PaperFill.paper_portfolio_id == portfolio.id).order_by(PaperFill.id.desc()).limit(100).all()
    curve_rows = db.query(PaperPerformanceSnapshot).filter(PaperPerformanceSnapshot.paper_portfolio_id == portfolio.id).order_by(PaperPerformanceSnapshot.created_at.desc()).limit(200).all()
    starting = float(portfolio.starting_balance or 100000.0)
    return {
        "status": "ok",
        "portfolio": {
            "paper_portfolio_id": portfolio.id,
            "name": portfolio.name,
            "starting_balance": portfolio.starting_balance,
            "cash": portfolio.cash,
            "buying_power": portfolio.buying_power,
            "equity": equity,
            "realized_pnl": equity - float(portfolio.starting_balance or 100000.0) - unrealized,
            "unrealized_pnl": unrealized,
            "position_count": len(positions),
            "return_pct": ((equity - starting) / starting * 100.0) if starting else None,
        },
        "positions": payload_positions,
        "orders": [_serialize_order(row) for row in orders],
        "fills": [{"fill_id": row.fill_id, "order_id": row.order_id, "symbol": row.symbol, "quantity": row.quantity, "fill_price": row.fill_price, "created_at": row.created_at, "simulated_fill_source": row.simulated_fill_source} for row in fills],
        "trade_history": [{"order_id": row.order_id, "symbol": row.symbol, "side": row.side, "quantity": row.quantity, "limit_price": row.limit_price, "status": row.status, "created_at": row.created_at} for row in orders],
        "equity_curve": [{"timestamp": row.created_at, "equity": row.equity, "cash": row.cash, "realized_pnl": row.realized_pnl, "unrealized_pnl": row.unrealized_pnl} for row in reversed(curve_rows)],
        "paper_risk": paper_risk,
        "recommendation_performance": performance,
        "source": "paper_tables_only",
    }


def create_paper_order(db: Session, payload: dict[str, Any], username: str) -> dict[str, Any]:
    forbidden = {"etrade_order_id", "broker_order_id", "account_id_key", "brokerage_account_id"}
    if forbidden.intersection(payload):
        raise ValueError("Paper orders cannot reference brokerage identifiers")
    portfolio = ensure_default_portfolio(db)
    recommendation_id = str(payload.get("recommendation_id") or "").strip() or None
    if recommendation_id:
        recommendation = db.query(PaperRecommendation).filter(PaperRecommendation.recommendation_id == recommendation_id).first()
        if not recommendation or recommendation.paper_portfolio_id != portfolio.id:
            raise ValueError("Recommendation is not part of the paper portfolio")
    symbol = str(payload.get("symbol") or "").strip().upper()
    if not symbol:
        raise ValueError("symbol is required")
    quantity = _safe_float(payload.get("quantity"))
    fill_price = _safe_float(payload.get("fill_price"))
    if not quantity or quantity <= 0 or not fill_price or fill_price <= 0:
        raise ValueError("positive quantity and fill_price are required")
    side = str(payload.get("side") or "BUY_TO_OPEN").upper()
    if side not in {"BUY_TO_OPEN", "SELL_TO_OPEN", "SELL_TO_CLOSE", "BUY_TO_CLOSE"}:
        raise ValueError("unsupported paper order side")
    notional = quantity * fill_price * 100.0
    if side in {"BUY_TO_OPEN", "BUY_TO_CLOSE"} and portfolio.cash < notional:
        raise ValueError("insufficient paper cash")
    now = _now()
    order_id = f"paper-{uuid.uuid4().hex}"
    fill_id = f"paper-fill-{uuid.uuid4().hex}"
    order = PaperOrder(
        paper_portfolio_id=portfolio.id,
        recommendation_id=recommendation_id,
        order_id=order_id,
        symbol=symbol,
        contract_symbol=payload.get("contract_symbol"),
        side=side,
        quantity=quantity,
        limit_price=fill_price,
        status="FILLED",
        simulated_fill_source=str(payload.get("simulated_fill_source") or "PAPER_SIMULATION"),
        model_version=payload.get("model_version"),
        strategy_version=payload.get("strategy_version") or "paper-v1",
        created_at=now,
    )
    fill = PaperFill(
        paper_portfolio_id=portfolio.id,
        recommendation_id=recommendation_id,
        order_id=order_id,
        fill_id=fill_id,
        symbol=symbol,
        quantity=quantity,
        fill_price=fill_price,
        simulated_fill_source=order.simulated_fill_source,
        model_version=order.model_version,
        strategy_version=order.strategy_version,
        created_at=now,
    )
    if side in {"BUY_TO_OPEN", "BUY_TO_CLOSE"}:
        portfolio.cash -= notional
    else:
        portfolio.cash += notional
    portfolio.buying_power = max(0.0, portfolio.cash)
    position_key = str(payload.get("position_key") or f"{symbol}:{payload.get('contract_symbol') or symbol}")
    position = db.query(PaperPosition).filter(PaperPosition.paper_portfolio_id == portfolio.id, PaperPosition.position_key == position_key).first()
    if side in {"BUY_TO_OPEN", "SELL_TO_OPEN"}:
        if position:
            position.quantity += quantity if side == "BUY_TO_OPEN" else -quantity
            position.current_price = fill_price
            position.market_value = abs(position.quantity) * fill_price * 100.0
            position.updated_at = now
        else:
            position = PaperPosition(
                paper_portfolio_id=portfolio.id,
                recommendation_id=recommendation_id,
                position_key=position_key,
                symbol=symbol,
                contract_symbol=payload.get("contract_symbol"),
                direction="LONG" if side == "BUY_TO_OPEN" else "SHORT",
                quantity=quantity,
                entry_price=fill_price,
                current_price=fill_price,
                cost_basis=notional,
                market_value=notional,
                status="OPEN",
                simulated_fill_source=order.simulated_fill_source,
                model_version=order.model_version,
                strategy_version=order.strategy_version,
                opened_at=now,
                updated_at=now,
            )
            db.add(position)
    elif position:
        position.quantity = max(0.0, position.quantity - quantity)
        position.current_price = fill_price
        position.market_value = position.quantity * fill_price * 100.0
        position.updated_at = now
        if position.quantity <= 0:
            position.status = "CLOSED"
            position.closed_at = now
    db.add(order)
    db.add(fill)
    db.add(PaperPortfolioEvent(
        paper_portfolio_id=portfolio.id,
        event_type="PAPER_ORDER_FILLED",
        recommendation_id=recommendation_id,
        details_json=json.dumps({"order_id": order_id, "fill_id": fill_id, "symbol": symbol, "side": side, "quantity": quantity, "fill_price": fill_price, "created_by": username}),
    ))
    db.commit()
    return {"ok": True, "order_id": order_id, "fill_id": fill_id, "source": "paper_tables_only"}
