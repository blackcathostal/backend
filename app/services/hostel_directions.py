"""Routes from Black Cat Hostal to a place in Santiago (metro/bus, car, walk last)."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx

from app.core.config import settings
from app.services.short_links import shorten_url

HOSTEL_ADDRESS = "Compañía de Jesús 1921, Barrio Brasil, Santiago, Chile"
HOSTEL_LAT = -33.4396639
HOSTEL_LNG = -70.6638072
USER_AGENT = "BlackCatHostal/1.0 (reservas@blackcathostal.com)"
# Prefer public transport, then car. Walking only if metro/bus is not available.
FROM_HOSTEL_MODES = ("transit", "driving", "walking")


def _public_api_origin() -> str:
    """Instagram must fetch the map image over public HTTPS (not localhost)."""
    redirect = (settings.meta_redirect_uri or "").strip()
    if redirect.startswith("https://"):
        from urllib.parse import urlparse

        parsed = urlparse(redirect)
        host = (parsed.netloc or "").lower()
        if host and "localhost" not in host and not host.startswith("127."):
            return f"https://{parsed.netloc}"
    return "https://api.blackcathostal.com"


def _save_map_bytes(content: bytes) -> str:
    is_jpg = content.startswith(b"\xff\xd8\xff")
    is_png = content.startswith(b"\x89PNG")
    if len(content) < 800 or not (is_jpg or is_png):
        return ""
    folder = settings.uploads_dir / "maps"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{uuid4().hex}.{'jpg' if is_jpg else 'png'}"
    (folder / name).write_bytes(content)
    return f"{_public_api_origin()}/uploads/maps/{name}"


def _google_static_map_params(*, dest_lat: float, dest_lng: float) -> dict[str, Any] | None:
    key = (settings.google_places_api_key or "").strip()
    if not key:
        return None
    origin = f"{HOSTEL_LAT},{HOSTEL_LNG}"
    dest = f"{dest_lat},{dest_lng}"
    return {
        "size": "800x500",
        "scale": "2",
        "format": "jpg",
        "maptype": "roadmap",
        "language": "es",
        "region": "cl",
        "markers": [
            f"color:0x111111|label:H|{origin}",
            f"color:0xc45a12|label:D|{dest}",
        ],
        "path": f"color:0x1a73e8ff|weight:5|{origin}|{dest}",
        "key": key,
    }


def _google_static_map_direct_url(*, dest_lat: float, dest_lng: float) -> str:
    """Google Static Maps image URL (not the giant /maps/dir directions link)."""
    params = _google_static_map_params(dest_lat=dest_lat, dest_lng=dest_lng)
    if not params:
        return ""
    # markers/path are repeated keys; build manually for a clean URL.
    parts = [
        f"size={params['size']}",
        f"scale={params['scale']}",
        f"format={params['format']}",
        f"maptype={params['maptype']}",
        f"language={params['language']}",
        f"region={params['region']}",
    ]
    for marker in params["markers"]:
        parts.append(f"markers={quote(marker, safe=':|,')}")
    parts.append(f"path={quote(str(params['path']), safe=':|,')}")
    parts.append(f"key={quote(str(params['key']), safe='')}")
    return "https://maps.googleapis.com/maps/api/staticmap?" + "&".join(parts)


def _store_google_static_map(*, dest_lat: float, dest_lng: float) -> str:
    params = _google_static_map_params(dest_lat=dest_lat, dest_lng=dest_lng)
    if not params:
        return ""
    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.get(
                "https://maps.googleapis.com/maps/api/staticmap",
                params=params,
            )
            response.raise_for_status()
            hosted = _save_map_bytes(response.content)
            if hosted:
                return hosted
    except (httpx.HTTPError, ValueError):
        pass
    # Server IP may be blocked by the Google key; Meta can still fetch Google’s image.
    return _google_static_map_direct_url(dest_lat=dest_lat, dest_lng=dest_lng)


def _latlng_to_tile(lat: float, lng: float, zoom: int) -> tuple[float, float]:
    import math

    lat = min(max(lat, -85.05112878), 85.05112878)
    n = 2 ** zoom
    x = (lng + 180.0) / 360.0 * n
    lat_rad = math.radians(lat)
    y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def _store_osm_static_map(*, dest_lat: float, dest_lng: float) -> str:
    """Build a map from OSM tiles when Google Static Maps is blocked on the server."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return ""

    import math

    zoom = 13
    width, height = 800, 500
    mid_lat = (HOSTEL_LAT + dest_lat) / 2.0
    mid_lng = (HOSTEL_LNG + dest_lng) / 2.0
    # Zoom out a bit for longer routes.
    try:
        dist_km = abs(HOSTEL_LAT - dest_lat) * 111 + abs(HOSTEL_LNG - dest_lng) * 85
        if dist_km > 4:
            zoom = 12
        if dist_km > 8:
            zoom = 11
    except Exception:
        pass

    cx, cy = _latlng_to_tile(mid_lat, mid_lng, zoom)
    tile_size = 256
    cols = math.ceil(width / tile_size) + 1
    rows = math.ceil(height / tile_size) + 1
    start_x = int(cx) - cols // 2
    start_y = int(cy) - rows // 2
    canvas = Image.new("RGB", (cols * tile_size, rows * tile_size), "#e8e8e8")
    try:
        with httpx.Client(
            timeout=20.0,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        ) as client:
            for ty in range(rows):
                for tx in range(cols):
                    x = start_x + tx
                    y = start_y + ty
                    n = 2 ** zoom
                    if y < 0 or y >= n:
                        continue
                    x = x % n
                    tile_url = f"https://tile.openstreetmap.org/{zoom}/{x}/{y}.png"
                    try:
                        response = client.get(tile_url)
                        response.raise_for_status()
                        tile = Image.open(__import__("io").BytesIO(response.content)).convert("RGB")
                        canvas.paste(tile, (tx * tile_size, ty * tile_size))
                    except (httpx.HTTPError, OSError, ValueError):
                        continue
    except httpx.HTTPError:
        return ""

    # Crop around the center point.
    center_px = int((cx - start_x) * tile_size)
    center_py = int((cy - start_y) * tile_size)
    left = max(0, center_px - width // 2)
    top = max(0, center_py - height // 2)
    image = canvas.crop((left, top, left + width, top + height))
    draw = ImageDraw.Draw(image)

    def _plot(lat: float, lng: float, fill: str) -> None:
        x, y = _latlng_to_tile(lat, lng, zoom)
        px = int((x - start_x) * tile_size) - left
        py = int((y - start_y) * tile_size) - top
        r = 10
        draw.ellipse((px - r, py - r, px + r, py + r), fill=fill, outline="#ffffff", width=2)

    _plot(HOSTEL_LAT, HOSTEL_LNG, "#111111")
    _plot(dest_lat, dest_lng, "#c45a12")
    # Draw a simple line between markers.
    x1, y1 = _latlng_to_tile(HOSTEL_LAT, HOSTEL_LNG, zoom)
    x2, y2 = _latlng_to_tile(dest_lat, dest_lng, zoom)
    p1 = (int((x1 - start_x) * tile_size) - left, int((y1 - start_y) * tile_size) - top)
    p2 = (int((x2 - start_x) * tile_size) - left, int((y2 - start_y) * tile_size) - top)
    draw.line([p1, p2], fill="#1a73e8", width=4)

    import io

    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=85)
    return _save_map_bytes(buf.getvalue())


def _store_static_map(*, dest_lat: float, dest_lng: float, label: str = "") -> str:
    """Google Maps image for Instagram (never the giant directions URL in the text)."""
    del label  # reserved for future captioning
    google = _store_google_static_map(dest_lat=dest_lat, dest_lng=dest_lng)
    if google:
        return google
    # Last resort only if Google key is missing entirely.
    return _store_osm_static_map(dest_lat=dest_lat, dest_lng=dest_lng)


def directions_to_hostel(origin: str, mode: str = "transit") -> dict[str, Any]:
    """Route from a Santiago point to the hostel, with a Google Maps link."""
    place = (origin or "").strip()
    travel = "walking" if mode == "walking" else "transit"
    maps_url = (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={quote(f'{place}, Santiago, Chile')}"
        f"&destination={quote(HOSTEL_ADDRESS)}"
        f"&travelmode={travel}"
    )
    maps_url = shorten_url(maps_url)
    result: dict[str, Any] = {
        "ok": True,
        "origin": place,
        "destination": HOSTEL_ADDRESS,
        "mode": travel,
        "distance": "",
        "duration": "",
        "lines": [],
        "maps_url": maps_url,
    }
    located: dict[str, Any] | None = None
    try:
        located = _geocode(place)
    except httpx.HTTPError:
        located = None
    route = _google_route(located, travel) if located else None
    if route is None and located and travel == "walking":
        route = _foot_summary(located["lat"], located["lng"], HOSTEL_LAT, HOSTEL_LNG)
    if route:
        result.update(route)
    sentences = [f"Desde {place} hasta Black Cat Hostal, {HOSTEL_ADDRESS}."]
    if result["duration"]:
        sentences.append(f"Tiempo: {result['duration']}. Distancia: {result['distance']}.")
    if result["lines"]:
        sentences.append("En el camino: " + ", ".join(result["lines"]) + ".")
    if maps_url:
        sentences.append(f"Mapa: {maps_url}")
    result["summary"] = " ".join(sentences)
    return result


def _google_route(origin: dict[str, Any] | None, mode: str) -> dict[str, Any] | None:
    if not origin:
        return None
    return _google_directions(
        origin_lat=float(origin["lat"]),
        origin_lng=float(origin["lng"]),
        dest_lat=HOSTEL_LAT,
        dest_lng=HOSTEL_LNG,
        mode=mode,
    )


def _google_directions(
    *,
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float,
    mode: str,
) -> dict[str, Any] | None:
    from app.core.config import settings

    key = (settings.google_places_api_key or "").strip()
    if not key:
        return None
    try:
        with httpx.Client(timeout=12.0) as client:
            response = client.get(
                "https://maps.googleapis.com/maps/api/directions/json",
                params={
                    "origin": f"{origin_lat},{origin_lng}",
                    "destination": f"{dest_lat},{dest_lng}",
                    "mode": mode,
                    "language": "es",
                    "region": "cl",
                    "key": key,
                },
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if payload.get("status") != "OK":
        return None
    routes = payload.get("routes") or []
    legs = (routes[0].get("legs") if routes else None) or []
    if not legs:
        return None
    leg = legs[0]
    lines: list[str] = []
    has_metro = False
    has_bus = False
    for step in leg.get("steps") or []:
        transit = step.get("transit_details") or {}
        line = transit.get("line") or {}
        vehicle_info = line.get("vehicle") or {}
        vehicle = str(vehicle_info.get("name") or vehicle_info.get("type") or "").strip()
        vehicle_type = str(vehicle_info.get("type") or "").strip().upper()
        label = str(line.get("short_name") or line.get("name") or "").strip()
        is_metro = vehicle_type in {"SUBWAY", "METRO", "HEAVY_RAIL", "COMMUTER_TRAIN"} or "metro" in vehicle.lower()
        is_bus = vehicle_type in {"BUS", "TROLLEYBUS"} or "bus" in vehicle.lower() or "micro" in vehicle.lower()
        if is_metro:
            has_metro = True
        if is_bus:
            has_bus = True
        if label:
            if is_metro:
                lines.append(f"metro {label}")
            elif is_bus:
                lines.append(f"micro {label}")
            else:
                lines.append(f"{vehicle} {label}".strip() if vehicle else label)
        if len(lines) >= 4:
            break
    return {
        "mode": mode,
        "distance": str((leg.get("distance") or {}).get("text") or ""),
        "duration": str((leg.get("duration") or {}).get("text") or ""),
        "lines": lines,
        "has_metro": has_metro,
        "has_bus": has_bus,
        "moves": [],
    }


def _foot_summary(lat1: float, lng1: float, lat2: float, lng2: float) -> dict[str, Any] | None:
    url = f"https://router.project-osrm.org/route/v1/foot/{lng1},{lat1};{lng2},{lat2}"
    try:
        with httpx.Client(timeout=12.0, headers={"User-Agent": USER_AGENT}) as client:
            response = client.get(url, params={"overview": "false", "steps": "false"})
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    routes = payload.get("routes") or []
    if payload.get("code") != "Ok" or not routes:
        return None
    meters = int(routes[0].get("distance") or 0)
    seconds = int(round(meters / 1.33))
    return {
        "distance": _format_distance(meters),
        "duration": _format_duration(seconds),
        "lines": [],
    }


def directions_from_hostel(destination: str) -> dict[str, Any]:
    """How to get from the hostel: metro/bus first, then car; walk only if no transit."""
    place = (destination or "").strip()
    if len(place) < 2:
        return {"ok": False, "error": "Indica el lugar de destino."}

    try:
        dest = _geocode(place)
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"No se pudo buscar el lugar: {exc}"}
    if not dest:
        return {
            "ok": False,
            "error": f"No encontré '{place}' en Santiago. Pide un nombre más preciso.",
        }

    route = _route_from_hostel(dest["lat"], dest["lng"])
    if not route:
        return {
            "ok": False,
            "destination": dest["name"],
            "error": "No encontré una ruta en metro, bus o auto hasta ese lugar.",
        }

    hint = _place_hint(place) or _place_hint(str(dest.get("name") or ""))
    route_mode = str(route.get("mode") or "")
    use_hint: dict[str, Any] | None = None
    # Nearby walks stay as walks. Hints cover Google failures and key metro landmarks.
    google_missed = not (route.get("has_metro") or route.get("has_bus") or route.get("lines"))
    prefer_metro_hint = bool(hint and hint.get("has_metro") and not route.get("has_metro"))
    if hint and route_mode != "walking" and (google_missed or prefer_metro_hint):
        use_hint = hint
        route = {
            **route,
            "mode": "transit",
            "has_metro": bool(hint.get("has_metro")),
            "has_bus": bool(hint.get("has_bus")),
            "lines": list(hint.get("lines") or []),
        }

    travel = str(route.get("mode") or "transit")
    travelmode = "transit" if travel == "transit" else ("walking" if travel == "walking" else "driving")
    raw_maps = (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={quote(HOSTEL_ADDRESS)}"
        f"&destination={quote(dest['name'] + ', Santiago, Chile')}"
        f"&travelmode={travelmode}"
    )
    maps_url = shorten_url(raw_maps) or ""
    map_image_url = _store_static_map(
        dest_lat=float(dest["lat"]),
        dest_lng=float(dest["lng"]),
        label=str(dest["name"] or place),
    )
    replies = _guest_replies(
        destination=dest["name"],
        distance=route["distance"],
        duration=route["duration"],
        mode=travel,
        lines=list(route.get("lines") or []),
        has_metro=bool(route.get("has_metro")),
        has_bus=bool(route.get("has_bus")),
        maps_url="",
        moves=list(route.get("moves") or []),
        hint=use_hint,
    )
    return {
        "ok": True,
        "origin": HOSTEL_ADDRESS,
        "destination": dest["name"],
        "destination_address": dest["address"],
        "mode": travel,
        "distance": route["distance"],
        "duration": route["duration"],
        "lines": list(route.get("lines") or []),
        "summary": replies["es"].split("\n\n", 1)[0],
        "maps_url": maps_url,
        "map_image_url": map_image_url,
        "replies": replies,
        "instruction": (
            "Usa replies según el idioma del huésped, casi textual y UNA sola vez. "
            "No repitas el mismo párrafo. Prioriza metro/micro/auto. "
            "Cierra con «Te lo indico en el siguiente mapa.» sin pegar ninguna URL. "
            "La imagen del mapa se envía aparte. No digas Google Maps. No mandes a WhatsApp."
        ),
    }


def _duration_minutes(duration: str) -> int | None:
    text = str(duration or "").strip().lower()
    if not text:
        return None
    total = 0
    found = False
    for match in re.finditer(r"(\d+)\s*(h|hr|hora|horas|min|mins|minute|minutes)?", text):
        value = int(match.group(1))
        unit = (match.group(2) or "").lower()
        if unit.startswith("h"):
            total += value * 60
            found = True
        elif unit.startswith("min") or not unit:
            total += value
            found = True
    return total if found else None


def _place_hint(place: str) -> dict[str, Any] | None:
    sample = (place or "").strip().lower()
    if not sample:
        return None
    catalog = [
        (
            ("estadio nacional",),
            {
                "has_metro": True,
                "has_bus": False,
                "lines": ["metro L5 (Ñuble o Grecia)"],
                "es": "Lo más cómodo es el metro Línea 5 (Ñuble o Grecia).",
                "en": "The easiest way is metro Line 5 (Ñuble or Grecia).",
                "pt": "O mais cômodo é o metrô Linha 5 (Ñuble ou Grecia).",
            },
        ),
        (
            ("santander arena", "movistar arena"),
            {
                "has_metro": False,
                "has_bus": True,
                "lines": ["micro por Beaucheff / Parque O'Higgins"],
                "es": "Te sirve micro por Beaucheff / Parque O'Higgins; si no, taxi o Uber.",
                "en": "Take a bus toward Beaucheff / Parque O'Higgins, or a taxi/Uber.",
                "pt": "Pegue um ônibus por Beaucheff / Parque O'Higgins; senão, táxi ou Uber.",
            },
        ),
        (
            ("la moneda", "palacio de la moneda", "moneda"),
            {
                "has_metro": True,
                "has_bus": False,
                "lines": ["metro L1 (La Moneda)"],
                "es": "Queda cerca; a pie o metro Línea 1 (La Moneda).",
                "en": "It is close by; walk or take metro Line 1 (La Moneda).",
                "pt": "Fica perto; a pé ou metrô Linha 1 (La Moneda).",
            },
        ),
    ]
    for keys, hint in catalog:
        if any(key in sample for key in keys):
            return hint
    return None


def _osrm_route(dest_lat: float, dest_lng: float, *, profile: str) -> dict[str, Any] | None:
    url = (
        f"https://router.project-osrm.org/route/v1/{profile}/"
        f"{HOSTEL_LNG},{HOSTEL_LAT};{dest_lng},{dest_lat}"
    )
    try:
        with httpx.Client(timeout=12.0, headers={"User-Agent": USER_AGENT}) as client:
            response = client.get(url, params={"overview": "false", "steps": "false"})
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    routes = payload.get("routes") or []
    if payload.get("code") != "Ok" or not routes:
        return None
    meters = int(routes[0].get("distance") or 0)
    seconds = int(routes[0].get("duration") or 0)
    if profile == "foot":
        seconds = int(round(meters / 1.33))
    mode = "walking" if profile == "foot" else "driving"
    return {
        "mode": mode,
        "distance": _format_distance(meters),
        "duration": _format_duration(seconds),
        "lines": [],
        "moves": [],
        "has_metro": False,
        "has_bus": False,
    }


def _route_from_hostel(dest_lat: float, dest_lng: float) -> dict[str, Any] | None:
    """Nearby walk first; else metro/bus, then car. Never suggest a long walk as 'simple'."""
    try:
        walking = _walking_route(dest_lat, dest_lng)
    except httpx.HTTPError:
        walking = None
    walk_mins = _duration_minutes(str((walking or {}).get("duration") or ""))
    if walking and walk_mins is not None and walk_mins <= 25:
        walking["mode"] = "walking"
        return walking

    transit = _google_directions(
        origin_lat=HOSTEL_LAT,
        origin_lng=HOSTEL_LNG,
        dest_lat=dest_lat,
        dest_lng=dest_lng,
        mode="transit",
    )
    if transit and (transit.get("has_metro") or transit.get("has_bus") or transit.get("lines")):
        return transit

    driving = _google_directions(
        origin_lat=HOSTEL_LAT,
        origin_lng=HOSTEL_LNG,
        dest_lat=dest_lat,
        dest_lng=dest_lng,
        mode="driving",
    )
    if driving:
        return driving

    # Production Google Directions is often blocked: fall back to OSRM car.
    osrm_car = _osrm_route(dest_lat, dest_lng, profile="driving")
    if osrm_car and (walk_mins is None or walk_mins > 25):
        return osrm_car

    if walking:
        walking["mode"] = "walking"
        return walking
    if transit:
        return transit
    return _osrm_route(dest_lat, dest_lng, profile="foot")


def _geocode(place: str) -> dict[str, Any] | None:
    query = place if "santiago" in place.lower() else f"{place}, Santiago, Chile"
    with httpx.Client(timeout=12.0, headers={"User-Agent": USER_AGENT}) as client:
        response = client.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": query,
                "format": "jsonv2",
                "limit": 1,
                "countrycodes": "cl",
            },
        )
        response.raise_for_status()
        rows = response.json()
    if not rows:
        return None
    row = rows[0]
    return {
        "name": (row.get("name") or place).strip() or place,
        "address": row.get("display_name") or place,
        "lat": float(row["lat"]),
        "lng": float(row["lon"]),
    }


