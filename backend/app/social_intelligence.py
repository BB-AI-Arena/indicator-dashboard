from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from .config import config_manager
from .db import engine
from .history import get_candles_from_sql
from .models import AdvisorySetting, Candle, OptionPositioningSnapshot, SocialMention, SocialSnapshot
from .news_feeds import _fetch_feed


EASTERN = ZoneInfo("America/New_York")
SOCIAL_VERSION = "social-v1"
FINANCE_CONTEXT = {"stock", "shares", "earnings", "guidance", "revenue", "eps", "options", "calls", "puts", "market", "price", "ticker", "trade"}
COMMON_WORD_TICKERS = {"ALL", "ARE", "CAT", "CRM", "IT", "ON", "REAL", "SPY", "THE", "T", "YOU"}
STANCE_WORDS = {
    "bullish": 1, "bull": 1, "buy": 1, "buying": 1, "long": 1, "breakout": 1, "squeeze": 1, "beat": 1, "beats": 1, "growth": 1, "upgrade": 1, "support": 1, "rally": 1,
    "bearish": -1, "bear": -1, "sell": -1, "selling": -1, "short": -1, "breakdown": -1, "miss": -1, "missed": -1, "downgrade": -1, "lawsuit": -1, "dilution": -1, "dump": -1, "collapse": -1,
}
TOPIC_WORDS = {
    "earnings": {"earnings", "eps", "revenue", "quarter"},
    "guidance": {"guidance", "outlook", "forecast"},
    "product": {"product", "launch", "release"},
    "analyst_action": {"upgrade", "downgrade", "price target", "analyst"},
    "legal_regulatory": {"lawsuit", "regulatory", "sec", "investigation", "court"},
    "short_squeeze": {"squeeze", "short interest", "shorts"},
    "dilution": {"dilution", "offering", "capital raise"},
    "insider_activity": {"insider", "director", "ceo bought"},
    "acquisition": {"acquisition", "merger", "takeover"},
    "cybersecurity": {"cyber", "hack", "breach", "ransomware"},
    "valuation": {"valuation", "overvalued", "undervalued", "multiple"},
    "technical": {"breakout", "breakdown", "vwap", "support", "resistance", "moving average"},
    "options_flow": {"options", "calls", "puts", "gamma", "delta", "sweep", "iv"},
    "meme_speculation": {"meme", "moon", "rocket", "yolo", "lambo"},
}


def ensure_social_schema() -> None:
    """Upgrade the early social table without dropping stored mentions."""
    from sqlalchemy import text

    with engine.begin() as connection:
        table_names = {
            name
            for (name,) in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('social_mentions', 'social_mentions_legacy')")
            ).fetchall()
        }
        row = connection.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='social_mentions'")
        ).scalar()
        if "social_mentions_legacy" in table_names:
            # A prior process may have completed the rename but stopped while
            # recreating indexes. Finish that migration idempotently.
            for index_name in (
                "ix_social_mentions_published_at",
                "ix_social_mentions_symbol",
                "ix_social_mentions_id",
                "ix_social_mentions_symbol_published_at",
                "ix_social_mentions_symbol_source",
                "ix_social_mentions_duplicate_group",
            ):
                connection.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
            SocialMention.__table__.create(connection, checkfirst=True)
            columns = [column.name for column in SocialMention.__table__.columns if column.name != "id"]
            column_sql = ", ".join(columns)
            connection.execute(
                text(
                    f"INSERT OR IGNORE INTO social_mentions ({column_sql}) "
                    f"SELECT {column_sql} FROM social_mentions_legacy"
                )
            )
            connection.execute(text("DROP TABLE social_mentions_legacy"))
            return
        if row and "uq_social_mentions_source_external_id" in str(row):
            for index_name in (
                "ix_social_mentions_symbol_published_at",
                "ix_social_mentions_symbol_source",
                "ix_social_mentions_duplicate_group",
                "ix_social_mentions_published_at",
                "ix_social_mentions_symbol",
                "ix_social_mentions_id",
            ):
                connection.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
            connection.execute(text("ALTER TABLE social_mentions RENAME TO social_mentions_legacy"))
            SocialMention.__table__.create(connection, checkfirst=True)
            columns = [column.name for column in SocialMention.__table__.columns if column.name != "id"]
            column_sql = ", ".join(columns)
            connection.execute(
                text(
                    f"INSERT OR IGNORE INTO social_mentions ({column_sql}) "
                    f"SELECT {column_sql} FROM social_mentions_legacy"
                )
            )
            connection.execute(text("DROP TABLE social_mentions_legacy"))


