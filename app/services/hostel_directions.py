"""Walking route from Black Cat Hostal to a named place in Santiago."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

HOSTEL_ADDRESS = "Compañía de Jesús 1921, Barrio Brasil, Santiago, Chile"
HOSTEL_LAT = -33.43997
HOSTEL_LNG = -70.66885
USER_AGENT = "BlackCatHostal/1.0 (reservas@blackcathostal.com)"


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
        f"&destination={quote(dest['name'])}"
        "&travelmode=walking"
    )
    return {
        "ok": True,
        "origin": HOSTEL_ADDRESS,
        "destination": dest["name"],
        "destination_address": dest["address"],
        "mode": "walking",
        "distance": route["distance"],
        "duration": route["duration"],
        "steps": route["steps"],
        "maps_url": maps_url,
        "instruction": (
            "Responde tú con esta ruta a pie. No derives la pregunta a WhatsApp. "
            "Menciona duración, distancia y los pasos. Puedes agregar el enlace de Maps."
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
    steps = _summarize_steps((legs[0].get("steps") or []))
    meters = int(route.get("distance") or 0)
    # The public OSRM foot profile reports car-like times. Use a normal walking pace.
    seconds = int(round(meters / 1.33))
    return {
        "distance": _format_distance(meters),
        "duration": _format_duration(seconds),
        "steps": steps,
    }


def _summarize_steps(steps: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for step in steps:
        maneuver = step.get("maneuver") or {}
        kind = str(maneuver.get("type") or "")
        modifier = str(maneuver.get("modifier") or "")
        name = str(step.get("name") or "").strip()
        if kind in {"depart", "arrive"} and not name:
            continue
        meters = int(step.get("distance") or 0)
        if meters < 40 and kind not in {"arrive"}:
            continue
        turn = _turn_label(kind, modifier)
        if name and turn:
            text = f"{turn} por {name} ({_format_distance(meters)})"
        elif name:
            text = f"Sigue por {name} ({_format_distance(meters)})"
        elif turn:
            text = f"{turn} ({_format_distance(meters)})"
        else:
            continue
        if lines and lines[-1].split(" (")[0] == text.split(" (")[0]:
            continue
        lines.append(text)
        if len(lines) >= 6:
            break
    if not lines:
        return ["Camina desde el hostal hasta el destino siguiendo el mapa."]
    return lines


def _turn_label(kind: str, modifier: str) -> str:
    labels = {
        "left": "Gira a la izquierda",
        "right": "Gira a la derecha",
        "slight left": "Sigue ligeramente a la izquierda",
        "slight right": "Sigue ligeramente a la derecha",
        "straight": "Sigue derecho",
        "uturn": "Da la vuelta",
    }
    if kind == "arrive":
        return "Llegas"
    if kind in {"turn", "end of road", "fork", "roundabout", "rotary"}:
        return labels.get(modifier, "Sigue")
    if kind == "new name":
        return "Continúa"
    if kind == "depart":
        return "Sal"
    return ""


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