def _walking_route(dest_lat: float, dest_lng: float) -> dict[str, Any] | None:
    url = (
        "https://router.project-osrm.org/route/v1/foot/"
        f"{HOSTEL_LNG},{HOSTEL_LAT};{dest_lng},{dest_lat}"
    )
    with httpx.Client(timeout=12.0, headers={"User-Agent": USER_AGENT}) as client:
        response = client.get(url, params={"overview": "false", "steps": "true"})
        response.raise_for_status()
        payload = response.json()
    routes = payload.get("routes") or []
    if payload.get("code") != "Ok" or not routes:
        return None
    route = routes[0]
    legs = route.get("legs") or [{}]
    moves = _walking_moves(legs[0].get("steps") or [])
    meters = int(route.get("distance") or 0)
    # The public OSRM foot profile reports car-like times. Use a normal walking pace.
    seconds = int(round(meters / 1.33))
    return {
        "mode": "walking",
        "distance": _format_distance(meters),
        "duration": _format_duration(seconds),
        "lines": [],
        "moves": moves,
        "has_metro": False,
        "has_bus": False,
    }


def _walking_moves(steps: list[dict[str, Any]]) -> list[dict[str, str]]:
    moves: list[dict[str, str]] = []
    for step in steps:
        maneuver = step.get("maneuver") or {}
        kind = str(maneuver.get("type") or "")
        modifier = str(maneuver.get("modifier") or "")
        name = _street_name(str(step.get("name") or ""))
        meters = int(step.get("distance") or 0)
        if kind == "arrive" or not name:
            continue
        if meters < 40 and kind != "depart":
            continue
        action = _move_action(kind, modifier)
        if not action:
            continue
        if moves and moves[-1]["street"] == name and moves[-1]["action"] == action:
            continue
        moves.append({"action": action, "street": name})
        if len(moves) >= 4:
            break
    return moves


