from __future__ import annotations

import html
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import requests

from .cache_policy import market_aware_ttl
from .config import config_manager
from .db import SessionLocal
from .models import Watchlist


_lock = threading.Lock()
_cache: dict[str, Any] = {"expires_at": 0.0, "payload": None}

INDEX_KEYWORDS = {
    "nasdaq": "QQQ",
    "nasdaq 100": "QQQ",
    "nasdaq-100": "QQQ",
    "s&p": "SPY",
    "s&p 500": "SPY",
    "spx": "SPY",
    "stock market": "SPY",
    "stocks": "SPY",
}

COMPANY_KEYWORDS = {
    "apple": "AAPL",
    "microsoft": "MSFT",
    "meta": "META",
    "facebook": "META",
    "nvidia": "NVDA",
    "tesla": "TSLA",
    "amazon": "AMZN",
    "alphabet": "GOOGL",
    "google": "GOOGL",
    "broadcom": "AVGO",
    "walmart": "WMT",
    "eli lilly": "LLY",
    "jpmorgan": "JPM",
    "jp morgan": "JPM",
    "exxon": "XOM",
    "johnson & johnson": "JNJ",
    "visa": "V",
    "mastercard": "MA",
    "costco": "COST",
    "oracle": "ORCL",
    "netflix": "NFLX",
    "chevron": "CVX",
    "micron": "MU",
    "abbvie": "ABBV",
    "palantir": "PLTR",
    "bank of america": "BAC",
    "amd": "AMD",
    "advanced micro devices": "AMD",
    "procter": "PG",
    "caterpillar": "CAT",
    "home depot": "HD",
    "coca-cola": "KO",
    "cisco": "CSCO",
    "general electric": "GE",
    "merck": "MRK",
    "applied materials": "AMAT",
    "goldman": "GS",
    "wells fargo": "WFC",
    "mcdonald": "MCD",
    "pepsico": "PEP",
    "verizon": "VZ",
    "american express": "AXP",
    "at&t": "T",
    "citigroup": "C",
    "disney": "DIS",
    "gilead": "GILD",
    "schwab": "SCHW",
    "intuitive surgical": "ISRG",
    "pfizer": "PFE",
    "boeing": "BA",
    "uber": "UBER",
    "adobe": "ADBE",
    "starbucks": "SBUX",
    "comcast": "CMCSA",
    "nike": "NKE",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: str | None) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _active_watchlist_symbols() -> set[str]:
    db = SessionLocal()
    try:
        return {str(row.symbol or "").upper() for row in db.query(Watchlist).filter(Watchlist.active.is_(True)).all()}
    finally:
        db.close()


def _add_symbol(symbols: list[str], reasons: dict[str, str], symbol: str, reason: str, watchlist: set[str]) -> None:
    normalized = str(symbol or "").upper().strip()
    if not normalized or normalized not in watchlist or normalized in symbols:
        return
    symbols.append(normalized)
    reasons[normalized] = reason


def _infer_symbols(title: str, summary: str, watchlist: set[str]) -> tuple[list[str], dict[str, str]]:
    text = f"{title} {summary}".strip()
    lowered = text.lower()
    symbols: list[str] = []
    reasons: dict[str, str] = {}

    for raw in re.findall(r"\$([A-Za-z][A-Za-z0-9.-]{0,9})", text):
        _add_symbol(symbols, reasons, raw.replace(".", "-"), "cashtag", watchlist)

    words = set(re.findall(r"\b[A-Z][A-Z0-9.-]{0,9}\b", text))
    ignore = {
        "CEO",
        "CFO",
        "COO",
        "EPS",
        "ETF",
        "ETFs",
        "Fed",
        "GDP",
        "IPO",
        "M&A",
        "NYSE",
        "SEC",
        "US",
        "USA",
        "WSJ",
    }
    for raw in words:
        candidate = raw.replace(".", "-")
        if raw in ignore:
            continue
        _add_symbol(symbols, reasons, candidate, "ticker text", watchlist)

    for keyword, symbol in COMPANY_KEYWORDS.items():
        if keyword in lowered:
            _add_symbol(symbols, reasons, symbol, f"company keyword: {keyword}", watchlist)

    for keyword, symbol in INDEX_KEYWORDS.items():
        if keyword in lowered:
            _add_symbol(symbols, reasons, symbol, f"market keyword: {keyword}", watchlist)

    return symbols[:6], reasons


