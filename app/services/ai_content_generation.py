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
from app.services.ai_image_service import download_source_image
from app.services.deepseek_client import DeepSeekError, generate_article
from app.services.mcp_sources import collect_source_material

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


def _parse_article(raw: str) -> dict[str, str]:
    content = raw.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.IGNORECASE)
        content = re.sub(r"\s*```$", "", content)
    try:
        article = json.loads(content)
    except json.JSONDecodeError as exc:
        raise DeepSeekError("DeepSeek no devolvió el JSON esperado.") from exc
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
    return {
        "title": title[:220],
        "slug": _slugify(str(article.get("slug") or title))[:220],
        "keywords": keywords[:500],
        "excerpt": excerpt[:500],
        "body": _limit_words(body, settings.deepseek_article_max_words),
        "category": str(article.get("category") or "Turismo")[:80],
    }


def _limit_words(value: str, maximum: int) -> str:
    words = value.split()
    if len(words) <= maximum:
        return value
    truncated = " ".join(words[:maximum]).rstrip(" .,;:") + "…"
    return truncated


def _is_repeated(
    article: dict[str, str],
    existing: list[tuple[str, str, str, str]],
) -> bool:
    normalized_title = re.sub(r"\W+", " ", article["title"].lower()).strip()
    new_title_words = set(re.findall(r"\w{4,}", normalized_title))
    new_body = article["body"].lower()
    new_opening = " ".join(re.findall(r"\w{4,}", new_body)[:14])
    new_phrases = _word_ngrams(new_body)
    new_landmarks = _landmark_phrases(article["title"] + " " + new_body)
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
            or title_overlap >= 0.60
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
        if new_landmarks & _landmark_phrases(f"{title} {old_body}"):
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


def _landmark_phrases(value: str) -> set[str]:
    words = re.findall(r"[a-záéíóúñ]+", value.lower())
    landmark_prefixes = {
        "cerro", "barrio", "palacio", "museo", "plaza", "parque",
        "mercado", "mirador",
    }
    standalone_landmarks = {"costanera", "lastarria", "yungay"}
    phrases: set[str] = set()
    for index, word in enumerate(words):
        if word in standalone_landmarks:
            phrases.add(word)
        elif word in landmark_prefixes:
            for size in (2, 3, 4):
                if index + size <= len(words):
                    phrases.add(" ".join(words[index : index + size]))
    return phrases


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


def _budget_used_today(db: Session) -> float:
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    value = (
        db.query(func.coalesce(func.sum(AiUsage.estimated_cost_usd), 0.0))
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
            raw_article, usage = await generate_article(
                _context(materials),
                avoid_articles=[
                    f"TÍTULO: {row[0]} | EXTRACTO: {row[2] or ''} | "
                    f"PALABRAS CLAVE: {row[3] or ''} | FRAGMENTO: {(row[1] or '')[:900]}"
                    for row in existing_rows[:12]
                ],
            )
            article = _parse_article(raw_article)
            if _has_template_language(article["body"]):
                raise DeepSeekError(
                    "El artículo generado contiene frases promocionales o una plantilla repetida."
                )
            if _is_repeated(article, existing_articles):
                raise DeepSeekError("El artículo generado repite contenido publicado anteriormente.")
            article["slug"] = _unique_slug(db, article["slug"])
            article["image_url"], article["image_source_url"] = await download_source_image(
                materials,
                {row[4] for row in existing_rows if row[4]},
                {row[5] for row in existing_rows if row[5]},
                (
                    f"{article['title']} {article['keywords']} "
                    f"{article['excerpt']} {article['body']}"
                ),
            )

            usage_row = AiUsage(
                run_id=run.id,
                provider="deepseek",
                model=settings.deepseek_model,
                operation="refresh_content",
                status="success",
                prompt_tokens=usage["prompt_tokens"],
                prompt_cache_hit_tokens=usage["prompt_cache_hit_tokens"],
                prompt_cache_miss_tokens=usage["prompt_cache_miss_tokens"],
                completion_tokens=usage["completion_tokens"],
                total_tokens=usage["total_tokens"],
                cache_hit_price_per_million=usage["pricing"][0],
                cache_miss_price_per_million=usage["pricing"][1],
                output_price_per_million=usage["pricing"][2],
                estimated_cost_usd=usage["estimated_cost_usd"],
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
            run.completed_at = datetime.now(timezone.utc)
            run.duration_ms = int((run.completed_at - started).total_seconds() * 1000)
            db.commit()
            db.refresh(usage_row)
            return {"post": post, "run": run, "usage": usage_row}
        except Exception as exc:
            db.rollback()
            failed_run = db.query(AiGenerationRuns).filter(AiGenerationRuns.id == run.id).first()
            if failed_run:
                failed_run.status = "failed"
                failed_run.error = str(exc)[:2000]
                failed_run.completed_at = datetime.now(timezone.utc)
                failed_run.duration_ms = int((failed_run.completed_at - started).total_seconds() * 1000)
                db.add(
                    AiUsage(
                        run_id=failed_run.id,
                        provider="deepseek",
                        model=settings.deepseek_model,
                        operation="refresh_content",
                        status="failed",
                    )
                )
                db.commit()
            raise
