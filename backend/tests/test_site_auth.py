from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from unittest.mock import patch

from fastapi.testclient import TestClient

TEST_ROOT = Path(tempfile.gettempdir()) / "indicator-dashboard-tests"
os.environ.setdefault("DATABASE_PATH", str(TEST_ROOT / "indicator.db"))
os.environ.setdefault("CONFIG_PATH", str(Path(__file__).resolve().parents[2] / "config" / "config.yml"))
os.environ.setdefault("CACHE_DIR", str(TEST_ROOT / "cache"))

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.main import app  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import AuthIpBlock, AuthLoginAttempt, AuthSession, AuthUser  # noqa: E402
from app.site_auth import site_auth  # noqa: E402


class SiteAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        self.ip_headers = {"x-forwarded-for": "127.0.0.1"}
        self.block_test_headers = {"x-forwarded-for": "8.8.8.8"}
        self._reset_state()
        self._ensure_user("admin", "admin-pass-123", role="admin", must_change_password=False)

    def tearDown(self) -> None:
        self.client.close()

    def _reset_state(self) -> None:
        with SessionLocal() as db:
            for model in (AuthSession, AuthLoginAttempt, AuthIpBlock, AuthUser):
                db.query(model).delete(synchronize_session=False)
            db.commit()

    def _ensure_user(self, username: str, password: str, *, role: str, must_change_password: bool) -> None:
        with SessionLocal() as db:
            site_auth.create_user(db, username=username, password=password, role=role, created_by="test", must_change_password=must_change_password)

    def test_blank_credentials_do_not_count_as_failed_attempts(self) -> None:
        response = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": ""},
            headers=self.ip_headers,
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("Username and password are required", response.text)

        with SessionLocal() as db:
            self.assertEqual(db.query(AuthLoginAttempt).count(), 0)
            self.assertEqual(db.query(AuthIpBlock).count(), 0)

    def test_allowlisted_ip_never_blocks_after_repeated_failures(self) -> None:
        with SessionLocal() as db:
            self.assertEqual(db.query(AuthIpBlock).count(), 0)

        for _ in range(3):
            response = self.client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "wrong-password"},
                headers=self.ip_headers,
            )
        self.assertEqual(response.status_code, 401, response.text)

        with SessionLocal() as db:
            self.assertEqual(db.query(AuthIpBlock).count(), 0)

    def test_ip_block_expires_and_allows_login_again(self) -> None:
        geo_payload = {
            "ip": "8.8.8.8",
            "private": False,
            "country_code": "US",
            "region_code": "TX",
            "region": "Texas",
            "city": "Austin",
            "source": "test",
            "allowed": True,
        }

        with patch("app.site_auth._lookup_geo", return_value=geo_payload):
            for _ in range(3):
                response = self.client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "wrong-password"},
                    headers=self.block_test_headers,
                )
            self.assertEqual(response.status_code, 403, response.text)
            self.assertIn("Login blocked after repeated failures", response.text)

            with SessionLocal() as db:
                self.assertEqual(db.query(AuthIpBlock).count(), 1)

            future = datetime.now(timezone.utc) + timedelta(minutes=16)
            with patch("app.site_auth._utcnow", return_value=future):
                response = self.client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "admin-pass-123"},
                    headers=self.block_test_headers,
                )

            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("access_token", response.json())

            with SessionLocal() as db:
                self.assertEqual(db.query(AuthIpBlock).count(), 0)


if __name__ == "__main__":
    unittest.main()
