"""
Korea Tourism API - English-friendly REST API wrapper
Data source: Korea Tourism Organization (KTO) via data.go.kr
Endpoint: https://apis.data.go.kr/B551011/KorService2
"""

import json
import os
from typing import Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import requests

app = FastAPI(
    title="Korea Tourism API",
    description="English-friendly REST API for Korean tourism information. "
                "Provides attractions, festivals, accommodations, and more across South Korea. "
                "Data source: Korea Tourism Organization (KTO).",
    version="1.0.0"
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

# Load translation dictionary
TRANSLATIONS_PATH = os.path.join(os.path.dirname(__file__), "translations.json")
try:
    with open(TRANSLATIONS_PATH, "r", encoding="utf-8") as f:
        TRANSLATIONS = json.load(f)
except FileNotFoundError:
    TRANSLATIONS = {"attractions": {}, "categories": {}, "regions": {}}


# =========================================================
# Romanization (Hangul -> Latin) - simple fallback
# =========================================================
INITIAL = ['g','kk','n','d','tt','r','m','b','pp','s','ss','','j','jj','ch','k','t','p','h']
MEDIAL = ['a','ae','ya','yae','eo','e','yeo','ye','o','wa','wae','oe','yo','u','wo','we','wi','yu','eu','ui','i']
FINAL = ['','g','kk','gs','n','nj','nh','d','l','lg','lm','lb','ls','lt','lp','lh','m','b','bs','s','ss','ng','j','ch','k','t','p','h']

def romanize(text: str) -> str:
    """Simple Korean romanization (Revised Romanization, approximate)."""
    if not text:
        return ""
    result = []
    for ch in text:
        code = ord(ch)
        if 0xAC00 <= code <= 0xD7A3:
            offset = code - 0xAC00
            i = offset // 588
            m = (offset % 588) // 28
            f = offset % 28
            result.append(INITIAL[i] + MEDIAL[m] + FINAL[f])
        else:
            result.append(ch)
    return ''.join(result).strip()


# =========================================================
# Translation helpers
# =========================================================
def translate_attraction(name_ko: str) -> Optional[str]:
    """Translate Korean attraction name to English. Returns None if not in dict."""
    if not name_ko:
        return None
    # Exact match
    if name_ko in TRANSLATIONS["attractions"]:
        return TRANSLATIONS["attractions"][name_ko]
    # Substring match (for cases like "경복궁 별빛야행")
    for ko_name, en_name in TRANSLATIONS["attractions"].items():
        if ko_name in name_ko:
            remainder = name_ko.replace(ko_name, "").strip()
            if remainder:
                return f"{en_name} ({romanize(remainder)})"
            return en_name
    return None


def translate_category(content_type_id: str) -> str:
    """Translate contenttypeid to English category."""
    return TRANSLATIONS["categories"].get(str(content_type_id), "Other")


def translate_region(area_code: str) -> Optional[str]:
    """Translate area code to English region name."""
    if not area_code:
        return None
    return TRANSLATIONS["regions"].get(str(area_code))


def translate_address(address_ko: str, area_code: str = None) -> str:
    """Replace 시도 (province) name in address with English."""
    if not address_ko:
        return ""
    province_map = {
        "서울특별시": "Seoul",
        "부산광역시": "Busan",
        "대구광역시": "Daegu",
        "인천광역시": "Incheon",
        "광주광역시": "Gwangju",
        "대전광역시": "Daejeon",
        "울산광역시": "Ulsan",
        "세종특별자치시": "Sejong",
        "경기도": "Gyeonggi-do",
        "강원도": "Gangwon-do",
        "강원특별자치도": "Gangwon-do",
        "충청북도": "Chungcheongbuk-do",
        "충청남도": "Chungcheongnam-do",
        "전라북도": "Jeollabuk-do",
        "전북특별자치도": "Jeollabuk-do",
        "전라남도": "Jeollanam-do",
        "경상북도": "Gyeongsangbuk-do",
        "경상남도": "Gyeongsangnam-do",
        "제주특별자치도": "Jeju-do",
    }
    result = address_ko
    for ko, en in province_map.items():
        if result.startswith(ko):
            result = en + result[len(ko):]
            break
    return result


# =========================================================
# Response transformer
# =========================================================
def transform_item(item: dict) -> dict:
    """Transform a raw KTO API item into English-friendly format."""
    if not isinstance(item, dict):
        return {}

    name_ko = item.get("title", "")
    addr_ko = item.get("addr1", "")
    area_code = item.get("areacode", "")
    content_type_id = item.get("contenttypeid", "")

    name_en = translate_attraction(name_ko)
    name_romanized = romanize(name_ko)

    try:
        latitude = float(item.get("mapy", 0)) if item.get("mapy") else None
    except (ValueError, TypeError):
        latitude = None
    try:
        longitude = float(item.get("mapx", 0)) if item.get("mapx") else None
    except (ValueError, TypeError):
        longitude = None

    def fmt_time(t: str) -> Optional[str]:
        """Convert YYYYMMDDHHMMSS to ISO8601."""
        if not t or len(t) < 8:
            return None
        try:
            return f"{t[0:4]}-{t[4:6]}-{t[6:8]}T{t[8:10]}:{t[10:12]}:{t[12:14]}"
        except IndexError:
            return None

    return {
        "id": item.get("contentid", ""),
        "name": name_en,
        "name_romanized": name_romanized,
        "name_ko": name_ko,
        "category": translate_category(content_type_id),
        "category_id": content_type_id,
        "region": translate_region(area_code),
        "region_code": area_code,
        "address": translate_address(addr_ko, area_code),
        "address_detail": item.get("addr2", ""),
        "address_ko": addr_ko,
        "zipcode": item.get("zipcode", ""),
        "phone": item.get("tel", ""),
        "location": {
            "latitude": latitude,
            "longitude": longitude,
        } if latitude and longitude else None,
        "image": item.get("firstimage", ""),
        "image_thumb": item.get("firstimage2", ""),
        "image_license": item.get("cpyrhtDivCd", ""),
        "created_at": fmt_time(item.get("createdtime", "")),
        "modified_at": fmt_time(item.get("modifiedtime", "")),
    }


# =========================================================
# KTO API caller
# =========================================================
def call_kto(endpoint: str, params: dict) -> dict:
    """Call KTO API and return parsed response."""
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
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Upstream API error: {str(e)}")
    except ValueError:
        raise HTTPException(status_code=502, detail="Invalid JSON from upstream API")

    # Parse standard KTO response structure
    body = data.get("response", {}).get("body", {})
    items = body.get("items", {})

    if not items:
        return {"total": 0, "page": params.get("pageNo", 1), "limit": params.get("numOfRows", 10), "results": []}

    item_list = items.get("item", []) if isinstance(items, dict) else []
    if isinstance(item_list, dict):
        item_list = [item_list]

    return {
        "total": body.get("totalCount", 0),
        "page": int(body.get("pageNo", 1)),
        "limit": int(body.get("numOfRows", 10)),
        "results": [transform_item(i) for i in item_list],
    }


# =========================================================
# Endpoints
# =========================================================
@app.get("/")
def root():
    return {
        "name": "Korea Tourism API",
        "version": "1.0.0",
        "description": "English-friendly Korean tourism data API",
        "endpoints": [
            "/search",
            "/attractions/by-location",
            "/attractions/by-region",
            "/attractions/{contentId}",
            "/festivals",
        ],
        "docs": "/docs",
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/search")
def search(
    keyword: str = Query(..., description="Search keyword (Korean or English supported)"),
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(10, ge=1, le=100, description="Results per page (max 100)"),
    category: Optional[int] = Query(None, description="Filter by category ID (12=attraction, 15=festival, 32=accommodation, 39=restaurant)"),
):
    """Search Korean tourism information by keyword.

    Searches across attractions, restaurants, festivals, accommodations, and more.
    Supports both Korean and English keywords (English keywords work for romanized place names).
    """
    params = {
        "keyword": keyword,
        "pageNo": page,
        "numOfRows": limit,
    }
    if category:
        params["contentTypeId"] = category
    return call_kto("searchKeyword2", params)


@app.get("/attractions/by-location")
def by_location(
    latitude: float = Query(..., description="Latitude (e.g., 37.5760)"),
    longitude: float = Query(..., description="Longitude (e.g., 126.9767)"),
    radius: int = Query(1000, ge=1, le=20000, description="Search radius in meters (max 20km)"),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    category: Optional[int] = Query(None, description="Filter by category ID"),
):
    """Find tourism information near a specific GPS location.

    Useful for "what's near me" features in mobile apps.
    """
    params = {
        "mapX": longitude,
        "mapY": latitude,
        "radius": radius,
        "pageNo": page,
        "numOfRows": limit,
    }
    if category:
        params["contentTypeId"] = category
    return call_kto("locationBasedList2", params)


@app.get("/attractions/by-region")
def by_region(
    area_code: int = Query(..., description="Region code (1=Seoul, 6=Busan, 39=Jeju, etc.)"),
    sigungu_code: Optional[int] = Query(None, description="District code within the region (optional)"),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    category: Optional[int] = Query(None, description="Filter by category ID"),
):
    """Browse tourism information by region (province/city).

    Region codes: 1=Seoul, 2=Incheon, 3=Daejeon, 4=Daegu, 5=Gwangju, 6=Busan,
    7=Ulsan, 8=Sejong, 31=Gyeonggi-do, 32=Gangwon-do, 39=Jeju-do, etc.
    """
    params = {
        "areaCode": area_code,
        "pageNo": page,
        "numOfRows": limit,
    }
    if sigungu_code:
        params["sigunguCode"] = sigungu_code
    if category:
        params["contentTypeId"] = category
    return call_kto("areaBasedList2", params)


@app.get("/attractions/{content_id}")
def attraction_detail(
    content_id: str,
    content_type_id: Optional[int] = Query(None, description="Content type ID for richer detail (optional)"),
):
    """Get detailed information about a specific attraction by its content ID.

    The content ID is returned in search/list responses as `id`.
    """
    params = {
        "contentId": content_id,
    }
    if content_type_id:
        params["contentTypeId"] = content_type_id

    result = call_kto("detailCommon2", params)

    if not result["results"]:
        raise HTTPException(status_code=404, detail=f"Attraction with ID {content_id} not found")

    return result["results"][0]


@app.get("/festivals")
def festivals(
    start_date: str = Query(..., description="Festival start date filter (YYYYMMDD format)"),
    end_date: Optional[str] = Query(None, description="Festival end date filter (YYYYMMDD, optional)"),
    area_code: Optional[int] = Query(None, description="Filter by region code"),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
):
    """Search Korean festivals and events by date.

    Returns festivals happening on or after the specified start date.
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
    return call_kto("searchFestival2", params)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
