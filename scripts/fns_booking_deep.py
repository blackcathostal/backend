from __future__ import annotations

import json

import httpx


def resolve(schemas: dict, node):
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        return resolve(schemas, schemas.get(node["$ref"].split("/")[-1]) or {})
    out = {k: v for k, v in node.items() if k != "$ref"}
    if "properties" in node:
        out["properties"] = {k: resolve(schemas, v) for k, v in node["properties"].items()}
    if "items" in node:
        out["items"] = resolve(schemas, node["items"])
    if "allOf" in node:
        merged = {"type": "object", "properties": {}, "required": []}
        for part in node["allOf"]:
            part = resolve(schemas, part)
            merged["properties"].update(part.get("properties") or {})
            merged["required"] += list(part.get("required") or [])
            for k, v in part.items():
                if k not in {"properties", "required", "type"}:
                    merged.setdefault(k, v)
        return merged
    return out


def dump(name: str, node: dict, indent: int = 0) -> None:
    pad = "  " * indent
    req = set(node.get("required") or [])
    props = node.get("properties") or {}
    if not props and node.get("items"):
        print(f"{pad}{name}[] type=array")
        dump(name + "_item", node["items"], indent + 1)
        return
    print(f"{pad}{name} type={node.get('type')} req={sorted(req) if req else []}")
    if node.get("description"):
        print(f"{pad}  desc: {node['description'][:180]}")
    if node.get("enum"):
        print(f"{pad}  enum: {node['enum']}")
    for k, v in props.items():
        mark = "REQ" if k in req else "opt"
        if v.get("properties") or v.get("items"):
            print(f"{pad}  [{mark}] {k}:")
            dump(k, v, indent + 2)
        else:
            extra = ""
            if v.get("enum"):
                extra = f" enum={v['enum']}"
            print(
                f"{pad}  [{mark}] {k}: {v.get('type')}{(v.get('description') or '') and ' — ' + v.get('description')[:140]}{extra}"
            )


def main() -> None:
    schema = httpx.get(
        "https://adminprees1.fnsmanager.com/api-doc/channel/json/api-schema.json",
        timeout=45,
    ).json()
    schemas = (schema.get("components") or {}).get("schemas") or {}
    booking = resolve(schemas, schemas.get("BookingRequest") or {})
    dump("BookingRequest", booking)

    # Useful related enums/examples if present
    for key in sorted(schemas):
        if "book" in key.lower() or "reserv" in key.lower() or "guest" in key.lower():
            print("\nSCHEMA", key)


if __name__ == "__main__":
    main()
