"""Walking route from Black Cat Hostal to a named place in Santiago."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

HOSTEL_ADDRESS = "Compañía de Jesús 1921, Barrio Brasil, Santiago, Chile"
HOSTEL_LAT = -33.4396639
HOSTEL_LNG = -70.6638072
USER_AGENT = "BlackCatHostal/1.0 (reservas@blackcathostal.com)"


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
    sentences.append(f"Google Maps: {maps_url}")
    result["summary"] = " ".join(sentences)
    return result


def _google_route(origin: dict[str, Any] | None, mode: str) -> dict[str, Any] | None:
    if not origin:
        return None
    from app.core.config import settings

    key = (settings.google_places_api_key or "").strip()
    if not key:
        return None
    try:
        with httpx.Client(timeout=12.0) as client:
            response = client.get(
                "https://maps.googleapis.com/maps/api/directions/json",
                params={
                    "origin": f"{origin['lat']},{origin['lng']}",
                    "destination": f"{HOSTEL_LAT},{HOSTEL_LNG}",
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
    for step in leg.get("steps") or []:
        transit = step.get("transit_details") or {}
        line = transit.get("line") or {}
        label = str(line.get("short_name") or line.get("name") or "").strip()
        vehicle = str((line.get("vehicle") or {}).get("name") or "").strip()
        if label:
            lines.append(f"{vehicle} {label}".strip())
        if len(lines) >= 4:
            break
    return {
        "distance": str((leg.get("distance") or {}).get("text") or ""),
        "duration": str((leg.get("duration") or {}).get("text") or ""),
        "lines": lines,
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

    try:
        route = _walking_route(dest["lat"], dest["lng"])
    except httpx.HTTPError as exc:
        return {"ok": False, "destination": dest["name"], "error": f"No se pudo calcular la ruta: {exc}"}
    if not route:
        return {"ok": False, "destination": dest["name"], "error": "No hay una ruta a pie disponible."}

    maps_url = (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={quote(HOSTEL_ADDRESS)}"
        f"&destination={quote(dest['name'] + ', Santiago, Chile')}"
        "&travelmode=walking"
    )
    replies = _guest_replies(
        destination=dest["name"],
        distance=route["distance"],
        duration=route["duration"],
        moves=route["moves"],
        maps_url=maps_url,
    )
    return {
        "ok": True,
        "origin": HOSTEL_ADDRESS,
        "destination": dest["name"],
        "destination_address": dest["address"],
        "mode": "walking",
        "distance": route["distance"],
        "duration": route["duration"],
        "summary": replies["es"].split("\n\n", 1)[0],
        "maps_url": maps_url,
        "replies": replies,
        "instruction": (
            "Usa replies según el idioma del huésped, casi textual. "
            "No reescribas las calles ni armes otra URL. No mandes a WhatsApp."
        ),
    }


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
        "distance": _format_distance(meters),
        "duration": _format_duration(seconds),
        "moves": moves,
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


def _guest_replies(
    *,
    destination: str,
    distance: str,
    duration: str,
    moves: list[dict[str, str]],
    maps_url: str,
) -> dict[str, str]:
    spoken = {
        "es": _spoken_duration(duration, "es"),
        "en": _spoken_duration(duration, "en"),
        "pt": _spoken_duration(duration, "pt"),
    }
    place = _with_article(destination)
    es_path = _route_clauses(moves, "es")
    en_path = _route_clauses(moves, "en")
    pt_path = _route_clauses(moves, "pt")
    return {
        "es": (
            f"¡Hola! {place} queda a unos {spoken['es']} a pie, cerca de {distance}. "
            f"{es_path}. Ahí lo encuentras, en pleno centro.\n\n"
            f"Ver en Google Maps\n{maps_url}"
        ),
        "en": (
            f"Hi! {destination} is about a {spoken['en']} walk from the hostel, around {distance}. "
            f"{en_path}. You will find it right there, in the center.\n\n"
            f"See on Google Maps\n{maps_url}"
        ),
        "pt": (
            f"Olá! {destination} fica a uns {spoken['pt']} a pé, cerca de {distance}. "
            f"{pt_path}. Você encontra logo ali, no centro.\n\n"
            f"Ver no Google Maps\n{maps_url}"
        ),
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
