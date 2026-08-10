"""Sync local MySQL `bc` content into production Black Cat API."""

from __future__ import annotations

import json
import mimetypes
import re
import sys
from pathlib import Path

import httpx
from sqlalchemy import create_engine, text

LOCAL_DB = "mysql+pymysql://root@127.0.0.1:3306/bc"
API = "https://api.blackcathostal.com/api"
ADMIN_EMAIL = "admin@blackcathostal.com"
ADMIN_PASSWORD = "admin123"
UPLOADS = Path(r"C:\Users\jesus\Desktop\proyecto_blackcat\backend\uploads")


def rows(conn, sql: str) -> list[dict]:
    result = conn.execute(text(sql))
    return [dict(r._mapping) for r in result]


def local_file_for_url(url: str | None) -> Path | None:
    if not url:
        return None
    u = str(url).replace("\\", "/")
    if u.startswith("http://") or u.startswith("https://"):
        # only handle our relative production/local paths later
        idx = u.find("/uploads/")
        if idx < 0:
            return None
        u = u[idx:]
    if not u.startswith("/uploads/"):
        return None
    path = UPLOADS / u[len("/uploads/") :]
    return path if path.is_file() else None


def main() -> None:
    engine = create_engine(LOCAL_DB)
    summary: dict[str, dict[str, int]] = {}

    with engine.connect() as conn:
        local = {
            "roles": rows(conn, "SELECT id, name, description FROM roles"),
            "users": rows(
                conn,
                "SELECT id, role_id, full_name, email, is_active FROM users",
            ),
            "mail_accounts": rows(conn, "SELECT * FROM mail_accounts"),
            "rooms": rows(conn, "SELECT * FROM rooms"),
            "services": rows(conn, "SELECT * FROM services"),
            "posts": rows(conn, "SELECT * FROM posts"),
            "sliders": rows(conn, "SELECT * FROM sliders"),
            "campaigns": rows(conn, "SELECT * FROM campaigns"),
            "medias": rows(conn, "SELECT * FROM medias"),
            "contact_groups": rows(conn, "SELECT * FROM contact_groups"),
            "contacts_count": conn.execute(text("SELECT COUNT(*) FROM contacts")).scalar(),
        }

    print("LOCAL snapshot:")
    for k, v in local.items():
        if k == "contacts_count":
            print(f"  contacts: {v}")
        else:
            print(f"  {k}: {len(v)}")
    print("  users emails:", ", ".join(u["email"] for u in local["users"]))

    with httpx.Client(base_url=API, timeout=120.0, follow_redirects=True) as client:
        token = client.post(
            "/auth/login",
            json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        )
        token.raise_for_status()
        headers = {"Authorization": f"Bearer {token.json()['access_token']}"}

        def get_list(path: str) -> list:
            r = client.get(path, headers=headers)
            r.raise_for_status()
            return r.json()

        def sync_(name: str, created: int, skipped: int, failed: int) -> None:
            summary[name] = {"created": created, "skipped": skipped, "failed": failed}
            print(f"[{name}] created={created} skipped={skipped} failed={failed}")

        # --- mail accounts ---
        remote_mails = {m["email"].lower(): m for m in get_list("/mail-accounts/")}
        c = s = f = 0
        for acc in local["mail_accounts"]:
            email = acc["email"].lower()
            payload = {
                "name": acc["name"],
                "email": email,
                "password": acc["password"],
                "smtp_host": acc["smtp_host"],
                "smtp_port": str(acc["smtp_port"] or "587"),
                "imap_host": acc["imap_host"],
                "imap_port": str(acc["imap_port"] or "993"),
                "use_ssl": bool(acc["use_ssl"]),
                "is_active": bool(acc["is_active"]),
                "is_default": bool(acc["is_default"]),
                "signature": acc.get("signature") or "",
            }
            # rewrite signature local upload paths stay relative (/uploads/...) — OK for same host
            if email in remote_mails:
                rid = remote_mails[email]["id"]
                r = client.put(f"/mail-accounts/{rid}", headers=headers, json=payload)
                if r.status_code < 300:
                    s += 1
                else:
                    f += 1
                    print("  mail update fail", email, r.status_code, r.text[:180])
            else:
                r = client.post("/mail-accounts/", headers=headers, json=payload)
                if r.status_code in (200, 201):
                    c += 1
                    remote_mails[email] = r.json()
                else:
                    f += 1
                    print("  mail create fail", email, r.status_code, r.text[:180])
        # upload signature images referenced in signatures
        for acc in local["mail_accounts"]:
            sig = acc.get("signature") or ""
            for match in re.findall(r"/uploads/signatures/[^\"'\s>]+", sig):
                path = local_file_for_url(match)
                if not path:
                    continue
                with path.open("rb") as fh:
                    mime = mimetypes.guess_type(path.name)[0] or "image/png"
                    up = client.post(
                        "/mail-accounts/signature-image",
                        headers=headers,
                        files={"file": (path.name, fh, mime)},
                    )
                if up.status_code not in (200, 201):
                    print("  signature img fail", path.name, up.status_code)
        sync_("mail_accounts", c, s, f)

        # --- rooms ---
        remote_rooms = {r["name"].lower(): r for r in get_list("/rooms/")}
        c = s = f = 0
        for room in local["rooms"]:
            payload = {
                "name": room["name"],
                "type": room["type"],
                "capacity": int(room["capacity"]),
                "price": int(room["price"]),
                "status": room["status"],
            }
            key = room["name"].lower()
            if key in remote_rooms:
                r = client.put(
                    f"/rooms/{remote_rooms[key]['id']}",
                    headers=headers,
                    json=payload,
                )
                if r.status_code < 300:
                    s += 1
                else:
                    f += 1
            else:
                r = client.post("/rooms/", headers=headers, json=payload)
                if r.status_code in (200, 201):
                    c += 1
                    remote_rooms[key] = r.json()
                else:
                    f += 1
                    print("  room fail", room["name"], r.status_code, r.text[:180])
        sync_("rooms", c, s, f)

        # --- services ---
        remote_services = {r["name"].lower(): r for r in get_list("/services/")}
        c = s = f = 0
        for svc in local["services"]:
            payload = {
                "name": svc["name"],
                "category": svc["category"],
                "price": str(svc["price"]),
                "status": svc["status"],
                "description": svc.get("description") or "",
            }
            key = svc["name"].lower()
            if key in remote_services:
                r = client.put(
                    f"/services/{remote_services[key]['id']}",
                    headers=headers,
                    json=payload,
                )
                if r.status_code < 300:
                    s += 1
                else:
                    f += 1
            else:
                r = client.post("/services/", headers=headers, json=payload)
                if r.status_code in (200, 201):
                    c += 1
                    remote_services[key] = r.json()
                else:
                    f += 1
                    print("  service fail", svc["name"], r.status_code, r.text[:180])
        sync_("services", c, s, f)

        # --- posts ---
        remote_posts = {p["slug"].lower(): p for p in get_list("/posts/")}
        c = s = f = 0
        for post in local["posts"]:
            image_url = post.get("image_url") or ""
            payload = {
                "slug": post["slug"],
                "title": post["title"],
                "excerpt": post.get("excerpt") or "",
                "body": post.get("body") or "",
                "category": post.get("category") or "Blog",
                "image_url": image_url,
                "author": post.get("author") or "Black Cat Hostal",
                "sort_order": int(post.get("sort_order") or 0),
                "is_active": bool(post.get("is_active", True)),
            }
            key = post["slug"].lower()
            if key in remote_posts:
                r = client.put(
                    f"/posts/{remote_posts[key]['id']}",
                    headers=headers,
                    json=payload,
                )
                pid = remote_posts[key]["id"]
                if r.status_code < 300:
                    s += 1
                else:
                    f += 1
                    print("  post update fail", key, r.status_code, r.text[:180])
                    continue
            else:
                r = client.post("/posts/", headers=headers, json=payload)
                if r.status_code in (200, 201):
                    c += 1
                    remote_posts[key] = r.json()
                    pid = r.json()["id"]
                else:
                    f += 1
                    print("  post create fail", key, r.status_code, r.text[:180])
                    continue
            # upload image if local file exists
            path = local_file_for_url(image_url)
            if path:
                with path.open("rb") as fh:
                    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
                    up = client.post(
                        f"/posts/{pid}/image",
                        headers=headers,
                        files={"file": (path.name, fh, mime)},
                    )
                if up.status_code >= 300:
                    print("  post image fail", path.name, up.status_code, up.text[:120])
        sync_("posts", c, s, f)

        # --- sliders ---
        remote_sliders = {p["title"].lower(): p for p in get_list("/sliders/")}
        c = s = f = 0
        for slider in local["sliders"]:
            image_url = slider.get("image_url") or ""
            payload = {
                "eyebrow": slider.get("eyebrow") or "",
                "title": slider["title"],
                "image_url": image_url,
                "overlay": int(slider.get("overlay") or 3),
                "sort_order": int(slider.get("sort_order") or 0),
                "is_active": bool(slider.get("is_active", True)),
            }
            key = slider["title"].lower()
            if key in remote_sliders:
                r = client.put(
                    f"/sliders/{remote_sliders[key]['id']}",
                    headers=headers,
                    json=payload,
                )
                sid = remote_sliders[key]["id"]
                if r.status_code < 300:
                    s += 1
                else:
                    f += 1
                    continue
            else:
                r = client.post("/sliders/", headers=headers, json=payload)
                if r.status_code in (200, 201):
                    c += 1
                    remote_sliders[key] = r.json()
                    sid = r.json()["id"]
                else:
                    f += 1
                    print("  slider fail", key, r.status_code, r.text[:180])
                    continue
            path = local_file_for_url(image_url)
            if path:
                with path.open("rb") as fh:
                    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
                    up = client.post(
                        f"/sliders/{sid}/image",
                        headers=headers,
                        files={"file": (path.name, fh, mime)},
                    )
                if up.status_code >= 300:
                    print("  slider image fail", path.name, up.status_code)
        sync_("sliders", c, s, f)

        # --- campaigns ---
        remote_camps = {(p["name"].lower(), p["subject"].lower()): p for p in get_list("/campaigns/")}
        c = s = f = 0
        for camp in local["campaigns"]:
            recipients = camp.get("recipients") or []
            if isinstance(recipients, str):
                recipients = json.loads(recipients)
            attachments = camp.get("attachments") or []
            if isinstance(attachments, str):
                attachments = json.loads(attachments)
            # strip local attachment paths that won't exist on prod
            clean_atts = []
            for att in attachments:
                if not isinstance(att, dict):
                    continue
                path = local_file_for_url(att.get("url") or att.get("path"))
                if path:
                    with path.open("rb") as fh:
                        mime = att.get("content_type") or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                        up = client.post(
                            "/campaigns/attachments/upload",
                            headers=headers,
                            files={"file": (att.get("name") or path.name, fh, mime)},
                        )
                    if up.status_code in (200, 201):
                        clean_atts.append(up.json())
                else:
                    # keep metadata without local path
                    clean_atts.append(
                        {
                            "name": att.get("name") or "file",
                            "size": att.get("size") or 0,
                            "path": None,
                            "url": att.get("url"),
                            "content_type": att.get("content_type"),
                        }
                    )
            payload = {
                "name": camp["name"],
                "from_email": camp["from_email"],
                "subject": camp["subject"],
                "html_body": camp["html_body"],
                "status": camp.get("status") or "Borrador",
                "sent": int(camp.get("sent") or 0),
                "recipients": recipients,
                "attachments": clean_atts,
            }
            key = (camp["name"].lower(), camp["subject"].lower())
            if key in remote_camps:
                r = client.put(
                    f"/campaigns/{remote_camps[key]['id']}",
                    headers=headers,
                    json=payload,
                )
                if r.status_code < 300:
                    s += 1
                else:
                    f += 1
                    print("  campaign update fail", camp["name"], r.status_code, r.text[:180])
            else:
                r = client.post("/campaigns/", headers=headers, json=payload)
                if r.status_code in (200, 201):
                    c += 1
                else:
                    f += 1
                    print("  campaign create fail", camp["name"], r.status_code, r.text[:180])
        sync_("campaigns", c, s, f)

        # --- medias ---
        remote_media = {(m["filename"].lower(), m.get("category", "")): m for m in get_list("/medias/")}
        c = s = f = 0
        for media in local["medias"]:
            key = (media["filename"].lower(), media.get("category") or "")
            path = local_file_for_url(media.get("url"))
            if not path:
                # try uploads/media/filename
                alt = UPLOADS / "media" / media["filename"]
                path = alt if alt.is_file() else None
            if key in remote_media:
                s += 1
                continue
            if not path:
                f += 1
                print("  media missing file", media["filename"], media.get("url"))
                continue
            with path.open("rb") as fh:
                mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
                up = client.post(
                    "/medias/upload",
                    headers=headers,
                    files={"file": (path.name, fh, mime)},
                    data={
                        "category": media.get("category") or "general",
                        "alt_text": media.get("alt_text") or "",
                    },
                )
            if up.status_code in (200, 201):
                c += 1
            else:
                f += 1
                print("  media upload fail", media["filename"], up.status_code, up.text[:180])
        sync_("medias", c, s, f)

        # --- users / roles note ---
        me = client.get("/auth/me", headers=headers)
        me.raise_for_status()
        print("[users] local users:", [u["email"] for u in local["users"]])
        print("[users] prod /auth/me:", me.json().get("email") or me.json())
        print("[roles] local:", [r["name"] for r in local["roles"]], "(seeded on API boot; no public users CRUD)")
        print("[contacts] local count:", local["contacts_count"], "(already imported earlier)")

        # final counts
        print("\nPROD counts:")
        for path, label in [
            ("/mail-accounts/", "mail_accounts"),
            ("/rooms/", "rooms"),
            ("/services/", "services"),
            ("/posts/", "posts"),
            ("/sliders/", "sliders"),
            ("/campaigns/", "campaigns"),
            ("/medias/", "medias"),
            ("/contacts/", "contacts"),
            ("/contact-groups/", "contact_groups"),
        ]:
            data = get_list(path)
            print(f"  {label}: {len(data)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ERROR:", exc, file=sys.stderr)
        raise
