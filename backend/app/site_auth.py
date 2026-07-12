from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from fastapi import HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from .config import config_manager
from .db import SessionLocal
from .models import AuthIpBlock, AuthLoginAttempt, AuthSession, AuthUser


SESSION_TTL_HOURS = 12
FAILED_LOGIN_WINDOW_MINUTES = 15
FAILED_LOGIN_BLOCK_THRESHOLD = 3
GEO_CACHE_TTL_SECONDS = 3600
DEFAULT_SEED_USERS = (
    {"username": "admin", "role": "admin"},
    {"username": "brant", "role": "user"},
    {"username": "nik", "role": "user"},
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _login_block_minutes() -> int:
    try:
        value = config_manager.get("security", "login_block_minutes", default=15)
        return max(1, int(value or 15))
    except Exception:
        return 15


def _normalize_username(username: str) -> str:
    return str(username or "").strip().lower()


def _hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    iterations = int(os.getenv("AUTH_PBKDF2_ITERATIONS", "310000"))
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${digest.hex()}"


def _verify_password(password: str, stored_hash: str) -> bool:
    try:
        algo, iterations_text, salt, digest = stored_hash.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations).hex()
        return hmac.compare_digest(candidate, digest)
    except Exception:
        return False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_private_ip(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_private or ipaddress.ip_address(ip).is_loopback or ipaddress.ip_address(ip).is_reserved
    except Exception:
        return False


def _client_ip_from_request(request: Request) -> str:
    header_chain = request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip") or request.headers.get("cf-connecting-ip")
    if header_chain:
        ip = header_chain.split(",")[0].strip()
        if ip:
            return ip
    client = request.client.host if request.client else ""
    return client or "0.0.0.0"


_GEO_CACHE: dict[str, dict[str, Any]] = {}


def _configured_ip_allowlist() -> tuple[set[str], list[ipaddress._BaseNetwork]]:
    security_cfg = config_manager.get("security", default={}) or {}
    raw_ips = list(security_cfg.get("allowed_ips") or [])
    raw_cidrs = list(security_cfg.get("allowed_cidrs") or [])

    env_ips = [item.strip() for item in os.getenv("AUTH_WHITELIST_IPS", "").split(",") if item.strip()]
    env_cidrs = [item.strip() for item in os.getenv("AUTH_WHITELIST_CIDRS", "").split(",") if item.strip()]

    ip_values = {str(item).strip() for item in (*raw_ips, *env_ips) if str(item).strip()}
    cidr_values: list[ipaddress._BaseNetwork] = []
    for item in (*raw_cidrs, *env_cidrs):
        text = str(item).strip()
        if not text:
            continue
        try:
            cidr_values.append(ipaddress.ip_network(text, strict=False))
        except Exception:
            try:
                cidr_values.append(ipaddress.ip_network(f"{text}/32", strict=False))
            except Exception:
                continue

    # Never block local operators or internal container traffic.
    ip_values.update({"127.0.0.1", "::1", "localhost"})
    return ip_values, cidr_values


def _ip_is_allowlisted(ip: str | None) -> bool:
    text = str(ip or "").strip()
    if not text:
        return False
    ip_values, cidrs = _configured_ip_allowlist()
    if text in ip_values:
        return True
    try:
        parsed = ipaddress.ip_address(text)
    except Exception:
        return False
    if parsed.is_private or parsed.is_loopback or parsed.is_reserved:
        return True
    return any(parsed in network for network in cidrs)


def _normalize_geo_payload(ip: str, *, source: str, data: dict[str, Any]) -> dict[str, Any]:
    country_code = str(data.get("country_code") or data.get("country") or "").upper().strip()
    region = str(data.get("region") or data.get("state") or data.get("regionName") or "").strip()
    region_code = str(data.get("region_code") or data.get("regionCode") or data.get("state_code") or data.get("stateCode") or "").upper().strip()
    city = str(data.get("city") or "").strip()

    if country_code == "US" and not region_code and region.lower() == "texas":
        region_code = "TX"

    return {
        "ip": ip,
        "private": False,
        "country_code": country_code,
        "region_code": region_code,
        "region": region,
        "city": city,
        "source": source,
    }


def _lookup_geo_ipwhois(ip: str, timeout: float) -> dict[str, Any]:
    response = requests.get(f"https://ipwho.is/{ip}", timeout=timeout, headers={"Accept": "application/json"})
    response.raise_for_status()
    data = response.json() if response.content else {}
    if not data.get("success", True):
        raise RuntimeError(str(data.get("message") or "ipwho.is lookup failed"))
    return _normalize_geo_payload(ip, source="ipwho.is", data=data)


def _lookup_geo_ipinfo(ip: str, timeout: float) -> dict[str, Any]:
    response = requests.get(f"https://ipinfo.io/{ip}/json", timeout=timeout, headers={"Accept": "application/json"})
    response.raise_for_status()
    data = response.json() if response.content else {}
    return _normalize_geo_payload(ip, source="ipinfo.io", data=data)


def _lookup_geo_ipapi(ip: str, timeout: float) -> dict[str, Any]:
    response = requests.get(f"https://ipapi.co/{ip}/json/", timeout=timeout, headers={"Accept": "application/json"})
    response.raise_for_status()
    data = response.json() if response.content else {}
    if data.get("error") is True:
        raise RuntimeError(str(data.get("message") or data.get("reason") or "ipapi.co lookup failed"))
    return _normalize_geo_payload(ip, source="ipapi.co", data=data)


def _lookup_geo(ip: str) -> dict[str, Any]:
    now = time.time()
    cached = _GEO_CACHE.get(ip)
    if cached and cached.get("expires_at", 0) > now:
        return cached["payload"]

    if _ip_is_allowlisted(ip):
        try:
            parsed_ip = ipaddress.ip_address(ip)
            private = parsed_ip.is_private or parsed_ip.is_loopback or parsed_ip.is_reserved
        except Exception:
            private = False
        payload = {
            "ip": ip,
            "private": private,
            "country_code": "US",
            "region_code": "TX",
            "region": "Texas",
            "city": "",
            "source": "ip_whitelist",
            "allowed": True,
        }
        _GEO_CACHE[ip] = {"payload": payload, "expires_at": now + GEO_CACHE_TTL_SECONDS}
        return payload

    if _is_private_ip(ip):
        payload = {
            "ip": ip,
            "private": True,
            "country_code": "US",
            "region_code": "TX",
            "region": "Texas",
            "allowed": True,
            "source": "private_network",
        }
        _GEO_CACHE[ip] = {"payload": payload, "expires_at": now + GEO_CACHE_TTL_SECONDS}
        return payload

    timeout = float(config_manager.get("security", "geo_lookup_timeout_seconds", default=2) or 2)
    providers = (_lookup_geo_ipwhois, _lookup_geo_ipinfo, _lookup_geo_ipapi)
    checked: list[dict[str, Any]] = []
    lookup_errors: list[dict[str, Any]] = []
    best_payload: dict[str, Any] | None = None
    provider_labels = {
        _lookup_geo_ipwhois: "ipwho.is",
        _lookup_geo_ipinfo: "ipinfo.io",
        _lookup_geo_ipapi: "ipapi.co",
    }

    for provider in providers:
        provider_name = provider_labels.get(provider, provider.__name__)
        try:
            payload = provider(ip, timeout)
            checked.append(
                {
                    "source": payload.get("source", provider_name),
                    "country_code": payload.get("country_code") or "",
                    "region_code": payload.get("region_code") or "",
                    "region": payload.get("region") or "",
                }
            )
            if payload.get("country_code") == "US" and (payload.get("region_code") == "TX" or str(payload.get("region") or "").strip().lower() == "texas"):
                payload["region_code"] = "TX"
                payload["allowed"] = True
                payload["providers_checked"] = checked
                _GEO_CACHE[ip] = {"payload": payload, "expires_at": now + GEO_CACHE_TTL_SECONDS}
                return payload
            if best_payload is None:
                best_payload = payload
        except Exception as exc:
            message = str(exc)
            lookup_errors.append({"source": provider_name, "error": message})
            checked.append({"source": provider_name, "error": message})
            continue

    payload = best_payload or {
        "ip": ip,
        "private": False,
        "country_code": "",
        "region_code": "",
        "region": "",
        "city": "",
        "source": "lookup_failed",
    }
    payload["allowed"] = False
    payload["providers_checked"] = checked
    if lookup_errors:
        payload["lookup_errors"] = lookup_errors
        if all("rate" in str(item.get("error", "")).lower() or "429" in str(item.get("error", "")) for item in lookup_errors):
            payload["reason"] = "Geolocation provider rate limit reached. Try again shortly."
        else:
            payload["reason"] = "Unable to verify Texas location from available geo providers."
    else:
        payload["reason"] = "Access is restricted to Texas, United States."
    _GEO_CACHE[ip] = {"payload": payload, "expires_at": now + GEO_CACHE_TTL_SECONDS}
    return payload


def _session_expiry() -> datetime:
    return _utcnow() + timedelta(hours=SESSION_TTL_HOURS)


def _session_payload(user: AuthUser, token: str) -> dict[str, Any]:
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_at": _session_expiry().isoformat(),
        "user": {
            "username": user.username,
            "role": user.role,
            "must_change_password": bool(getattr(user, "must_change_password", False)),
        },
    }