def _effective_social_config(db=None) -> dict[str, Any]:
    base = dict(config_manager.get("social", default={}) or {})
    if db is None:
        return base
    row = db.query(AdvisorySetting).filter(AdvisorySetting.key == "social_settings").first()
    if not row:
        return base
    try:
        override = json.loads(row.value or "{}")
    except Exception:
        override = {}
    if isinstance(override, dict):
        base.update(override)
    return base


def get_social_settings(db) -> dict[str, Any]:
    cfg = _effective_social_config(db)
    sources = []
    for source in cfg.get("sources") or []:
        if not isinstance(source, dict):
            continue
        # Return only safe configuration. API tokens remain in .env.
        sources.append(
            {
                key: source.get(key)
                for key in ("name", "type", "enabled", "url", "items_path", "token_env", "fields", "credibility")
                if key in source
            }
        )
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "lookback_days": int(cfg.get("lookback_days", 7) or 7),
        "baseline_days": int(cfg.get("baseline_days", 30) or 30),
        "source_cache_ttl_seconds": int(cfg.get("source_cache_ttl_seconds", 900) or 900),
        "minimum_mentions": int(cfg.get("minimum_mentions", 5) or 5),
        "minimum_unique_authors": int(cfg.get("minimum_unique_authors", 3) or 3),
        "spam_threshold": float(cfg.get("spam_threshold", 0.45) or 0.45),
        "relevance_threshold": float(cfg.get("relevance_threshold", 0.7) or 0.7),
        "max_items_per_source": int(cfg.get("max_items_per_source", 200) or 200),
        "aliases": cfg.get("aliases") or {},
        "sources": sources,
        "credentials_note": "Credentials are read from environment variables named by each source token_env field and are never returned.",
    }


def update_social_settings(db, payload: dict[str, Any], username: str | None = None) -> dict[str, Any]:
    current = get_social_settings(db)
    allowed = ("enabled", "lookback_days", "baseline_days", "source_cache_ttl_seconds", "minimum_mentions", "minimum_unique_authors", "spam_threshold", "relevance_threshold", "max_items_per_source", "aliases")
    updated = {key: payload[key] for key in allowed if key in payload}
    sources = payload.get("sources")
    if isinstance(sources, list):
        safe_sources = []
        for source in sources:
            if not isinstance(source, dict):
                continue
            safe_sources.append({
                key: source.get(key)
                for key in ("name", "type", "enabled", "url", "items_path", "token_env", "fields", "credibility")
                if key in source
            })
        updated["sources"] = safe_sources
    merged = {**current, **updated}
    row = db.query(AdvisorySetting).filter(AdvisorySetting.key == "social_settings").first()
    value = {key: merged[key] for key in allowed + ("sources",) if key in merged}
    if row:
        row.value = json.dumps(value, sort_keys=True)
        row.updated_by = username
        row.updated_at = _now_iso()
    else:
        db.add(AdvisorySetting(key="social_settings", value=json.dumps(value, sort_keys=True), updated_by=username, updated_at=_now_iso()))
    db.commit()
    return get_social_settings(db)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except Exception:
        return default


def _clean(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "ignore")).hexdigest()