def _street_name(name: str) -> str:
    cleaned = " ".join(name.replace("Avenida", "avenida").split())
    for prefix in ("Calle ", "Av. ", "Avda. "):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break
    return cleaned.strip()


def _move_action(kind: str, modifier: str) -> str:
    if kind == "depart":
        return "start"
    if modifier in {"left", "slight left", "sharp left"}:
        return "left"
    if modifier in {"right", "slight right", "sharp right"}:
        return "right"
    if modifier == "uturn":
        return "back"
    if kind in {"turn", "new name", "continue", "fork", "end of road", "depart"} or modifier == "straight":
        return "straight"
    return ""


def _join_clauses(clauses: list[str], conjunction: str = "y") -> str:
    cleaned = [item.strip() for item in clauses if item.strip()]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    return f"{', '.join(cleaned[:-1])} {conjunction} {cleaned[-1]}"


def _route_clauses(moves: list[dict[str, str]], language: str) -> str:
    clauses: list[str] = []
    for move in moves:
        street = move["street"]
        action = move["action"]
        if language == "en":
            if action == "start":
                clauses.append(f"leave the hostel along {street}")
            elif action == "left":
                clauses.append(f"turn left onto {street}")
            elif action == "right":
                clauses.append(f"turn right onto {street}")
            elif action == "back":
                clauses.append(f"turn around onto {street}")
            else:
                clauses.append(f"continue along {street}")
        elif language == "pt":
            if action == "start":
                clauses.append(f"saia do hostel pela {street}")
            elif action == "left":
                clauses.append(f"vire à esquerda na {street}")
            elif action == "right":
                clauses.append(f"vire à direita na {street}")
            elif action == "back":
                clauses.append(f"dê a volta para a {street}")
            else:
                clauses.append(f"siga pela {street}")
        else:
            if action == "start":
                clauses.append(f"sales del hostal por {street}")
            elif action == "left":
                clauses.append(f"tomas a la izquierda por {street}")
            elif action == "right":
                clauses.append(f"doblas a la derecha en {street}")
            elif action == "back":
                clauses.append(f"das la vuelta hacia {street}")
            else:
                clauses.append(f"sigues por {street}")
    text = _join_clauses(clauses, "and" if language == "en" else "e" if language == "pt" else "y")
    if text:
        return text[0].upper() + text[1:]
    if language == "en":
        return "It is a short walk from the hostel"
    if language == "pt":
        return "É uma caminhada curta a partir do hostel"
    return "Es una caminata corta desde el hostal"