def _ensure_active_user(user: AuthUser) -> None:
    if not user.active:
        raise HTTPException(status_code=403, detail="User account is disabled")


def _count_recent_failures(db: Session, ip: str) -> int:
    cutoff = (_utcnow() - timedelta(minutes=FAILED_LOGIN_WINDOW_MINUTES)).isoformat()
    return (
        db.query(AuthLoginAttempt)
        .filter(AuthLoginAttempt.ip_address == ip)
        .filter(AuthLoginAttempt.success.is_(False))
        .filter(AuthLoginAttempt.created_at >= cutoff)
        .count()
    )


def _block_ip(db: Session, ip: str, reason: str, fail_count: int) -> None:
    if _ip_is_allowlisted(ip):
        row = db.query(AuthIpBlock).filter(AuthIpBlock.ip_address == ip).first()
        if row:
            db.delete(row)
            db.commit()
        return
    row = db.query(AuthIpBlock).filter(AuthIpBlock.ip_address == ip).first()
    now = _now_iso()
    if row is None:
        row = AuthIpBlock(
            ip_address=ip,
            reason=reason,
            fail_count=fail_count,
            blocked_at=now,
            updated_at=now,
            last_failed_at=now,
        )
        db.add(row)
    else:
        row.reason = reason
        row.fail_count = fail_count
        row.updated_at = now
        row.last_failed_at = now
    db.commit()