def _source_cache_path(source: dict[str, Any]) -> Path:
    cache_dir = Path(os.getenv("CACHE_DIR", "/app/data/cache"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = _hash(f"{source.get('name')}|{source.get('url')}")[:24]
    return cache_dir / f"social_source_{key}.json"


def _parse_time(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        try:
            from email.utils import parsedate_to_datetime
            parsed = parsedate_to_datetime(text)
        except Exception:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _json_path(payload: Any, path: str | None) -> Any:
    value = payload
    for part in str(path or "").split("."):
        if not part:
            continue
        if isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def _fetch_json_source(source: dict[str, Any], timeout: int) -> tuple[list[dict[str, Any]], str | None]:
    url = str(source.get("url") or "").strip()
    if not url:
        return [], f"{source.get('name') or 'JSON source'}: missing URL"
    headers = {"Accept": "application/json"}
    token_env = str(source.get("token_env") or "").strip()
    token = os.getenv(token_env, "").strip() if token_env else ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        if response.status_code >= 400:
            return [], f"{source.get('name') or 'JSON source'}: HTTP {response.status_code}"
        payload = response.json()
        rows = _json_path(payload, source.get("items_path")) if source.get("items_path") else payload
        if not isinstance(rows, list):
            return [], f"{source.get('name') or 'JSON source'}: expected a list at items_path"
        mapping = source.get("fields") or {}
        normalized = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            get = lambda name, default=None: row.get(mapping.get(name, name), default)
            normalized.append(
                {
                    "id": get("id") or get("url") or _hash(json.dumps(row, sort_keys=True))[:32],
                    "title": get("title") or get("text") or "",
                    "text": get("text") or get("body") or get("description") or "",
                    "url": get("url") or "",
                    "published_at": _parse_time(get("published_at") or get("created_at") or get("date")),
                    "author": get("author") or get("author_id"),
                    "engagement_count": get("engagement_count") or get("likes") or 0,
                    "replies": get("replies") or 0,
                    "upvotes": get("upvotes") or get("likes") or 0,
                }
            )
        return normalized, None
    except Exception as exc:
        return [], f"{source.get('name') or 'JSON source'}: {exc}"


def _source_items(source: dict[str, Any], cfg: dict[str, Any], *, force_refresh: bool = False) -> tuple[list[dict[str, Any]], str | None, bool]:
    ttl = int(cfg.get("source_cache_ttl_seconds", 900) or 900)
    path = _source_cache_path(source)
    if path.exists() and not force_refresh:
        try:
            if time.time() - path.stat().st_mtime <= ttl:
                payload = json.loads(path.read_text())
                return list(payload.get("items") or []), payload.get("error"), True
        except Exception:
            pass
    timeout = int(cfg.get("request_timeout_seconds", 8) or 8)
    source_type = str(source.get("type") or "rss").lower()
    if source_type == "rss":
        items, error = _fetch_feed(source, timeout)
        items = [
            {
                "id": item.get("link") or _hash(f"{item.get('title')}|{item.get('published_at')}"),
                "title": item.get("title") or "",
                "text": item.get("summary") or "",
                "url": item.get("link") or "",
                "published_at": item.get("published_at"),
                "author": None,
                "engagement_count": 0,
                "replies": 0,
                "upvotes": 0,
            }
            for item in items
        ]
    elif source_type == "json":
        items, error = _fetch_json_source(source, timeout)
    else:
        items, error = [], f"{source.get('name') or 'Social source'}: unsupported source type {source_type}"
    try:
        path.write_text(json.dumps({"items": items, "error": error, "retrieved_at": _now_iso()}, sort_keys=True))
    except Exception:
        pass
    return items, error, False


def _relevance(symbol: str, text: str, aliases: list[str]) -> float:
    normalized = symbol.upper()
    upper = text.upper()
    if re.search(rf"\${re.escape(normalized)}\b", upper):
        return 1.0
    alias_match = any(alias and re.search(rf"\b{re.escape(alias.upper())}\b", upper) for alias in aliases)
    ticker_match = bool(re.search(rf"\b{re.escape(normalized)}\b", upper))
    if alias_match:
        return 0.95
    if ticker_match and normalized not in COMMON_WORD_TICKERS:
        return 0.82
    if ticker_match and any(word in upper.lower().split() for word in FINANCE_CONTEXT):
        return 0.72
    return 0.0


def _stance(text: str) -> str:
    tokens = set(re.findall(r"[a-z][a-z-]+", text.lower()))
    score = sum(STANCE_WORDS.get(token, 0) for token in tokens)
    if score >= 2:
        return "BULLISH"
    if score <= -2:
        return "BEARISH"
    if score:
        return "MIXED"
    if any(word in text.lower() for word in ("rumor", "maybe", "unclear", "not sure")):
        return "UNCERTAIN"
    return "NEUTRAL"


def _topics(text: str) -> list[str]:
    lowered = text.lower()
    return [topic for topic, words in TOPIC_WORDS.items() if any(word in lowered for word in words)]


def _spam_probability(text: str, source: str) -> float:
    lowered = text.lower()
    score = 0.0
    if lowered.count("!") >= 3 or any(word in lowered for word in ("guaranteed", "100x", "moonshot", "join my", "alert service")):
        score += 0.45
    if text.count("$") >= 5 or len(text) < 20:
        score += 0.2
    if source.lower() in {"stocktwits", "reddit"}:
        score += 0.0
    return min(1.0, score)


def _identity_aliases(symbol: str, db, cfg: dict[str, Any]) -> list[str]:
    from .models import TickerProfile
    profile = db.query(TickerProfile).filter(TickerProfile.symbol == symbol).first()
    aliases = [symbol]
    if profile:
        aliases.extend([profile.company_name, profile.industry, profile.sector])
    configured = (cfg.get("aliases") or {}).get(symbol) or []
    aliases.extend(configured if isinstance(configured, list) else [configured])
    return [str(alias).strip() for alias in aliases if str(alias or "").strip()]


def _daily_reactions(symbol: str, db, spike_dates: list[str]) -> dict[str, Any]:
    frame = get_candles_from_sql(symbol, "1d", period="1y", db=db)
    if frame is None or frame.empty:
        return {"sample_size": 0, "confidence": "INSUFFICIENT", "data_status": "unavailable"}
    rows = []
    for timestamp, row in frame.iterrows():
        try:
            parsed = timestamp if hasattr(timestamp, "date") else datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
            if getattr(parsed, "tzinfo", None) is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            day = parsed.astimezone(EASTERN).date().isoformat()
            rows.append((day, _safe_float(row.get("close"))))
        except Exception:
            continue
    rows.sort()
    outcomes = []
    for day in spike_dates:
        idx = next((index for index, (row_day, _) in enumerate(rows) if row_day >= day), None)
        if idx is None or idx == 0 or rows[idx][1] in (None, 0):
            continue
        base = rows[idx - 1][1]
        for offset in (1, 3, 5):
            target = idx + offset - 1
            if target < len(rows) and rows[target][1] is not None:
                outcomes.append((rows[target][1] / base - 1.0) * 100.0)
                break
    if not outcomes:
        return {"sample_size": 0, "confidence": "INSUFFICIENT", "data_status": "unavailable"}
    positive = sum(1 for value in outcomes if value > 0.5)
    return {
        "sample_size": len(outcomes),
        "positive_rate": round(positive / len(outcomes), 4),
        "average_return_pct": round(sum(outcomes) / len(outcomes), 3),
        "median_return_pct": round(sorted(outcomes)[len(outcomes) // 2], 3),
        "confidence": "LOW" if len(outcomes) < 10 else "MODERATE" if len(outcomes) < 30 else "HIGH",
        "data_status": "observed",
        "note": "Historical reactions use only candles after each recorded social spike.",
    }


def _current_price_confirmation(symbol: str, db, sentiment_score: float) -> str:
    frame = get_candles_from_sql(symbol, "15m", period="5d", db=db)
    if frame is None or frame.empty or len(frame) < 3:
        return "UNAVAILABLE"
    try:
        latest = frame.iloc[-1]
        previous = frame.iloc[-2]
        close = _safe_float(latest.get("close"))
        previous_close = _safe_float(previous.get("close"))
        volume = _safe_float(latest.get("volume"), 0.0) or 0.0
        average_volume = _safe_float(frame["volume"].tail(20).mean(), 0.0) or 0.0
        if close is None or previous_close in (None, 0):
            return "UNAVAILABLE"
        rising = close > previous_close
        expanding = volume >= average_volume if average_volume else False
        if sentiment_score >= 25:
            return "CONFIRMED" if rising and expanding else "PARTIAL" if rising else "CONFLICTED"
        if sentiment_score <= -25:
            return "CONFIRMED" if not rising and expanding else "PARTIAL" if not rising else "CONFLICTED"
        return "NEUTRAL"
    except Exception:
        return "UNAVAILABLE"


def _aggregate(symbol: str, db, cfg: dict[str, Any]) -> dict[str, Any]:
    lookback_days = int(cfg.get("lookback_days", 7) or 7)
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=lookback_days)).isoformat()
    rows = db.query(SocialMention).filter(SocialMention.symbol == symbol).filter(SocialMention.published_at >= start).all()
    baseline_start = (now - timedelta(days=lookback_days * 5)).isoformat()
    baseline_rows = db.query(SocialMention).filter(SocialMention.symbol == symbol).filter(SocialMention.published_at >= baseline_start).filter(SocialMention.published_at < start).all()
    unique_authors = {row.author_hash for row in rows if row.author_hash}
    discussion_ids = {row.duplicate_group or row.external_id for row in rows}
    sources = {row.source for row in rows}
    stance_values = {"BULLISH": 1, "BEARISH": -1, "MIXED": 0, "NEUTRAL": 0, "UNCERTAIN": 0}
    weighted = [stance_values.get(row.stance or "NEUTRAL", 0) * (1.0 - (_safe_float(row.spam_probability, 0.0) or 0.0)) * (_safe_float(row.relevance_score, 0.0) or 0.0) for row in rows]
    sentiment_score = round(max(-100.0, min(100.0, (sum(weighted) / max(sum(1.0 - (_safe_float(row.spam_probability, 0.0) or 0.0) for row in rows), 1.0)) * 100.0)), 2) if rows else 0.0
    counts = Counter(row.stance or "NEUTRAL" for row in rows)
    topic_counts: Counter[str] = Counter()
    for row in rows:
        try:
            topic_counts.update(json.loads(row.topics_json or "[]"))
        except Exception:
            pass
    spam_risk = round(sum(_safe_float(row.spam_probability, 0.0) or 0.0 for row in rows) / len(rows), 3) if rows else 0.0
    baseline_per_day = len(baseline_rows) / max(lookback_days * 4, 1)
    velocity = round(len(rows) / max(baseline_per_day * lookback_days, 1.0), 2) if rows else 0.0
    if len(unique_authors) < int(cfg.get("minimum_unique_authors", 3) or 3) or len(rows) < int(cfg.get("minimum_mentions", 5) or 5):
        classification = "INSUFFICIENT DATA"
    elif spam_risk >= float(cfg.get("spam_threshold", 0.45) or 0.45) and velocity >= 2:
        classification = "HYPE RISK" if sentiment_score >= 0 else "PANIC RISK"
    elif sentiment_score >= 60:
        classification = "STRONGLY BULLISH"
    elif sentiment_score >= 25:
        classification = "MODERATELY BULLISH"
    elif sentiment_score <= -60:
        classification = "STRONGLY BEARISH"
    elif sentiment_score <= -25:
        classification = "MODERATELY BEARISH"
    else:
        classification = "NEUTRAL"
    confidence = "LOW" if len(unique_authors) < 5 or len(rows) < 10 else "MODERATE" if len(unique_authors) < 10 else "HIGH"
    spike_dates = []
    by_day: defaultdict[str, int] = defaultdict(int)
    for row in rows:
        if row.published_at:
            by_day[str(row.published_at)[:10]] += 1
    threshold = max(3, int(max(baseline_per_day * 2, 1)))
    spike_dates = [day for day, count in by_day.items() if count >= threshold]
    historical = _daily_reactions(symbol, db, spike_dates)
    price_confirmation = _current_price_confirmation(symbol, db, sentiment_score)
    representative = [
        {
            "source": row.source,
            "published_at": row.published_at,
            "title": row.title,
            "url": row.url,
            "stance": row.stance,
            "topics": json.loads(row.topics_json or "[]"),
            "engagement_count": row.engagement_count,
        }
        for row in sorted(rows, key=lambda item: (item.relevance_score or 0, item.engagement_count or 0), reverse=True)[:5]
    ]
    return {
        "classification": classification,
        "sentiment_score": sentiment_score,
        "sentiment_confidence": confidence,
        "mention_count": len(rows),
        "unique_author_count": len(unique_authors),
        "unique_discussion_count": len(discussion_ids),
        "mention_velocity": velocity,
        "mention_velocity_score": round(min(100.0, velocity * 25.0), 2),
        "baseline_mentions_per_day": round(baseline_per_day, 2),
        "stance_counts": dict(counts),
        "primary_topics": [{"topic": topic, "mentions": count} for topic, count in topic_counts.most_common(5)],
        "source_diversity": round(min(1.0, len(sources) / 3.0), 3),
        "source_count": len(sources),
        "spam_risk_score": spam_risk,
        "price_confirmation": price_confirmation,
        "options_confirmation": "UNAVAILABLE",
        "trade_impact": "SUPPORTING SIGNAL ONLY",
        "historical_behavior": historical,
        "representative_posts": representative,
        "window": {"start": start, "end": now.isoformat(), "session": "CURRENT_OR_NEXT_SESSION_PLANNING"},
        "data_status": "observed" if rows else "unavailable",
        "data_version": SOCIAL_VERSION,
        "updated_at": _now_iso(),
    }


def build_social_profile(symbol: str, db, *, force_refresh: bool = False) -> dict[str, Any]:
    normalized = str(symbol or "").strip().upper()
    cfg = _effective_social_config(db)
    if not bool(cfg.get("enabled", True)):
        return {"symbol": normalized, "classification": "INSUFFICIENT DATA", "data_status": "disabled", "sources": []}
    aliases = _identity_aliases(normalized, db, cfg)
    sources = list(cfg.get("sources") or [])
    errors: list[str] = []
    stored = 0
    for source in sources:
        if not bool(source.get("enabled", True)):
            continue
        items, error, _cached = _source_items(source, cfg, force_refresh=force_refresh)
        if error:
            errors.append(error)
        source_name = str(source.get("name") or "Social source")
        for item in items[: int(cfg.get("max_items_per_source", 200) or 200)]:
            title = _clean(item.get("title"))
            text = _clean(item.get("text"))
            relevance = _relevance(normalized, f"{title} {text}", aliases)
            if relevance < float(cfg.get("relevance_threshold", 0.7) or 0.7):
                continue
            published = _parse_time(item.get("published_at"))
            external_id = _clean(item.get("id") or item.get("url") or _hash(f"{title}|{published}"), 256)
            duplicate_group = _hash(re.sub(r"[^a-z0-9 ]", "", f"{title} {text}".lower()))[:32]
            author = _clean(item.get("author"), 256)
            row = db.query(SocialMention).filter(SocialMention.source == source_name).filter(SocialMention.external_id == external_id).first()
            values = {
                "symbol": normalized,
                "source": source_name,
                "external_id": external_id,
                "author_hash": _hash(author) if author else None,
                "published_at": published,
                "retrieved_at": _now_iso(),
                "title": title,
                "text_excerpt": text,
                "url": _clean(item.get("url"), 1000),
                "engagement_count": int(_safe_float(item.get("engagement_count"), 0) or 0),
                "replies": int(_safe_float(item.get("replies"), 0) or 0),
                "upvotes": int(_safe_float(item.get("upvotes"), 0) or 0),
                "relevance_score": relevance,
                "stance": _stance(f"{title} {text}"),
                "topics_json": json.dumps(_topics(f"{title} {text}")),
                "spam_probability": _spam_probability(f"{title} {text}", source_name),
                "bot_indicator": "UNKNOWN",
                "duplicate_group": duplicate_group,
                "source_credibility": _safe_float(source.get("credibility"), 0.5),
                "language": str(item.get("language") or "en"),
                "data_version": SOCIAL_VERSION,
            }
            if row:
                for key, value in values.items():
                    setattr(row, key, value)
            else:
                db.add(SocialMention(**values))
            stored += 1
    if stored:
        db.commit()
    summary = _aggregate(normalized, db, cfg)
    latest_option = db.query(OptionPositioningSnapshot).filter(OptionPositioningSnapshot.symbol == normalized).order_by(OptionPositioningSnapshot.created_at.desc()).first()
    if latest_option:
        option_score = _safe_float(latest_option.bias_score)
        if option_score is None or abs(summary.get("sentiment_score") or 0) < 25:
            summary["options_confirmation"] = latest_option.classification or "AVAILABLE"
        elif (summary.get("sentiment_score") or 0) * option_score > 0:
            summary["options_confirmation"] = "ALIGNED"
        else:
            summary["options_confirmation"] = "CONFLICTED"
    summary["source_status"] = [{"source": str(source.get("name") or "Social source"), "enabled": bool(source.get("enabled", True)), "available": not any(str(source.get("name") or "Social source") in error for error in errors)} for source in sources]
    summary["source_errors"] = errors[:8]
    snapshot = SocialSnapshot(
        symbol=normalized,
        session_state=str((summary.get("window") or {}).get("session") or "UNKNOWN"),
        sentiment_score=summary.get("sentiment_score"),
        mention_count=int(summary.get("mention_count") or 0),
        unique_author_count=int(summary.get("unique_author_count") or 0),
        source_diversity=summary.get("source_diversity"),
        spam_risk=summary.get("spam_risk_score"),
        summary_json=json.dumps(summary, sort_keys=True),
        data_version=SOCIAL_VERSION,
    )
    db.add(snapshot)
    db.commit()
    return {"symbol": normalized, **summary}
