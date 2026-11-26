"""Modelos ORM (SQLAlchemy 2.0, tipado con ``Mapped``).

Esquema relacional normalizado que reemplaza los documentos planos de MongoDB:

* ``movies`` 1:1 ``movie_tech_specs``
* ``movies`` N:M ``genres``     vía ``movie_genres``
* ``movies`` N:M ``companies``  vía ``movie_companies``
* ``movies`` N:M ``people``     vía ``movie_credits`` (con rol, personaje y orden)
* ``users`` N:M ``movies``      vía ``reviews`` (diario: varias entradas por usuario)
* ``users`` N:M ``movies``      vía ``watchlist`` y ``favorite_movies``
* ``users`` N:M ``users``       vía ``follows``
* ``movies`` 1:N ``media_sources`` (magnet / .torrent / URL directa / HLS)
* ``movies`` (series) 1:N ``seasons`` 1:N ``episodes`` 1:N ``media_sources``
* ``movies`` 1:N ``watch_links`` (plataformas oficiales, "Dónde ver")

Todas las marcas de tiempo se guardan en UTC *naive* para que SQLite y PostgreSQL
se comporten igual.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    SmallInteger,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, synonym

from rascal.data.enums import (
    ColorKind,
    CreditRole,
    MediaType,
    OfferType,
    PartnerRequestStatus,
    PlaybackMode,
    Role,
    SourceKind,
    SourceStatus,
)

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    """Fecha/hora actual en UTC sin tzinfo (formato de almacenamiento)."""
    return datetime.now(UTC).replace(tzinfo=None)


def _enum(enum_cls: type[StrEnum], name: str) -> SAEnum:
    """Enum portable: VARCHAR + CHECK, guardando el *valor* (no el nombre del miembro)."""
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=24,
        validate_strings=True,
        values_callable=lambda cls: [member.value for member in cls],
    )


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# ─────────────────────────────────────────────────────────────────────────────
# Tablas puente N:M sin atributos propios
# ─────────────────────────────────────────────────────────────────────────────
movie_genres = Table(
    "movie_genres",
    Base.metadata,
    Column("movie_id", ForeignKey("movies.id", ondelete="CASCADE"), primary_key=True),
    Column("genre_id", ForeignKey("genres.id", ondelete="CASCADE"), primary_key=True),
    Index("ix_movie_genres_genre_id", "genre_id"),
)

movie_companies = Table(
    "movie_companies",
    Base.metadata,
    Column("movie_id", ForeignKey("movies.id", ondelete="CASCADE"), primary_key=True),
    Column("company_id", ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True),
    Index("ix_movie_companies_company_id", "company_id"),
)


# ─────────────────────────────────────────────────────────────────────────────
# Usuarios y autenticación
# ─────────────────────────────────────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(254))
    password_hash: Mapped[str | None] = mapped_column(String(255))  # None → cuenta importada sin login
    role: Mapped[Role] = mapped_column(_enum(Role, "user_role"), default=Role.USER, index=True)
    display_name: Mapped[str | None] = mapped_column(String(80))
    bio: Mapped[str | None] = mapped_column(Text)
    avatar_path: Mapped[str | None] = mapped_column(String(500))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    mfa_email_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_login_count: Mapped[int] = mapped_column(SmallInteger, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)
    legacy_id: Mapped[str | None] = mapped_column(String(64), unique=True)  # ObjectId Mongo o "ml:<n>"

    reviews: Mapped[list[Review]] = relationship(back_populates="user", passive_deletes=True)
    owned_movies: Mapped[list[Movie]] = relationship(back_populates="owner")


# Unicidad case-insensitive (``Ana`` y ``ana`` no pueden coexistir). Los NULL de email no colisionan.
Index("uq_users_username_lower", func.lower(User.username), unique=True)
Index("uq_users_email_lower", func.lower(User.email), unique=True)


class LoginChallenge(Base):
    """Código OTP de un solo uso (2FA por correo). Se guarda sólo el HMAC del código."""

    __tablename__ = "login_challenges"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    code_hash: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(SmallInteger, default=0)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (Index("ix_login_challenges_user_created", "user_id", "created_at"),)


# ─────────────────────────────────────────────────────────────────────────────
# Catálogo
# ─────────────────────────────────────────────────────────────────────────────
class Movie(Base):
    """Título del catálogo: película (``media_type='movie'``) o serie (``'series'``).

    Las series reutilizan esta tabla (ficha, créditos, géneros, reseñas, watchlist y
    "Dónde ver" funcionan igual) y añaden ``seasons`` → ``episodes``.

    ``rating_avg``…``popularity`` son **agregados desnormalizados** que mantiene
    ``rascal.data.stats`` en la misma transacción que cada reseña o entrada de watchlist.
    Los listados (inicio, catálogo, búsqueda, filmografías) ordenan y filtran por estas
    columnas indexadas en vez de agrupar toda la tabla ``reviews`` en cada petición.
    """

    __tablename__ = "movies"

    id: Mapped[int] = mapped_column(primary_key=True)
    media_type: Mapped[MediaType] = mapped_column(
        _enum(MediaType, "media_type"), default=MediaType.MOVIE, server_default=MediaType.MOVIE.value
    )
    slug: Mapped[str] = mapped_column(String(120), unique=True)
    title: Mapped[str] = mapped_column(String(300), index=True)
    original_title: Mapped[str | None] = mapped_column(String(300))
    tagline: Mapped[str | None] = mapped_column(String(300))
    synopsis: Mapped[str | None] = mapped_column(Text)
    release_year: Mapped[int | None] = mapped_column(SmallInteger, index=True)
    runtime_min: Mapped[int | None] = mapped_column(SmallInteger)
    poster_path: Mapped[str | None] = mapped_column(String(500))
    backdrop_path: Mapped[str | None] = mapped_column(String(500))
    trailer_url: Mapped[str | None] = mapped_column(String(500))
    trailer_path: Mapped[str | None] = mapped_column(String(500))
    imdb_id: Mapped[str | None] = mapped_column(String(16), unique=True)
    # TMDB numera películas y series por separado (movie/1399 ≠ tv/1399): único por (media_type, tmdb_id).
    tmdb_id: Mapped[int | None] = mapped_column(Integer, index=True)
    legacy_movie_id: Mapped[int | None] = mapped_column(Integer, unique=True)  # movieId de Mongo
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    is_published: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    # Agregados desnormalizados (ver rascal.data.stats). Escala de rating_avg: 1..10.
    rating_avg: Mapped[float | None] = mapped_column(Float)
    rating_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    review_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    watchlist_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    popularity: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # reseñas + watchlist

    owner: Mapped[User | None] = relationship(back_populates="owned_movies")
    genres: Mapped[list[Genre]] = relationship(secondary=movie_genres, back_populates="movies", order_by="Genre.name")
    companies: Mapped[list[Company]] = relationship(
        secondary=movie_companies, back_populates="movies", order_by="Company.name"
    )
    credits: Mapped[list[MovieCredit]] = relationship(
        back_populates="movie",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by=lambda: (MovieCredit.role, MovieCredit.billing_order),
    )
    tech_specs: Mapped[MovieTechSpecs | None] = relationship(
        back_populates="movie", cascade="all, delete-orphan", passive_deletes=True, uselist=False
    )
    reviews: Mapped[list[Review]] = relationship(back_populates="movie", passive_deletes=True)
    sources: Mapped[list[MediaSource]] = relationship(
        back_populates="movie", cascade="all, delete-orphan", passive_deletes=True
    )
    seasons: Mapped[list[Season]] = relationship(
        back_populates="series", cascade="all, delete-orphan", passive_deletes=True,
        order_by=lambda: Season.season_number,
    )
    watch_links: Mapped[list[WatchLink]] = relationship(
        back_populates="movie", cascade="all, delete-orphan", passive_deletes=True,
        order_by=lambda: (WatchLink.offer_type, WatchLink.platform_name),
    )

    @property
    def is_series(self) -> bool:
        return self.media_type is MediaType.SERIES

    __table_args__ = (
        CheckConstraint("release_year IS NULL OR release_year BETWEEN 1870 AND 2100", name="release_year_range"),
        CheckConstraint("runtime_min IS NULL OR runtime_min > 0", name="runtime_positive"),
        UniqueConstraint("media_type", "tmdb_id", name="uq_movies_media_type_tmdb_id"),
        # Listados: filtro por tipo + publicadas + año (catálogo, filmografías con rango de años).
        Index("ix_movies_type_published_year", "media_type", "is_published", "release_year"),
        # Ordenaciones del inicio/catálogo sin agrupar reseñas: top-N por índice.
        Index("ix_movies_published_popularity", "is_published", "popularity"),
        Index("ix_movies_published_rating", "is_published", "rating_avg"),
        Index("ix_movies_published_created", "is_published", "created_at"),
        # Orden alfabético paginado (catálogo «Título A-Z») sin ordenar en memoria.
        Index("ix_movies_published_title", "is_published", "title", "id"),
    )


class MovieTechSpecs(Base):
    """Métricas técnicas 1:1 (separadas para no ensanchar ``movies``)."""

    __tablename__ = "movie_tech_specs"

    movie_id: Mapped[int] = mapped_column(ForeignKey("movies.id", ondelete="CASCADE"), primary_key=True)
    aspect_ratio: Mapped[str | None] = mapped_column(String(16))  # "2.39:1"
    color: Mapped[ColorKind | None] = mapped_column(_enum(ColorKind, "color_kind"))
    sound_mix: Mapped[str | None] = mapped_column(String(64))  # "Dolby Atmos"
    filmed_format: Mapped[str | None] = mapped_column(String(64))  # "35 mm", "Digital"
    original_language: Mapped[str | None] = mapped_column(String(8))  # ISO 639-1
    country: Mapped[str | None] = mapped_column(String(64))
    budget_usd: Mapped[int | None] = mapped_column(BigInteger)
    box_office_usd: Mapped[int | None] = mapped_column(BigInteger)

    movie: Mapped[Movie] = relationship(back_populates="tech_specs")

    __table_args__ = (
        CheckConstraint("budget_usd IS NULL OR budget_usd >= 0", name="budget_non_negative"),
        CheckConstraint("box_office_usd IS NULL OR box_office_usd >= 0", name="box_office_non_negative"),
    )


class Genre(Base):
    __tablename__ = "genres"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)

    movies: Mapped[list[Movie]] = relationship(secondary=movie_genres, back_populates="genres")


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    country: Mapped[str | None] = mapped_column(String(64))
    logo_path: Mapped[str | None] = mapped_column(String(500))

    movies: Mapped[list[Movie]] = relationship(secondary=movie_companies, back_populates="companies")


class Person(Base):
    __tablename__ = "people"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    bio: Mapped[str | None] = mapped_column(Text)
    birth_date: Mapped[date | None] = mapped_column(Date)
    death_date: Mapped[date | None] = mapped_column(Date)
    photo_path: Mapped[str | None] = mapped_column(String(500))
    place_of_birth: Mapped[str | None] = mapped_column(String(200))
    known_for_department: Mapped[str | None] = mapped_column(String(64))  # "Acting", "Directing"…
    tmdb_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, onupdate=utcnow)

    # Alias explícito pedido por la API de TMDB (/person/{id}); comparte la columna tmdb_id.
    tmdb_person_id = synonym("tmdb_id")

    credits: Mapped[list[MovieCredit]] = relationship(back_populates="person", passive_deletes=True)


class MovieCredit(Base):
    """Tabla puente N:M película↔persona con atributos (rol, personaje, orden de créditos)."""

    __tablename__ = "movie_credits"

    id: Mapped[int] = mapped_column(primary_key=True)
    movie_id: Mapped[int] = mapped_column(ForeignKey("movies.id", ondelete="CASCADE"))
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"))
    role: Mapped[CreditRole] = mapped_column(_enum(CreditRole, "credit_role"))
    character_name: Mapped[str | None] = mapped_column(String(200))
    billing_order: Mapped[int] = mapped_column(SmallInteger, default=0)

    movie: Mapped[Movie] = relationship(back_populates="credits")
    person: Mapped[Person] = relationship(back_populates="credits")

    __table_args__ = (
        UniqueConstraint("movie_id", "person_id", "role", name="uq_movie_credits_movie_person_role"),
        Index("ix_movie_credits_person_role", "person_id", "role"),
        Index("ix_movie_credits_movie_role_order", "movie_id", "role", "billing_order"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Actividad social
# ─────────────────────────────────────────────────────────────────────────────
class Review(Base):
    """Entrada de diario/reseña. ``rating`` en medias estrellas: 1..10 (= 0.5..5 ★)."""

    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    movie_id: Mapped[int] = mapped_column(ForeignKey("movies.id", ondelete="CASCADE"))
    rating: Mapped[int | None] = mapped_column(SmallInteger)
    body: Mapped[str | None] = mapped_column(Text)
    watched_on: Mapped[date | None] = mapped_column(Date)
    is_rewatch: Mapped[bool] = mapped_column(Boolean, default=False)
    contains_spoilers: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    legacy_id: Mapped[str | None] = mapped_column(String(64), unique=True)

    user: Mapped[User] = relationship(back_populates="reviews")
    movie: Mapped[Movie] = relationship(back_populates="reviews")

    __table_args__ = (
        CheckConstraint("rating IS NULL OR rating BETWEEN 1 AND 10", name="rating_half_stars"),
        Index("ix_reviews_movie_created", "movie_id", "created_at"),
        Index("ix_reviews_user_created", "user_id", "created_at"),
        Index("ix_reviews_movie_rating", "movie_id", "rating"),
        Index("ix_reviews_created", "created_at"),
    )


class ReviewVote(Base):
    __tablename__ = "review_votes"

    review_id: Mapped[int] = mapped_column(ForeignKey("reviews.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    value: Mapped[int] = mapped_column(SmallInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (CheckConstraint("value IN (-1, 1)", name="vote_value"),)


class WatchlistEntry(Base):
    __tablename__ = "watchlist"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    movie_id: Mapped[int] = mapped_column(ForeignKey("movies.id", ondelete="CASCADE"), primary_key=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (Index("ix_watchlist_movie_id", "movie_id"),)


class FavoriteMovie(Base):
    __tablename__ = "favorite_movies"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    movie_id: Mapped[int] = mapped_column(ForeignKey("movies.id", ondelete="CASCADE"), primary_key=True)
    position: Mapped[int] = mapped_column(SmallInteger, default=0)

    __table_args__ = (CheckConstraint("position >= 0", name="position_non_negative"),)


class Follow(Base):
    __tablename__ = "follows"

    follower_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    followed_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        CheckConstraint("follower_id <> followed_id", name="no_self_follow"),
        Index("ix_follows_followed_id", "followed_id"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Fuentes de reproducción (P2P y directas)
# ─────────────────────────────────────────────────────────────────────────────
class MediaSource(Base):
    """Fuente de reproducción de una película o de un capítulo.

    * P2P: ``magnet`` (``magnet_uri``) o ``torrent_file`` (``torrent_data``) con ``info_hash``.
    * Directa: ``direct_url`` (MP4/WebM/MKV por HTTPS) o ``hls`` (.m3u8) con ``stream_url``.
      No pasa por libtorrent: el navegador la reproduce tal cual.

    ``movie_id`` siempre apunta al título (la serie, en el caso de un capítulo) y
    ``episode_id`` concreta el capítulo. Así las consultas por título siguen siendo baratas
    y el borrado en cascada de una serie arrastra todas sus fuentes.
    """

    __tablename__ = "media_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    movie_id: Mapped[int] = mapped_column(ForeignKey("movies.id", ondelete="CASCADE"), index=True)
    episode_id: Mapped[int | None] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"), index=True)
    kind: Mapped[SourceKind] = mapped_column(_enum(SourceKind, "source_kind"))
    label: Mapped[str] = mapped_column(String(120), default="Principal")
    audio_language: Mapped[str | None] = mapped_column(String(40))  # "Español Latino", "Japonés (VO)"
    quality: Mapped[str | None] = mapped_column(String(20))  # "1080p", "720p", "4K"
    magnet_uri: Mapped[str | None] = mapped_column(Text)
    torrent_data: Mapped[bytes | None] = mapped_column(LargeBinary)
    info_hash: Mapped[str | None] = mapped_column(String(64), index=True)  # v1 (40 hex) o v2 (64 hex); NULL si es URL
    stream_url: Mapped[str | None] = mapped_column(Text)  # direct_url / hls
    url_hash: Mapped[str | None] = mapped_column(String(64))  # sha256(stream_url): evita duplicados
    display_name: Mapped[str | None] = mapped_column(String(500))
    total_size: Mapped[int | None] = mapped_column(BigInteger)
    file_index: Mapped[int | None] = mapped_column(Integer)
    file_path: Mapped[str | None] = mapped_column(String(1000))
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    container: Mapped[str | None] = mapped_column(String(16))
    video_codec: Mapped[str | None] = mapped_column(String(32))
    audio_codec: Mapped[str | None] = mapped_column(String(32))
    duration_s: Mapped[float | None] = mapped_column()
    playback_mode: Mapped[PlaybackMode] = mapped_column(
        _enum(PlaybackMode, "playback_mode"), default=PlaybackMode.UNKNOWN
    )
    status: Mapped[SourceStatus] = mapped_column(
        _enum(SourceStatus, "source_status"), default=SourceStatus.PENDING, index=True
    )
    status_message: Mapped[str | None] = mapped_column(String(500))
    rights_statement: Mapped[str] = mapped_column(String(500))  # licencia/titularidad declarada
    added_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    movie: Mapped[Movie] = relationship(back_populates="sources")
    episode: Mapped[Episode | None] = relationship(back_populates="sources")

    @property
    def source_type(self) -> str:
        """torrent | magnet | direct_url | hls (nomenclatura estilo Stremio)."""
        return self.kind.source_type

    @property
    def is_p2p(self) -> bool:
        return self.kind.is_p2p

    __table_args__ = (
        UniqueConstraint("movie_id", "info_hash", name="uq_media_sources_movie_info_hash"),
        UniqueConstraint("movie_id", "url_hash", name="uq_media_sources_movie_url_hash"),
        CheckConstraint(
            "(kind = 'magnet' AND magnet_uri IS NOT NULL AND info_hash IS NOT NULL)"
            " OR (kind = 'torrent_file' AND torrent_data IS NOT NULL AND info_hash IS NOT NULL)"
            " OR (kind IN ('direct_url', 'hls') AND stream_url IS NOT NULL AND url_hash IS NOT NULL)",
            name="payload_matches_kind",
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Series: temporadas y capítulos
# ─────────────────────────────────────────────────────────────────────────────
class Season(Base):
    __tablename__ = "seasons"

    id: Mapped[int] = mapped_column(primary_key=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("movies.id", ondelete="CASCADE"))
    season_number: Mapped[int] = mapped_column(SmallInteger)  # 0 = especiales (convención TMDB)
    title: Mapped[str | None] = mapped_column(String(300))
    overview: Mapped[str | None] = mapped_column(Text)
    poster_path: Mapped[str | None] = mapped_column(String(500))
    air_year: Mapped[int | None] = mapped_column(SmallInteger)
    tmdb_season_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, server_default=func.now())

    series: Mapped[Movie] = relationship(back_populates="seasons")
    episodes: Mapped[list[Episode]] = relationship(
        back_populates="season", cascade="all, delete-orphan", passive_deletes=True,
        order_by=lambda: Episode.episode_number,
    )

    __table_args__ = (
        UniqueConstraint("series_id", "season_number", name="uq_seasons_series_number"),
        CheckConstraint("season_number BETWEEN 0 AND 999", name="season_number_range"),
        CheckConstraint("air_year IS NULL OR air_year BETWEEN 1870 AND 2100", name="air_year_range"),
    )


class Episode(Base):
    __tablename__ = "episodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id", ondelete="CASCADE"))
    episode_number: Mapped[int] = mapped_column(SmallInteger)
    title: Mapped[str | None] = mapped_column(String(300))
    synopsis: Mapped[str | None] = mapped_column(Text)
    runtime_min: Mapped[int | None] = mapped_column(SmallInteger)
    still_path: Mapped[str | None] = mapped_column(String(500))
    air_date: Mapped[date | None] = mapped_column(Date)
    tmdb_episode_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, server_default=func.now())

    season: Mapped[Season] = relationship(back_populates="episodes")
    sources: Mapped[list[MediaSource]] = relationship(back_populates="episode", passive_deletes=True)

    __table_args__ = (
        UniqueConstraint("season_id", "episode_number", name="uq_episodes_season_number"),
        CheckConstraint("episode_number BETWEEN 0 AND 9999", name="episode_number_range"),
        CheckConstraint("runtime_min IS NULL OR runtime_min > 0", name="episode_runtime_positive"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Plataformas oficiales ("Dónde ver")
# ─────────────────────────────────────────────────────────────────────────────
class WatchLink(Base):
    __tablename__ = "watch_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    movie_id: Mapped[int] = mapped_column(ForeignKey("movies.id", ondelete="CASCADE"), index=True)
    platform_name: Mapped[str] = mapped_column(String(80))
    platform_slug: Mapped[str] = mapped_column(String(80))
    url: Mapped[str] = mapped_column(String(1000))
    offer_type: Mapped[OfferType] = mapped_column(_enum(OfferType, "offer_type"), default=OfferType.SUBSCRIPTION)
    region: Mapped[str] = mapped_column(String(2), default="MX")
    logo_path: Mapped[str | None] = mapped_column(String(500))
    added_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, server_default=func.now())

    movie: Mapped[Movie] = relationship(back_populates="watch_links")

    __table_args__ = (
        UniqueConstraint("movie_id", "platform_slug", "offer_type", "region", name="uq_watch_links_offer"),
    )


class AuditLog(Base):
    """Bitácora de cambios del panel Partner/Admin (se escribe en la misma transacción)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        Index("ix_audit_log_entity", "entity_type", "entity_id"),
        Index("ix_audit_log_created", "created_at"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Solicitudes para ser Partner
# ─────────────────────────────────────────────────────────────────────────────
class PartnerRequest(Base):
    """Solicitud de un usuario para obtener el rol ``partner``.

    Un usuario sólo puede tener UNA solicitud pendiente a la vez; la restricción se aplica
    en la base de datos con un índice único parcial (SQLite y PostgreSQL lo soportan).
    """

    __tablename__ = "partner_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[PartnerRequestStatus] = mapped_column(
        _enum(PartnerRequestStatus, "partner_request_status"),
        default=PartnerRequestStatus.PENDING,
        index=True,
    )
    display_name: Mapped[str | None] = mapped_column(String(80))
    reason: Mapped[str] = mapped_column(Text)
    portfolio_url: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, server_default=func.now())
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    user: Mapped[User] = relationship(foreign_keys=[user_id])

    __table_args__ = (
        Index(
            "uq_partner_requests_one_pending",
            "user_id",
            unique=True,
            sqlite_where=text("status = 'pending'"),
            postgresql_where=text("status = 'pending'"),
        ),
    )