def _record_login_attempt(db: Session, ip: str, username: str | None, success: bool, reason: str | None) -> int:
    db.add(
        AuthLoginAttempt(
            ip_address=ip,
            username=username,
            success=success,
            reason=reason,
            created_at=_now_iso(),
        )
    )
    db.commit()
    failures = _count_recent_failures(db, ip)
    if not success and failures >= FAILED_LOGIN_BLOCK_THRESHOLD:
        _block_ip(db, ip, f"Failed login threshold reached ({failures})", failures)
    return failures


def _ensure_not_blocked(db: Session, ip: str) -> AuthIpBlock | None:
    if _ip_is_allowlisted(ip):
        row = db.query(AuthIpBlock).filter(AuthIpBlock.ip_address == ip).first()
        if row:
            db.delete(row)
            db.commit()
        return None
    row = db.query(AuthIpBlock).filter(AuthIpBlock.ip_address == ip).first()
    if not row:
        return None
    last_failed = _parse_iso(row.last_failed_at or row.blocked_at)
    if last_failed is not None:
        expires_at = last_failed + timedelta(minutes=_login_block_minutes())
        if expires_at <= _utcnow():
            db.delete(row)
            db.commit()
            return None
    return row


def _blocked_ip_payload(row: AuthIpBlock) -> dict[str, Any]:
    payload = {
        "ip_address": row.ip_address,
        "reason": row.reason,
        "fail_count": row.fail_count,
        "blocked_at": row.blocked_at,
        "updated_at": row.updated_at,
        "last_failed_at": row.last_failed_at,
        "blocked_until": None,
        "remaining_seconds": None,
    }
    last_failed = _parse_iso(row.last_failed_at or row.blocked_at)
    if last_failed is not None:
        blocked_until = last_failed + timedelta(minutes=_login_block_minutes())
        remaining = int((blocked_until - _utcnow()).total_seconds())
        if remaining < 0:
            remaining = 0
        payload["blocked_until"] = blocked_until.isoformat()
        payload["remaining_seconds"] = remaining
        if remaining > 0:
            remaining_minutes = max(1, (remaining + 59) // 60)
            payload["reason"] = f"{row.reason} Try again in about {remaining_minutes} minute(s)."
    return payload


def _cleanup_expired_sessions(db: Session) -> None:
    expired = _utcnow().isoformat()
    rows = (
        db.query(AuthSession)
        .filter(AuthSession.revoked_at.is_(None))
        .filter(AuthSession.expires_at < expired)
        .all()
    )
    for row in rows:
        row.revoked_at = _now_iso()
    if rows:
        db.commit()


def _get_token_from_request(request: Request) -> str | None:
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        if token:
            return token
    token = request.headers.get("x-auth-token", "").strip()
    return token or None


@dataclass
class CurrentAuth:
    user: AuthUser
    session: AuthSession


class SiteAuth:
    def client_ip(self, request: Request | None) -> str:
        if request is None:
            return "0.0.0.0"
        return _client_ip_from_request(request)

    def ensure_schema(self, db: Session) -> None:
        columns = {
            str(row["name"] or "")
            for row in db.execute(text("PRAGMA table_info(auth_users)")).mappings().all()
        }
        if "must_change_password" not in columns:
            db.execute(text("ALTER TABLE auth_users ADD COLUMN must_change_password BOOLEAN NOT NULL DEFAULT 0"))
            db.commit()

    def seed_default_users(self, db: Session) -> list[str]:
        initial_password = os.getenv("AUTH_INITIAL_PASSWORD", "").strip()
        if not initial_password:
            return []
        created: list[str] = []
        for seed in DEFAULT_SEED_USERS:
            username = _normalize_username(seed["username"])
            if db.query(AuthUser).filter(AuthUser.username == username).first():
                continue
            user = AuthUser(
                username=username,
                password_hash=_hash_password(initial_password),
                role=str(seed["role"]),
                active=True,
                must_change_password=True,
                created_at=_now_iso(),
                updated_at=_now_iso(),
            )
            db.add(user)
            created.append(username)
        if created:
            db.commit()
        return created

    def public_status(self, db: Session, request: Request | None = None) -> dict[str, Any]:
        user_count = db.query(AuthUser).count()
        ip = self.client_ip(request) if request else None
        blocked = None
        blocked_payload = None
        geo = None
        if ip:
            blocked = _ensure_not_blocked(db, ip)
            if blocked is not None:
                blocked_payload = _blocked_ip_payload(blocked)
            else:
                geo = _lookup_geo(ip)
        return {
            "authenticated": False,
            "setup_required": user_count == 0,
            "user_count": user_count,
            "ip_address": ip,
            "ip_whitelisted": bool(ip and _ip_is_allowlisted(ip)),
            "geo": geo,
            "ip_blocked": bool(blocked),
            "ip_block_reason": blocked_payload["reason"] if blocked_payload else None,
            "ip_blocked_until": blocked_payload["blocked_until"] if blocked_payload else None,
            "ip_block_remaining_seconds": blocked_payload["remaining_seconds"] if blocked_payload else None,
        }

    def status(self, db: Session, request: Request | None = None, current: CurrentAuth | None = None) -> dict[str, Any]:
        payload = self.public_status(db, request)
        payload["authenticated"] = current is not None
        if current:
            payload["user"] = {
                "username": current.user.username,
                "role": current.user.role,
                "last_login_at": current.user.last_login_at,
                "must_change_password": bool(getattr(current.user, "must_change_password", False)),
            }
            payload["expires_at"] = current.session.expires_at
        return payload

    def check_geo(self, request: Request) -> dict[str, Any]:
        ip = self.client_ip(request)
        if _ip_is_allowlisted(ip):
            return _lookup_geo(ip)
        geo = _lookup_geo(ip)
        strict = bool(config_manager.get("security", "strict_texas_only", default=True))
        if not strict:
            geo["allowed"] = True
            return geo
        allow_private = bool(config_manager.get("security", "allow_private_networks", default=True))
        if geo.get("private") and allow_private:
            geo["allowed"] = True
            return geo
        geo["allowed"] = geo.get("country_code") == "US" and geo.get("region_code") == "TX"
        if not geo["allowed"]:
            geo["reason"] = "Access is restricted to Texas, United States."
        return geo

    def enforce_geo(self, request: Request) -> dict[str, Any]:
        geo = self.check_geo(request)
        if not geo.get("allowed"):
            raise HTTPException(status_code=403, detail=geo.get("reason") or "Access is restricted to Texas, United States.")
        return geo

    def bootstrap_user(self, db: Session, setup_key: str, username: str, password: str, role: str = "admin") -> dict[str, Any]:
        if db.query(AuthUser).count() > 0:
            raise HTTPException(status_code=400, detail="Users already exist")
        expected = os.getenv("AUTH_BOOTSTRAP_TOKEN", "").strip()
        if expected and not hmac.compare_digest(setup_key.strip(), expected):
            raise HTTPException(status_code=403, detail="Invalid setup key")
        return self.create_user(db, username=username, password=password, role=role, created_by="bootstrap", must_change_password=True)

    def create_user(self, db: Session, username: str, password: str, role: str = "user", created_by: str | None = None, must_change_password: bool = False) -> dict[str, Any]:
        normalized = _normalize_username(username)
        if not normalized:
            raise HTTPException(status_code=400, detail="Username is required")
        if len(password or "") < 10:
            raise HTTPException(status_code=400, detail="Password must be at least 10 characters")
        if role not in {"admin", "user"}:
            raise HTTPException(status_code=400, detail="Invalid role")
        if db.query(AuthUser).filter(AuthUser.username == normalized).first():
            raise HTTPException(status_code=400, detail="Username already exists")
        user = AuthUser(
            username=normalized,
            password_hash=_hash_password(password),
            role=role,
            active=True,
            must_change_password=must_change_password,
            created_at=_now_iso(),
            updated_at=_now_iso(),
        )
        db.add(user)
        db.commit()
        return self.user_payload(user, created_by=created_by)

    def user_payload(self, user: AuthUser, created_by: str | None = None) -> dict[str, Any]:
        return {
            "username": user.username,
            "role": user.role,
            "active": bool(user.active),
            "must_change_password": bool(getattr(user, "must_change_password", False)),
            "created_at": user.created_at,
            "updated_at": user.updated_at,
            "last_login_at": user.last_login_at,
            "created_by": created_by,
        }

    def list_users(self, db: Session) -> list[dict[str, Any]]:
        rows = db.query(AuthUser).order_by(AuthUser.username.asc()).all()
        return [self.user_payload(row) for row in rows]

    def update_user(self, db: Session, username: str, *, password: str | None = None, role: str | None = None, active: bool | None = None) -> dict[str, Any]:
        normalized = _normalize_username(username)
        user = db.query(AuthUser).filter(AuthUser.username == normalized).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        if password:
            if len(password) < 10:
                raise HTTPException(status_code=400, detail="Password must be at least 10 characters")
            user.password_hash = _hash_password(password)
        if role:
            if role not in {"admin", "user"}:
                raise HTTPException(status_code=400, detail="Invalid role")
            user.role = role
        if active is not None:
            user.active = bool(active)
        user.updated_at = _now_iso()
        db.commit()
        return self.user_payload(user)

    def login(self, db: Session, request: Request, username: str, password: str) -> dict[str, Any]:
        ip = self.client_ip(request)
        allowlisted_ip = _ip_is_allowlisted(ip)
        blocked = _ensure_not_blocked(db, ip)
        if blocked:
            blocked_payload = _blocked_ip_payload(blocked)
            raise HTTPException(status_code=403, detail=f"Access blocked for this IP: {blocked_payload['reason']}")
        self.enforce_geo(request)
        normalized = _normalize_username(username)
        password_value = str(password or "").strip()
        if not normalized or not password_value:
            raise HTTPException(status_code=400, detail="Username and password are required")
        user = db.query(AuthUser).filter(AuthUser.username == normalized).first()
        if not user or not user.active or not _verify_password(password_value, user.password_hash):
            _record_login_attempt(db, ip, normalized or None, False, "Invalid username or password")
            failures = _count_recent_failures(db, ip)
            if failures >= FAILED_LOGIN_BLOCK_THRESHOLD and not allowlisted_ip:
                raise HTTPException(status_code=403, detail="Login blocked after repeated failures from this IP")
            raise HTTPException(status_code=401, detail="Invalid username or password")
        session_token = secrets.token_urlsafe(32)
        token_hash = _hash_token(session_token)
        expires_at = (_utcnow() + timedelta(hours=SESSION_TTL_HOURS)).isoformat()
        db.add(
            AuthSession(
                username=user.username,
                token_hash=token_hash,
                ip_address=ip,
                user_agent=request.headers.get("user-agent"),
                created_at=_now_iso(),
                last_seen_at=_now_iso(),
                expires_at=expires_at,
                revoked_at=None,
            )
        )
        user.last_login_at = _now_iso()
        user.updated_at = _now_iso()
        db.commit()
        _record_login_attempt(db, ip, user.username, True, "Login successful")
        return _session_payload(user, session_token)

    def change_password(self, db: Session, current: CurrentAuth, new_password: str) -> dict[str, Any]:
        _ensure_active_user(current.user)
        if len(new_password or "") < 10:
            raise HTTPException(status_code=400, detail="Password must be at least 10 characters")
        user = db.query(AuthUser).filter(AuthUser.username == current.user.username).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        user.password_hash = _hash_password(new_password)
        user.must_change_password = False
        user.updated_at = _now_iso()
        other_sessions = (
            db.query(AuthSession)
            .filter(AuthSession.username == user.username)
            .filter(AuthSession.token_hash != current.session.token_hash)
            .filter(AuthSession.revoked_at.is_(None))
            .all()
        )
        for session in other_sessions:
            session.revoked_at = _now_iso()
        db.commit()
        return self.user_payload(user)

    def revoke_token(self, db: Session, token: str) -> None:
        token_hash = _hash_token(token)
        row = db.query(AuthSession).filter(AuthSession.token_hash == token_hash).first()
        if row:
            row.revoked_at = _now_iso()
            db.commit()

    def current_auth(self, db: Session, request: Request) -> CurrentAuth | None:
        self.enforce_geo(request)
        _cleanup_expired_sessions(db)
        token = _get_token_from_request(request)
        if not token:
            return None
        row = db.query(AuthSession).filter(AuthSession.token_hash == _hash_token(token)).first()
        if not row or row.revoked_at is not None:
            return None
        expires_at = _parse_iso(row.expires_at)
        if expires_at and expires_at <= _utcnow():
            row.revoked_at = _now_iso()
            db.commit()
            return None
        user = db.query(AuthUser).filter(AuthUser.username == row.username).first()
        if not user or not user.active:
            return None
        row.last_seen_at = _now_iso()
        db.commit()
        return CurrentAuth(user=user, session=row)

    def block_ip(self, db: Session, ip: str, reason: str, fail_count: int = 0) -> dict[str, Any]:
        _block_ip(db, ip, reason, fail_count)
        return self.blocked_ip(db, ip)

    def blocked_ip(self, db: Session, ip: str) -> dict[str, Any] | None:
        if _ip_is_allowlisted(ip):
            row = db.query(AuthIpBlock).filter(AuthIpBlock.ip_address == ip).first()
            if row:
                db.delete(row)
                db.commit()
            return None
        row = _ensure_not_blocked(db, ip)
        if not row:
            return None
        return _blocked_ip_payload(row)

    def list_blocked_ips(self, db: Session) -> list[dict[str, Any]]:
        rows = db.query(AuthIpBlock).order_by(AuthIpBlock.blocked_at.desc()).all()
        return [_blocked_ip_payload(row) for row in rows]

    def unblock_ip(self, db: Session, ip: str) -> None:
        row = db.query(AuthIpBlock).filter(AuthIpBlock.ip_address == ip).first()
        if row:
            db.delete(row)
            db.commit()


site_auth = SiteAuth()
