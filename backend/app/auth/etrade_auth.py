from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from requests_oauthlib import OAuth1Session

from ..config import config_manager


class ETradeAuth:
    REQUEST_TOKEN_PATH = Path(os.getenv("ETRADE_REQUEST_TOKEN_PATH", "/app/data/etrade_request_token.json"))
    ACCESS_TOKEN_PATH = Path(os.getenv("ETRADE_ACCESS_TOKEN_PATH", "/app/data/etrade_access_token.json"))

    def __init__(self) -> None:
        self.REQUEST_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.ACCESS_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)

    def _cfg(self) -> dict[str, Any]:
        return config_manager.config

    def _etrade_cfg(self) -> dict[str, Any]:
        return self._cfg().get("etrade", {})

    def enabled(self) -> bool:
        return bool(self._etrade_cfg().get("enabled", False))

    def sandbox(self) -> bool:
        env = os.getenv("ETRADE_SANDBOX")
        if env is not None:
            return env.strip().lower() in {"1", "true", "yes"}
        return bool(self._etrade_cfg().get("sandbox", False))

    def consumer_key(self) -> str:
        return os.getenv("ETRADE_CONSUMER_KEY", "").strip()

    def consumer_secret(self) -> str:
        return os.getenv("ETRADE_CONSUMER_SECRET", "").strip()

    def callback_url(self) -> str:
        return os.getenv("ETRADE_CALLBACK_URL", self._etrade_cfg().get("callback_url", "http://localhost:8000/api/auth/etrade/callback"))

    def configured(self) -> bool:
        return bool(self.consumer_key() and self.consumer_secret())

    def base_url(self) -> str:
        cfg = self._etrade_cfg()
        return cfg.get("base_url_sandbox") if self.sandbox() else cfg.get("base_url_live")

    def authorize_url_base(self) -> str:
        # E*TRADE OAuth authorization should be handled on us.etrade.com,
        # not the API host, to avoid broken authorize action endpoints.
        return os.getenv("ETRADE_AUTHORIZE_URL_BASE", "https://us.etrade.com/e/t/etws/authorize")

    def _read_json(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except Exception:
            return None

    def _write_json(self, path: Path, data: dict[str, Any]) -> None:
        path.write_text(json.dumps(data))

    def _delete(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass

    def get_access_token(self) -> dict[str, Any] | None:
        return self._read_json(self.ACCESS_TOKEN_PATH)

    def is_connected(self) -> bool:
        token = self.get_access_token() or {}
        return bool(token.get("oauth_token") and token.get("oauth_token_secret"))

    def status(self) -> dict[str, Any]:
        enabled = self.enabled()
        configured = self.configured()
        connected = self.is_connected() if configured else False

        message = "Connected" if connected else "Not connected"
        if not enabled:
            message = "E*TRADE disabled in config"
        elif not configured:
            message = "Missing E*TRADE credentials"

        return {
            "enabled": enabled,
            "configured": configured,
            "connected": connected,
            "sandbox": self.sandbox(),
            "message": message,
            "provider": "etrade",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def start_connect(self) -> dict[str, Any]:
        if not self.enabled():
            raise ValueError("E*TRADE is disabled in config")
        if not self.configured():
            raise ValueError("E*TRADE credentials are missing")

        token_url = f"{self.base_url()}/oauth/request_token"
        callback_mode = "web"
        callback_uri = self.callback_url()
        session = OAuth1Session(
            client_key=self.consumer_key(),
            client_secret=self.consumer_secret(),
            callback_uri=callback_uri,
        )
        try:
            fetch = session.fetch_request_token(token_url)
        except Exception as exc:
            # Some E*TRADE apps only accept out-of-band callbacks.
            if "oauth_acceptable_callback=oob" not in str(exc):
                raise
            callback_mode = "oob"
            callback_uri = "oob"
            session = OAuth1Session(
                client_key=self.consumer_key(),
                client_secret=self.consumer_secret(),
                callback_uri=callback_uri,
            )
            fetch = session.fetch_request_token(token_url)

        payload = {
            "oauth_token": fetch.get("oauth_token"),
            "oauth_token_secret": fetch.get("oauth_token_secret"),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "callback_mode": callback_mode,
            "callback_uri": callback_uri,
        }
        self._write_json(self.REQUEST_TOKEN_PATH, payload)

        auth_url = f"{self.authorize_url_base()}?{urlencode({'key': self.consumer_key(), 'token': fetch.get('oauth_token')})}"
        return {
            "url": auth_url,
            "callback_mode": callback_mode,
            "message": "Authorize in E*TRADE and paste verifier code into Settings." if callback_mode == "oob" else "Authorize in E*TRADE.",
        }

    def finish_connect(self, oauth_verifier: str) -> dict[str, Any]:
        req = self._read_json(self.REQUEST_TOKEN_PATH)
        if not req:
            raise ValueError("No pending E*TRADE request token. Start connect again.")

        oauth = OAuth1Session(
            client_key=self.consumer_key(),
            client_secret=self.consumer_secret(),
            resource_owner_key=req.get("oauth_token"),
            resource_owner_secret=req.get("oauth_token_secret"),
            verifier=oauth_verifier,
        )
        access = oauth.fetch_access_token(f"{self.base_url()}/oauth/access_token")

        token = {
            "oauth_token": access.get("oauth_token"),
            "oauth_token_secret": access.get("oauth_token_secret"),
            "token_type": "OAuth1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "sandbox": self.sandbox(),
        }
        self._write_json(self.ACCESS_TOKEN_PATH, token)
        self._delete(self.REQUEST_TOKEN_PATH)
        return token

    def disconnect(self) -> None:
        self._delete(self.ACCESS_TOKEN_PATH)
        self._delete(self.REQUEST_TOKEN_PATH)

    def signed_session(self) -> OAuth1Session:
        token = self.get_access_token() or {}
        if not token.get("oauth_token") or not token.get("oauth_token_secret"):
            raise ValueError("E*TRADE token not connected")

        return OAuth1Session(
            client_key=self.consumer_key(),
            client_secret=self.consumer_secret(),
            resource_owner_key=token["oauth_token"],
            resource_owner_secret=token["oauth_token_secret"],
        )


etrade_auth = ETradeAuth()