def _map_closing(language: str) -> str:
    """Cue only — the map image is sent as the next Instagram bubble."""
    if language == "en":
        return "\n\nHere is the map."
    if language == "pt":
        return "\n\nTe indico o mapa a seguir."
    return "\n\nTe lo indico en el siguiente mapa."


def _guest_replies(
    *,
    destination: str,
    distance: str,
    duration: str,
    mode: str,
    lines: list[str],
    maps_url: str,
    moves: list[dict[str, str]] | None = None,
    has_metro: bool = False,
    has_bus: bool = False,
    hint: dict[str, Any] | None = None,
) -> dict[str, str]:
    spoken = {
        "es": _spoken_duration(duration, "es"),
        "en": _spoken_duration(duration, "en"),
        "pt": _spoken_duration(duration, "pt"),
    }
    place = _with_article(destination)
    travel = (mode or "transit").lower()
    line_text = _join_clauses(lines) if lines else ""
    if not has_metro and not has_bus and lines:
        sample = " ".join(lines).lower()
        has_metro = "metro" in sample
        has_bus = "micro" in sample or "bus" in sample

    # Known landmarks when Google transit is missing: metro tip + realistic car ETA.
    tip_es = str((hint or {}).get("es") or "").strip()
    if tip_es and travel != "walking":
        tip_en = str(hint.get("en") or tip_es).strip()
        tip_pt = str(hint.get("pt") or tip_es).strip()
        return {
            "es": f"{tip_es} En auto o taxi son unos {spoken['es']}." + _map_closing("es"),
            "en": f"{tip_en} By car/taxi about {spoken['en']}." + _map_closing("en"),
            "pt": f"{tip_pt} De carro/táxi uns {spoken['pt']}." + _map_closing("pt"),
        }

    # One short human line + optional line name. Distance only once.
    if travel == "transit":
        if has_metro and not has_bus:
            es = f"{place} queda a unos {spoken['es']} en metro."
            en = f"{destination} is about a {spoken['en']} metro ride from the hostel."
            pt = f"{destination} fica a uns {spoken['pt']} de metrô."
        elif has_bus and not has_metro:
            es = f"{place} queda a unos {spoken['es']} en micro."
            en = f"{destination} is about a {spoken['en']} bus ride from the hostel."
            pt = f"{destination} fica a uns {spoken['pt']} de ônibus."
        else:
            es = f"{place} queda a unos {spoken['es']} en metro o micro."
            en = f"{destination} is about {spoken['en']} by metro or bus from the hostel."
            pt = f"{destination} fica a uns {spoken['pt']} de metrô ou ônibus."
        if line_text:
            es += f" Te sirve {line_text}."
            en += f" Take {line_text}."
            pt += f" Pode pegar {line_text}."
        return {
            "es": es + _map_closing("es"),
            "en": en + _map_closing("en"),
            "pt": pt + _map_closing("pt"),
        }

    if travel == "driving":
        return {
            "es": f"{place} queda a unos {spoken['es']} en auto o taxi." + _map_closing("es"),
            "en": (
                f"{destination} is about a {spoken['en']} drive/taxi from the hostel."
                + _map_closing("en")
            ),
            "pt": f"{destination} fica a uns {spoken['pt']} de carro ou táxi." + _map_closing("pt"),
        }

    # Walking only for nearby places — short and natural.
    minutes = _duration_minutes(duration)
    if minutes is not None and minutes <= 25:
        return {
            "es": f"{place} queda cerca: a pie son unos {spoken['es']}." + _map_closing("es"),
            "en": (
                f"{destination} is close by — about a {spoken['en']} walk from the hostel."
                + _map_closing("en")
            ),
            "pt": (
                f"{destination} fica perto: a pé são uns {spoken['pt']}."
                + _map_closing("pt")
            ),
        }
    return {
        "es": f"{place} queda a unos {spoken['es']} en auto o taxi." + _map_closing("es"),
        "en": (
            f"{destination} is about a {spoken['en']} by car/taxi from the hostel."
            + _map_closing("en")
        ),
        "pt": f"{destination} fica a uns {spoken['pt']} de carro ou táxi." + _map_closing("pt"),
    }


def _with_article(name: str) -> str:
    lower = name.lower()
    if lower.startswith(("el ", "la ", "los ", "las ")):
        return name[0].upper() + name[1:]
    feminine = ("plaza", "estacion", "estación", "iglesia", "catedral", "vega", "torre")
    if any(lower.startswith(word) for word in feminine):
        return f"La {name}"
    return f"El {name}"


def _spoken_duration(duration: str, language: str) -> str:
    if not duration.endswith(" min"):
        return duration
    minutes = duration[: -len(" min")]
    if language == "en":
        return f"{minutes}-minute" if minutes != "1" else "1-minute"
    return f"{minutes} minutos"


def _format_distance(meters: int) -> str:
    if meters >= 1000:
        km = meters / 1000
        return f"{km:.1f} km".replace(".", ",")
    return f"{meters} m"


def _format_duration(seconds: int) -> str:
    minutes = max(1, round(seconds / 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    if rest:
        return f"{hours} h {rest} min"
    return f"{hours} h"
