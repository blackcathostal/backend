from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import html
import re
from typing import Any

import httpx

from app.core.config import settings


def _clean(value: Any, limit: int = 320) -> str:
    text = re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()
    return text[:limit]


def _future_date(value: Any) -> bool:
    if not value:
        return True
    text = str(value).replace("Z", "+00:00")
    try:
        if len(text) == 10:
            return datetime.fromisoformat(text).date() >= datetime.now(timezone.utc).date()
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed >= datetime.now(timezone.utc)


def _material(
    source: str,
    endpoint: str,
    records: list[dict[str, Any]],
    *,
    note: str = "",
) -> dict[str, Any]:
    lines = [f"FUENTE API: {source}", f"ENDPOINT: {endpoint}"]
    if note:
        lines.append(note)
    for index, record in enumerate(records, start=1):
        lines.append(
            f"{index}. "
            + " | ".join(
                f"{key}: {_clean(value)}"
                for key, value in record.items()
                if value not in (None, "", [], {})
            )
        )
    return {
        "source": source,
        "name": source,
        "endpoint": endpoint,
        "url": endpoint,
        "text": "\n".join(lines),
        "records": len(records),
    }


async def _get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    try:
        async with httpx.AsyncClient(
            timeout=settings.deepseek_source_timeout_seconds,
            follow_redirects=True,
        ) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, (dict, list)) else None
    except (httpx.HTTPError, ValueError):
        return None


