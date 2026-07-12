from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
import sys

from fastapi.testclient import TestClient

TEST_ROOT = Path(tempfile.gettempdir()) / "indicator-dashboard-tests"
os.environ.setdefault("DATABASE_PATH", str(TEST_ROOT / "indicator.db"))
os.environ.setdefault("CONFIG_PATH", str(Path(__file__).resolve().parents[2] / "config" / "config.yml"))
os.environ.setdefault("ETRADE_REQUEST_TOKEN_PATH", str(TEST_ROOT / "etrade_request_token.json"))
os.environ.setdefault("ETRADE_ACCESS_TOKEN_PATH", str(TEST_ROOT / "etrade_access_token.json"))
os.environ.setdefault("CACHE_DIR", str(TEST_ROOT / "cache"))
os.environ.setdefault("OPENAI_API_KEY", "")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.main import app  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    AuthIpBlock,
    AuthLoginAttempt,
    AuthSession,
    AuthUser,
    Candle,
    TradeReviewAccount,
    TradeReviewAnalysisCache,
    TradeReviewAuditLog,
    TradeReviewFill,
    TradeReviewSelection,
    TradeReviewSyncRun,
    TradeReviewTrade,
)
from app.site_auth import site_auth  # noqa: E402
from app.trade_review import (  # noqa: E402
    _rebuild_trades_for_account,
    _upsert_fill,
    build_overview,
    build_trade_detail,
    parse_option_symbol,
)


class TradeReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        self.ip_headers = {"x-forwarded-for": "127.0.0.1"}
        os.environ["OPENAI_API_KEY"] = ""
        self._reset_state()
        self._ensure_user("admin", "admin-pass-123", role="admin", must_change_password=False)
        self._ensure_user("viewer", "viewer-pass-123", role="user", must_change_password=False)
        self.admin_headers = self._login("admin", "admin-pass-123")
        self.viewer_headers = self._login("viewer", "viewer-pass-123")

    def tearDown(self) -> None:
        self.client.close()

    def _reset_state(self) -> None:
        with SessionLocal() as db:
            for model in (
                TradeReviewAnalysisCache,
                TradeReviewTrade,
                TradeReviewFill,
                TradeReviewAccount,
                TradeReviewSelection,
                TradeReviewSyncRun,
                TradeReviewAuditLog,
                Candle,
                AuthSession,
                AuthLoginAttempt,
                AuthIpBlock,
            ):
                db.query(model).delete(synchronize_session=False)
            db.commit()

    def _ensure_user(self, username: str, password: str, *, role: str, must_change_password: bool) -> None:
        with SessionLocal() as db:
            user = db.query(AuthUser).filter(AuthUser.username == username).first()
            if user is None:
                site_auth.create_user(db, username=username, password=password, role=role, created_by="test", must_change_password=must_change_password)
                return
            site_auth.update_user(db, username=username, password=password, role=role, active=True)
            user = db.query(AuthUser).filter(AuthUser.username == username).first()
            user.must_change_password = must_change_password
            user.active = True
            db.commit()

    def _login(self, username: str, password: str) -> dict[str, str]:
        response = self.client.post(
            "/api/auth/login",
            json={"username": username, "password": password},
            headers=self.ip_headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        token = response.json()["access_token"]
        return {"Authorization": f"Bearer {token}", **self.ip_headers}

    def _add_account(self, account_ref: str = "acct_test_1", account_id_key: str = "12345678") -> TradeReviewAccount:
        with SessionLocal() as db:
            account = TradeReviewAccount(
                account_ref=account_ref,
                account_id_key=account_id_key,
                account_mask="****1234",
                account_desc="Brokerage",
                account_name="Primary",
                account_type="BROKERAGE",
                account_mode="INDIVIDUAL",
                institution_type="BROKERAGE",
                imported_at="2026-07-10T12:00:00+00:00",
                updated_at="2026-07-10T12:00:00+00:00",
            )
            db.add(account)
            db.commit()
            db.refresh(account)
            return account

    def _add_candle(self, symbol: str, timestamp: str, open_: float, high: float, low: float, close: float, volume: float, provider: str = "sqlite") -> None:
        ts = int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp())
        with SessionLocal() as db:
            db.add(
                Candle(
                    symbol=symbol,
                    interval="5m",
                    timestamp=ts,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                    provider=provider,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
            db.commit()

    def _base_fill(
        self,
        *,
        account: TradeReviewAccount,
        occ_symbol: str,
        action: str,
        qty: int,
        timestamp: str,
        fill_price: float,
        source_hash: str,
        order_id: str,
        execution_id: str,
        raw_payload_json: str = "{}",
        dte_at_entry: int = 10,
    ) -> dict[str, object]:
        return {
            "account_ref": account.account_ref,
            "account_id_key": account.account_id_key,
            "account_mask": account.account_mask,
            "source_type": "orders",
            "source_record_id": execution_id,
            "order_id": order_id,
            "execution_id": execution_id,
            "parent_order_id": None,
            "execution_timestamp_utc": timestamp,
            "execution_timestamp_et": timestamp,
            "underlying_symbol": "AAPL",
            "occ_symbol": occ_symbol,
            "option_symbol": occ_symbol,
            "call_put": "CALL",
            "strike": 210.0,
            "expiration": "2026-07-18",
            "dte_at_entry": dte_at_entry,
            "action": action,
            "quantity": qty,
            "fill_price": fill_price,
            "commission": 1.0,
            "fees": 0.5,
            "net_cash_effect": (-1 if action.startswith("buy") else 1) * fill_price * qty * 100.0,
            "bid": fill_price - 0.05,
            "ask": fill_price + 0.05,
            "midpoint": fill_price,
            "spread_pct": 4.0,
            "underlying_price": 211.25,
            "quote_source": "etrade",
            "data_status": "observed",
            "confidence_level": "HIGH",
            "match_status": "UNRESOLVED",
            "raw_payload_json": raw_payload_json,
            "source_hash": source_hash,
        }

    def test_non_admin_receives_403(self) -> None:
        response = self.client.get("/api/trade-review/overview", headers=self.viewer_headers)
        self.assertEqual(response.status_code, 403)
        self.assertIn("Admin access required", response.text)

    def test_account_selection_persists_and_secret_fields_are_not_returned(self) -> None:
        account_1 = self._add_account("acct_test_1", "12345678")
        account_2 = self._add_account("acct_test_2", "87654321")

        response = self.client.post(
            "/api/trade-review/accounts/selection",
            json={"selection_mode": "EXPLICIT", "account_refs": [account_1.account_ref]},
            headers=self.admin_headers,
        )
        self.assertEqual(response.status_code, 200, response.text)

        accounts_response = self.client.get("/api/trade-review/accounts", headers=self.admin_headers)
        self.assertEqual(accounts_response.status_code, 200, accounts_response.text)
        payload = accounts_response.json()
        self.assertEqual(payload["selection"]["selection_mode"], "EXPLICIT")
        self.assertEqual(payload["selection"]["selected_account_refs"], [account_1.account_ref])
        selected_flags = {row["account_ref"]: row["selected"] for row in payload["accounts"]}
        self.assertTrue(selected_flags[account_1.account_ref])
        self.assertFalse(selected_flags[account_2.account_ref])
        self.assertNotIn("account_id_key", payload["accounts"][0])
        self.assertNotIn("raw_payload_json", payload)

    def test_selection_and_sync_fail_closed_when_no_accounts_selected(self) -> None:
        response = self.client.post(
            "/api/trade-review/accounts/selection",
            json={"selection_mode": "EXPLICIT", "account_refs": []},
            headers=self.admin_headers,
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("Select at least one account", response.text)

        sync_response = self.client.post(
            "/api/trade-review/sync",
            json={"refresh_accounts": False},
            headers=self.admin_headers,
        )
        self.assertEqual(sync_response.status_code, 400, sync_response.text)
        self.assertIn("Select one or more accounts", sync_response.text)

    def test_option_symbol_parsing(self) -> None:
        parsed = parse_option_symbol("AAPL260718C00210000")
        self.assertEqual(parsed["underlying_symbol"], "AAPL")
        self.assertEqual(parsed["call_put"], "CALL")
        self.assertAlmostEqual(parsed["strike"], 210.0)
        self.assertEqual(parsed["expiration"], "2026-07-18")

    def test_duplicate_prevention_partial_fill_matching_and_trade_detail(self) -> None:
        account = self._add_account()
        self._add_candle("AAPL", "2026-07-10T14:25:00Z", 210.0, 211.5, 209.8, 211.0, 1200)
        self._add_candle("AAPL", "2026-07-10T14:30:00Z", 211.0, 212.0, 210.8, 211.6, 1400)
        self._add_candle("AAPL", "2026-07-10T14:35:00Z", 211.6, 213.2, 211.2, 212.8, 1600)
        self._add_candle("AAPL", "2026-07-10T14:40:00Z", 212.8, 214.1, 212.5, 213.7, 1700)
        self._add_candle("AAPL", "2026-07-10T15:00:00Z", 213.7, 214.8, 213.0, 214.2, 1800)

        fills = [
            self._base_fill(
                account=account,
                occ_symbol="AAPL260718C00210000",
                action="buy to open",
                qty=1,
                timestamp="2026-07-10T14:30:00+00:00",
                fill_price=1.00,
                source_hash="hash-open-1",
                order_id="order-1",
                execution_id="exec-1",
            ),
            self._base_fill(
                account=account,
                occ_symbol="AAPL260718C00210000",
                action="buy to open",
                qty=1,
                timestamp="2026-07-10T14:35:00+00:00",
                fill_price=1.20,
                source_hash="hash-open-2",
                order_id="order-2",
                execution_id="exec-2",
            ),
            self._base_fill(
                account=account,
                occ_symbol="AAPL260718C00210000",
                action="sell to close",
                qty=1,
                timestamp="2026-07-10T15:00:00+00:00",
                fill_price=1.50,
                source_hash="hash-close-1",
                order_id="order-3",
                execution_id="exec-3",
            ),
            self._base_fill(
                account=account,
                occ_symbol="AAPL260718C00210000",
                action="sell to close",
                qty=1,
                timestamp="2026-07-10T15:05:00+00:00",
                fill_price=1.60,
                source_hash="hash-close-2",
                order_id="order-4",
                execution_id="exec-4",
            ),
        ]

        with SessionLocal() as db:
            inserted_first = _upsert_fill(db, fills[0])
            self.assertTrue(inserted_first)
            db.commit()
            inserted_second = _upsert_fill(db, fills[0])
            self.assertFalse(inserted_second)
            for fill in fills[1:]:
                self.assertTrue(_upsert_fill(db, fill))
            db.commit()
            result = _rebuild_trades_for_account(db, account.account_ref)
            trade = db.query(TradeReviewTrade).filter(TradeReviewTrade.account_ref == account.account_ref).first()
            self.assertIsNotNone(trade)
            self.assertEqual(result["trades_reconstructed"], 1)
            self.assertEqual(result["unresolved_fills"], 0)
            self.assertEqual(trade.total_quantity, 2)
            self.assertAlmostEqual(float(trade.average_entry_price), 1.1, places=4)
            self.assertAlmostEqual(float(trade.average_exit_price), 1.55, places=4)
            self.assertAlmostEqual(float(trade.realized_pnl), 84.0, places=2)
            self.assertEqual(trade.status, "COMPLETE")
            self.assertEqual(trade.setup_type, "MULTI_LEG")
            self.assertEqual(trade.data_confidence_label, "HIGH")
            self.assertIn(trade.grade, {"A", "B", "C", "D", "F"})

        trade_id = trade.id

        with SessionLocal() as detail_db:
            detail = build_trade_detail(detail_db, trade_id, include_analysis=False)
        self.assertIn("trade", detail)
        self.assertIsNotNone(detail["trade"]["mfe"])
        self.assertIsNotNone(detail["trade"]["mae"])
        self.assertEqual(detail["trade"]["market_context"]["entry"]["value"]["underlying_candle"]["volume"], 1400.0)
        self.assertIn("vwap", detail["trade"]["market_context"]["entry"]["value"])

    def test_roll_assignment_exercise_expiration_and_unresolved_fill_behavior(self) -> None:
        account = self._add_account("acct_test_3", "33334444")

        def insert_trade_group(occ_symbol: str, timestamp_open: str, timestamp_close: str, raw_payload: str, expiration: str, action_close: str = "sell to close") -> None:
            open_fill = self._base_fill(
                account=account,
                occ_symbol=occ_symbol,
                action="buy to open",
                qty=1,
                timestamp=timestamp_open,
                fill_price=1.00,
                source_hash=f"{occ_symbol}-open",
                order_id=f"{occ_symbol}-order-open",
                execution_id=f"{occ_symbol}-exec-open",
                raw_payload_json=raw_payload,
            )
            close_fill = self._base_fill(
                account=account,
                occ_symbol=occ_symbol,
                action=action_close,
                qty=1,
                timestamp=timestamp_close,
                fill_price=1.40,
                source_hash=f"{occ_symbol}-close",
                order_id=f"{occ_symbol}-order-close",
                execution_id=f"{occ_symbol}-exec-close",
                raw_payload_json=raw_payload,
            )
            open_fill["expiration"] = expiration
            close_fill["expiration"] = expiration
            with SessionLocal() as db:
                self.assertTrue(_upsert_fill(db, open_fill))
                self.assertTrue(_upsert_fill(db, close_fill))
                db.commit()

        insert_trade_group("AAPL260710C00205000", "2026-07-01T14:30:00+00:00", "2026-07-02T14:30:00+00:00", "{\"note\":\"roll\"}", "2026-07-01")
        insert_trade_group("AAPL260710P00205000", "2026-07-03T14:30:00+00:00", "2026-07-03T16:00:00+00:00", "{\"note\":\"assignment\"}", "2026-07-10")
        insert_trade_group("AAPL260710P00200000", "2026-07-04T14:30:00+00:00", "2026-07-04T16:00:00+00:00", "{\"note\":\"exercise\"}", "2026-07-10")
        insert_trade_group("AAPL260701C00190000", "2026-06-30T14:30:00+00:00", "2026-07-02T14:30:00+00:00", "{\"note\":\"expired\"}", "2026-07-01")

        with SessionLocal() as db:
            result = _rebuild_trades_for_account(db, account.account_ref)
            trades = db.query(TradeReviewTrade).filter(TradeReviewTrade.account_ref == account.account_ref).all()
            by_symbol = {trade.occ_symbol: trade for trade in trades}
            self.assertEqual(by_symbol["AAPL260710C00205000"].setup_type, "ROLL")
            self.assertEqual(by_symbol["AAPL260710P00205000"].setup_type, "ASSIGNMENT")
            self.assertEqual(by_symbol["AAPL260710P00205000"].assignment_outcome, "ASSIGNED")
            self.assertEqual(by_symbol["AAPL260710P00200000"].setup_type, "EXERCISE")
            self.assertEqual(by_symbol["AAPL260710P00200000"].exercise_outcome, "EXERCISED")
            self.assertEqual(by_symbol["AAPL260701C00190000"].expiration_outcome, "EXPIRED")
            self.assertGreaterEqual(result["trades_reconstructed"], 4)

            unresolved_fill = self._base_fill(
                account=account,
                occ_symbol="AAPL260718C00220000",
                action="journal",
                qty=1,
                timestamp="2026-07-10T16:00:00+00:00",
                fill_price=0.25,
                source_hash="hash-unresolved",
                order_id="order-unresolved",
                execution_id="exec-unresolved",
            )
            self.assertTrue(_upsert_fill(db, unresolved_fill))
            db.commit()
            result = _rebuild_trades_for_account(db, account.account_ref)
            unresolved = db.query(TradeReviewFill).filter(TradeReviewFill.source_hash == "hash-unresolved").first()
            self.assertEqual(unresolved.match_status, "UNRESOLVED")
            self.assertGreaterEqual(result["unresolved_fills"], 1)

    def test_overview_and_detail_payloads_hide_backend_secrets(self) -> None:
        account = self._add_account("acct_test_4", "44445555")
        with SessionLocal() as db:
            db.add(
                TradeReviewSelection(
                    username="admin",
                    selection_mode="ALL",
                    selected_account_refs="[]",
                    created_at="2026-07-10T12:00:00+00:00",
                    updated_at="2026-07-10T12:00:00+00:00",
                )
            )
            db.commit()
            overview = build_overview(db, "admin", {"account_refs": [account.account_ref], "limit": 25})
            self.assertIn("accounts", overview)
            self.assertNotIn("account_id_key", str(overview))
            self.assertNotIn("raw_payload_json", str(overview))

        # No trades yet, but the endpoint should still authorize and not leak backend-only fields.
        response = self.client.get("/api/trade-review/overview", headers=self.admin_headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("account_id_key", response.text)
        self.assertNotIn("raw_payload_json", response.text)


if __name__ == "__main__":
    unittest.main()
