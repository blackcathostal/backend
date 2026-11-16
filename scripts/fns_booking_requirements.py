"""Print required fields for FNS Motor booking from OpenAPI schema."""

from __future__ import annotations

import json

import httpx


def main() -> None:
    schema = httpx.get(
        "https://adminprees1.fnsmanager.com/api-doc/channel/json/api-schema.json",
        timeout=45,
    ).json()
    path = "/booking_json.php"
    post = ((schema.get("paths") or {}).get(path) or {}).get("post") or {}
    print("PATH", path)
    print("SUMMARY", post.get("summary") or post.get("description") or "")
    print("SECURITY", post.get("security"))

    # request body schema
    content = ((post.get("requestBody") or {}).get("content") or {})
    app_json = content.get("application/json") or next(iter(content.values()), {})
    schema_ref = (app_json.get("schema") or {})
    print("\nREQUEST SCHEMA RAW:")
    print(json.dumps(schema_ref, ensure_ascii=False, indent=2)[:4000])

    components = schema.get("components") or {}
    schemas = components.get("schemas") or {}

    def resolve(node):
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            name = node["$ref"].split("/")[-1]
            return resolve(schemas.get(name) or node)
        out = dict(node)
        if "properties" in out:
            out["properties"] = {
                k: resolve(v) for k, v in (out.get("properties") or {}).items()
            }
        if "items" in out:
            out["items"] = resolve(out["items"])
        if "allOf" in out:
            merged = {"properties": {}, "required": []}
            for part in out["allOf"]:
                part = resolve(part)
                merged["properties"].update(part.get("properties") or {})
                merged["required"] += part.get("required") or []
                for k, v in part.items():
                    if k not in {"properties", "required"}:
                        merged[k] = v
            return merged
        return out

    body = resolve(schema_ref)
    print("\nREQUIRED TOP:", body.get("required"))
    props = body.get("properties") or {}
    for name, prop in props.items():
        req = name in (body.get("required") or [])
        print(f"\n## {name} required={req}")
        print("  type:", prop.get("type"), "desc:", (prop.get("description") or "")[:200])
        if prop.get("properties"):
            nested_req = set(prop.get("required") or [])
            for k, v in prop["properties"].items():
                mark = "REQ" if k in nested_req else "opt"
                print(
                    f"  - [{mark}] {k}: type={v.get('type')} "
                    f"{(v.get('description') or '')[:160]}"
                )
                if v.get("properties"):
                    nested2 = set(v.get("required") or [])
                    for k2, v2 in v["properties"].items():
                        mark2 = "REQ" if k2 in nested2 else "opt"
                        print(
                            f"      - [{mark2}] {k2}: type={v2.get('type')} "
                            f"{(v2.get('description') or '')[:140]}"
                        )
                if v.get("items"):
                    items = v["items"]
                    print("      items:", items.get("type"), (items.get("description") or "")[:120])
                    if items.get("properties"):
                        ireq = set(items.get("required") or [])
                        for k2, v2 in items["properties"].items():
                            mark2 = "REQ" if k2 in ireq else "opt"
                            print(
                                f"      - [{mark2}] {k2}: type={v2.get('type')} "
                                f"{(v2.get('description') or '')[:140]}"
                            )

    # Also show search_availability response room fields useful for booking
    print("\n\n==== ALSO availability endpoint ====")
    av = ((schema.get("paths") or {}).get("/search_availability_json.php") or {}).get("post") or {}
    print("summary:", av.get("summary") or av.get("description") or "")
    # servers / path prefix
    print("\nservers:", schema.get("servers"))
    print("paths:", list((schema.get("paths") or {}).keys()))


if __name__ == "__main__":
    main()
