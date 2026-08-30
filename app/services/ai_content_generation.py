from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.ai_generation_runs import AiGenerationRuns
from app.models.ai_usage import AiUsage
from app.models.posts import Posts
from app.services.ai_image_service import download_free_image
from app.services.deepseek_billing import calculate_platform_cost, get_deepseek_balance
from app.services.deepseek_client import DeepSeekError, generate_article
from app.services.mcp_sources import collect_google_places, collect_source_material

generation_lock = asyncio.Lock()


def _slugify(value: str) -> str:
    normalized = (
        value.strip()
        .lower()
        .replace("á", "a")
        .replace("é", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("ú", "u")
        .replace("ñ", "n")
    )
    return re.sub(r"[^a-z0-9]+", "-", normalized).strip("-") or "articulo-turismo"


def _unique_slug(db: Session, value: str) -> str:
    base = _slugify(value)
    candidate = base
    index = 2
    while db.query(Posts.id).filter(Posts.slug == candidate).first():
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def _parse_article(raw: str) -> dict[str, Any]:
    content = raw.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.IGNORECASE)
        content = re.sub(r"\s*```$", "", content)
    try:
        article = json.loads(content)
    except json.JSONDecodeError as exc:
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end <= start:
            raise DeepSeekError("DeepSeek no devolvió el JSON esperado.") from exc
        try:
            article = json.loads(content[start : end + 1])
        except json.JSONDecodeError as nested_exc:
            raise DeepSeekError("DeepSeek no devolvió el JSON esperado.") from nested_exc
    if not isinstance(article, dict):
        raise DeepSeekError("La respuesta de DeepSeek no tiene formato de artículo.")

    title = str(article.get("title") or "").strip()
    excerpt = str(article.get("excerpt") or "").strip()
    body = str(article.get("body") or article.get("content") or "").strip()
    if not title or not excerpt or not body:
        raise DeepSeekError("El artículo generado no tiene todos los campos obligatorios.")
    raw_keywords = article.get("keywords") or []
    if isinstance(raw_keywords, list):
        keywords = ", ".join(str(item).strip() for item in raw_keywords if str(item).strip())
    else:
        keywords = str(raw_keywords).strip()
    raw_queries = article.get("place_queries") or []
    if isinstance(raw_queries, list):
        place_queries = [
            str(item).strip()[:180] for item in raw_queries if str(item).strip()
        ][:4]
    else:
        place_queries = []
    return {
        "title": title[:220],
        "slug": _slugify(str(article.get("slug") or title))[:220],
        "keywords": keywords[:500],
        "excerpt": excerpt[:500],
        "body": _limit_words(body, settings.deepseek_article_max_words),
        "category": str(article.get("category") or "Turismo")[:80],
        "place_queries": place_queries,
    }


def _limit_words(value: str, maximum: int) -> str:
    words = value.split()
    if len(words) <= maximum:
        return value
    truncated = " ".join(words[:maximum]).rstrip(" .,;:") + "…"
    return truncated


