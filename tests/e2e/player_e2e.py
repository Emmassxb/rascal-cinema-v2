"""E2E del reproductor en Chromium headless (opcional, fuera de ``pytest``).

Comprueba con un navegador real: selectores de idioma/calidad/subtítulos arriba y en la barra de
Video.js, cambio de versión conservando la posición, HLS con dos pistas de audio
(``audioTracks()``), CSP estricta sin violaciones y la vista de temporadas/capítulos.

Requisitos: ``pip install playwright && playwright install chromium``, ffmpeg y
``npm install video.js@8.10.0`` en ``RASCAL_E2E_DIR`` (por defecto /tmp/pw). Los medios de
prueba se generan con ffmpeg en ``$RASCAL_E2E_DIR/media`` (ver README de pruebas).

    RASCAL_ALLOW_PRIVATE_URLS=true python tests/e2e/player_e2e.py
"""
import json, os, sys, threading, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT / "tests"))
import uvicorn
from sqlalchemy.orm import sessionmaker
from playwright.sync_api import sync_playwright

from rascal.config import Settings
from rascal.data.db import build_engine, create_schema, session_scope
from rascal.data.enums import Role, MediaType
from rascal.auth.service import AuthService
from rascal.auth.mailer import ConsoleMailer
from rascal.domain.schemas import RegisterForm, DirectSourceForm, SeasonForm, EpisodeForm
from rascal.domain.services.catalog import CatalogService
from rascal.domain.services.series import SeriesService
from rascal.streaming.api import create_app
from rascal.providers import MediaTarget, build_registry

BASE = Path(os.environ.get("RASCAL_E2E_DIR", "/tmp/pw"))
MEDIA = BASE / "media"
VJS = BASE / "node_modules/video.js/dist"
tmp = BASE / "run"; import shutil; shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir()

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles
# CDN de prueba con soporte de Range (como un CDN real) y CORS para HLS.
cdn = Starlette(routes=[Mount("/", StaticFiles(directory=str(MEDIA)))],
                middleware=[Middleware(CORSMiddleware, allow_origins=["*"])])
cdn_server = uvicorn.Server(uvicorn.Config(cdn, host="127.0.0.1", port=9660, log_level="warning"))
threading.Thread(target=cdn_server.run, daemon=True).start()
while not cdn_server.started: time.sleep(0.1)

settings = Settings(env="test", data_dir=tmp / "var", database_url=f"sqlite:///{tmp/'t.db'}", stream_engine="local",
                    secret_key="x" * 40, service_key="svc", allow_private_urls=True, allow_insecure_stream_urls=True,
                    streamer_public_url="http://127.0.0.1:9861", streamer_internal_url="http://127.0.0.1:9861",
                    ui_public_url="http://127.0.0.1:9861")
engine = build_engine(settings.database_url); create_schema(engine)
factory = sessionmaker(bind=engine, expire_on_commit=False)
B = "http://127.0.0.1:9660"
with session_scope(factory) as s:
    auth = AuthService(s, settings, ConsoleMailer())
    partner = auth.register(RegisterForm(username="partner", email="p@example.com", password="clave-segura-1"), role=Role.PARTNER)
    from test_catalog import movie_form  # noqa
with session_scope(factory) as s:
    svc = CatalogService(s, partner, settings)
    mid = svc.create_movie(movie_form())
    ids = {}
    for key, url, audio, q in (("es1080", f"{B}/es.mp4", "Español Latino", "1080p"), ("es720", f"{B}/es.mp4?v=720", "Español Latino", "720p"),
                               ("en1080", f"{B}/en.mp4", "Inglés", "1080p"), ("hls", f"{B}/hls/master.m3u8", "Dual (HLS)", "Auto")):
        ids[key] = svc.add_direct_source(mid, DirectSourceForm(stream_url=url, label=key, audio_language=audio, quality=q,
                                                               rights_statement="CC prueba")).id
    sid = svc.create_movie(movie_form(title="Serie Demo", media_type=MediaType.SERIES, imdb_id=None, tmdb_id=None))
    ss = SeriesService(s, partner)
    season = ss.add_season(sid, SeasonForm(season_number=1, title="Primera"))
    ep1 = ss.add_episode(season, EpisodeForm(episode_number=1, title="Piloto", runtime_min=42, synopsis="Empieza todo."))
    ss.add_episode(season, EpisodeForm(episode_number=2, title="Segundo"))
    ids["ep"] = svc.add_direct_source(sid, DirectSourceForm(stream_url=f"{B}/en.mp4", audio_language="Inglés", quality="1080p",
                                                            rights_statement="CC prueba"), episode_id=ep1).id
    slugs = {"movie": s.get(__import__("rascal.data.models", fromlist=["Movie"]).Movie, mid).slug,
             "series": s.get(__import__("rascal.data.models", fromlist=["Movie"]).Movie, sid).slug}
