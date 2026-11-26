"""Interfaz web completa (Dashboard + Catálogo + Partner + Subtítulos + Tráiler + Reproductor P2P)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import json
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from fastapi import APIRouter, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import ValidationError

from rascal.auth.mailer import build_mailer
from rascal.auth.rbac import PermissionDenied, Principal, can_edit_movie, can_manage_sources
from rascal.auth.service import (
    AuthService,
    ChallengeRequired,
    LoginFailed,
    LoginSuccess,
    RegistrationError,
)
from rascal.config import Settings
from rascal.data.db import get_sessionmaker, session_scope
from rascal.data.enums import ColorKind, CreditRole, MediaType, Role, SourceStatus
from rascal.data.models import MediaSource, Movie, User
from rascal.domain.schemas import (
    CastInput,
    CrewInput,
    FilmographyFilters,
    MagnetSourceForm,
    MovieForm,
    PersonInput,
    RegisterForm,
    ReviewForm,
    TechSpecsForm,
    TorrentSourceForm,
)
from rascal.domain.schemas import (
    AUDIO_LANGUAGE_SUGGESTIONS,
    QUALITY_SUGGESTIONS,
    PartnerRequestForm,
    PasswordChangeForm,
    ProfileForm,
)
from rascal.domain.services.catalog import CatalogError, CatalogService, NotFound
from rascal.domain.services.queries import CatalogQueries
from rascal.domain.services.reviews import ReviewService
from rascal.domain.services.partner_requests import PartnerRequestService
from rascal.domain.services.profile import ProfileService
from rascal.domain.services.series import SeriesQueries, SeriesService
from rascal.domain.services.watch import list_watch_links
from rascal.domain.services.social import SocialService
from rascal.data.repositories.reviews import ReviewRepository
from rascal.integrations.tmdb import TmdbError
from rascal.media.avatars import AvatarError, delete_avatar, save_avatar
from rascal.media.subtitles import SubtitleError, list_subtitles, save_subtitle
from rascal.providers import MediaTarget, ProviderError, ProviderRegistry, build_registry
from rascal.security import csp_nonce, install_security, is_safe_href
from rascal.streaming.client import StreamerClient, StreamerUnavailable
from rascal.streaming.downloads import PurgeError, purge_local_download

COOKIE_NAME = "rascal_session"
CHALLENGE_COOKIE = "rascal_2fa"
SESSION_TTL_S = 60 * 60 * 24 * 7
CHALLENGE_TTL_S = 10 * 60
SUBTITLES_DIR = Path("var") / "subtitles"  # sólo compatibilidad; las rutas usan Settings.subtitles_dir
PAGE_SIZE = 60
MAX_TMDB_SEASONS = 60
REVIEWS_PAGE_SIZE = 30


def _uid(principal: Any) -> int | None:
    if principal is None:
        return None
    return getattr(principal, "user_id", None) or getattr(principal, "id", None)


def _sign_payload(secret: str, purpose: str, data: dict[str, Any], ttl_s: int) -> str:
    """Token compacto firmado con HMAC-SHA256; ``purpose`` separa sesiones de retos 2FA."""
    payload = json.dumps({**data, "p": purpose, "exp": int(time.time()) + ttl_s}, separators=(",", ":")).encode()
    b64 = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    sig = hmac.new(secret.encode(), b64.encode(), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def _verify_payload(secret: str, purpose: str, token: str | None) -> dict[str, Any] | None:
    if not token or "." not in token:
        return None
    b64, sig = token.rsplit(".", 1)
    expected = hmac.new(secret.encode(), b64.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        pad = "=" * (-len(b64) % 4)
        data = json.loads(base64.urlsafe_b64decode(b64 + pad))
        if int(data.get("exp", 0)) < int(time.time()) or data.get("p", "session") != purpose:
            return None
        return data
    except Exception:
        return None


def _sign_session(secret: str, user_id: int) -> str:
    return _sign_payload(secret, "session", {"uid": user_id}, SESSION_TTL_S)


def _verify_session(secret: str, token: str | None) -> int | None:
    data = _verify_payload(secret, "session", token)
    try:
        return int(data["uid"]) if data else None
    except (KeyError, TypeError, ValueError):
        return None


def _call_compat(fn: Any, *args: Any, **kwargs: Any) -> Any:
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())
    pos_p = [
        x
        for x in params
        if x.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    u_args = args[: len(pos_p)]
    pos_n = {x.name for x in pos_p[: len(u_args)]}
    acc = {k: v for k, v in kwargs.items() if k in sig.parameters and k not in pos_n}
    return fn(*u_args, **acc)


def _unwrap_list(res: Any) -> list[Any]:
    if res is None:
        return []
    if isinstance(res, tuple):
        return list(res[0]) if res else []
    if hasattr(res, "items") and not isinstance(res, dict):
        return list(res.items)
    return list(res)


def _auth_service(session: Any, settings: Settings) -> AuthService:
    """SMTP real si está configurado; si no, ``ConsoleMailer`` (escribe el código en el log)."""
    return AuthService(session, settings, build_mailer(settings))


def _fmt_bytes(num: int | None) -> str:
    if not num:
        return "—"
    val = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024.0:
            return f"{val:.1f} {unit}"
        val /= 1024.0
    return f"{val:.1f} PB"


def _youtube_embed_url(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(
        r"(?:youtube\.com/(?:watch\?v=|embed/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})",
        url.strip(),
    )
    if m:
        return f"https://www.youtube.com/embed/{m.group(1)}"
    return None


def _save_subtitle(movie_id: int, filename: str, raw: bytes, label: str = "Español", base: Path | None = None) -> str:
    """Compatibilidad: guarda un subtítulo (la conversión y validación viven en rascal.media.subtitles)."""
    return save_subtitle(base or SUBTITLES_DIR, movie_id, raw, label)


def _list_subtitles(movie_id: int, base: Path | None = None) -> list[dict[str, Any]]:
    """Compatibilidad: pistas de una película con su URL pública."""
    return list_subtitles(base or SUBTITLES_DIR, movie_id)


def _parse_csv(raw: str) -> list[str]:
    return [x.strip() for x in raw.split(",") if x.strip()]


def _parse_people(raw: str) -> list[PersonInput]:
    return [PersonInput(name=x.strip()) for x in raw.split(",") if x.strip()]


def _parse_cast(raw: str) -> list[CastInput]:
    items: list[CastInput] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            name, char = line.split(":", 1)
            items.append(CastInput(name=name.strip(), character_name=char.strip() or None))
        else:
            items.append(CastInput(name=line))
    return items


def _parse_crew(raw: str) -> list[CrewInput]:
    items: list[CrewInput] = []
    valid_roles = {r.value for r in CreditRole if r not in (CreditRole.CAST, CreditRole.DIRECTOR)}
    for line in raw.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        name, role_str = line.split(":", 1)
        role_clean = role_str.strip().lower()
        if role_clean in valid_roles:
            items.append(CrewInput(name=name.strip(), role=CreditRole(role_clean)))
    return items


def _to_int(val: str | None) -> int | None:
    if val is None or not val.strip():
        return None
    try:
        return int(val.strip())
    except ValueError:
        return None


# ==============================================================================
# PLANTILLAS JINJA2 (archivos en src/rascal/templates/web/, tema oscuro estilo Letterboxd)
# ==============================================================================
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates" / "web"
USER_TABS = ("perfil", "diario", "watchlist", "favoritas", "seguidores", "seguidos")
PARTNER_ROLES = (Role.PARTNER, Role.ADMIN)
_CSS_SAFE_URL = re.compile(r"^https?://[^\s'\"()<>\\]+$")
_AVATAR_MEDIA = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}


def _media_url(path: str | None) -> str | None:
    """URL utilizable en <img>: http(s) tal cual; avatares locales -> /avatars/...; lo demás, None."""
    if not path:
        return None
    if path.startswith(("http://", "https://")):
        return path
    if path.startswith("avatars/"):
        return "/" + path
    return None


def _css_safe_url(url: str | None) -> str | None:
    """Sólo URLs que se pueden meter con seguridad dentro de url(...) de un atributo style."""
    return url if url and _CSS_SAFE_URL.match(url) else None


def _source_live(cfg: Settings, source_id: int) -> dict[str, Any] | None:
    """Estado en vivo de una fuente (sólo lectura: nunca arranca la descarga). None si no está activa."""
    client = StreamerClient(cfg, timeout=3.0)
    try:
        data = client.status(source_id)
    except StreamerUnavailable:
        return None
    finally:
        client.close()
    if not data or "progress" not in data:
        return None
    return data


def _pager(page: int, total: int, size: int) -> dict[str, int]:
    pages = max(1, -(-total // size))
    page = min(max(1, page), pages)
    return {"page": page, "pages": pages, "total": total, "size": size, "offset": (page - 1) * size}


@dataclass
class WebContext:
    """Dependencias compartidas por ``web.py`` y los routers de ``rascal.webui``."""

    cfg: Settings
    factory: Any
    env: Environment
    providers: ProviderRegistry

    def render(self, template_name: str, *, title: str, viewer: Principal | None = None, nav: str = "",
               msg: str | None = None, err: str | None = None, q: str = "", **ctx: Any) -> HTMLResponse:
        html = self.env.get_template(template_name).render(
            title=title, viewer=viewer, viewer_uid=_uid(viewer), nav=nav, msg=msg, err=err, q=q, **ctx
        )
        return HTMLResponse(html)

    def viewer(self, request: Request, session: Any) -> Principal | None:
        uid = _verify_session(self.cfg.secret_key.get_secret_value(), request.cookies.get(COOKIE_NAME))
        if not uid:
            return None
        return _auth_service(session, self.cfg).refresh_principal(uid)

    def set_session(self, response: Response, user_id: int) -> None:
        response.set_cookie(
            COOKIE_NAME, _sign_session(self.cfg.secret_key.get_secret_value(), int(user_id)), httponly=True,
            samesite="lax", secure=self.cfg.secure_cookies, max_age=SESSION_TTL_S, path="/",
        )

    @staticmethod
    def back(url: str, *, msg: str | None = None, err: str | None = None, anchor: str = "") -> RedirectResponse:
        sep = "&" if "?" in url else "?"
        if msg:
            url += f"{sep}msg={quote(msg)}"
        elif err:
            url += f"{sep}err={quote(err)}"
        return RedirectResponse(url + anchor, status_code=303)


def build_template_env() -> Environment:
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=select_autoescape(["html", "xml"]))
    env.globals["fmt_bytes"] = _fmt_bytes
    env.globals["getattr"] = getattr
    env.globals["ColorKind"] = ColorKind
    env.globals["media_url"] = _media_url
    env.globals["csp_nonce"] = csp_nonce
    env.globals["safe_href"] = lambda url: url if is_safe_href(url) else None
    return env


def create_web_router(settings: Settings | None = None, session_factory: Any = None) -> APIRouter:
    cfg = settings or Settings()
    factory = session_factory or get_sessionmaker()
    env = build_template_env()
    ctx = WebContext(cfg=cfg, factory=factory, env=env, providers=build_registry(cfg))
    subtitles_base = cfg.subtitles_dir
    router = APIRouter(include_in_schema=False)
    render = ctx.render
    current_viewer = ctx.viewer

    def import_series_seasons(session: Any, viewer: Principal, series_id: int, tmdb_id: int) -> str:
        """Descarga temporadas y capítulos de TMDB y los aplica (aditivo). Devuelve una nota legible."""
        try:
            client = ctx.providers.tmdb.client()
            _, numbers = client.fetch_series(tmdb_id)
            seasons = [client.fetch_season(tmdb_id, n) for n in numbers[:MAX_TMDB_SEASONS]]
            stats = SeriesService(session, viewer).import_tmdb_seasons(series_id, seasons)
        except (ProviderError, TmdbError) as exc:
            return f"temporadas no importadas: {exc}"
        return (f"{stats['seasons_created']} temporada(s) y {stats['episodes_created']} capítulo(s) "
                "importados de TMDB")

    ctx.import_series_seasons = import_series_seasons  # type: ignore[attr-defined]

    @router.get("/subtitles/episode/{episode_id}/{filename}")
    def serve_episode_subtitle(episode_id: int, filename: str):
        path = ctx.providers.local_subtitles.file_path(MediaTarget(movie_id=0, episode_id=episode_id), filename)
        if path is None:
            raise HTTPException(status_code=404, detail="Subtítulo no encontrado")
        return Response(content=path.read_bytes(), media_type="text/vtt; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})

    @router.get("/subtitles/{movie_id}/{filename}")
    def serve_subtitle(movie_id: int, filename: str):
        safe_name = Path(filename).name  # evita ../ en la URL
        if not safe_name.lower().endswith(".vtt"):
            raise HTTPException(status_code=404, detail="Subtítulo no encontrado")
        path = subtitles_base / str(int(movie_id)) / safe_name
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Subtítulo no encontrado")
        return Response(content=path.read_bytes(), media_type="text/vtt; charset=utf-8",
                        headers={"Cache-Control": "no-cache"})

    @router.get("/avatars/{filename}")
    def serve_avatar(filename: str):
        safe_name = Path(filename).name
        path = cfg.data_dir / "avatars" / safe_name
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Imagen no encontrada")
        media = _AVATAR_MEDIA.get(path.suffix.lstrip(".").lower(), "application/octet-stream")
        return FileResponse(path, media_type=media, headers={"Cache-Control": "public, max-age=3600"})

    @router.get("/", response_class=HTMLResponse)
    def home_page(request: Request, msg: str | None = None):
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            feed = CatalogQueries(session, viewer).home()
            return render("home.html", title="Inicio", viewer=viewer, nav="home", msg=msg, feed=feed)

    @router.get("/browse", response_class=HTMLResponse)
    def browse_page(
        request: Request,
        genre: str = "",
        sort: str = "popularity",
        year_from: str = "",
        year_to: str = "",
        min_stars: str = "",
        type: str = "",  # noqa: A002 - nombre público del parámetro (?type=series)
        page: int = 1,
    ):
        media_type = {"movie": MediaType.MOVIE, "series": MediaType.SERIES}.get(type)
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            queries = CatalogQueries(session, viewer)
            genres = queries.genres()
            try:
                filters = FilmographyFilters(
                    genre_slug=genre or None,
                    sort=sort if sort in ("popularity", "rating", "recent", "title") else "popularity",  # type: ignore[arg-type]
                    year_from=_to_int(year_from),
                    year_to=_to_int(year_to),
                    min_stars=float(min_stars) if min_stars.strip() else None,
                    limit=PAGE_SIZE,
                    offset=max(0, (max(page, 1) - 1) * PAGE_SIZE),
                )
            except Exception:
                filters = FilmographyFilters(limit=PAGE_SIZE)
            movies, total = queries.browse(filters, media_type=media_type)
            pager = _pager(page, total, PAGE_SIZE)
            qs = urlencode({k: v for k, v in (("genre", genre), ("sort", sort), ("year_from", year_from),
                                              ("year_to", year_to), ("min_stars", min_stars), ("type", type)) if v})
            return render(
                "browse.html",
                title="Series" if media_type is MediaType.SERIES else "Catálogo",
                viewer=viewer,
                nav="series" if media_type is MediaType.SERIES else "browse",
                genres=genres,
                cards=movies,
                genre=genre,
                sort=sort,
                year_from=year_from,
                year_to=year_to,
                min_stars=min_stars,
                media_type=type,
                pager=pager,
                page_base=f"/browse?{qs}&" if qs else "/browse?",
            )

    @router.get("/search", response_class=HTMLResponse)
    def search_page(request: Request, q: str = ""):
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            queries = CatalogQueries(session, viewer)
            q_clean = q.strip()[:100]
            hits = (queries.search(q_clean, limit=40) + queries.search(q_clean, kind="people", limit=20)) if q_clean else []
            return render("search.html", title=f"Buscar: {q_clean}", viewer=viewer, q=q_clean, hits=hits)

    @router.get("/series/{slug}")
    def series_alias(slug: str):
        return RedirectResponse(f"/movie/{quote(slug)}", status_code=307)

    @router.get("/movie/{slug}", response_class=HTMLResponse)
    def movie_page(
        request: Request,
        slug: str,
        source: int | None = None,
        play: str = "",
        season: int | None = None,
        episode: int | None = None,
        rpage: int = 1,
        msg: str | None = None,
        err: str | None = None,
    ):
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            queries = CatalogQueries(session, viewer)
            movie = queries.movie_detail(slug=slug)
            if movie is None:
                raise HTTPException(status_code=404, detail="Título no encontrado")

            review_pager = _pager(rpage, ReviewRepository(session).count_for_movie(movie.id), REVIEWS_PAGE_SIZE)
            reviews = queries.movie_reviews(movie.id, limit=REVIEWS_PAGE_SIZE, offset=review_pager["offset"])
            vstate = queries.viewer_state(movie.id) if viewer else None
            in_watchlist = bool((vstate or {}).get("in_watchlist", False))
            is_favorite = bool(viewer and movie.id in ProfileService(session, viewer).favorites(_uid(viewer) or 0))

            can_edit = can_edit_movie(viewer, movie.owner_id)
            can_purge = bool(viewer and viewer.role in PARTNER_ROLES)

            # ── Series: temporada → capítulo → fuentes del capítulo ──────────────
            selected_season = selected_episode = None
            episodes: list[Any] = []
            sources = list(movie.sources)
            if movie.is_series:
                series_q = SeriesQueries(session)
                if episode is not None:
                    found = series_q.episode(episode)
                    if found and found[1].series_id == movie.id:
                        season = found[1].season_number
                    else:
                        episode = None
                seasons = movie.seasons
                selected_season = next((x for x in seasons if x.season_number == season), None) or next(
                    (x for x in seasons if x.season_number > 0), seasons[0] if seasons else None
                )
                if selected_season is not None:
                    episodes = series_q.episodes(selected_season.id, include_disabled_sources=can_edit)
                    selected_episode = next((e for e in episodes if e.id == episode), None)
                sources = (
                    queries.title_sources(movie.id, movie.owner_id, episode_id=selected_episode.id)
                    if selected_episode else []
                )

            # Sólo se elige la fuente: NO se arranca nada hasta que el usuario pulse «Reproducir» (play=1).
            selected_source = None
            if sources:
                selected_source = next((x for x in sources if x.id == source), None)
                if selected_source is None:
                    playable = [x for x in sources if x.status.value != "disabled"]
                    selected_source = playable[0] if playable else sources[0]

            player_src = stream_err = None
            plan = ctx.providers.plan(selected_source) if selected_source is not None else None
            if play.lower() in ("1", "true", "yes") and selected_source is not None and plan is not None:
                if viewer is None:
                    stream_err = "Inicia sesión para reproducir esta versión."
                elif selected_source.status.value == "disabled":
                    stream_err = "Esta versión está desactivada por el equipo."
                else:
                    client = StreamerClient(cfg)
                    try:
                        if plan.needs_engine:  # P2P: arranca libtorrent. Directas/HLS: sólo el token.
                            client.start(selected_source.id)
                        raw_url = client.player_url(user_id=_uid(viewer) or 0, source_id=selected_source.id)
                        parts = urlsplit(raw_url)
                        player_src = f"{parts.path}?{parts.query}" if parts.query else parts.path
                    except StreamerUnavailable as exc:
                        stream_err = str(exc)
                    finally:
                        client.close()

            sources_live = {x.id: _source_live(cfg, x.id) for x in sources if x.is_p2p}
            episode_qs = f"&season={selected_season.season_number}&episode={selected_episode.id}" if selected_episode else ""
            return render(
                "movie.html",
                title=movie.title,
                viewer=viewer,
                msg=msg,
                err=err,
                movie=movie,
                reviews=reviews,
                review_pager=review_pager,
                in_watchlist=in_watchlist,
                is_favorite=is_favorite,
                sources=sources,
                selected_source=selected_source,
                selected_plan=plan,
                player_src=player_src,
                stream_err=stream_err,
                sources_live=sources_live,
                can_edit=can_edit,
                can_manage=can_manage_sources(viewer, movie.owner_id),
                can_purge=can_purge,
                selected_season=selected_season,
                selected_episode=selected_episode,
                episodes=episodes,
                episode_qs=episode_qs,
                youtube_embed=_youtube_embed_url(movie.trailer_url),
                backdrop_css=_css_safe_url(movie.backdrop_path),
            )

    @router.post("/movie/{slug}/favorite")
    def toggle_favorite(request: Request, slug: str):
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            if not viewer:
                return RedirectResponse("/login", status_code=303)
            movie = _call_compat(CatalogQueries(session, viewer).movie_detail, slug, slug=slug)
            if movie is None:
                raise HTTPException(status_code=404, detail="Película no encontrada")
            try:
                marked = ProfileService(session, viewer).toggle_favorite(movie.id)
                text = "Añadida a tus favoritas" if marked else "Quitada de tus favoritas"
                return RedirectResponse(f"/movie/{slug}?msg={quote(text)}", status_code=303)
            except Exception as exc:  # noqa: BLE001
                return RedirectResponse(f"/movie/{slug}?err={quote(str(exc))}", status_code=303)

    @router.post("/movie/{slug}/sources/{source_id}/purge")
    def purge_source(request: Request, slug: str, source_id: int):
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            if not viewer or viewer.role not in PARTNER_ROLES:
                text = "Solo partners o administradores pueden eliminar descargas."
                return RedirectResponse(f"/movie/{slug}?err={quote(text)}#p2p", status_code=303)
            source = session.get(MediaSource, source_id)
            movie = session.get(Movie, source.movie_id) if source is not None else None
            if source is None or movie is None or movie.slug != slug:
                return RedirectResponse(f"/movie/{slug}?err={quote('Fuente no encontrada')}#p2p", status_code=303)
            if not source.is_p2p:
                text = "Los enlaces directos no descargan nada en este equipo: no hay archivos que eliminar."
                return RedirectResponse(f"/movie/{slug}?err={quote(text)}#p2p", status_code=303)
            try:
                engine = getattr(request.app.state, "engine", None)
                result = purge_local_download(engine=engine, settings=cfg, source_id=source.id, info_hash=source.info_hash)
            except PurgeError as exc:
                return RedirectResponse(f"/movie/{slug}?err={quote(str(exc))}#p2p", status_code=303)
            # El registro (.torrent o magnet) se conserva; sólo cambia el estado.
            source.status = SourceStatus.PENDING
            source.file_size = None
            source.status_message = "Descarga eliminada del dispositivo. Puedes volver a iniciarla cuando quieras."
            session.flush()
            text = f"Descarga eliminada · {_fmt_bytes(result.freed_bytes)} liberados. El registro se conserva."
            return RedirectResponse(f"/movie/{slug}?msg={quote(text)}#p2p", status_code=303)

    @router.post("/movie/{slug}/review")
    def post_review(
        request: Request,
        slug: str,
        rating: int = Form(0),
        body: str = Form(""),
        watched_on: str = Form(""),
        is_rewatch: str | None = Form(None),
        contains_spoilers: str | None = Form(None),
    ):
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            if not viewer:
                return RedirectResponse("/login", status_code=303)
            movie = _call_compat(CatalogQueries(session, viewer).movie_detail, slug, slug=slug)
            try:
                form = ReviewForm(
                    rating=rating,
                    body=body or None,
                    watched_on=date.fromisoformat(watched_on) if watched_on.strip() else None,
                    is_rewatch=bool(is_rewatch),
                    contains_spoilers=bool(contains_spoilers),
                )
                ReviewService(session, viewer).add(movie.id, form)
                return RedirectResponse(f"/movie/{slug}?msg={quote('Reseña publicada')}", status_code=303)
            except Exception as exc:
                return RedirectResponse(f"/movie/{slug}?err={quote(str(exc))}", status_code=303)

    @router.post("/movie/{slug}/watchlist")
    def toggle_watchlist(request: Request, slug: str):
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            if not viewer:
                return RedirectResponse("/login", status_code=303)
            movie = _call_compat(CatalogQueries(session, viewer).movie_detail, slug, slug=slug)
            SocialService(session, viewer).toggle_watchlist(movie.id)
            return RedirectResponse(f"/movie/{slug}", status_code=303)

    @router.post("/review/{review_id}/vote")
    def vote_review(request: Request, review_id: int, value: int = Form(...), slug: str = Form(...)):
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            if not viewer:
                return RedirectResponse("/login", status_code=303)
            try:
                ReviewService(session, viewer).vote(review_id, value)
            except Exception:
                pass
            return RedirectResponse(f"/movie/{slug}", status_code=303)

    def _render_entity(request: Request, kind: str, slug: str, role: str = "", sort: str = "popularity",
                       page: int = 1, msg: str | None = None):
        valid_role = role if role in {r.value for r in CreditRole} else ""
        valid_sort = sort if sort in ("popularity", "rating", "recent", "title") else "popularity"
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            filters = FilmographyFilters(
                role=CreditRole(valid_role) if valid_role else None,
                sort=valid_sort,  # type: ignore[arg-type]
                limit=PAGE_SIZE,
                offset=max(0, (max(page, 1) - 1) * PAGE_SIZE),
            )
            prof = CatalogQueries(session, viewer).entity_profile(kind, slug, filters)  # type: ignore[arg-type]
            if prof is None:
                raise HTTPException(status_code=404, detail="No encontrado")
            qs = urlencode({k: v for k, v in (("role", valid_role), ("sort", valid_sort)) if v})
            return render(
                "entity.html", title=prof.ref.name, viewer=viewer, prof=prof, msg=msg,
                pager=_pager(page, prof.filmography_total, PAGE_SIZE), page_base=f"/{kind}/{slug}?{qs}&",
            )

    @router.get("/person/{slug}", response_class=HTMLResponse)
    def person_page(request: Request, slug: str, role: str = "", sort: str = "popularity", page: int = 1,
                    msg: str | None = None):
        return _render_entity(request, "person", slug, role=role, sort=sort, page=page, msg=msg)

    @router.get("/company/{slug}", response_class=HTMLResponse)
    def company_page(request: Request, slug: str, sort: str = "popularity", page: int = 1):
        return _render_entity(request, "company", slug, sort=sort, page=page)

    @router.get("/genre/{slug}", response_class=HTMLResponse)
    def genre_page(request: Request, slug: str, sort: str = "popularity", page: int = 1):
        return _render_entity(request, "genre", slug, sort=sort, page=page)

    @router.get("/user/{username}", response_class=HTMLResponse)
    def user_page(request: Request, username: str, tab: str = "perfil", msg: str | None = None):
        if tab not in USER_TABS:
            tab = "perfil"
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            prof = CatalogQueries(session, viewer).user_profile(username)
            if prof is None:
                raise HTTPException(status_code=404, detail=f"Usuario no encontrado: {username}")
            profile = ProfileService(session, viewer)
            diary = profile.diary(prof.user.id) if tab == "diario" else []
            people: list[Any] = []
            if tab in ("seguidores", "seguidos"):
                kind = "followers" if tab == "seguidores" else "following"
                people = profile.follow_list(prof.user.username, kind)
            return render(
                "user.html",
                title=f"@{prof.user.username}",
                viewer=viewer,
                prof=prof,
                tab=tab,
                diary=diary,
                people=people,
                msg=msg,
            )

    @router.post("/user/{username}/follow")
    def follow_toggle(request: Request, username: str):
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            if not viewer:
                return RedirectResponse("/login", status_code=303)
            prof = CatalogQueries(session, viewer).user_profile(username)
            social = SocialService(session, viewer)
            if prof.is_following:
                social.unfollow(username)
            else:
                social.follow(username)
            return RedirectResponse(f"/user/{username}", status_code=303)

    @router.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, err: str | None = None):
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            return render("login.html", title="Acceso", viewer=viewer, err=err)

    @router.post("/login")
    def login_submit(username: str = Form(...), password: str = Form(...)):
        with session_scope(factory) as session:
            res = _auth_service(session, cfg).authenticate(username, password)
            if isinstance(res, LoginSuccess):
                resp = RedirectResponse(f"/?msg={quote('Bienvenido, ' + res.principal.username)}", status_code=303)
                ctx.set_session(resp, res.principal.user_id)
                return resp
            if isinstance(res, ChallengeRequired):
                # Segundo factor: el reto viaja en una cookie firmada de 10 min. Antes la sesión se
                # emitía aquí mismo y el 2FA quedaba anulado (bug corregido).
                resp = RedirectResponse("/login/verify", status_code=303)
                token = _sign_payload(cfg.secret_key.get_secret_value(), "2fa",
                                      {"cid": res.challenge_id, "to": res.masked_email}, CHALLENGE_TTL_S)
                resp.set_cookie(CHALLENGE_COOKIE, token, httponly=True, samesite="strict",
                                secure=cfg.secure_cookies, max_age=CHALLENGE_TTL_S, path="/login")
                return resp
            msg = res.message if isinstance(res, LoginFailed) else "Credenciales inválidas"
            return RedirectResponse(f"/login?err={quote(msg)}", status_code=303)

    @router.get("/login/verify", response_class=HTMLResponse)
    def login_verify_page(request: Request, err: str | None = None):
        data = _verify_payload(cfg.secret_key.get_secret_value(), "2fa", request.cookies.get(CHALLENGE_COOKIE))
        if data is None:
            return RedirectResponse("/login?err=" + quote("El código expiró. Inicia sesión de nuevo."), status_code=303)
        return render("login_verify.html", title="Verificación", masked_email=data.get("to", ""), err=err)

    @router.post("/login/verify")
    def login_verify_submit(request: Request, code: str = Form(...)):
        data = _verify_payload(cfg.secret_key.get_secret_value(), "2fa", request.cookies.get(CHALLENGE_COOKIE))
        if data is None:
            return RedirectResponse("/login?err=" + quote("El código expiró. Inicia sesión de nuevo."), status_code=303)
        with session_scope(factory) as session:
            res = _auth_service(session, cfg).verify_challenge(int(data["cid"]), code)
            if isinstance(res, LoginSuccess):
                resp = RedirectResponse(f"/?msg={quote('Bienvenido, ' + res.principal.username)}", status_code=303)
                resp.delete_cookie(CHALLENGE_COOKIE, path="/login")
                ctx.set_session(resp, res.principal.user_id)
                return resp
            message = res.message if isinstance(res, LoginFailed) else "Código incorrecto."
        if "Inicia sesión de nuevo" in message:
            resp = RedirectResponse("/login?err=" + quote(message), status_code=303)
            resp.delete_cookie(CHALLENGE_COOKIE, path="/login")
            return resp
        return RedirectResponse("/login/verify?err=" + quote(message), status_code=303)

    @router.post("/register")
    def register_submit(username: str = Form(...), email: str = Form(""), password: str = Form(...)):
        with session_scope(factory) as session:
            try:
                form = RegisterForm(username=username, email=email.strip() or None, password=password)  # type: ignore[arg-type]
                principal = _auth_service(session, cfg).register(form)
            except ValidationError as exc:
                first = exc.errors()[0].get("msg", "Datos inválidos") if exc.errors() else "Datos inválidos"
                return RedirectResponse(f"/login?err={quote(str(first).removeprefix('Value error, '))}", status_code=303)
            except (RegistrationError, Exception) as exc:  # noqa: BLE001
                return RedirectResponse(f"/login?err={quote(str(exc))}", status_code=303)
            resp = RedirectResponse(f"/?msg={quote('Cuenta creada correctamente')}", status_code=303)
            ctx.set_session(resp, principal.user_id)
            return resp

    @router.get("/logout")
    def logout():
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

    def _editor_ctx(action_url: str, form: MovieForm | None, movie_id: int | None = None, sources: list[Any] | None = None) -> dict[str, Any]:
        f = form or MovieForm.model_construct(
            title="",
            original_title=None,
            tagline=None,
            synopsis=None,
            release_year=None,
            runtime_min=None,
            poster_path=None,
            backdrop_path=None,
            trailer_url=None,
            trailer_path=None,
            imdb_id=None,
            tmdb_id=None,
            is_published=True,
            media_type=MediaType.MOVIE,
            genres=[],
            companies=[],
            directors=[],
            cast=[],
            crew=[],
            tech=TechSpecsForm(),
        )
        return {
            "action_url": action_url,
            "f": f,
            "movie_id": movie_id,
            "sources": sources or [],
            "subtitles": list_subtitles(subtitles_base, movie_id) if movie_id else [],
            "genres_str": ", ".join(f.genres),
            "companies_str": ", ".join(f.companies),
            "directors_str": ", ".join(d.name for d in f.directors),
            "cast_str": "\n".join(f"{c.name} : {c.character_name}" if c.character_name else c.name for c in f.cast),
            "crew_str": "\n".join(f"{c.name} : {c.role.value}" for c in f.crew),
            "tmdb_enabled": bool(cfg.tmdb_api_key and cfg.tmdb_api_key.get_secret_value()),
            "audio_suggestions": AUDIO_LANGUAGE_SUGGESTIONS,
            "quality_suggestions": QUALITY_SUGGESTIONS,
            "is_series": f.media_type is MediaType.SERIES,
        }

    def _review_partner_request(request: Request, request_id: int, *, approve: bool) -> RedirectResponse:
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            if not viewer or viewer.role != Role.ADMIN:
                return RedirectResponse("/partner?err=" + quote("Solo los administradores revisan solicitudes"), status_code=303)
            service = PartnerRequestService(session, viewer, _auth_service(session, cfg))
            try:
                (service.approve if approve else service.reject)(request_id)
            except Exception as exc:  # noqa: BLE001 - mensaje presentable al administrador
                return RedirectResponse("/partner?err=" + quote(str(exc)), status_code=303)
            text = "Solicitud aprobada: el usuario ya es Partner" if approve else "Solicitud rechazada"
            return RedirectResponse("/partner?msg=" + quote(text), status_code=303)

    @router.get("/partner", response_class=HTMLResponse)
    def partner_dashboard(request: Request, msg: str | None = None, err: str | None = None, pq: str = "",
                          page: int = 1):
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            if not viewer:
                return RedirectResponse("/login?err=" + quote("Inicia sesión para acceder al Panel Partner"), status_code=303)
            if viewer.role not in PARTNER_ROLES:
                return render("partner_denied.html", title="Panel Partner", viewer=viewer, nav="partner")
            # Paginado y con búsqueda: nunca se carga el catálogo completo (antes: SELECT * sin LIMIT).
            svc = CatalogService(session, viewer, cfg)
            _, total = svc.manageable_movies(q=pq.strip() or None, limit=1)
            pager = _pager(page, total, PAGE_SIZE)
            movies, _ = svc.manageable_movies(q=pq.strip() or None, limit=PAGE_SIZE, offset=pager["offset"])
            requests = (
                PartnerRequestService(session, viewer, _auth_service(session, cfg)).list_pending()
                if viewer.role == Role.ADMIN else None
            )
            return render("partner_list.html", title="Panel Partner", viewer=viewer, nav="partner",
                          msg=msg, err=err, movies=movies, requests=requests, pq=pq, pager=pager)

    def _build_movie_form(
        title: str,
        original_title: str,
        release_year: str,
        runtime_min: str,
        poster_path: str,
        trailer_url: str,
        imdb_id: str,
        tagline: str,
        synopsis: str,
        genres: str,
        companies: str,
        directors: str,
        cast: str,
        crew: str,
        aspect_ratio: str,
        color: str,
        original_language: str,
        country: str,
        is_published: str | None,
        *,
        backdrop_path: str = "",
        tmdb_id: str = "",
        trailer_path: str = "",
        media_type: str = "movie",
    ) -> MovieForm:
        tech = TechSpecsForm(
            aspect_ratio=aspect_ratio.strip() or None,
            color=ColorKind(color) if color in {c.value for c in ColorKind} else None,
            original_language=original_language.strip() or None,
            country=country.strip() or None,
        )
        return MovieForm(
            title=title,
            original_title=original_title.strip() or None,
            release_year=_to_int(release_year),
            runtime_min=_to_int(runtime_min),
            poster_path=poster_path.strip() or None,
            backdrop_path=backdrop_path.strip() or None,  # no se pierde al guardar (bug corregido)
            trailer_url=trailer_url.strip() or None,  # type: ignore[arg-type]
            trailer_path=trailer_path.strip() or None,  # idem: el tráiler subido se conserva
            imdb_id=imdb_id.strip() or None,
            tmdb_id=_to_int(tmdb_id),
            tagline=tagline.strip() or None,
            synopsis=synopsis.strip() or None,
            is_published=bool(is_published),
            media_type=MediaType.SERIES if media_type == "series" else MediaType.MOVIE,
            genres=_parse_csv(genres),
            companies=_parse_csv(companies),
            directors=_parse_people(directors),
            cast=_parse_cast(cast),
            crew=_parse_crew(crew),
            tech=tech,
        )

    @router.get("/partner/movie/new", response_class=HTMLResponse)
    def partner_new_movie_page(request: Request, err: str | None = None, type: str = "movie"):  # noqa: A002
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            if not viewer or viewer.role not in (Role.PARTNER, Role.ADMIN):
                return RedirectResponse("/partner", status_code=303)
            editor = _editor_ctx("/partner/movie/new", None)
            if type == "series":
                editor["f"] = editor["f"].model_copy(update={"media_type": MediaType.SERIES})
                editor["is_series"] = True
            return render("partner_edit.html", title="Nueva serie" if type == "series" else "Nueva película",
                          viewer=viewer, nav="partner", err=err, **editor)

    @router.post("/partner/movie/new")
    async def partner_new_movie_submit(
        request: Request,
        title: str = Form(...),
        original_title: str = Form(""),
        release_year: str = Form(""),
        runtime_min: str = Form(""),
        poster_path: str = Form(""),
        backdrop_path: str = Form(""),
        tmdb_id: str = Form(""),
        trailer_url: str = Form(""),
        trailer_path: str = Form(""),
        imdb_id: str = Form(""),
        tagline: str = Form(""),
        synopsis: str = Form(""),
        genres: str = Form(""),
        companies: str = Form(""),
        directors: str = Form(""),
        cast: str = Form(""),
        crew: str = Form(""),
        aspect_ratio: str = Form(""),
        color: str = Form(""),
        original_language: str = Form(""),
        country: str = Form(""),
        is_published: str | None = Form(None),
        media_type: str = Form("movie"),
        tmdb_import_seasons: str | None = Form(None),
        torrent_file: UploadFile | None = File(None),
        magnet_uri: str = Form(""),
        subtitle_file: UploadFile | None = File(None),
        subtitle_label: str = Form("Español"),
        source_label: str = Form("1080p Principal"),
        source_audio: str = Form(""),
        source_quality: str = Form(""),
        rights_statement: str = Form("Distribución autorizada / Dominio público"),
    ):
        torrent_bytes = await torrent_file.read() if torrent_file and torrent_file.filename else b""
        sub_bytes = await subtitle_file.read() if subtitle_file and subtitle_file.filename else b""
        sub_name = subtitle_file.filename if subtitle_file and subtitle_file.filename else ""

        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            if not viewer or viewer.role not in PARTNER_ROLES:
                return RedirectResponse("/partner", status_code=303)
            try:
                form = _build_movie_form(
                    title, original_title, release_year, runtime_min, poster_path, trailer_url, imdb_id,
                    tagline, synopsis, genres, companies, directors, cast, crew,
                    aspect_ratio, color, original_language, country, is_published,
                    backdrop_path=backdrop_path, tmdb_id=tmdb_id, trailer_path=trailer_path, media_type=media_type,
                )
                svc = CatalogService(session, viewer, cfg)
                # create_movie devuelve el id (int). La versión anterior leía .id y perdía subtítulos/torrents.
                movie_id = svc.create_movie(form)
                is_series = form.media_type is MediaType.SERIES
                notes = ["Serie creada" if is_series else "Película creada"]
                if is_series and tmdb_import_seasons and form.tmdb_id:
                    notes.append(import_series_seasons(session, viewer, movie_id, form.tmdb_id))
                if is_series and (torrent_bytes or magnet_uri.strip()):
                    notes.append("las fuentes de una serie se añaden por capítulo")
                    torrent_bytes, magnet_uri = b"", ""

                if sub_bytes and sub_name:
                    try:
                        save_subtitle(subtitles_base, movie_id, sub_bytes, subtitle_label)
                        notes.append("subtítulos guardados")
                    except SubtitleError as exc:
                        notes.append(f"subtítulos omitidos: {exc}")

                if torrent_bytes:
                    svc.add_torrent_source(
                        movie_id,
                        TorrentSourceForm(label=source_label, audio_language=source_audio or None,
                                          quality=source_quality or None, rights_statement=rights_statement),
                        torrent_bytes,
                    )
                    notes.append("torrent guardado")
                elif magnet_uri.strip():
                    svc.add_magnet_source(
                        movie_id,
                        MagnetSourceForm(magnet_uri=magnet_uri.strip(), label=source_label,
                                         audio_language=source_audio or None, quality=source_quality or None,
                                         rights_statement=rights_statement),
                    )
                    notes.append("magnet guardado")
                if torrent_bytes or magnet_uri.strip():
                    notes.append("pulsa «Iniciar descarga y reproducir» en la ficha para empezar")
                return RedirectResponse(f"/partner/movie/{movie_id}?msg={quote(' · '.join(notes))}", status_code=303)
            except Exception as exc:  # noqa: BLE001
                return RedirectResponse(f"/partner/movie/new?err={quote(str(exc))}", status_code=303)

    @router.get("/partner/movie/{movie_id}", response_class=HTMLResponse)
    def partner_edit_movie_page(request: Request, movie_id: int, msg: str | None = None, err: str | None = None):
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            if not viewer or viewer.role not in (Role.PARTNER, Role.ADMIN):
                return RedirectResponse("/partner", status_code=303)
            svc = CatalogService(session, viewer, cfg)
            try:
                form = svc.movie_form(movie_id)
                sources = svc.list_sources(movie_id)
                seasons = (
                    SeriesService(session, viewer).seasons_with_episodes(movie_id)
                    if form.media_type is MediaType.SERIES else []
                )
            except (NotFound, CatalogError, Exception) as exc:
                return RedirectResponse(f"/partner?err={quote(str(exc))}", status_code=303)
            movie_row = session.get(Movie, movie_id)
            people = (
                [("Dirección", d) for d in form.directors]
                + [("Reparto", c) for c in form.cast]
                + [(c.role.value, c) for c in form.crew]
            )
            return render(
                "partner_edit.html",
                title=f"Editar: {form.title}",
                viewer=viewer,
                nav="partner",
                msg=msg,
                err=err,
                seasons=seasons,
                watch_links=list_watch_links(session, movie_id),
                people=people,
                movie_slug=movie_row.slug if movie_row else "",
                region=cfg.tmdb_region,
                **_editor_ctx(f"/partner/movie/{movie_id}", form, movie_id=movie_id, sources=sources),
            )

    @router.post("/partner/movie/{movie_id}")
    def partner_edit_movie_submit(
        request: Request,
        movie_id: int,
        title: str = Form(...),
        original_title: str = Form(""),
        release_year: str = Form(""),
        runtime_min: str = Form(""),
        poster_path: str = Form(""),
        backdrop_path: str = Form(""),
        tmdb_id: str = Form(""),
        trailer_url: str = Form(""),
        trailer_path: str = Form(""),
        imdb_id: str = Form(""),
        tagline: str = Form(""),
        synopsis: str = Form(""),
        genres: str = Form(""),
        companies: str = Form(""),
        directors: str = Form(""),
        cast: str = Form(""),
        crew: str = Form(""),
        aspect_ratio: str = Form(""),
        color: str = Form(""),
        original_language: str = Form(""),
        country: str = Form(""),
        is_published: str | None = Form(None),
        media_type: str = Form("movie"),
    ):
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            if not viewer or viewer.role not in PARTNER_ROLES:
                return RedirectResponse("/partner", status_code=303)
            try:
                form = _build_movie_form(
                    title, original_title, release_year, runtime_min, poster_path, trailer_url, imdb_id,
                    tagline, synopsis, genres, companies, directors, cast, crew,
                    aspect_ratio, color, original_language, country, is_published,
                    backdrop_path=backdrop_path, tmdb_id=tmdb_id, trailer_path=trailer_path, media_type=media_type,
                )
                CatalogService(session, viewer, cfg).update_movie(movie_id, form)
                return RedirectResponse(f"/partner/movie/{movie_id}?msg={quote('Cambios guardados')}", status_code=303)
            except Exception as exc:  # noqa: BLE001
                return RedirectResponse(f"/partner/movie/{movie_id}?err={quote(str(exc))}", status_code=303)

    @router.post("/partner/movie/{movie_id}/upload-subtitle")
    async def partner_upload_subtitle(
        request: Request,
        movie_id: int,
        subtitle_file: UploadFile = File(...),
        subtitle_label: str = Form("Español"),
    ):
        raw = await subtitle_file.read()
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            if not viewer or viewer.role not in PARTNER_ROLES:
                return RedirectResponse("/partner", status_code=303)
            if not raw or not subtitle_file.filename:
                return RedirectResponse(f"/partner/movie/{movie_id}?err={quote('Selecciona un archivo .srt o .vtt')}", status_code=303)
            try:
                # Comprueba permiso de edición (dueño o admin) antes de escribir en disco.
                CatalogService(session, viewer, cfg).movie_form(movie_id)
                save_subtitle(subtitles_base, movie_id, raw, subtitle_label)
                return RedirectResponse(f"/partner/movie/{movie_id}?msg={quote('Subtítulos cargados correctamente')}", status_code=303)
            except (SubtitleError, CatalogError, NotFound, PermissionDenied, Exception) as exc:  # noqa: BLE001
                return RedirectResponse(f"/partner/movie/{movie_id}?err={quote(str(exc))}", status_code=303)

    @router.post("/partner/movie/{movie_id}/upload-torrent")
    async def partner_upload_torrent(
        request: Request,
        movie_id: int,
        torrent_file: UploadFile = File(...),
        label: str = Form("Principal"),
        audio_language: str = Form(""),
        quality: str = Form(""),
        rights_statement: str = Form(...),
    ):
        raw = await torrent_file.read()
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            if not viewer or viewer.role not in PARTNER_ROLES:
                return RedirectResponse("/partner", status_code=303)
            try:
                form = TorrentSourceForm(label=label, audio_language=audio_language or None,
                                         quality=quality or None, rights_statement=rights_statement)
                CatalogService(session, viewer, cfg).add_torrent_source(movie_id, form, raw)
                text = "Archivo .torrent cargado. Pulsa «Iniciar descarga y reproducir» en la ficha para empezar."
                return RedirectResponse(f"/partner/movie/{movie_id}?msg={quote(text)}", status_code=303)
            except Exception as exc:  # noqa: BLE001
                return RedirectResponse(f"/partner/movie/{movie_id}?err={quote(str(exc))}", status_code=303)

    @router.post("/partner/movie/{movie_id}/add-magnet")
    def partner_add_magnet(
        request: Request,
        movie_id: int,
        magnet_uri: str = Form(...),
        label: str = Form("Principal"),
        audio_language: str = Form(""),
        quality: str = Form(""),
        rights_statement: str = Form(...),
    ):
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            if not viewer or viewer.role not in PARTNER_ROLES:
                return RedirectResponse("/partner", status_code=303)
            try:
                form = MagnetSourceForm(magnet_uri=magnet_uri, label=label, audio_language=audio_language or None,
                                        quality=quality or None, rights_statement=rights_statement)
                CatalogService(session, viewer, cfg).add_magnet_source(movie_id, form)
                text = "Enlace Magnet añadido. Pulsa «Iniciar descarga y reproducir» en la ficha para empezar."
                return RedirectResponse(f"/partner/movie/{movie_id}?msg={quote(text)}", status_code=303)
            except Exception as exc:  # noqa: BLE001
                return RedirectResponse(f"/partner/movie/{movie_id}?err={quote(str(exc))}", status_code=303)

    def _return_to(raw: str, movie_id: int) -> str:
        """Destino tras editar una fuente: sólo rutas internas del panel (evita open redirect)."""
        if raw.startswith("/partner/") and "//" not in raw and "\\" not in raw:
            return raw
        return f"/partner/movie/{movie_id}"

    @router.post("/partner/source/{source_id}/update")
    def partner_update_source(
        request: Request,
        source_id: int,
        movie_id: int = Form(...),
        audio_language: str = Form(""),
        quality: str = Form(""),
        label: str = Form(""),
        enabled: str = Form(""),
        return_to: str = Form(""),
    ):
        back = _return_to(return_to, movie_id)
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            if not viewer or viewer.role not in PARTNER_ROLES:
                return RedirectResponse("/partner", status_code=303)
            try:
                CatalogService(session, viewer, cfg).update_source(
                    source_id, audio_language=audio_language, quality=quality, label=label or None,
                    enabled={"1": True, "0": False}.get(enabled),
                )
                return ctx.back(back, msg="Fuente actualizada")
            except Exception as exc:  # noqa: BLE001
                return ctx.back(back, err=str(exc))

    @router.post("/partner/source/{source_id}/delete")
    def partner_delete_source(request: Request, source_id: int, movie_id: int = Form(...), return_to: str = Form("")):
        back = _return_to(return_to, movie_id)
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            if not viewer or viewer.role not in PARTNER_ROLES:
                return RedirectResponse("/partner", status_code=303)
            try:
                engine = getattr(request.app.state, "engine", None)
                if engine is not None and engine.has(source_id):
                    engine.remove(source_id, delete_files=False)  # para la sesión; los archivos se gestionan con «Eliminar descarga»
                CatalogService(session, viewer, cfg).remove_source(source_id)
                return ctx.back(back, msg="Fuente eliminada")
            except Exception as exc:  # noqa: BLE001
                return ctx.back(back, err=str(exc))

    @router.get("/partner/tmdb-lookup")
    def partner_tmdb_lookup(request: Request, ref: str = "", kind: str = "movie"):
        """Película (themoviedb.org/movie/…) o serie (themoviedb.org/tv/…); un ID suelto usa ``kind``."""
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
        if not viewer or viewer.role not in PARTNER_ROLES:
            return JSONResponse({"ok": False, "error": "Solo partners y administradores pueden importar desde TMDB."}, status_code=403)
        try:
            if kind not in ("movie", "series"):
                kind = "movie"
            result = ctx.providers.tmdb.lookup(kind, ref)
            if result.kind == "person":
                raise ProviderError("Ese enlace es de una persona: úsalo en «Editar bio».")
        except ProviderError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse(
            {
                "ok": True,
                "kind": result.kind,
                "fields": result.fields,
                "filled_from_fallback": [n for n in result.notes if "inglés" in n],
                "notes": result.notes,
                "seasons": result.seasons,
                "language": "es-MX",
            }
        )

    @router.post("/partner/requests/{request_id}/approve")
    def partner_request_approve(request: Request, request_id: int):
        return _review_partner_request(request, request_id, approve=True)

    @router.post("/partner/requests/{request_id}/reject")
    def partner_request_reject(request: Request, request_id: int):
        return _review_partner_request(request, request_id, approve=False)

    @router.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, q: str = "", msg: str | None = None, err: str | None = None):
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            if not viewer:
                return RedirectResponse("/login?err=" + quote("Inicia sesión para ver tus ajustes"), status_code=303)
            queries = CatalogQueries(session, viewer)
            prof = queries.user_profile(viewer.username)
            if prof is None:
                return RedirectResponse("/login", status_code=303)
            q_clean = q.strip()
            hits = [h for h in queries.search(q_clean, kind="movies", limit=12) if h.kind == "movie"] if q_clean else []
            partner_req = PartnerRequestService(session, viewer, _auth_service(session, cfg)).latest_for(viewer.user_id)
            return render(
                "settings.html",
                title="Ajustes",
                viewer=viewer,
                nav="settings",
                prof=prof,
                favorites=prof.favorites,
                hits=hits,
                q_fav=q_clean,
                partner_req=partner_req,
                msg=msg,
                err=err,
            )

    @router.post("/settings/profile")
    async def settings_profile(
        request: Request,
        display_name: str = Form(""),
        bio: str = Form(""),
        avatar_url: str = Form(""),
        remove_avatar: str | None = Form(None),
        avatar_file: UploadFile | None = File(None),
    ):
        raw = await avatar_file.read() if avatar_file and avatar_file.filename else b""
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            if not viewer:
                return RedirectResponse("/login", status_code=303)
            try:
                user = session.get(User, viewer.user_id)
                if user is None:
                    raise ValueError("Usuario no encontrado")
                previous = user.avatar_path
                if raw:
                    new_avatar = save_avatar(cfg.data_dir, viewer.username, raw)
                elif avatar_url.strip():
                    new_avatar = avatar_url.strip()
                elif remove_avatar:
                    new_avatar = None
                else:
                    new_avatar = previous
                if previous and previous.startswith("avatars/") and previous != new_avatar:
                    delete_avatar(cfg.data_dir, previous)  # no acumular fotos antiguas
                favorites = ProfileService(session, viewer).favorites(viewer.user_id)
                SocialService(session, viewer).update_profile(
                    ProfileForm(display_name=display_name, bio=bio, avatar_path=new_avatar, favorite_movie_ids=favorites)
                )
                return RedirectResponse("/settings?msg=" + quote("Perfil actualizado"), status_code=303)
            except (AvatarError, ValidationError, CatalogError, Exception) as exc:  # noqa: BLE001
                text = "La imagen o los datos del perfil no son válidos." if isinstance(exc, ValidationError) else str(exc)
                return RedirectResponse("/settings?err=" + quote(text), status_code=303)

    @router.post("/settings/password")
    def settings_password(request: Request, current_password: str = Form(...), new_password: str = Form(...)):
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            if not viewer:
                return RedirectResponse("/login", status_code=303)
            try:
                PasswordChangeForm(current_password=current_password, new_password=new_password)  # type: ignore[arg-type]
                _auth_service(session, cfg).change_password(viewer, current_password, new_password)
                return RedirectResponse("/settings?msg=" + quote("Contraseña actualizada"), status_code=303)
            except ValidationError:
                text = "La nueva contraseña debe tener 10 o más caracteres, con letras y números o símbolos."
                return RedirectResponse("/settings?err=" + quote(text), status_code=303)
            except (PermissionDenied, Exception) as exc:  # noqa: BLE001
                return RedirectResponse("/settings?err=" + quote(str(exc)), status_code=303)

    @router.post("/settings/favorites/add/{movie_id}")
    def settings_favorite_add(request: Request, movie_id: int):
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            if not viewer:
                return RedirectResponse("/login", status_code=303)
            try:
                ProfileService(session, viewer).add_favorite(movie_id)
                return RedirectResponse("/settings?msg=" + quote("Favorita añadida"), status_code=303)
            except Exception as exc:  # noqa: BLE001
                return RedirectResponse("/settings?err=" + quote(str(exc)), status_code=303)

    @router.post("/settings/favorites/remove/{movie_id}")
    def settings_favorite_remove(request: Request, movie_id: int):
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            if not viewer:
                return RedirectResponse("/login", status_code=303)
            ProfileService(session, viewer).remove_favorite(movie_id)
            return RedirectResponse("/settings?msg=" + quote("Favorita quitada"), status_code=303)

    @router.post("/settings/partner-request")
    def settings_partner_request(
        request: Request,
        display_name: str = Form(""),
        reason: str = Form(""),
        portfolio_url: str = Form(""),
    ):
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            if not viewer:
                return RedirectResponse("/login", status_code=303)
            try:
                form = PartnerRequestForm(display_name=display_name or None, reason=reason, portfolio_url=portfolio_url or None)
                PartnerRequestService(session, viewer, _auth_service(session, cfg)).submit(form)
                return RedirectResponse("/settings?msg=" + quote("Solicitud enviada. Un administrador la revisará."), status_code=303)
            except ValidationError:
                text = "Revisa el motivo (mínimo 30 caracteres) y el enlace del portafolio."
                return RedirectResponse("/settings?err=" + quote(text), status_code=303)
            except Exception as exc:  # noqa: BLE001
                return RedirectResponse("/settings?err=" + quote(str(exc)), status_code=303)

    from rascal.webui import register_extensions  # routers de series, plataformas, personas y fuentes directas

    register_extensions(router, ctx)
    return router


def mount_web(app: FastAPI, settings: Settings | None = None, session_factory: Any = None) -> None:
    cfg = settings or Settings()
    install_security(app, cfg)
    app.include_router(create_web_router(settings=cfg, session_factory=session_factory))