def _merge_usage(total: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    if not total:
        return dict(current)
    merged = dict(current)
    for key in (
        "api_requests",
        "prompt_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
        "completion_tokens",
        "total_tokens",
    ):
        merged[key] = int(total.get(key) or 0) + int(current.get(key) or 0)
    merged["estimated_cost_usd"] = round(
        float(total.get("estimated_cost_usd") or 0)
        + float(current.get("estimated_cost_usd") or 0),
        8,
    )
    return merged


def _is_repeated(
    article: dict[str, Any],
    existing: list[tuple[str, str, str, str]],
) -> bool:
    normalized_title = re.sub(r"\W+", " ", article["title"].lower()).strip()
    new_title_words = set(re.findall(r"\w{4,}", normalized_title))
    new_body = article["body"].lower()
    new_opening = " ".join(re.findall(r"\w{4,}", new_body)[:14])
    new_phrases = _word_ngrams(new_body)
    new_text = " ".join(
        [article["body"], article["excerpt"], article.get("keywords", "")]
    ).lower()
    new_words = set(re.findall(r"\w{4,}", new_text))
    for title, body, excerpt, keywords in existing:
        old_title = re.sub(r"\W+", " ", (title or "").lower()).strip()
        old_title_words = set(re.findall(r"\w{4,}", old_title))
        title_overlap = (
            len(new_title_words & old_title_words) / len(new_title_words | old_title_words)
            if new_title_words and old_title_words
            else 0
        )
        if (
            normalized_title == old_title
            or SequenceMatcher(None, normalized_title, old_title).ratio() >= 0.78
            or (len(new_title_words & old_title_words) >= 2 and title_overlap >= 0.75)
        ):
            return True
        old_body = (body or "").lower()
        old_opening = " ".join(re.findall(r"\w{4,}", old_body)[:14])
        if (
            new_opening
            and old_opening
            and SequenceMatcher(None, new_opening, old_opening).ratio() >= 0.82
        ):
            return True
        if len(new_phrases & _word_ngrams(old_body)) >= 3:
            return True
        old_text = " ".join([body or "", excerpt or "", keywords or ""]).lower()
        old_words = set(re.findall(r"\w{4,}", old_text))
        if new_words and old_words:
            overlap = len(new_words & old_words) / len(new_words | old_words)
            sequence = SequenceMatcher(None, new_text, old_text).ratio()
            if overlap >= 0.70 or sequence >= 0.72:
                return True
    return False


def _word_ngrams(value: str, size: int = 6) -> set[tuple[str, ...]]:
    words = re.findall(r"\w{4,}", value)
    return {tuple(words[index : index + size]) for index in range(len(words) - size + 1)}


def _has_template_language(value: str) -> bool:
    normalized = re.sub(r"\s+", " ", value.lower())
    forbidden_phrases = (
        "metrópolis vibrante",
        "a los pies de la cordillera",
        "una experiencia inolvidable",
        "un destino imperdible",
    )
    return any(phrase in normalized for phrase in forbidden_phrases)


def _context(materials: list[dict[str, Any]]) -> str:
    parts = []
    for material in materials:
        parts.append(
            f"FUENTE: {material.get('name') or material.get('title') or 'Sin nombre'}\n"
            f"URL: {material.get('url', '')}\n"
            f"CONTENIDO:\n{material.get('text', '')}"
        )
    # Spanish text can use fewer than four characters per token; leave a
    # conservative margin for the system prompt and source labels.
    max_chars = max(4_000, settings.deepseek_max_input_tokens * 3 - 3_000)
    return "\n\n".join(parts)[:max_chars]


def _fallback_place_queries(article: dict[str, Any]) -> list[str]:
    title = str(article.get("title") or "").strip()
    if not title:
        return ["sitios turísticos Santiago de Chile"]
    return [
        f"{title} Santiago de Chile",
        f"restaurantes y cafés {title} Santiago de Chile",
    ]


def _editorial_direction(run_id: int) -> str:
    directions = (
        "vida cultural local y espacios de creación",
        "arquitectura, patrimonio y memoria del lugar",
        "música, espectáculos y programación verificable",
        "recorrido práctico para una primera visita",
        "oficios, diseño, mercados y comercio de barrio",
        "parques, miradores y actividades al aire libre",
        "gastronomía local vinculada con el recorrido",
        "museos, exposiciones y experiencias familiares",
    )
    return directions[(max(run_id, 1) - 1) % len(directions)]


def _maps_context(places: list[dict[str, Any]]) -> str:
    if not places:
        return (
            "DATOS DE GOOGLE MAPS: No se encontraron resultados verificables. "
            "No incluyas direcciones, horarios ni nombres de establecimientos."
        )
    parts = ["DATOS VERIFICADOS DE GOOGLE MAPS:"]
    for place in places[:20]:
        lines = [
            f"LUGAR: {place.get('name', '')}",
            f"DIRECCIÓN: {place.get('address', '')}",
        ]
        if place.get("opening_hours"):
            lines.append("HORARIOS: " + " | ".join(place["opening_hours"]))
        if place.get("rating") is not None:
            lines.append(f"CALIFICACIÓN: {place['rating']}")
        if place.get("maps_url"):
            lines.append(f"ENLACE: {place['maps_url']}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)[:8_000]


def _budget_used_today(db: Session) -> float:
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    value = (
        db.query(
            func.coalesce(
                func.sum(
                    func.coalesce(AiUsage.platform_cost_usd, AiUsage.estimated_cost_usd)
                ),
                0.0,
            )
        )
        .filter(AiUsage.created_at >= today, AiUsage.status == "success")
        .scalar()
    )
    return float(value or 0.0)


def _recent_run(db: Session) -> AiGenerationRuns | None:
    return (
        db.query(AiGenerationRuns)
        .filter(AiGenerationRuns.operation == "refresh_content")
        .order_by(AiGenerationRuns.created_at.desc(), AiGenerationRuns.id.desc())
        .first()
    )


async def generate_and_publish(db: Session) -> dict[str, Any]:
    async with generation_lock:
        recent = _recent_run(db)
        now = datetime.now(timezone.utc)
        if recent and recent.status == "running":
            raise ValueError("Ya hay una generación de contenido en curso.")
        if _budget_used_today(db) >= settings.deepseek_daily_budget_usd:
            raise ValueError("Se alcanzó el presupuesto diario configurado para DeepSeek.")

        run = AiGenerationRuns(
            operation="refresh_content",
            provider="deepseek",
            model=settings.deepseek_model,
            status="running",
            started_at=now,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        started = now
        usage: dict[str, Any] = {}
        billing_before = await get_deepseek_balance()
        editorial_direction = _editorial_direction(run.id)

        try:
            materials, source_ids = await collect_source_material()
            if not materials:
                raise ValueError("No hay fuentes activas con contenido legible.")

            existing_rows = (
                db.query(
                    Posts.title,
                    Posts.body,
                    Posts.excerpt,
                    Posts.keywords,
                    Posts.image_source_url,
                    Posts.image_url,
                )
                .order_by(Posts.created_at.desc(), Posts.id.desc())
                .limit(50)
                .all()
            )
            existing_articles = [
                (row[0], row[1], row[2] or "", row[3] or "") for row in existing_rows
            ]
            post_history = [
                f"TÍTULO: {row[0]} | EXTRACTO: {row[2] or ''} | "
                f"PALABRAS CLAVE: {row[3] or ''} | FRAGMENTO: {(row[1] or '')[:900]}"
                for row in existing_rows[:12]
            ]
            historical_runs = (
                db.query(
                    AiGenerationRuns.generated_title,
                    AiGenerationRuns.generated_body,
                    AiGenerationRuns.generated_excerpt,
                    AiGenerationRuns.generated_keywords,
                )
                .filter(
                    AiGenerationRuns.status == "success",
                    AiGenerationRuns.generated_title.isnot(None),
                    AiGenerationRuns.id != run.id,
                )
                .order_by(AiGenerationRuns.created_at.desc(), AiGenerationRuns.id.desc())
                .limit(20)
                .all()
            )
            historical_articles = [
                f"TÍTULO: {row[0]} | EXTRACTO: {row[2] or ''} | "
                f"PALABRAS CLAVE: {row[3] or ''} | FRAGMENTO: {(row[1] or '')[:900]}"
                for row in historical_runs
            ]
            avoid_articles = (post_history + historical_articles)[:20]
            existing_articles.extend(
                (row[0], row[1] or "", row[2] or "", row[3] or "")
                for row in historical_runs
            )
            source_context = _context(materials)
            draft_raw, draft_usage = await generate_article(
                source_context,
                avoid_articles=avoid_articles,
                editorial_direction=editorial_direction,
            )
            usage = draft_usage
            draft = _parse_article(draft_raw)
            place_queries = draft.get("place_queries") or _fallback_place_queries(draft)
            maps_places = await collect_google_places(place_queries)
            generation_context = f"{source_context}\n\n{_maps_context(maps_places)}"
            article = None
            revision_note = ""
            for attempt in range(3):
                raw_article, attempt_usage = await generate_article(
                    generation_context,
                    avoid_articles=avoid_articles,
                    revision_note=revision_note,
                    editorial_direction=editorial_direction,
                )
                usage = _merge_usage(usage, attempt_usage)
                candidate = _parse_article(raw_article)
                if not _has_template_language(candidate["body"]) and not _is_repeated(
                    candidate, existing_articles
                ):
                    article = candidate
                    break
                if attempt == 2:
                    raise DeepSeekError(
                        "DeepSeek no logró redactar un artículo suficientemente diferente."
                    )
                revision_note = (
                    f"El borrador anterior ('{candidate['title']}') fue descartado por parecerse "
                    "demasiado a un artículo publicado. Descarta por completo ese enfoque: "
                    "reescribe desde cero con otro tema concreto, otros lugares, otros ejemplos "
                    "y otra estructura. No reutilices su apertura, párrafos ni conclusión. "
                    "Las palabras comunes pueden repetirse, pero no las frases, párrafos ni "
                    "la idea central."
                )
            if article is None:
                raise DeepSeekError("No se pudo validar el artículo generado.")
            article["slug"] = _unique_slug(db, article["slug"])
            article["image_url"], article["image_source_url"] = await download_free_image(
                {row[4] for row in existing_rows if row[4]},
                {row[5] for row in existing_rows if row[5]},
                (
                    f"TÍTULO: {article['title']}\n"
                    f"PALABRAS CLAVE: {article['keywords']}\n"
                    f"LUGARES: {', '.join(place_queries)}\n"
                    f"{article['excerpt']} {article['body']}"
                ),
            )
            billing_after = await get_deepseek_balance()
            platform_cost = calculate_platform_cost(billing_before, billing_after)

            usage_row = AiUsage(
                run_id=run.id,
                provider="deepseek",
                model=settings.deepseek_model,
                operation="refresh_content",
                status="success",
                api_requests=usage["api_requests"],
                prompt_tokens=usage["prompt_tokens"],
                prompt_cache_hit_tokens=usage["prompt_cache_hit_tokens"],
                prompt_cache_miss_tokens=usage["prompt_cache_miss_tokens"],
                completion_tokens=usage["completion_tokens"],
                total_tokens=usage["total_tokens"],
                cache_hit_price_per_million=usage["pricing"][0],
                cache_miss_price_per_million=usage["pricing"][1],
                output_price_per_million=usage["pricing"][2],
                estimated_cost_usd=usage["estimated_cost_usd"],
                platform_cost_usd=platform_cost,
                platform_balance_usd=(
                    billing_after.get("usd") if billing_after else None
                ),
            )
            post = Posts(
                slug=article["slug"],
                title=article["title"],
                keywords=article["keywords"],
                excerpt=article["excerpt"],
                body=article["body"],
                category=article["category"],
                image_url=article["image_url"],
                image_source_url=article["image_source_url"],
                author="Black Cat Hostal",
                sort_order=0,
                is_active=True,
                published_at=now,
            )
            db.add(post)
            db.add(usage_row)
            db.flush()
            run.status = "success"
            run.post_id = post.id
            run.source_ids = ",".join(str(item) for item in source_ids)
            run.generated_title = article["title"]
            run.generated_excerpt = article["excerpt"]
            run.generated_keywords = article["keywords"]
            run.generated_body = article["body"]
            run.completed_at = datetime.now(timezone.utc)
            run.duration_ms = int((run.completed_at - started).total_seconds() * 1000)
            db.commit()
            db.refresh(usage_row)
            return {"post": post, "run": run, "usage": usage_row}
        except Exception as exc:
            if isinstance(exc, DeepSeekError) and exc.usage:
                usage = _merge_usage(usage, exc.usage)
            db.rollback()
            failed_run = db.query(AiGenerationRuns).filter(AiGenerationRuns.id == run.id).first()
            if failed_run:
                failed_run.status = "failed"
                failed_run.error = str(exc)[:2000]
                failed_run.completed_at = datetime.now(timezone.utc)
                failed_run.duration_ms = int((failed_run.completed_at - started).total_seconds() * 1000)
                pricing = usage.get("pricing") or (0.0, 0.0, 0.0)
                billing_after = await get_deepseek_balance()
                db.add(
                    AiUsage(
                        run_id=failed_run.id,
                        provider="deepseek",
                        model=settings.deepseek_model,
                        operation="refresh_content",
                        status="failed",
                        api_requests=int(usage.get("api_requests") or 0),
                        prompt_tokens=int(usage.get("prompt_tokens") or 0),
                        prompt_cache_hit_tokens=int(usage.get("prompt_cache_hit_tokens") or 0),
                        prompt_cache_miss_tokens=int(usage.get("prompt_cache_miss_tokens") or 0),
                        completion_tokens=int(usage.get("completion_tokens") or 0),
                        total_tokens=int(usage.get("total_tokens") or 0),
                        cache_hit_price_per_million=pricing[0],
                        cache_miss_price_per_million=pricing[1],
                        output_price_per_million=pricing[2],
                        estimated_cost_usd=float(usage.get("estimated_cost_usd") or 0),
                        platform_cost_usd=calculate_platform_cost(
                            billing_before, billing_after
                        ),
                        platform_balance_usd=(
                            billing_after.get("usd") if billing_after else None
                        ),
                    )
                )
                db.commit()
            raise