async def _post_json(
    url: str,
    *,
    data: str,
    headers: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient(
            timeout=settings.deepseek_source_timeout_seconds,
            follow_redirects=True,
        ) as client:
            response = await client.post(url, content=data, headers=headers)
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else None
    except (httpx.HTTPError, ValueError):
        return None


async def fetch_sernatur_attractions() -> dict[str, Any] | None:
    payload = await _get_json(
        settings.sernatur_attractions_api_url,
        params={
            "where": "1=1",
            "outFields": "*",
            "returnGeometry": "false",
            "resultRecordCount": settings.ai_api_max_records,
            "f": "json",
        },
    )
    features = (payload or {}).get("features") or []
    records = [
        {
            "nombre": feature.get("attributes", {}).get("NOMBRE")
            or feature.get("attributes", {}).get("NOM_ATRAC")
            or feature.get("attributes", {}).get("NOMBRE_ATR"),
            "categoría": feature.get("attributes", {}).get("CATEGORIA"),
            "comuna": feature.get("attributes", {}).get("COMUNA"),
            "región": feature.get("attributes", {}).get("REGION"),
            "descripción": feature.get("attributes", {}).get("DESCRIPCION"),
        }
        for feature in features
        if isinstance(feature, dict)
    ]
    records = [record for record in records if record["nombre"]]
    return _material("SERNATUR: atractivos turísticos nacionales", settings.sernatur_attractions_api_url, records)
    

async def fetch_sernatur_network() -> dict[str, Any] | None:
    payload = await _get_json(
        settings.sernatur_network_api_url,
        params={
            "where": "1=1",
            "outFields": "*",
            "returnGeometry": "false",
            "resultRecordCount": settings.ai_api_max_records,
            "f": "json",
        },
    )
    features = (payload or {}).get("features") or []
    records = []
    for feature in features:
        attributes = feature.get("attributes", {}) if isinstance(feature, dict) else {}
        record = {
            "entidad o lugar": attributes.get("NOMBRE")
            or attributes.get("NOM_ESTAB")
            or attributes.get("NOMBRE_EST"),
            "tipo": attributes.get("TIPO") or attributes.get("CATEGORIA"),
            "comuna": attributes.get("COMUNA"),
            "región": attributes.get("REGION"),
            "dirección": attributes.get("DIRECCION"),
        }
        if record["entidad o lugar"]:
            records.append(record)
    return _material("SERNATUR: red de turismo de Chile", settings.sernatur_network_api_url, records)


async def fetch_open_data_catalog() -> dict[str, Any] | None:
    payload = await _get_json(
        settings.chile_open_data_api_url,
        params={
            "q": settings.chile_open_data_query,
            "rows": settings.ai_api_max_records,
            "sort": "metadata_modified desc",
        },
    )
    records = []
    for item in ((payload or {}).get("result") or {}).get("results") or []:
        if not isinstance(item, dict):
            continue
        records.append(
            {
                "conjunto de datos": item.get("title"),
                "organización": (item.get("organization") or {}).get("title"),
                "descripción": item.get("notes"),
                "actualizado": item.get("metadata_modified"),
                "enlace": f"https://datos.gob.cl/dataset/{item.get('name')}" if item.get("name") else "",
            }
        )
    return _material("Portal de Datos Abiertos de Chile", settings.chile_open_data_api_url, records)


async def fetch_ticketmaster_events() -> dict[str, Any] | None:
    if not settings.ticketmaster_api_key.strip():
        return None
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = await _get_json(
        settings.ticketmaster_events_api_url,
        params={
            "apikey": settings.ticketmaster_api_key,
            "city": settings.ai_api_city,
            "countryCode": "CL",
            "startDateTime": now,
            "size": settings.ai_api_max_records,
            "sort": "date,asc",
        },
    )
    records = []
    for event in ((payload or {}).get("_embedded") or {}).get("events") or []:
        dates = event.get("dates") or {}
        start = dates.get("start") or {}
        venue = ((event.get("_embedded") or {}).get("venues") or [{}])[0]
        if not _future_date(start.get("dateTime") or start.get("localDate")):
            continue
        records.append(
            {
                "evento": event.get("name"),
                "fecha": start.get("dateTime") or start.get("localDate"),
                "lugar": venue.get("name"),
                "dirección": venue.get("address", {}).get("line1"),
                "ciudad": venue.get("city", {}).get("name"),
                "categoría": ((event.get("classifications") or [{}])[0].get("segment") or {}).get("name"),
                "enlace oficial": event.get("url"),
            }
        )
    return _material("Ticketmaster Discovery API", settings.ticketmaster_events_api_url, records)


async def fetch_bandsintown_events() -> dict[str, Any] | None:
    if not settings.bandsintown_app_id.strip() or not settings.bandsintown_artists:
        return None
    results = await asyncio.gather(
        *(
            _get_json(
                f"{settings.bandsintown_api_url.rstrip('/')}/artists/{artist}/events",
                params={"app_id": settings.bandsintown_app_id, "date": "upcoming"},
                headers={"Accept": "application/json"},
            )
            for artist in settings.bandsintown_artists[: settings.ai_api_max_artists]
        )
    )
    records = []
    for payload in results:
        for event in payload or []:
            if not isinstance(event, dict) or not _future_date(event.get("datetime")):
                continue
            venue = event.get("venue") or {}
            records.append(
                {
                    "evento": event.get("title"),
                    "artista": event.get("lineup"),
                    "fecha": event.get("datetime"),
                    "lugar": venue.get("name"),
                    "ciudad": venue.get("city"),
                    "país": venue.get("country"),
                    "enlace oficial": event.get("url"),
                }
            )
    endpoint = f"{settings.bandsintown_api_url.rstrip('/')}/artists/{{artist}}/events"
    return _material("Bandsintown API", endpoint, records)


async def fetch_songkick_events() -> dict[str, Any] | None:
    if not settings.songkick_api_key.strip():
        return None
    today = datetime.now(timezone.utc).date()
    payload = await _get_json(
        settings.songkick_events_api_url,
        params={
            "apikey": settings.songkick_api_key,
            "location": settings.songkick_location,
            "min_date": today.isoformat(),
            "max_date": (today + timedelta(days=90)).isoformat(),
            "per_page": settings.ai_api_max_records,
        },
    )
    records = []
    for event in ((payload or {}).get("resultsPage") or {}).get("results", {}).get("event", []) or []:
        if not isinstance(event, dict) or not _future_date(event.get("start", {}).get("date")):
            continue
        venue = event.get("venue") or {}
        records.append(
            {
                "evento": event.get("displayName"),
                "fecha": event.get("start", {}).get("date"),
                "hora": event.get("start", {}).get("time"),
                "lugar": venue.get("displayName"),
                "ciudad": (venue.get("metroArea") or {}).get("displayName"),
                "enlace oficial": event.get("uri"),
            }
        )
    return _material("Songkick API", settings.songkick_events_api_url, records)


async def fetch_openstreetmap_places() -> dict[str, Any] | None:
    query = f"""
[out:json][timeout:20];
(
  nwr(around:{settings.ai_api_radius_meters},{settings.ai_api_latitude},{settings.ai_api_longitude})
    ["tourism"~"museum|gallery|attraction|viewpoint|theme_park"];
  nwr(around:{settings.ai_api_radius_meters},{settings.ai_api_latitude},{settings.ai_api_longitude})
    ["amenity"~"theatre|arts_centre|cinema|music_venue"];
);
out center tags;
"""
    payload = await _post_json(
        settings.openstreetmap_overpass_api_url,
        data=query,
        headers={"Content-Type": "text/plain", "User-Agent": "BlackCatTourismBot/1.0"},
    )
    records = []
    for element in ((payload or {}).get("elements") or [])[: settings.ai_api_max_records]:
        tags = element.get("tags") or {}
        if not tags.get("name"):
            continue
        records.append(
            {
                "lugar": tags.get("name"),
                "tipo": tags.get("tourism") or tags.get("amenity"),
                "dirección": " ".join(
                    part for part in (tags.get("addr:street"), tags.get("addr:housenumber")) if part
                ),
                "sitio web": tags.get("website") or tags.get("contact:website"),
            }
        )
    return _material("OpenStreetMap Overpass API", settings.openstreetmap_overpass_api_url, records)


async def fetch_open_meteo() -> dict[str, Any] | None:
    payload = await _get_json(
        settings.open_meteo_api_url,
        params={
            "latitude": settings.ai_api_latitude,
            "longitude": settings.ai_api_longitude,
            "current": "temperature_2m,precipitation,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
            "timezone": "America/Santiago",
            "forecast_days": 7,
        },
    )
    if not payload:
        return None
    current = payload.get("current") or {}
    daily = payload.get("daily") or {}
    records = [
        {
            "tipo": "condiciones actuales en Santiago",
            "fecha": current.get("time"),
            "temperatura": current.get("temperature_2m"),
            "precipitación": current.get("precipitation"),
            "código meteorológico": current.get("weather_code"),
        }
    ]
    for index, date in enumerate(daily.get("time") or []):
        records.append(
            {
                "tipo": "pronóstico diario en Santiago",
                "fecha": date,
                "máxima": (daily.get("temperature_2m_max") or [])[index],
                "mínima": (daily.get("temperature_2m_min") or [])[index],
                "probabilidad de precipitación": (daily.get("precipitation_probability_max") or [])[index],
                "código meteorológico": (daily.get("weather_code") or [])[index],
            }
        )
    return _material("Open-Meteo API", settings.open_meteo_api_url, records)


async def collect_external_api_materials() -> list[dict[str, Any]]:
    """Collect optional REST API data without failing the article generation."""
    results = await asyncio.gather(
        fetch_sernatur_attractions(),
        fetch_sernatur_network(),
        fetch_open_data_catalog(),
        fetch_ticketmaster_events(),
        fetch_bandsintown_events(),
        fetch_songkick_events(),
        fetch_openstreetmap_places(),
        fetch_open_meteo(),
        return_exceptions=True,
    )
    return [
        result
        for result in results
        if isinstance(result, dict) and result.get("text") and result.get("records", 0) > 0
    ]