def _parse_datetime(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except Exception:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _child_text(node: ET.Element, names: list[str]) -> str | None:
    for name in names:
        found = node.find(name)
        if found is not None and found.text:
            return found.text
    for child in list(node):
        tag = child.tag.split("}", 1)[-1].lower()
        if tag in {n.lower().split("}", 1)[-1] for n in names} and child.text:
            return child.text
    return None


def _child_link(node: ET.Element) -> str | None:
    direct = _child_text(node, ["link"])
    if direct:
        return direct
    for child in list(node):
        if child.tag.split("}", 1)[-1].lower() == "link":
            href = child.attrib.get("href")
            if href:
                return href
    return None


def _is_block_page(text: str) -> bool:
    lower = (text or "").lower()
    return any(
        marker in lower
        for marker in [
            "<html",
            "access denied",
            "too many requests",
            "enable javascript",
            "just a moment",
            "captcha",
            "cloudflare",
        ]
    )


def _parse_feed(source: str, text: str) -> list[dict[str, Any]]:
    if _is_block_page(text):
        raise ValueError("feed returned block/rate-limit page instead of XML")
    root = ET.fromstring(text.encode("utf-8"))
    nodes = root.findall(".//item")
    if not nodes:
        nodes = root.findall(".//{http://www.w3.org/2005/Atom}entry")

    items: list[dict[str, Any]] = []
    for node in nodes:
        title = _clean_text(_child_text(node, ["title"]))
        if not title:
            continue
        link = _clean_text(_child_link(node))
        published = _parse_datetime(
            _child_text(
                node,
                [
                    "pubDate",
                    "published",
                    "updated",
                    "{http://purl.org/dc/elements/1.1/}date",
                ],
            )
        )
        summary = _clean_text(_child_text(node, ["description", "summary"]))
        items.append(
            {
                "source": source,
                "title": title,
                "link": link,
                "published_at": published,
                "summary": summary[:240],
            }
        )
    return items


def _fetch_feed(feed: dict[str, Any], timeout: int) -> tuple[list[dict[str, Any]], str | None]:
    name = str(feed.get("name") or "RSS").strip()
    url = str(feed.get("url") or "").strip()
    if not url:
        return [], f"{name}: missing URL"
    try:
        response = requests.get(
            url,
            headers={
                "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
                "User-Agent": "indicator-dashboard/1.0 RSS reader",
            },
            timeout=timeout,
        )
        if response.status_code >= 400:
            return [], f"{name}: HTTP {response.status_code}"
        return _parse_feed(name, response.text or ""), None
    except Exception as exc:
        return [], f"{name}: {exc}"


def market_news_feed(*, force_refresh: bool = False) -> dict[str, Any]:
    news_cfg = config_manager.get("news", default={}) or {}
    enabled = bool(news_cfg.get("enabled", True))
    max_items = int(news_cfg.get("max_items", 28) or 28)
    ttl = market_aware_ttl(int(news_cfg.get("cache_ttl_seconds", 180) or 180))
    timeout = int(news_cfg.get("request_timeout_seconds", 8) or 8)
    feeds = list(news_cfg.get("feeds") or [])

    if not enabled:
        return {
            "enabled": False,
            "items": [],
            "errors": [],
            "sources": [],
            "updated_at": _now_iso(),
            "cache_ttl_seconds": ttl,
        }

    now = time.time()
    with _lock:
        cached = _cache.get("payload")
        if cached and not force_refresh and now < float(_cache.get("expires_at") or 0):
            return {**cached, "cached": True}

    all_items: list[dict[str, Any]] = []
    errors: list[str] = []
    watchlist = _active_watchlist_symbols()
    for feed in feeds:
        items, error = _fetch_feed(feed, timeout)
        all_items.extend(items)
        if error:
            errors.append(error)

    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for item in all_items:
        key = f"{item.get('title')}|{item.get('link')}"
        if key in seen:
            continue
        seen.add(key)
        symbols, reasons = _infer_symbols(str(item.get("title") or ""), str(item.get("summary") or ""), watchlist)
        item["symbols"] = symbols
        item["symbol_reason"] = reasons
        deduped.append(item)

    deduped.sort(key=lambda row: row.get("published_at") or "", reverse=True)
    payload = {
        "enabled": True,
        "items": deduped[:max_items],
        "errors": errors[:8],
        "sources": sorted({str(item.get("source") or "") for item in deduped if item.get("source")}),
        "updated_at": _now_iso(),
        "cache_ttl_seconds": ttl,
        "cached": False,
    }
    with _lock:
        _cache["payload"] = payload
        _cache["expires_at"] = now + max(ttl, 30)
    return payload
