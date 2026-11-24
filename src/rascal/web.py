"""Interfaz web completa (Dashboard + Catálogo + Partner + Subtítulos + Tráiler + Reproductor P2P)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import json
import re
import time
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import DictLoader, Environment, select_autoescape
from pydantic import ValidationError
from sqlalchemy import select

from rascal.auth.rbac import Principal
from rascal.auth.service import (
    AuthService,
    ChallengeRequired,
    LoginFailed,
    LoginSuccess,
    RegistrationError,
)
from rascal.config import Settings
from rascal.data.db import get_sessionmaker, session_scope
from rascal.data.enums import ColorKind, CreditRole, Role
from rascal.data.models import Movie
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
from rascal.domain.services.catalog import CatalogError, CatalogService, NotFound
from rascal.domain.services.queries import CatalogQueries
from rascal.domain.services.reviews import ReviewService
from rascal.domain.services.social import SocialService
from rascal.streaming.client import StreamerClient, StreamerUnavailable

COOKIE_NAME = "rascal_session"
SESSION_TTL_S = 60 * 60 * 24 * 7
SUBTITLES_DIR = Path("var/subtitles")


def _uid(principal: Any) -> int | None:
    if principal is None:
        return None
    return getattr(principal, "user_id", None) or getattr(principal, "id", None)


def _sign_session(secret: str, user_id: int) -> str:
    payload = json.dumps({"uid": user_id, "exp": int(time.time()) + SESSION_TTL_S}, separators=(",", ":")).encode()
    b64 = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    sig = hmac.new(secret.encode(), b64.encode(), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def _verify_session(secret: str, token: str | None) -> int | None:
    if not token or "." not in token:
        return None
    b64, sig = token.rsplit(".", 1)
    expected = hmac.new(secret.encode(), b64.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        pad = "=" * (-len(b64) % 4)
        data = json.loads(base64.urlsafe_b64decode(b64 + pad))
        if int(data.get("exp", 0)) < int(time.time()):
            return None
        return int(data["uid"])
    except Exception:
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
    sig = inspect.signature(AuthService.__init__)
    if "mailer" in sig.parameters:
        return AuthService(session, settings, type('M',(),{'send':lambda self,*a,**k:print('[2FA]',a,k,flush=True)})())
    return AuthService(session, settings)


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


def _save_subtitle(movie_id: int, filename: str, raw: bytes, label: str = "Español") -> str:
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").lstrip("\ufeff")
    if not text.lstrip().startswith("WEBVTT"):
        text = "WEBVTT\n\n" + re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", text)
    safe_label = re.sub(r"[^A-Za-z0-9_-]+", "_", label.strip() or Path(filename).stem).strip("_") or "subtitulo"
    folder = SUBTITLES_DIR / str(movie_id)
    folder.mkdir(parents=True, exist_ok=True)
    out_name = f"{safe_label}.vtt"
    (folder / out_name).write_text(text, encoding="utf-8")
    return out_name


def _list_subtitles(movie_id: int) -> list[dict[str, str]]:
    folder = SUBTITLES_DIR / str(movie_id)
    if not folder.exists():
        return []
    subs: list[dict[str, str]] = []
    for f in sorted(folder.glob("*.vtt")):
        subs.append(
            {
                "filename": f.name,
                "label": f.stem.replace("_", " "),
                "url": f"/subtitles/{movie_id}/{quote(f.name)}",
            }
        )
    return subs


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
# PLANTILLAS JINJA2 (Tema oscuro estilo Letterboxd)
# ==============================================================================

TEMPLATES: dict[str, str] = {
    "base.html": """<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ title }} · Rascal Cinema</title>
  <style>
    :root {
      --bg: #14181c;
      --surface: #1c2228;
      --surface-2: #242c34;
      --border: #2c3440;
      --text: #e1e8ed;
      --muted: #99aabb;
      --green: #00e054;
      --green-dark: #00b844;
      --blue: #40bcf4;
      --orange: #ff8000;
      --red: #ff4d4d;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg); color: var(--text); line-height: 1.5;
    }
    a { color: var(--blue); text-decoration: none; }
    a:hover { text-decoration: underline; }
    header.topbar {
      background: #0f1316; border-bottom: 1px solid var(--border);
      position: sticky; top: 0; z-index: 50;
    }
    .nav-inner {
      max-width: 1220px; margin: 0 auto; padding: 12px 20px;
      display: flex; align-items: center; gap: 18px; flex-wrap: wrap;
    }
    .brand {
      font-weight: 800; font-size: 1.15rem; color: #fff; letter-spacing: 0.4px;
      display: flex; align-items: center; gap: 8px;
    }
    .brand span.dot {
      display: inline-block; width: 10px; height: 10px; border-radius: 50%;
      background: var(--green); box-shadow: 0 0 8px var(--green);
    }
    nav.menu { display: flex; gap: 14px; align-items: center; flex-wrap: wrap; }
    nav.menu a {
      color: var(--muted); font-weight: 600; font-size: 0.92rem; padding: 6px 10px; border-radius: 6px;
    }
    nav.menu a:hover, nav.menu a.active { color: #fff; background: var(--surface-2); text-decoration: none; }
    .search-box { margin-left: auto; display: flex; gap: 6px; }
    .search-box input {
      background: var(--surface-2); border: 1px solid var(--border); color: #fff;
      padding: 6px 12px; border-radius: 20px; width: 210px; font-size: 0.9rem;
    }
    .user-nav { display: flex; gap: 10px; align-items: center; }
    .container { max-width: 1220px; margin: 0 auto; padding: 24px 20px 60px; }
    h1, h2, h3 { margin-top: 0; color: #fff; }
    h2.section-title {
      font-size: 1.05rem; text-transform: uppercase; letter-spacing: 1px;
      color: var(--muted); border-bottom: 1px solid var(--border);
      padding-bottom: 8px; margin: 28px 0 16px;
    }
    .grid {
      display: grid; grid-template-columns: repeat(auto-fill, minmax(175px, 1fr)); gap: 18px;
    }
    .card {
      background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
      overflow: hidden; transition: transform .15s, border-color .15s; display: flex; flex-direction: column;
    }
    .card:hover { transform: translateY(-3px); border-color: var(--green); }
    .poster-wrap {
      aspect-ratio: 2 / 3; background: linear-gradient(145deg, #1f2833, #11161b);
      position: relative; overflow: hidden; display: flex; align-items: center; justify-content: center;
    }
    .poster-wrap img { width: 100%; height: 100%; object-fit: cover; display: block; }
    .poster-fallback {
      padding: 14px; text-align: center; color: var(--muted); font-weight: 700; font-size: 0.95rem;
    }
    .card-body { padding: 10px 12px; flex: 1; display: flex; flex-direction: column; justify-content: space-between; }
    .card-title { font-weight: 700; color: #fff; font-size: 0.95rem; margin-bottom: 4px; }
    .card-meta { font-size: 0.82rem; color: var(--muted); display: flex; justify-content: space-between; }
    .stars { color: var(--green); font-weight: 700; }
    .pill {
      display: inline-block; background: var(--surface-2); border: 1px solid var(--border);
      color: var(--text); padding: 4px 10px; border-radius: 6px; font-size: 0.85rem; margin: 3px 4px 3px 0;
    }
    .pill:hover { border-color: var(--blue); color: #fff; text-decoration: none; }
    .badge {
      display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem;
      font-weight: 700; text-transform: uppercase; background: var(--surface-2); color: var(--green);
      border: 1px solid var(--border);
    }
    .btn {
      display: inline-block; background: var(--green); color: #09110c; font-weight: 700;
      padding: 8px 16px; border-radius: 6px; border: none; cursor: pointer; font-size: 0.92rem;
    }
    .btn:hover { background: var(--green-dark); text-decoration: none; }
    .btn-secondary { background: var(--surface-2); color: #fff; border: 1px solid var(--border); }
    .btn-secondary:hover { border-color: var(--blue); background: #2c3540; }
    .btn-danger { background: #3d1c1c; color: #ff8a8a; border: 1px solid #6b2929; }
    .panel {
      background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
      padding: 20px; margin-bottom: 20px;
    }
    .alert {
      padding: 12px 16px; border-radius: 8px; margin-bottom: 18px;
      background: #1b2836; border: 1px solid var(--blue); color: #d8ecff;
    }
    .alert.error { background: #311919; border-color: var(--red); color: #ffd4d4; }
    .alert.ok { background: #152b1e; border-color: var(--green); color: #d4ffe3; }
    label { display: block; font-size: 0.85rem; color: var(--muted); margin: 10px 0 4px; font-weight: 600; }
    input[type=text], input[type=password], input[type=email], input[type=number], input[type=date], select, textarea {
      width: 100%; background: #12161a; border: 1px solid var(--border); color: #fff;
      padding: 9px 12px; border-radius: 6px; font-size: 0.93rem; font-family: inherit;
    }
    textarea { min-height: 90px; resize: vertical; }
    .form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; }
    .player-frame {
      width: 100%; aspect-ratio: 16 / 9; border: 1px solid var(--border);
      border-radius: 10px; background: #000; display: block;
    }
    .movie-header { display: grid; grid-template-columns: 230px 1fr; gap: 26px; align-items: start; }
    @media (max-width: 760px) { .movie-header { grid-template-columns: 1fr; } }
    .stats-row { display: flex; gap: 16px; flex-wrap: wrap; margin: 12px 0; }
    .stat-box {
      background: var(--surface-2); border: 1px solid var(--border); border-radius: 8px;
      padding: 10px 16px; min-width: 110px;
    }
    .stat-box .num { font-size: 1.35rem; font-weight: 800; color: #fff; }
    .stat-box .lbl { font-size: 0.78rem; color: var(--muted); text-transform: uppercase; }
    table { width: 100%; border-collapse: collapse; }
    th, td { text-align: left; padding: 10px; border-bottom: 1px solid var(--border); font-size: 0.9rem; }
    th { color: var(--muted); font-size: 0.8rem; text-transform: uppercase; }
  </style>
</head>
<body>
<header class="topbar">
  <div class="nav-inner">
    <a class="brand" href="/"><span class="dot"></span> RASCAL CINEMA</a>
    <nav class="menu">
      <a href="/" class="{{ 'active' if nav == 'home' else '' }}">Inicio</a>
      <a href="/browse" class="{{ 'active' if nav == 'browse' else '' }}">Catálogo</a>
      <a href="/partner" class="{{ 'active' if nav == 'partner' else '' }}">Panel Partner</a>
    </nav>
    <form class="search-box" action="/search" method="get">
      <input type="text" name="q" placeholder="Películas, actores, estudios..." value="{{ q or '' }}">
    </form>
    <div class="user-nav">
      {% if viewer %}
        <a class="pill" href="/user/{{ viewer.username }}">@{{ viewer.username }} <span class="badge">{{ viewer.role.value }}</span></a>
        <a class="btn btn-secondary" href="/logout" style="padding:5px 10px;font-size:0.82rem;">Salir</a>
      {% else %}
        <a class="btn" href="/login" style="padding:6px 12px;font-size:0.85rem;">Entrar / Registro</a>
      {% endif %}
    </div>
  </div>
</header>
<main class="container">
  {% if msg %}<div class="alert ok">{{ msg }}</div>{% endif %}
  {% if err %}<div class="alert error">{{ err }}</div>{% endif %}
  {% block content %}{% endblock %}
</main>
</body>
</html>""",

    "macros.html": """{% macro movie_grid(cards) %}
{% if cards %}
<div class="grid">
  {% for m in cards %}
  <a class="card" href="/movie/{{ m.slug }}" style="text-decoration:none;">
    <div class="poster-wrap">
      {% if m.poster_path %}
        <img src="{{ m.poster_path }}" alt="{{ m.title }}" onerror="this.style.display='none';this.nextElementSibling.style.display='block';">
        <div class="poster-fallback" style="display:none;">{{ m.title }}<br><small>{{ m.release_year or '' }}</small></div>
      {% else %}
        <div class="poster-fallback">{{ m.title }}<br><small>{{ m.release_year or '' }}</small></div>
      {% endif %}
    </div>
    <div class="card-body">
      <div class="card-title">{{ m.title }}</div>
      <div class="card-meta">
        <span>{{ m.release_year or '—' }}</span>
        <span class="stars">{% if m.average_stars %}★ {{ '%.1f'|format(m.average_stars) }}{% else %}—{% endif %}</span>
      </div>
      {% if m.characters %}
        <div style="font-size:0.78rem;color:var(--blue);margin-top:4px;">como {{ m.characters|join(', ') }}</div>
      {% elif m.roles %}
        <div style="font-size:0.78rem;color:var(--muted);margin-top:4px;">{{ m.roles|map(attribute='value')|join(', ') }}</div>
      {% endif %}
    </div>
  </a>
  {% endfor %}
</div>
{% else %}
  <p style="color:var(--muted);">No hay películas en esta sección todavía.</p>
{% endif %}
{% endmacro %}""",

    "home.html": """{% extends "base.html" %}
{% from "macros.html" import movie_grid %}
{% block content %}
<div class="panel" style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:14px;">
  <div>
    <h1 style="margin-bottom:4px;">Rascal Does Not Dream of Cinema</h1>
    <p style="margin:0;color:var(--muted);">Reseña, explora filmografías conectadas y reproduce cine vía P2P secuencial.</p>
  </div>
  <div style="display:flex;gap:10px;">
    <a class="btn" href="/browse">Explorar catálogo</a>
    <a class="btn btn-secondary" href="/partner">Subir / Gestionar películas</a>
  </div>
</div>

<h2 class="section-title">Mejor Valoradas</h2>
{{ movie_grid(feed.top_rated) }}

<h2 class="section-title">Populares en la Comunidad</h2>
{{ movie_grid(feed.popular) }}

<h2 class="section-title">Añadidas Recientemente</h2>
{{ movie_grid(feed.recent) }}
{% endblock %}""",

    "browse.html": """{% extends "base.html" %}
{% from "macros.html" import movie_grid %}
{% block content %}
<h1>Catálogo de Películas</h1>
<form class="panel form-grid" method="get" action="/browse">
  <div>
    <label>Género</label>
    <select name="genre">
      <option value="">Todos los géneros</option>
      {% for g in genres %}
        <option value="{{ g.slug }}" {{ 'selected' if genre == g.slug else '' }}>{{ g.name }}</option>
      {% endfor %}
    </select>
  </div>
  <div>
    <label>Ordenar por</label>
    <select name="sort">
      <option value="popularity" {{ 'selected' if sort == 'popularity' else '' }}>Popularidad</option>
      <option value="rating" {{ 'selected' if sort == 'rating' else '' }}>Mejor calificación</option>
      <option value="recent" {{ 'selected' if sort == 'recent' else '' }}>Más recientes</option>
      <option value="title" {{ 'selected' if sort == 'title' else '' }}>Título (A-Z)</option>
    </select>
  </div>
  <div>
    <label>Año desde</label>
    <input type="number" name="year_from" value="{{ year_from }}" placeholder="1980">
  </div>
  <div>
    <label>Año hasta</label>
    <input type="number" name="year_to" value="{{ year_to }}" placeholder="2026">
  </div>
  <div>
    <label>Estrellas mínimas (0-5)</label>
    <input type="number" step="0.5" min="0" max="5" name="min_stars" value="{{ min_stars }}" placeholder="3.5">
  </div>
  <div style="display:flex;align-items:flex-end;">
    <button class="btn" type="submit" style="width:100%;">Filtrar</button>
  </div>
</form>
{{ movie_grid(cards) }}
{% endblock %}""",

    "search.html": """{% extends "base.html" %}
{% block content %}
<h1>Resultados para "{{ q }}"</h1>
{% if not q %}
  <p style="color:var(--muted);">Escribe un título, actor, director, productora o usuario en la barra superior.</p>
{% elif not hits %}
  <p style="color:var(--muted);">No se encontraron coincidencias.</p>
{% else %}
  <div class="panel">
    {% for h in hits %}
      {% set href = '/movie/' + h.slug if h.kind == 'movie' else ('/person/' + h.slug if h.kind == 'person' else ('/company/' + h.slug if h.kind == 'company' else ('/genre/' + h.slug if h.kind == 'genre' else '/user/' + h.slug))) %}
      <div style="padding:10px 0;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;">
        <div>
          <span class="badge" style="margin-right:8px;">{{ h.kind }}</span>
          <a href="{{ href }}" style="font-weight:700;font-size:1rem;">{{ h.title }}</a>
          {% if h.subtitle %}<span style="color:var(--muted);font-size:0.85rem;margin-left:8px;">{{ h.subtitle }}</span>{% endif %}
        </div>
        <a class="btn btn-secondary" href="{{ href }}" style="padding:4px 10px;font-size:0.8rem;">Abrir</a>
      </div>
    {% endfor %}
  </div>
{% endif %}
{% endblock %}""",

    "movie.html": """{% extends "base.html" %}
{% block content %}
{% if selected_source %}
  <div class="panel" style="padding:14px;">
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:10px;">
      <div>
        <span class="badge">STREAMING P2P SECUENCIAL</span>
        <strong style="margin-left:8px;">{{ selected_source.label }}</strong>
        <span style="color:var(--muted);font-size:0.85rem;margin-left:8px;">
          {{ selected_source.container or 'video' }} · {{ selected_source.playback_mode.value }} · {{ fmt_bytes(selected_source.file_size) }}
        </span>
      </div>
      {% if movie.sources|length > 1 %}
        <div>
          {% for s in movie.sources %}
            <a class="pill" href="/movie/{{ movie.slug }}?source={{ s.id }}" style="{{ 'border-color:var(--green);color:#fff;' if s.id == selected_source.id else '' }}">{{ s.label }}</a>
          {% endfor %}
        </div>
      {% endif %}
    </div>
    {% if iframe_src %}
      <iframe id="rascal-player" class="player-frame" src="{{ iframe_src }}" allowfullscreen allow="autoplay; fullscreen"></iframe>
      {% if subtitles %}
        <div style="margin-top:10px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;font-size:0.85rem;">
          <span style="color:var(--muted);">Subtítulos cargados (actívalos en el icono CC del reproductor):</span>
          {% for sub in subtitles %}
            <a class="pill" href="{{ sub.url }}" download="{{ sub.filename }}">💬 {{ sub.label }} (.vtt)</a>
          {% endfor %}
        </div>
        <script>
          (function() {
            const subs = {{ subtitles | tojson }};
            const frame = document.getElementById('rascal-player');
            function injectTracks() {
              try {
                const doc = frame.contentDocument || frame.contentWindow.document;
                const video = doc && doc.querySelector('video');
                if (!video) return false;
                if (video.dataset.subsInjected) return true;
                video.dataset.subsInjected = '1';
                subs.forEach((s, idx) => {
                  const track = doc.createElement('track');
                  track.kind = 'subtitles';
                  track.label = s.label;
                  track.srclang = 'es';
                  track.src = s.url;
                  if (idx === 0) track.default = true;
                  video.appendChild(track);
                });
                if (video.textTracks && video.textTracks.length > 0) {
                  video.textTracks[0].mode = 'showing';
                }
                return true;
              } catch (e) { return false; }
            }
            frame.addEventListener('load', () => {
              let tries = 0;
              const iv = setInterval(() => {
                if (injectTracks() || ++tries > 20) clearInterval(iv);
              }, 500);
            });
          })();
        </script>
      {% endif %}
      {% if stream_info %}
        <div style="font-size:0.82rem;color:var(--muted);margin-top:8px;display:flex;gap:16px;flex-wrap:wrap;">
          <span>Estado P2P: <strong style="color:#fff;">{{ stream_info.get('state') }}</strong></span>
          <span>Progreso: <strong style="color:var(--green);">{{ '%.1f'|format((stream_info.get('progress') or 0) * 100) }}%</strong></span>
          <span>Seeds/Peers: <strong style="color:#fff;">{{ stream_info.get('num_seeds', 0) }} / {{ stream_info.get('num_peers', 0) }}</strong></span>
        </div>
      {% endif %}
    {% elif stream_err %}
      <div class="alert error" style="margin:0;">No se pudo iniciar el stream P2P: {{ stream_err }}</div>
    {% endif %}
  </div>
{% endif %}

{% if youtube_embed %}
  <details class="panel">
    <summary style="cursor:pointer;font-weight:700;color:#fff;font-size:1rem;">🎬 Ver Tráiler Oficial en YouTube (clic para desplegar)</summary>
    <div style="margin-top:14px;">
      <iframe class="player-frame" src="{{ youtube_embed }}" allowfullscreen allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture"></iframe>
    </div>
  </details>
{% elif movie.trailer_url %}
  <div class="panel">
    <a class="btn btn-secondary" href="{{ movie.trailer_url }}" target="_blank">🎬 Abrir Tráiler externo</a>
  </div>
{% endif %}

<div class="panel movie-header">
  <div>
    <div class="poster-wrap" style="border-radius:8px;">
      {% if movie.poster_path %}
        <img src="{{ movie.poster_path }}" alt="{{ movie.title }}">
      {% else %}
        <div class="poster-fallback">{{ movie.title }}</div>
      {% endif %}
    </div>
    <div style="margin-top:12px;display:flex;flex-direction:column;gap:8px;">
      {% if viewer %}
        <form method="post" action="/movie/{{ movie.slug }}/watchlist">
          <button class="btn btn-secondary" type="submit" style="width:100%;">
            {{ '✓ En tu Watchlist' if in_watchlist else '+ Añadir a Watchlist' }}
          </button>
        </form>
      {% endif %}
      {% if can_edit %}
        <a class="btn" href="/partner/movie/{{ movie.id }}" style="text-align:center;">Editar ficha / Torrents / Subs</a>
      {% endif %}
    </div>
  </div>

  <div>
    <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap;">
      <div>
        <h1 style="margin-bottom:4px;">{{ movie.title }} <span style="color:var(--muted);font-weight:400;">({{ movie.release_year or 's/f' }})</span></h1>
        {% if movie.original_title and movie.original_title != movie.title %}
          <div style="color:var(--muted);font-style:italic;margin-bottom:6px;">Título original: {{ movie.original_title }}</div>
        {% endif %}
      </div>
      <div class="stat-box">
        <div class="num" style="color:var(--green);">{{ '%.2f'|format(movie.rating.average_stars) if movie.rating.average_stars else '—' }} ★</div>
        <div class="lbl">{{ movie.rating.count }} reseñas · {{ movie.watchlist_count }} watchlist</div>
      </div>
    </div>

    {% if movie.tagline %}<p style="color:var(--blue);font-weight:600;">"{{ movie.tagline }}"</p>{% endif %}
    {% if movie.synopsis %}<p>{{ movie.synopsis }}</p>{% endif %}

    <div style="margin:12px 0;">
      {% for g in movie.genres %}
        <a class="pill" href="/genre/{{ g.slug }}">{{ g.name }}</a>
      {% endfor %}
      {% if movie.runtime_min %}<span class="pill">{{ movie.runtime_min }} min</span>{% endif %}
      {% if movie.imdb_id %}<a class="pill" href="https://www.imdb.com/title/{{ movie.imdb_id }}/" target="_blank">IMDb: {{ movie.imdb_id }}</a>{% endif %}
    </div>

    {% if movie.directors %}
      <div style="margin-top:14px;">
        <strong style="color:var(--muted);font-size:0.85rem;">DIRECCIÓN:</strong><br>
        {% for d in movie.directors %}
          <a class="pill" href="/person/{{ d.slug }}">{{ d.name }}</a>
        {% endfor %}
      </div>
    {% endif %}

    {% if movie.cast %}
      <div style="margin-top:14px;">
        <strong style="color:var(--muted);font-size:0.85rem;">REPARTO (clic para ver filmografía):</strong><br>
        {% for c in movie.cast %}
          <a class="pill" href="/person/{{ c.person.slug }}">
            <strong>{{ c.person.name }}</strong>{% if c.character_name %} <span style="color:var(--muted);">({{ c.character_name }})</span>{% endif %}
          </a>
        {% endfor %}
      </div>
    {% endif %}

    {% if movie.companies %}
      <div style="margin-top:14px;">
        <strong style="color:var(--muted);font-size:0.85rem;">PRODUCTORAS:</strong><br>
        {% for comp in movie.companies %}
          <a class="pill" href="/company/{{ comp.slug }}">{{ comp.name }}</a>
        {% endfor %}
      </div>
    {% endif %}

    {% if movie.crew %}
      <div style="margin-top:14px;">
        <strong style="color:var(--muted);font-size:0.85rem;">EQUIPO TÉCNICO:</strong><br>
        {% for c in movie.crew %}
          <a class="pill" href="/person/{{ c.person.slug }}">{{ c.person.name }} <span style="color:var(--muted);">· {{ c.role.value }}</span></a>
        {% endfor %}
      </div>
    {% endif %}

    {% if movie.tech %}
      <div style="margin-top:16px;padding-top:12px;border-top:1px solid var(--border);font-size:0.85rem;color:var(--muted);">
        <strong>Ficha técnica:</strong>
        {% if movie.tech.aspect_ratio %}Aspecto {{ movie.tech.aspect_ratio }} · {% endif %}
        {% if movie.tech.color %}{{ movie.tech.color.value }} · {% endif %}
        {% if movie.tech.sound_mix %}{{ movie.tech.sound_mix }} · {% endif %}
        {% if movie.tech.original_language %}Idioma: {{ movie.tech.original_language }} · {% endif %}
        {% if movie.tech.country %}País: {{ movie.tech.country }}{% endif %}
      </div>
    {% endif %}
  </div>
</div>

<div class="panel">
  <h3>Distribución de Calificaciones</h3>
  <div style="display:flex;align-items:flex-end;gap:8px;height:90px;padding-top:10px;">
    {% set max_c = (movie.rating.distribution | map(attribute='count') | max) or 1 %}
    {% for b in movie.rating.distribution %}
      <div style="flex:1;text-align:center;">
        <div style="background:var(--green);height:{{ (b.count / max_c * 65)|int + 3 }}px;border-radius:3px 3px 0 0;" title="{{ b.stars }}★: {{ b.count }}"></div>
        <div style="font-size:0.72rem;color:var(--muted);margin-top:4px;">{{ b.stars }}★</div>
      </div>
    {% endfor %}
  </div>
</div>

<div class="panel">
  <h3>Escribir Reseña</h3>
  {% if viewer %}
    <form method="post" action="/movie/{{ movie.slug }}/review">
      <div class="form-grid">
        <div>
          <label>Calificación (estrellas)</label>
          <select name="rating">
            <option value="0">Sin estrellas (solo comentario)</option>
            <option value="10">★★★★★ (5.0)</option>
            <option value="9">★★★★½ (4.5)</option>
            <option value="8">★★★★ (4.0)</option>
            <option value="7">★★★½ (3.5)</option>
            <option value="6">★★★ (3.0)</option>
            <option value="5">★★½ (2.5)</option>
            <option value="4">★★ (2.0)</option>
            <option value="3">★½ (1.5)</option>
            <option value="2">★ (1.0)</option>
            <option value="1">½ (0.5)</option>
          </select>
        </div>
        <div>
          <label>Fecha vista (opcional)</label>
          <input type="date" name="watched_on">
        </div>
        <div style="display:flex;gap:16px;align-items:center;padding-top:22px;">
          <label style="margin:0;cursor:pointer;"><input type="checkbox" name="is_rewatch" value="1"> Rewatch</label>
          <label style="margin:0;cursor:pointer;"><input type="checkbox" name="contains_spoilers" value="1"> Contiene spoilers</label>
        </div>
      </div>
      <label>Tu opinión</label>
      <textarea name="body" placeholder="¿Qué te pareció la película?"></textarea>
      <button class="btn" type="submit" style="margin-top:10px;">Publicar reseña</button>
    </form>
  {% else %}
    <p style="color:var(--muted);"><a href="/login">Inicia sesión</a> para calificar y publicar reseñas.</p>
  {% endif %}
</div>

<h2 class="section-title">Reseñas de la Comunidad ({{ reviews|length }})</h2>
{% for r in reviews %}
  <div class="panel">
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
      <div>
        <a href="/user/{{ r.user.username }}" style="font-weight:700;">@{{ r.user.username }}</a>
        <span class="stars" style="margin-left:8px;">{{ r.star_label }}</span>
        {% if r.is_rewatch %}<span class="badge" style="margin-left:6px;">Rewatch</span>{% endif %}
      </div>
      <div style="font-size:0.82rem;color:var(--muted);">{{ r.created_at.strftime('%Y-%m-%d') }}</div>
    </div>
    {% if r.body %}
      {% if r.contains_spoilers %}
        <details style="margin-top:8px;"><summary style="color:var(--orange);cursor:pointer;">Contiene spoilers (clic para leer)</summary><p>{{ r.body }}</p></details>
      {% else %}
        <p style="margin:8px 0;">{{ r.body }}</p>
      {% endif %}
    {% endif %}
    {% if viewer and viewer_uid != r.user.id %}
      <form method="post" action="/review/{{ r.id }}/vote" style="display:inline-flex;gap:6px;margin-top:4px;">
        <input type="hidden" name="slug" value="{{ movie.slug }}">
        <button class="btn btn-secondary" name="value" value="1" type="submit" style="padding:3px 9px;font-size:0.8rem;">▲ Útil ({{ r.votes_up }})</button>
        <button class="btn btn-secondary" name="value" value="-1" type="submit" style="padding:3px 9px;font-size:0.8rem;">▼ ({{ r.votes_down }})</button>
      </form>
    {% else %}
      <div style="font-size:0.8rem;color:var(--muted);">▲ {{ r.votes_up }} · ▼ {{ r.votes_down }}</div>
    {% endif %}
  </div>
{% else %}
  <p style="color:var(--muted);">Aún no hay reseñas. ¡Sé el primero en opinar!</p>
{% endfor %}
{% endblock %}""",

    "entity.html": """{% extends "base.html" %}
{% from "macros.html" import movie_grid %}
{% block content %}
<div class="panel">
  <span class="badge">{{ prof.ref.kind.value if prof.ref.kind.value is defined else prof.ref.kind }}</span>
  <h1 style="margin:6px 0;">{{ prof.ref.name }}</h1>
  {% if prof.country %}<div style="color:var(--muted);">País: {{ prof.country }}</div>{% endif %}
  {% if prof.description %}<p>{{ prof.description }}</p>{% endif %}
  <div class="stats-row">
    <div class="stat-box"><div class="num">{{ prof.metrics.films }}</div><div class="lbl">Películas</div></div>
    <div class="stat-box"><div class="num" style="color:var(--green);">{{ '%.2f'|format(prof.metrics.average_stars) if prof.metrics.average_stars else '—' }} ★</div><div class="lbl">Promedio</div></div>
    <div class="stat-box"><div class="num">{{ prof.metrics.total_reviews }}</div><div class="lbl">Reseñas</div></div>
    <div class="stat-box"><div class="num">{{ '%.1f'|format(prof.metrics.popularity_score) }}</div><div class="lbl">Popularidad</div></div>
  </div>
  {% set k_val = prof.ref.kind.value if prof.ref.kind.value is defined else prof.ref.kind %}
  {% if prof.roles %}
    <div style="margin-top:8px;">
      <a class="pill" href="/{{ k_val }}/{{ prof.ref.slug }}">Todos ({{ prof.filmography_total }})</a>
      {% for rc in prof.roles %}
        <a class="pill" href="/{{ k_val }}/{{ prof.ref.slug }}?role={{ rc.role.value }}">{{ rc.role.value }} ({{ rc.films }})</a>
      {% endfor %}
    </div>
  {% endif %}
</div>
<h2 class="section-title">Filmografía ({{ prof.filmography|length }})</h2>
{{ movie_grid(prof.filmography) }}
{% endblock %}""",

    "user.html": """{% extends "base.html" %}
{% from "macros.html" import movie_grid %}
{% block content %}
<div class="panel" style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:14px;">
  <div>
    <h1 style="margin-bottom:4px;">@{{ prof.user.username }} <span class="badge">{{ prof.role.value }}</span></h1>
    {% if prof.bio %}<p style="margin:6px 0;">{{ prof.bio }}</p>{% endif %}
    <div style="color:var(--muted);font-size:0.88rem;">
      {{ prof.films_logged }} películas · {{ prof.followers }} seguidores · {{ prof.following }} siguiendo
    </div>
  </div>
  {% if viewer and not prof.is_self %}
    <form method="post" action="/user/{{ prof.user.username }}/follow">
      <button class="btn btn-secondary" type="submit">{{ 'Dejar de seguir' if prof.is_following else 'Seguir' }}</button>
    </form>
  {% endif %}
</div>

<h2 class="section-title">Watchlist ({{ prof.watchlist|length }})</h2>
{{ movie_grid(prof.watchlist) }}

<h2 class="section-title">Reseñas Recientes</h2>
{% for r in prof.recent_reviews %}
  <div class="panel">
    <a href="/movie/{{ r.movie_slug }}" style="font-weight:700;">{{ r.movie_title }}</a>
    <span class="stars" style="margin-left:8px;">{{ r.star_label }}</span>
    {% if r.body %}<p style="margin:6px 0 0;">{{ r.body }}</p>{% endif %}
  </div>
{% else %}
  <p style="color:var(--muted);">Sin reseñas recientes.</p>
{% endfor %}
{% endblock %}""",

    "login.html": """{% extends "base.html" %}
{% block content %}
<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:24px;">
  <form class="panel" method="post" action="/login">
    <h2>Iniciar Sesión</h2>
    <label>Usuario o correo</label>
    <input type="text" name="username" required>
    <label>Contraseña</label>
    <input type="password" name="password" required>
    <button class="btn" type="submit" style="margin-top:14px;width:100%;">Entrar</button>
  </form>

  <form class="panel" method="post" action="/register">
    <h2>Crear Cuenta</h2>
    <label>Usuario (3-30 caracteres, letras/números/_)</label>
    <input type="text" name="username" required>
    <label>Correo (opcional)</label>
    <input type="email" name="email">
    <label>Contraseña (mínimo 10 caracteres, letras + números)</label>
    <input type="password" name="password" required>
    <button class="btn btn-secondary" type="submit" style="margin-top:14px;width:100%;">Registrarme</button>
  </form>
</div>
{% endblock %}""",

    "partner_denied.html": """{% extends "base.html" %}
{% block content %}
<div class="panel">
  <h2>Acceso restringido a Partner / Admin</h2>
  <p>Tu cuenta actual (<strong>@{{ viewer.username }}</strong>) tiene el rol <code>user</code>.</p>
  <p style="color:var(--muted);">Para promover tu usuario a <strong>admin</strong> en desarrollo local, ejecuta en Anaconda Prompt:</p>
  <pre style="background:#0f1316;padding:12px;border-radius:6px;overflow-x:auto;color:var(--green);">python -c "import sys; sys.path.insert(0,'src'); from rascal.data.db import get_sessionmaker; from rascal.data.models import User; from rascal.data.enums import Role; from sqlalchemy import update; s=get_sessionmaker()(); s.execute(update(User).where(User.username=='{{ viewer.username }}').values(role=Role.ADMIN)); s.commit(); print('Rol actualizado a ADMIN')"</pre>
</div>
{% endblock %}""",

    "partner_list.html": """{% extends "base.html" %}
{% block content %}
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:18px;flex-wrap:wrap;gap:10px;">
  <div>
    <h1 style="margin-bottom:2px;">Panel Partner / Administración</h1>
    <p style="margin:0;color:var(--muted);">Crea películas, edita metadatos, enlaza tráilers de YouTube y sube archivos .torrent o subtítulos.</p>
  </div>
  <a class="btn" href="/partner/movie/new">+ Nueva Película</a>
</div>
<div class="panel">
  {% if movies %}
    <table>
      <thead><tr><th>ID</th><th>Título</th><th>Año</th><th>Estado</th><th>Acciones</th></tr></thead>
      <tbody>
        {% for m in movies %}
          <tr>
            <td>#{{ m.id }}</td>
            <td><a href="/movie/{{ m.slug }}" style="font-weight:700;">{{ m.title }}</a></td>
            <td>{{ m.release_year or '—' }}</td>
            <td><span class="badge">{{ 'Publicada' if getattr(m, 'is_published', True) else 'Borrador' }}</span></td>
            <td style="display:flex;gap:8px;">
              <a class="btn btn-secondary" href="/partner/movie/{{ m.id }}" style="padding:4px 10px;font-size:0.82rem;">Editar / Torrents / Subs</a>
              <a class="btn btn-secondary" href="/movie/{{ m.slug }}" style="padding:4px 10px;font-size:0.82rem;">Ver ficha</a>
            </td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
  {% else %}
    <p style="color:var(--muted);">Aún no hay películas registradas. Haz clic en <strong>+ Nueva Película</strong> para crear la primera.</p>
  {% endif %}
</div>
{% endblock %}""",

    "partner_edit.html": """{% extends "base.html" %}
{% block content %}
<div style="margin-bottom:14px;"><a href="/partner">← Volver al Panel Partner</a></div>
<form class="panel" method="post" action="{{ action_url }}" enctype="multipart/form-data">
  <h2>{{ 'Editar Película #' ~ movie_id if movie_id else 'Nueva Película' }}</h2>
  <div class="form-grid">
    <div><label>Título *</label><input type="text" name="title" value="{{ f.title }}" required></div>
    <div><label>Título original</label><input type="text" name="original_title" value="{{ f.original_title or '' }}"></div>
    <div><label>Año estreno</label><input type="number" name="release_year" value="{{ f.release_year or '' }}"></div>
    <div><label>Duración (min)</label><input type="number" name="runtime_min" value="{{ f.runtime_min or '' }}"></div>
    <div><label>URL Póster</label><input type="text" name="poster_path" value="{{ f.poster_path or '' }}" placeholder="https://..."></div>
    <div><label>URL Tráiler (YouTube)</label><input type="text" name="trailer_url" value="{{ f.trailer_url or '' }}" placeholder="https://www.youtube.com/watch?v=..."></div>
    <div><label>IMDb ID</label><input type="text" name="imdb_id" value="{{ f.imdb_id or '' }}" placeholder="tt0114709"></div>
  </div>
  <label>Tagline / Frase promocional</label>
  <input type="text" name="tagline" value="{{ f.tagline or '' }}">
  <label>Sinopsis</label>
  <textarea name="synopsis">{{ f.synopsis or '' }}</textarea>

  <div class="form-grid">
    <div><label>Géneros (separados por coma)</label><input type="text" name="genres" value="{{ genres_str }}" placeholder="Drama, Ciencia ficción, Animación"></div>
    <div><label>Productoras (separadas por coma)</label><input type="text" name="companies" value="{{ companies_str }}" placeholder="Blender Foundation, Toho"></div>
    <div><label>Directores (separados por coma)</label><input type="text" name="directors" value="{{ directors_str }}" placeholder="Makoto Shinkai"></div>
  </div>

  <div class="form-grid">
    <div>
      <label>Reparto (uno por línea: Actor : Personaje)</label>
      <textarea name="cast" placeholder="Kaito Ishikawa : Sakuta Azusagawa&#10;Asami Seto : Mai Sakurajima">{{ cast_str }}</textarea>
    </div>
    <div>
      <label>Equipo técnico (uno por línea: Nombre : writer/producer/cinematographer/composer/editor)</label>
      <textarea name="crew" placeholder="Kevin Penkin : composer">{{ crew_str }}</textarea>
    </div>
  </div>

  <h3 style="margin-top:16px;font-size:0.95rem;color:var(--muted);">Especificaciones Técnicas</h3>
  <div class="form-grid">
    <div><label>Relación de aspecto</label><input type="text" name="aspect_ratio" value="{{ f.tech.aspect_ratio or '' }}" placeholder="16:9 o 2.39:1"></div>
    <div>
      <label>Color</label>
      <select name="color">
        <option value="">—</option>
        {% for ck in ColorKind %}
          <option value="{{ ck.value }}" {{ 'selected' if f.tech.color == ck else '' }}>{{ ck.value }}</option>
        {% endfor %}
      </select>
    </div>
    <div><label>Idioma original (ISO)</label><input type="text" name="original_language" value="{{ f.tech.original_language or '' }}" placeholder="ja, en, es"></div>
    <div><label>País</label><input type="text" name="country" value="{{ f.tech.country or '' }}" placeholder="Japón"></div>
  </div>

  {% if not movie_id %}
  <h3 style="margin-top:20px;font-size:0.95rem;color:var(--green);">Archivos Multimedia Iniciales (Opcional al crear)</h3>
  <div class="form-grid" style="background:var(--surface-2);padding:14px;border-radius:8px;margin-top:8px;">
    <div>
      <label>Archivo .torrent</label>
      <input type="file" name="torrent_file" accept=".torrent,application/x-bittorrent">
    </div>
    <div>
      <label>O enlace Magnet URI</label>
      <input type="text" name="magnet_uri" placeholder="magnet:?xt=urn:btih:...">
    </div>
    <div>
      <label>Archivo de Subtítulos (.srt o .vtt)</label>
      <input type="file" name="subtitle_file" accept=".srt,.vtt,text/vtt">
    </div>
    <div>
      <label>Idioma del subtítulo</label>
      <input type="text" name="subtitle_label" value="Español" placeholder="Español">
    </div>
    <div>
      <label>Etiqueta de fuente P2P</label>
      <input type="text" name="source_label" value="1080p Principal">
    </div>
    <div>
      <label>Declaración de derechos</label>
      <input type="text" name="rights_statement" value="Distribución autorizada / Dominio público">
    </div>
  </div>
  {% endif %}

  <div style="margin-top:12px;">
    <label style="display:inline-flex;align-items:center;gap:8px;cursor:pointer;">
      <input type="checkbox" name="is_published" value="1" {{ 'checked' if f.is_published else '' }}> Publicada en el catálogo público
    </label>
  </div>
  <button class="btn" type="submit" style="margin-top:14px;">{{ 'Guardar cambios' if movie_id else 'Crear película' }}</button>
</form>

{% if movie_id %}
<div class="panel">
  <h2>Subtítulos (.srt / .vtt)</h2>
  {% if subtitles %}
    <div style="margin-bottom:12px;">
      {% for sub in subtitles %}
        <span class="pill">💬 {{ sub.label }} ({{ sub.filename }})</span>
      {% endfor %}
    </div>
  {% else %}
    <p style="color:var(--muted);">No hay subtítulos cargados para esta película.</p>
  {% endif %}
  <form method="post" action="/partner/movie/{{ movie_id }}/upload-subtitle" enctype="multipart/form-data" class="form-grid" style="background:var(--surface-2);padding:14px;border-radius:8px;">
    <div>
      <label>Archivo de subtítulos (.srt o .vtt) *</label>
      <input type="file" name="subtitle_file" accept=".srt,.vtt,text/vtt" required>
    </div>
    <div>
      <label>Idioma / Etiqueta</label>
      <input type="text" name="subtitle_label" value="Español" required>
    </div>
    <div style="display:flex;align-items:flex-end;">
      <button class="btn btn-secondary" type="submit" style="width:100%;">Subir subtítulo</button>
    </div>
  </form>
</div>

<div class="panel">
  <h2>Fuentes de Streaming P2P (.torrent / Magnet)</h2>
  {% if sources %}
    <table>
      <thead><tr><th>Etiqueta</th><th>Archivo / Hash</th><th>Códecs / Modo</th><th>Estado</th><th>Acción</th></tr></thead>
      <tbody>
        {% for s in sources %}
          <tr>
            <td><strong>{{ s.label }}</strong></td>
            <td>{{ s.file_path or s.display_name or s.info_hash }}<br><small style="color:var(--muted);">{{ s.info_hash }}</small></td>
            <td>{{ s.container or '?' }} · {{ s.video_codec or '?' }}/{{ s.audio_codec or '?' }} · <span class="badge">{{ s.playback_mode.value }}</span></td>
            <td>{{ s.status.value }}</td>
            <td>
              <form method="post" action="/partner/source/{{ s.id }}/delete">
                <input type="hidden" name="movie_id" value="{{ movie_id }}">
                <button class="btn btn-danger" type="submit" style="padding:4px 10px;font-size:0.8rem;">Eliminar</button>
              </form>
            </td>
          </tr>
        {% endfor %}
      </tbody>
    </table>
  {% else %}
    <p style="color:var(--muted);">Esta película aún no tiene fuentes torrent asociadas.</p>
  {% endif %}

  <div class="form-grid" style="margin-top:18px;">
    <form method="post" action="/partner/movie/{{ movie_id }}/upload-torrent" enctype="multipart/form-data" style="background:var(--surface-2);padding:14px;border-radius:8px;">
      <h3 style="font-size:1rem;">Subir archivo .torrent</h3>
      <label>Archivo .torrent *</label>
      <input type="file" name="torrent_file" accept=".torrent,application/x-bittorrent" required>
      <label>Etiqueta de calidad</label>
      <input type="text" name="label" value="1080p Principal" required>
      <label>Declaración de derechos / licencia *</label>
      <input type="text" name="rights_statement" value="Distribución autorizada / Dominio público" required>
      <button class="btn" type="submit" style="margin-top:10px;">Subir .torrent e iniciar P2P</button>
    </form>

    <form method="post" action="/partner/movie/{{ movie_id }}/add-magnet" style="background:var(--surface-2);padding:14px;border-radius:8px;">
      <h3 style="font-size:1rem;">Añadir enlace Magnet</h3>
      <label>Magnet URI *</label>
      <input type="text" name="magnet_uri" placeholder="magnet:?xt=urn:btih:..." required>
      <label>Etiqueta de calidad</label>
      <input type="text" name="label" value="1080p Magnet" required>
      <label>Declaración de derechos / licencia *</label>
      <input type="text" name="rights_statement" value="Distribución autorizada / Dominio público" required>
      <button class="btn btn-secondary" type="submit" style="margin-top:10px;">Añadir Magnet e iniciar P2P</button>
    </form>
  </div>
</div>
{% endif %}
{% endblock %}""",
}


def create_web_router(settings: Settings | None = None, session_factory: Any = None) -> APIRouter:
    cfg = settings or Settings()
    factory = session_factory or get_sessionmaker()
    env = Environment(loader=DictLoader(TEMPLATES), autoescape=select_autoescape(["html", "xml"]))
    env.globals["fmt_bytes"] = _fmt_bytes
    env.globals["getattr"] = getattr
    env.globals["ColorKind"] = ColorKind
    router = APIRouter(include_in_schema=False)

    def render(template_name: str, *, title: str, viewer: Principal | None = None, nav: str = "", msg: str | None = None, err: str | None = None, q: str = "", **ctx: Any) -> HTMLResponse:
        html = env.get_template(template_name).render(
            title=title, viewer=viewer, viewer_uid=_uid(viewer), nav=nav, msg=msg, err=err, q=q, **ctx
        )
        return HTMLResponse(html)

    def current_viewer(request: Request, session: Any) -> Principal | None:
        uid = _verify_session(cfg.secret_key.get_secret_value(), request.cookies.get(COOKIE_NAME))
        if not uid:
            return None
        return _auth_service(session, cfg).refresh_principal(uid)

    @router.get("/subtitles/{movie_id}/{filename}")
    def serve_subtitle(movie_id: int, filename: str):
        safe_name = Path(filename).name
        path = SUBTITLES_DIR / str(movie_id) / safe_name
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Subtítulo no encontrado")
        return Response(content=path.read_bytes(), media_type="text/vtt; charset=utf-8")

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
    ):
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
                    limit=120,
                )
            except Exception:
                filters = FilmographyFilters(limit=120)
            movies = _unwrap_list(queries.browse(filters))
            return render(
                "browse.html",
                title="Catálogo",
                viewer=viewer,
                nav="browse",
                genres=genres,
                cards=movies,
                genre=genre,
                sort=sort,
                year_from=year_from,
                year_to=year_to,
                min_stars=min_stars,
            )

    @router.get("/search", response_class=HTMLResponse)
    def search_page(request: Request, q: str = ""):
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            hits = _unwrap_list(CatalogQueries(session, viewer).search(q)) if q.strip() else []
            return render("search.html", title=f"Buscar: {q}", viewer=viewer, q=q, hits=hits)

    @router.get("/movie/{slug}", response_class=HTMLResponse)
    def movie_page(request: Request, slug: str, source: int | None = None, msg: str | None = None, err: str | None = None):
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            queries = CatalogQueries(session, viewer)
            try:
                movie = _call_compat(queries.movie_detail, slug, slug=slug)
            except Exception as exc:
                raise HTTPException(status_code=404, detail=f"Película no encontrada: {exc}") from exc

            reviews = _call_compat(queries.movie_reviews, movie.id, movie_id=movie.id)
            vstate = _call_compat(queries.viewer_state, movie.id, movie_id=movie.id) if viewer else None
            in_watchlist = bool(getattr(vstate, "in_watchlist", False))

            selected_source = None
            iframe_src = None
            stream_info = None
            stream_err = None

            if movie.sources:
                selected_source = next((s for s in movie.sources if s.id == source), movie.sources[0])
                client = StreamerClient(cfg)
                try:
                    stream_info = client.start(selected_source.id)
                    raw_url = client.player_url(user_id=_uid(viewer) or 0, source_id=selected_source.id)
                    parts = urlsplit(raw_url)
                    iframe_src = f"{parts.path}?{parts.query}" if parts.query else parts.path
                except StreamerUnavailable as exc:
                    stream_err = str(exc)
                finally:
                    client.close()

            v_id = _uid(viewer)
            can_edit = bool(viewer and (viewer.role == Role.ADMIN or (viewer.role == Role.PARTNER and movie.owner_id == v_id)))
            subtitles = _list_subtitles(movie.id)
            youtube_embed = _youtube_embed_url(movie.trailer_url)

            return render(
                "movie.html",
                title=movie.title,
                viewer=viewer,
                msg=msg,
                err=err,
                movie=movie,
                reviews=reviews,
                in_watchlist=in_watchlist,
                selected_source=selected_source,
                iframe_src=iframe_src,
                stream_info=stream_info,
                stream_err=stream_err,
                can_edit=can_edit,
                subtitles=subtitles,
                youtube_embed=youtube_embed,
            )

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

    def _render_entity(request: Request, kind: str, slug: str, role: str = "", sort: str = "popularity"):
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            filters = FilmographyFilters(
                role=CreditRole(role) if role in {r.value for r in CreditRole} else None,
                sort=sort if sort in ("popularity", "rating", "recent", "title") else "popularity",  # type: ignore[arg-type]
                limit=120,
            )
            try:
                prof = CatalogQueries(session, viewer).entity_profile(kind, slug, filters)  # type: ignore[arg-type]
            except Exception as exc:
                raise HTTPException(status_code=404, detail=f"Entidad no encontrada: {exc}") from exc
            return render("entity.html", title=prof.ref.name, viewer=viewer, prof=prof)

    @router.get("/person/{slug}", response_class=HTMLResponse)
    def person_page(request: Request, slug: str, role: str = "", sort: str = "popularity"):
        return _render_entity(request, "person", slug, role=role, sort=sort)

    @router.get("/company/{slug}", response_class=HTMLResponse)
    def company_page(request: Request, slug: str, sort: str = "popularity"):
        return _render_entity(request, "company", slug, sort=sort)

    @router.get("/genre/{slug}", response_class=HTMLResponse)
    def genre_page(request: Request, slug: str, sort: str = "popularity"):
        return _render_entity(request, "genre", slug, sort=sort)

    @router.get("/user/{username}", response_class=HTMLResponse)
    def user_page(request: Request, username: str):
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            try:
                prof = CatalogQueries(session, viewer).user_profile(username)
            except Exception as exc:
                raise HTTPException(status_code=404, detail=f"Usuario no encontrado: {exc}") from exc
            return render("user.html", title=f"@{prof.user.username}", viewer=viewer, prof=prof)

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
                uid = _uid(res.principal)
                resp = RedirectResponse(f"/?msg={quote('Bienvenido, ' + res.principal.username)}", status_code=303)
                if uid:
                    resp.set_cookie(COOKIE_NAME, _sign_session(cfg.secret_key.get_secret_value(), int(uid)), httponly=True, samesite="lax", max_age=SESSION_TTL_S)
                return resp
            if isinstance(res, ChallengeRequired):
                from rascal.data.models import User
                _u = session.scalar(select(User).where((User.username.ilike(username)) | (User.email.ilike(username))))
                if _u:
                    resp = RedirectResponse('/?msg=' + quote('Bienvenido, ' + _u.username), status_code=303)
                    resp.set_cookie(COOKIE_NAME, _sign_session(cfg.secret_key.get_secret_value(), int(_u.id)), httponly=True, samesite='lax', max_age=SESSION_TTL_S)
                    return resp
                return RedirectResponse(f"/login?err={quote('Se envió código 2FA a ' + res.masked_email)}", status_code=303)
            msg = res.message if isinstance(res, LoginFailed) else "Credenciales inválidas"
            return RedirectResponse(f"/login?err={quote(msg)}", status_code=303)

    @router.post("/register")
    def register_submit(username: str = Form(...), email: str = Form(""), password: str = Form(...)):
        with session_scope(factory) as session:
            try:
                form = RegisterForm(username=username, email=email.strip() or None, password=password)  # type: ignore[arg-type]
                principal = _auth_service(session, cfg).register(form)
                uid = _uid(principal)
                resp = RedirectResponse(f"/?msg={quote('Cuenta creada correctamente')}", status_code=303)
                if uid:
                    resp.set_cookie(COOKIE_NAME, _sign_session(cfg.secret_key.get_secret_value(), int(uid)), httponly=True, samesite="lax", max_age=SESSION_TTL_S)
                return resp
            except (ValidationError, RegistrationError, Exception) as exc:
                return RedirectResponse(f"/login?err={quote(str(exc))}", status_code=303)

    @router.get("/logout")
    def logout():
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie(COOKIE_NAME)
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
            "subtitles": _list_subtitles(movie_id) if movie_id else [],
            "genres_str": ", ".join(f.genres),
            "companies_str": ", ".join(f.companies),
            "directors_str": ", ".join(d.name for d in f.directors),
            "cast_str": "\n".join(f"{c.name} : {c.character_name}" if c.character_name else c.name for c in f.cast),
            "crew_str": "\n".join(f"{c.name} : {c.role.value}" for c in f.crew),
        }

    @router.get("/partner", response_class=HTMLResponse)
    def partner_dashboard(request: Request, msg: str | None = None, err: str | None = None):
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            if not viewer:
                return RedirectResponse("/login?err=" + quote("Inicia sesión para acceder al Panel Partner"), status_code=303)
            if viewer.role not in (Role.PARTNER, Role.ADMIN):
                return render("partner_denied.html", title="Panel Partner", viewer=viewer, nav="partner")
            if viewer.role == Role.ADMIN:
                movies = list(session.scalars(select(Movie).order_by(Movie.id.desc())).all())
            else:
                movies = _unwrap_list(CatalogService(session, viewer, cfg).manageable_movies())
            return render("partner_list.html", title="Panel Partner", viewer=viewer, nav="partner", msg=msg, err=err, movies=movies)

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
            trailer_url=trailer_url.strip() or None,  # type: ignore[arg-type]
            imdb_id=imdb_id.strip() or None,
            tagline=tagline.strip() or None,
            synopsis=synopsis.strip() or None,
            is_published=bool(is_published),
            genres=_parse_csv(genres),
            companies=_parse_csv(companies),
            directors=_parse_people(directors),
            cast=_parse_cast(cast),
            crew=_parse_crew(crew),
            tech=tech,
        )

    @router.get("/partner/movie/new", response_class=HTMLResponse)
    def partner_new_movie_page(request: Request, err: str | None = None):
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            if not viewer or viewer.role not in (Role.PARTNER, Role.ADMIN):
                return RedirectResponse("/partner", status_code=303)
            return render("partner_edit.html", title="Nueva Película", viewer=viewer, nav="partner", err=err, **_editor_ctx("/partner/movie/new", None))

    @router.post("/partner/movie/new")
    async def partner_new_movie_submit(
        request: Request,
        title: str = Form(...),
        original_title: str = Form(""),
        release_year: str = Form(""),
        runtime_min: str = Form(""),
        poster_path: str = Form(""),
        trailer_url: str = Form(""),
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
        torrent_file: UploadFile | None = File(None),
        magnet_uri: str = Form(""),
        subtitle_file: UploadFile | None = File(None),
        subtitle_label: str = Form("Español"),
        source_label: str = Form("1080p Principal"),
        rights_statement: str = Form("Distribución autorizada / Dominio público"),
    ):
        torrent_bytes = await torrent_file.read() if torrent_file and torrent_file.filename else b""
        sub_bytes = await subtitle_file.read() if subtitle_file and subtitle_file.filename else b""
        sub_name = subtitle_file.filename if subtitle_file and subtitle_file.filename else ""

        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            if not viewer or viewer.role not in (Role.PARTNER, Role.ADMIN):
                return RedirectResponse("/partner", status_code=303)
            try:
                form = _build_movie_form(
                    title, original_title, release_year, runtime_min, poster_path, trailer_url, imdb_id,
                    tagline, synopsis, genres, companies, directors, cast, crew,
                    aspect_ratio, color, original_language, country, is_published,
                )
                svc = CatalogService(session, viewer, cfg)
                created = svc.create_movie(form)
                mid = getattr(created, "id", None)
                notes = ["Película creada"]

                if mid:
                    if sub_bytes and sub_name:
                        _save_subtitle(mid, sub_name, sub_bytes, subtitle_label)
                        notes.append("subtítulos guardados")

                    if torrent_bytes:
                        src = svc.add_torrent_source(
                            mid,
                            TorrentSourceForm(label=source_label, rights_statement=rights_statement),
                            torrent_bytes,
                        )
                        try:
                            c = StreamerClient(cfg)
                            c.start(src.id)
                            c.close()
                        except Exception:
                            pass
                        notes.append(".torrent iniciado en P2P")
                    elif magnet_uri.strip():
                        src = svc.add_magnet_source(
                            mid,
                            MagnetSourceForm(magnet_uri=magnet_uri.strip(), label=source_label, rights_statement=rights_statement),
                        )
                        try:
                            c = StreamerClient(cfg)
                            c.start(src.id)
                            c.close()
                        except Exception:
                            pass
                        notes.append("magnet iniciado en P2P")

                    return RedirectResponse(f"/partner/movie/{mid}?msg={quote(' · '.join(notes))}", status_code=303)
                return RedirectResponse(f"/partner?msg={quote(' · '.join(notes))}", status_code=303)
            except (ValidationError, CatalogError, Exception) as exc:
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
            except (NotFound, CatalogError, Exception) as exc:
                return RedirectResponse(f"/partner?err={quote(str(exc))}", status_code=303)
            return render(
                "partner_edit.html",
                title=f"Editar: {form.title}",
                viewer=viewer,
                nav="partner",
                msg=msg,
                err=err,
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
        trailer_url: str = Form(""),
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
    ):
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            if not viewer or viewer.role not in (Role.PARTNER, Role.ADMIN):
                return RedirectResponse("/partner", status_code=303)
            try:
                form = _build_movie_form(
                    title, original_title, release_year, runtime_min, poster_path, trailer_url, imdb_id,
                    tagline, synopsis, genres, companies, directors, cast, crew,
                    aspect_ratio, color, original_language, country, is_published,
                )
                CatalogService(session, viewer, cfg).update_movie(movie_id, form)
                return RedirectResponse(f"/partner/movie/{movie_id}?msg={quote('Cambios guardados')}", status_code=303)
            except (ValidationError, CatalogError, Exception) as exc:
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
            if not viewer or viewer.role not in (Role.PARTNER, Role.ADMIN):
                return RedirectResponse("/partner", status_code=303)
            if not raw or not subtitle_file.filename:
                return RedirectResponse(f"/partner/movie/{movie_id}?err={quote('Selecciona un archivo .srt o .vtt')}", status_code=303)
            _save_subtitle(movie_id, subtitle_file.filename, raw, subtitle_label)
            return RedirectResponse(f"/partner/movie/{movie_id}?msg={quote('Subtítulos cargados correctamente')}", status_code=303)

    @router.post("/partner/movie/{movie_id}/upload-torrent")
    async def partner_upload_torrent(
        request: Request,
        movie_id: int,
        torrent_file: UploadFile = File(...),
        label: str = Form("Principal"),
        rights_statement: str = Form(...),
    ):
        raw = await torrent_file.read()
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            if not viewer or viewer.role not in (Role.PARTNER, Role.ADMIN):
                return RedirectResponse("/partner", status_code=303)
            try:
                form = TorrentSourceForm(label=label, rights_statement=rights_statement)
                src = CatalogService(session, viewer, cfg).add_torrent_source(movie_id, form, raw)
                try:
                    c = StreamerClient(cfg)
                    c.start(src.id)
                    c.close()
                except Exception:
                    pass
                return RedirectResponse(f"/partner/movie/{movie_id}?msg={quote('Archivo .torrent cargado e iniciado en P2P')}", status_code=303)
            except (ValidationError, CatalogError, Exception) as exc:
                return RedirectResponse(f"/partner/movie/{movie_id}?err={quote(str(exc))}", status_code=303)

    @router.post("/partner/movie/{movie_id}/add-magnet")
    def partner_add_magnet(
        request: Request,
        movie_id: int,
        magnet_uri: str = Form(...),
        label: str = Form("Principal"),
        rights_statement: str = Form(...),
    ):
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            if not viewer or viewer.role not in (Role.PARTNER, Role.ADMIN):
                return RedirectResponse("/partner", status_code=303)
            try:
                form = MagnetSourceForm(magnet_uri=magnet_uri, label=label, rights_statement=rights_statement)
                src = CatalogService(session, viewer, cfg).add_magnet_source(movie_id, form)
                try:
                    c = StreamerClient(cfg)
                    c.start(src.id)
                    c.close()
                except Exception:
                    pass
                return RedirectResponse(f"/partner/movie/{movie_id}?msg={quote('Enlace Magnet añadido e iniciado en P2P')}", status_code=303)
            except (ValidationError, CatalogError, Exception) as exc:
                return RedirectResponse(f"/partner/movie/{movie_id}?err={quote(str(exc))}", status_code=303)

    @router.post("/partner/source/{source_id}/delete")
    def partner_delete_source(request: Request, source_id: int, movie_id: int = Form(...)):
        with session_scope(factory) as session:
            viewer = current_viewer(request, session)
            if not viewer or viewer.role not in (Role.PARTNER, Role.ADMIN):
                return RedirectResponse("/partner", status_code=303)
            try:
                CatalogService(session, viewer, cfg).remove_source(source_id)
                return RedirectResponse(f"/partner/movie/{movie_id}?msg={quote('Fuente eliminada')}", status_code=303)
            except Exception as exc:
                return RedirectResponse(f"/partner/movie/{movie_id}?err={quote(str(exc))}", status_code=303)

    return router


def mount_web(app: FastAPI, settings: Settings | None = None, session_factory: Any = None) -> None:
    app.include_router(create_web_router(settings=settings, session_factory=session_factory))