"""
Korea Tourism API - English-friendly REST API wrapper
Data source: Korea Tourism Organization (KTO) via data.go.kr
Endpoint: https://apis.data.go.kr/B551011/KorService2

v2 ADDS:
- Daily call counter (KST midnight reset) to protect data.go.kr 1,000/day limit
- 24-hour in-memory cache to reduce duplicate calls
- 3-tier protection: 800/950/1000 thresholds
- X-Daily-Limit-Remaining response header
"""

import json
import os
import time
import hashlib
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
import requests

app = FastAPI(
    title="Korea Tourism API",
    description="English-friendly REST API for Korean tourism information. "
                "Provides attractions, festivals, accommodations, and more across South Korea. "
                "Data source: Korea Tourism Organization (KTO).",
    version="1.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
SERVICE_KEY = os.environ.get(
    "KTO_SERVICE_KEY",
    "b224a568eb8ac5542169cdec26a817dcbb84f29a5f8a4d14429ee560dfea8b53"
)
BASE_URL = "https://apis.data.go.kr/B551011/KorService2"
DEFAULT_PARAMS = {
    "MobileOS": "ETC",
    "MobileApp": "KoreaTourismAPI",
    "_type": "json",
}

# ============================================================================
# DAILY LIMIT PROTECTION + CACHING
# ============================================================================
# data.go.kr free tier limit: 1,000 calls/day per key
# Our protection thresholds:
#   0-800: normal operation
#   800-950: cache-only mode (only return cached, no new upstream calls except for new queries)
#   950-1000: emergency mode (only cache, no upstream at all)
#   1000+: 503 Service Unavailable

DAILY_LIMIT = 1000
THRESHOLD_WARN = 800       # Start preferring cache
THRESHOLD_CRITICAL = 950   # Cache only for known queries
THRESHOLD_EXHAUSTED = 1000 # Block all upstream calls

CACHE_TTL_SECONDS = 24 * 60 * 60  # 24 hours

# In-memory state (thread-safe with lock)
_state_lock = threading.Lock()
_daily_counter = {"date_kst": "", "count": 0}
_cache = {}  # key -> {"data": dict, "expires_at": float}

# Translation dictionary (loaded once)
TRANSLATIONS = {}
try:
    _trans_path = os.path.join(os.path.dirname(__file__), "translations.json")
    if os.path.exists(_trans_path):
        with open(_trans_path, "r", encoding="utf-8") as f:
            TRANSLATIONS = json.load(f)
except Exception:
    TRANSLATIONS = {}


def _kst_today() -> str:
    """Get today's date string in KST (YYYY-MM-DD)."""
    kst = timezone(timedelta(hours=9))
    return datetime.now(kst).strftime("%Y-%m-%d")


def _check_and_reset_daily_counter():
    """Reset counter if KST date has changed. Must be called inside _state_lock."""
    today = _kst_today()
    if _daily_counter["date_kst"] != today:
        _daily_counter["date_kst"] = today
        _daily_counter["count"] = 0


def _get_remaining_quota() -> int:
    """Get remaining daily quota."""
    with _state_lock:
        _check_and_reset_daily_counter()
        return max(0, DAILY_LIMIT - _daily_counter["count"])


def _make_cache_key(endpoint: str, params: dict) -> str:
    """Generate cache key from endpoint + params (excluding service key)."""
    relevant = {k: v for k, v in params.items() if k != "serviceKey"}
    serialized = endpoint + "|" + json.dumps(relevant, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(serialized.encode("utf-8")).hexdigest()


def _get_from_cache(cache_key: str) -> Optional[dict]:
    """Return cached data if fresh, else None."""
    with _state_lock:
        entry = _cache.get(cache_key)
        if not entry:
            return None
        if time.time() > entry["expires_at"]:
            del _cache[cache_key]
            return None
        return entry["data"]


def _store_in_cache(cache_key: str, data: dict):
    """Store data in cache with 24h TTL."""
    with _state_lock:
        _cache[cache_key] = {
            "data": data,
            "expires_at": time.time() + CACHE_TTL_SECONDS,
        }
        # Cap cache size to prevent memory bloat
        if len(_cache) > 5000:
            # Remove oldest 1000 entries
            sorted_keys = sorted(_cache.items(), key=lambda x: x[1]["expires_at"])
            for k, _ in sorted_keys[:1000]:
                del _cache[k]


def _increment_daily_counter():
    """Increment daily counter atomically. Returns new count."""
    with _state_lock:
        _check_and_reset_daily_counter()
        _daily_counter["count"] += 1
        return _daily_counter["count"]


# ============================================================================
# CORE UPSTREAM CALL (with protection)
# ============================================================================

def call_kto(endpoint: str, params: dict) -> dict:
    """Call KTO API with daily limit protection and caching."""
    cache_key = _make_cache_key(endpoint, params)

    # Step 1: Try cache first (always, regardless of quota)
    cached = _get_from_cache(cache_key)

    # Step 2: Check current quota state
    remaining = _get_remaining_quota()

    # Step 3: Decide whether to call upstream
    if cached is not None:
        # Cache hit
        if remaining <= (DAILY_LIMIT - THRESHOLD_CRITICAL):
            # In critical zone, prefer cache aggressively
            return cached
        # Even in normal zone, cache is fresh (< 24h), return it
        return cached

    # Cache miss — need upstream call
    if remaining <= 0:
        # Quota exhausted
        raise HTTPException(
            status_code=503,
            detail=(
                "Daily upstream quota exhausted (data.go.kr free tier: "
                f"{DAILY_LIMIT}/day). Service will resume after KST midnight. "
                "Cached results may still be available for previously-queried items."
            )
        )

    if remaining <= (DAILY_LIMIT - THRESHOLD_CRITICAL):
        # In critical zone (< 50 calls left), block new queries entirely
        raise HTTPException(
            status_code=503,
            detail=(
                "Service temporarily limited to cached results only "
                f"({remaining} upstream calls remaining today). "
                "This query has not been seen in the last 24 hours. "
                "Please try a popular query (e.g., 경복궁, Gyeongbokgung) or retry tomorrow."
            )
        )

    # OK to call upstream
    full_params = {
        "serviceKey": SERVICE_KEY,
        **DEFAULT_PARAMS,
        **params,
    }
    try:
        response = requests.get(
            f"{BASE_URL}/{endpoint}",
            params=full_params,
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        _increment_daily_counter()
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Upstream API error: {str(e)}")
    except ValueError:
        raise HTTPException(status_code=502, detail="Invalid JSON from upstream API")

    # Parse standard KTO response structure
    body = data.get("response", {}).get("body", {})
    items = body.get("items", {})

    if not items:
        result = {
            "total": 0,
            "page": params.get("pageNo", 1),
            "limit": params.get("numOfRows", 10),
            "results": []
        }
        _store_in_cache(cache_key, result)
        return result

    item_list = items.get("item", []) if isinstance(items, dict) else items
    if not isinstance(item_list, list):
        item_list = [item_list]

    total = body.get("totalCount", 0)
    try:
        total = int(total)
    except (ValueError, TypeError):
        total = len(item_list)

    results = [_normalize_item(item) for item in item_list]
    response_data = {
        "total": total,
        "page": int(params.get("pageNo", 1)),
        "limit": int(params.get("numOfRows", 10)),
        "results": results,
    }

    _store_in_cache(cache_key, response_data)
    return response_data


# ============================================================================
# ITEM NORMALIZATION (with translation)
# ============================================================================

def _translate_name(name_ko: str) -> dict:
    """Translate Korean name to English using dictionary, fallback to romanization."""
    attractions = TRANSLATIONS.get("attractions", {})
    categories = TRANSLATIONS.get("categories", {})
    regions = TRANSLATIONS.get("regions", {})

    # Direct match
    if name_ko in attractions:
        return {"name": attractions[name_ko], "name_romanized": _romanize(name_ko)}

    # Substring match (for compound names)
    for ko, en in attractions.items():
        if ko in name_ko:
            return {"name": f"{en} ({_romanize(name_ko)})", "name_romanized": _romanize(name_ko)}

    # Fallback to romanization
    return {"name": _romanize(name_ko), "name_romanized": _romanize(name_ko)}


def _romanize(text: str) -> str:
    """Simple Korean romanization (basic). For production, use a library."""
    # Very basic — for now just return as-is. Romanization library can be added.
    return text


def _normalize_item(item: dict) -> dict:
    """Convert KTO API item to our schema with translations."""
    name_ko = item.get("title", "") or ""

    # Region translation
    area_code = str(item.get("areacode", "") or "")
    regions = TRANSLATIONS.get("regions", {})
    region_en = regions.get(area_code, "")

    # Category translation
    cat2 = str(item.get("cat2", "") or "")
    cat3 = str(item.get("cat3", "") or "")
    categories = TRANSLATIONS.get("categories", {})
    category_en = categories.get(cat3) or categories.get(cat2) or _content_type_label(item.get("contenttypeid"))

    translated = _translate_name(name_ko)

    return {
        "id": str(item.get("contentid", "")),
        "name": translated["name"],
        "name_romanized": translated["name_romanized"],
        "name_ko": name_ko,
        "category": category_en,
        "category_id": cat3 or cat2,
        "region": region_en,
        "region_code": area_code,
        "address": item.get("addr1", "") or "",
        "address_detail": item.get("addr2", "") or "",
        "address_ko": item.get("addr1", "") or "",
        "zipcode": item.get("zipcode", "") or "",
        "phone": item.get("tel", "") or "",
        "location": {
            "latitude": _safe_float(item.get("mapy")),
            "longitude": _safe_float(item.get("mapx")),
        },
        "image": item.get("firstimage", "") or "",
        "image_thumb": item.get("firstimage2", "") or "",
        "image_license": item.get("cpyrhtDivCd", "") or "",
        "created_at": _format_kto_date(item.get("createdtime", "")),
        "modified_at": _format_kto_date(item.get("modifiedtime", "")),
    }


def _safe_float(value) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def _content_type_label(content_type_id) -> str:
    """Map KTO contenttypeid to English category."""
    mapping = {
        "12": "Tourist Attraction",
        "14": "Cultural Facility",
        "15": "Festival/Event",
        "25": "Travel Course",
        "28": "Leisure Activity",
        "32": "Accommodation",
        "38": "Shopping",
        "39": "Restaurant",
    }
    return mapping.get(str(content_type_id), "Other")


def _format_kto_date(date_str: str) -> str:
    """Convert KTO date format YYYYMMDDHHMMSS to ISO 8601."""
    s = str(date_str or "")
    if len(s) < 14:
        return s
    try:
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}T{s[8:10]}:{s[10:12]}:{s[12:14]}"
    except IndexError:
        return s


# ============================================================================
# HEALTH / STATUS ENDPOINTS
# ============================================================================

@app.get("/")
def root():
    """Root endpoint with API info."""
    return {
        "name": "Korea Tourism API",
        "version": "1.1.0",
        "description": "English-friendly REST API for Korean tourism data",
        "data_source": "Korea Tourism Organization (KTO) via data.go.kr",
        "endpoints": {
            "search": "/search?keyword=...",
            "by_location": "/attractions/by-location?latitude=..&longitude=..",
            "by_region": "/attractions/by-region?area_code=..",
            "details": "/attractions/{content_id}",
            "festivals": "/festivals?start_date=YYYYMMDD",
        },
        "docs": "/docs",
    }


@app.get("/health")
def health():
    """Health check + daily quota status (for monitoring)."""
    with _state_lock:
        _check_and_reset_daily_counter()
        count = _daily_counter["count"]
        date = _daily_counter["date_kst"]
        cache_size = len(_cache)
    remaining = max(0, DAILY_LIMIT - count)
    if count >= THRESHOLD_EXHAUSTED:
        zone = "exhausted"
    elif count >= THRESHOLD_CRITICAL:
        zone = "critical"
    elif count >= THRESHOLD_WARN:
        zone = "warning"
    else:
        zone = "normal"
    return {
        "status": "ok",
        "date_kst": date,
        "daily_used": count,
        "daily_limit": DAILY_LIMIT,
        "daily_remaining": remaining,
        "zone": zone,
        "cache_entries": cache_size,
    }


# ============================================================================
# MAIN API ENDPOINTS (5 production endpoints)
# ============================================================================

@app.get("/search")
def search_tourism(
    response: Response,
    keyword: str = Query(..., description="Search keyword (Korean works best, e.g., 경복궁, 부산타워)"),
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(10, ge=1, le=100, description="Results per page (max 100)"),
):
    """
    Search Korean tourism content by keyword.
    Returns attractions, restaurants, shopping, accommodation, and more matching the keyword.
    """
    params = {
        "keyword": keyword,
        "pageNo": page,
        "numOfRows": limit,
    }
    result = call_kto("searchKeyword2", params)
    response.headers["X-Daily-Limit-Remaining"] = str(_get_remaining_quota())
    return result


@app.get("/attractions/by-location")
def attractions_by_location(
    response: Response,
    latitude: float = Query(..., description="GPS latitude (e.g., 37.5760 for Seoul)"),
    longitude: float = Query(..., description="GPS longitude (e.g., 126.9767 for Seoul)"),
    radius: int = Query(1000, ge=100, le=20000, description="Search radius in meters (default 1km)"),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
):
    """Find Korean tourism content within a radius of a GPS coordinate."""
    params = {
        "mapX": longitude,
        "mapY": latitude,
        "radius": radius,
        "pageNo": page,
        "numOfRows": limit,
    }
    result = call_kto("locationBasedList2", params)
    response.headers["X-Daily-Limit-Remaining"] = str(_get_remaining_quota())
    return result


@app.get("/attractions/by-region")
def attractions_by_region(
    response: Response,
    area_code: str = Query(..., description="Region code (1=Seoul, 6=Busan, 39=Jeju, etc.)"),
    sigungu_code: Optional[str] = Query(None, description="Optional sub-region code"),
    content_type_id: Optional[str] = Query(None, description="Optional content type filter (12=Attraction, 39=Restaurant, etc.)"),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
):
    """Browse Korean tourism content by administrative region."""
    params = {
        "areaCode": area_code,
        "pageNo": page,
        "numOfRows": limit,
    }
    if sigungu_code:
        params["sigunguCode"] = sigungu_code
    if content_type_id:
        params["contentTypeId"] = content_type_id
    result = call_kto("areaBasedList2", params)
    response.headers["X-Daily-Limit-Remaining"] = str(_get_remaining_quota())
    return result


@app.get("/attractions/{content_id}")
def attraction_details(
    content_id: str,
    response: Response,
    content_type_id: Optional[int] = Query(None, description="Content type ID for richer detail (optional)"),
):
    """Get detailed information about a specific attraction by its content ID."""
    params = {
        "contentId": content_id,
    }
    if content_type_id:
        params["contentTypeId"] = content_type_id
    result = call_kto("detailCommon2", params)
    response.headers["X-Daily-Limit-Remaining"] = str(_get_remaining_quota())
    if not result.get("results"):
        raise HTTPException(status_code=404, detail=f"Attraction with ID {content_id} not found")
    return result["results"][0]


@app.get("/festivals")
def search_festivals(
    response: Response,
    start_date: str = Query(..., description="Festival start date in YYYYMMDD format (e.g., 20260601)"),
    end_date: Optional[str] = Query(None, description="Optional end date YYYYMMDD"),
    area_code: Optional[str] = Query(None, description="Optional region filter"),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
):
    """
    Search Korean festivals running on or after the specified start date.
    Useful for travel planning and event discovery.
    """
    params = {
        "eventStartDate": start_date,
        "pageNo": page,
        "numOfRows": limit,
    }
    if end_date:
        params["eventEndDate"] = end_date
    if area_code:
        params["areaCode"] = area_code
    result = call_kto("searchFestival2", params)
    response.headers["X-Daily-Limit-Remaining"] = str(_get_remaining_quota())
    return result


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