build_registry(settings).local_subtitles.save(MediaTarget(mid), b"1\n00:00:00,500 --> 00:00:05,000\nHola mundo\n", "Español")

app = create_app(settings, session_factory=factory)
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=9861, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
while not server.started: time.sleep(0.1)

def serve_vjs(route):
    name = route.request.url.rsplit("/", 1)[-1]
    route.fulfill(path=str(VJS / name), content_type="text/css" if name.endswith(".css") else "application/javascript")

results = {}
with sync_playwright() as p:
    browser = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
    # 1) CSP aplicada: el reproductor debe montarse sin violaciones (sin evaluar JS desde el test).
    strict = browser.new_page(viewport={"width": 1280, "height": 1000})
    csp_errors = []
    strict.on("console", lambda m: csp_errors.append(m.text) if m.type == "error" else None)
    strict.route("https://cdn.jsdelivr.net/npm/video.js@8.10.0/dist/*", serve_vjs)
    strict.goto("http://127.0.0.1:9861/login")
    strict.fill("form[action='/login'] input[name=username]", "partner")
    strict.fill("form[action='/login'] input[name=password]", "clave-segura-1")
    strict.click("form[action='/login'] button")
    strict.wait_for_url("**/?msg=*")
    strict.goto(f"http://127.0.0.1:9861/movie/{slugs['movie']}?source={ids['es1080']}&play=1#p2p")
    sf = strict.frames[1]
    sf.wait_for_selector(".vjs-rascal-lang", state="attached", timeout=15000)
    time.sleep(2)
    results["strict_csp_controlbar_buttons"] = len(sf.query_selector_all(".vjs-rascal-choice"))
    results["strict_csp_errors"] = csp_errors
    strict.close()
    # 2) Pruebas funcionales (bypass_csp sólo para que Playwright pueda evaluar expresiones).
    context = browser.new_context(bypass_csp=True, viewport={"width": 1280, "height": 1400})
    page = context.new_page()
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.route("https://cdn.jsdelivr.net/npm/video.js@8.10.0/dist/*", serve_vjs)
    page.goto("http://127.0.0.1:9861/login")
    page.fill("form[action='/login'] input[name=username]", "partner")
    page.fill("form[action='/login'] input[name=password]", "clave-segura-1")
    page.click("form[action='/login'] button")
    page.wait_for_url("**/?msg=*")

    page.goto(f"http://127.0.0.1:9861/movie/{slugs['movie']}?source={ids['es1080']}&play=1#p2p")
    frame = page.frame_locator("#rascal-player")
    fr = page.frames[1]; fr.wait_for_load_state()
    fr.wait_for_function("window.rascalPlayer && window.rascalPlayer.player.readyState() >= 1", timeout=15000)
    results["lang_options"] = fr.eval_on_selector_all("#lang option", "els => els.map(e => e.textContent)")
    results["quality_options"] = fr.eval_on_selector_all("#quality option", "els => els.map(e => e.textContent)")
    results["subs_options"] = fr.eval_on_selector_all("#subs option", "els => els.map(e => e.textContent)")
    results["controlbar_buttons"] = fr.eval_on_selector_all(".vjs-rascal-choice:not(.vjs-hidden) .vjs-icon-placeholder", "els => els.map(e => e.textContent)")
    results["privacy_hidden_for_direct"] = fr.eval_on_selector("#privacy", "e => e.hidden")
    fr.evaluate("window.rascalPlayer.player.muted(true); window.rascalPlayer.player.play()")
    fr.wait_for_function("window.rascalPlayer.player.currentTime() > 2.2", timeout=15000)
    # menú de la barra de controles: abrir «Idioma» y elegir «Inglés»
    fr.click(".vjs-rascal-lang button.vjs-menu-button")
    items = fr.eval_on_selector_all(".vjs-rascal-lang .vjs-menu-item", "els => els.map(e => e.textContent.trim())")
    results["controlbar_lang_items"] = items
    with page.expect_event("framenavigated"):
        fr.click(".vjs-rascal-lang .vjs-menu-item:has-text('Inglés')")
    time.sleep(1.5)
    fr = page.frames[1]
    results["after_switch_url"] = fr.url.split("?")[0] + "?…" + ("&t=" if "&t=" in fr.url else "")
    fr.wait_for_function("window.rascalPlayer && window.rascalPlayer.player.readyState() >= 1", timeout=15000)
    time.sleep(1.5)
    results["after_switch_debug"] = [fr.url[-12:], fr.evaluate("[window.rascalPlayer.player.readyState(), window.rascalPlayer.player.currentTime(), window.rascalPlayer.config.startAt, window.rascalPlayer.player.duration(), window.rascalPlayer.player.seekable().length]")]
    results["after_switch_time"] = round(fr.evaluate("window.rascalPlayer.player.currentTime()"), 1)
    results["after_switch_lang_selected"] = fr.eval_on_selector("#lang", "s => s.options[s.selectedIndex].textContent")
    # selector de calidad de arriba (volver a Español → 720p)
    fr.select_option("#lang", "lang:Español Latino")
    time.sleep(1.5); fr = page.frames[1]
    fr.wait_for_function("window.rascalPlayer && window.rascalPlayer.player.readyState() >= 1", timeout=15000)
    results["back_to_spanish_quality"] = fr.eval_on_selector("#quality", "s => s.options[s.selectedIndex].textContent")
    with page.expect_event("framenavigated"):
        fr.select_option("#quality", f"src:{ids['es720']}")
    time.sleep(1.5); fr = page.frames[1]
    fr.wait_for_function("window.rascalPlayer && window.rascalPlayer.player.readyState() >= 1", timeout=15000)
    results["quality_now"] = fr.eval_on_selector("#quality", "s => s.options[s.selectedIndex].textContent")
    # subtítulos: activar desde el selector superior
    fr.select_option("#subs", "0")
    results["subtitle_mode"] = fr.evaluate("Array.from(window.rascalPlayer.player.textTracks()).filter(t=>t.kind==='subtitles').map(t=>t.mode)")
    page.screenshot(path=str(BASE / "movie_player.png"), full_page=False)

    # HLS con dos pistas de audio dentro del stream
    page.goto(f"http://127.0.0.1:9861/movie/{slugs['movie']}?source={ids['hls']}&play=1#p2p")
    fr = page.frames[1]; fr.wait_for_load_state()
    fr.wait_for_function("window.rascalPlayer && window.rascalPlayer.player.audioTracks().length > 1", timeout=20000)
    fr.evaluate("window.rascalPlayer.player.muted(true); window.rascalPlayer.player.play()")
    fr.wait_for_function("window.rascalPlayer.player.currentTime() > 1", timeout=20000)
    fr.click(".vjs-rascal-lang button.vjs-menu-button")
    page.screenshot(path=str(BASE / "hls_menu.png"))
    results["hls_lang_options"] = fr.eval_on_selector_all("#lang option", "els => els.map(e => e.value + '=' + e.textContent)")
    fr.select_option("#lang", "track:1")
    time.sleep(1.0)
    results["hls_enabled_tracks"] = fr.evaluate("Array.from(window.rascalPlayer.player.audioTracks()).map(t => (t.label||t.language) + ':' + t.enabled)")
    fr.wait_for_function("window.rascalPlayer.player.currentTime() > 3", timeout=20000)
    results["hls_still_playing_after_switch"] = fr.evaluate("!window.rascalPlayer.player.paused() && !window.rascalPlayer.player.error()")

    # Serie: pestañas y capítulos
    page.goto(f"http://127.0.0.1:9861/movie/{slugs['series']}")
    results["season_tabs"] = page.eval_on_selector_all(".season-tabs a", "els => els.map(e => e.textContent.trim())")
    results["episode_cards"] = page.eval_on_selector_all(".episode-card .episode-title", "els => els.map(e => e.textContent)")
    page.click(".episode-card:has-text('Piloto')")
    results["episode_sources"] = page.eval_on_selector_all(".sources-table tbody tr", "els => els.length")
    page.click(".sources-table a.btn:has-text('Reproducir')")
    page.wait_for_load_state()
    results["episode_page_alert"] = page.eval_on_selector_all(".alert", "els => els.map(e => e.textContent.trim())")
    page.wait_for_selector("#rascal-player", timeout=10000)
    fr = page.frame_locator("#rascal-player")
    time.sleep(1)
    fr = page.frames[1]; fr.wait_for_load_state()
    fr.wait_for_function("window.rascalPlayer && window.rascalPlayer.player.readyState() >= 1", timeout=15000)
    results["episode_player_title"] = fr.title()
    page.screenshot(path=str(BASE / "series.png"), full_page=True)
    results["console_errors"] = [e for e in errors if "favicon" not in e]
    browser.close()
print(json.dumps(results, ensure_ascii=False, indent=1))
server.should_exit = True
