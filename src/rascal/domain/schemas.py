"""Contratos de la capa de negocio (Pydantic v2).

* **DTOs de lectura** (``*View``, ``*Card``, ``*Summary``): inmutables y serializables;
  la UI nunca recibe objetos ORM, así que no hay ``DetachedInstanceError`` ni consultas
  perezosas disparadas desde Streamlit, y los resultados se pueden cachear con
  ``st.cache_data`` (se serializan con pickle sin problemas).
* **Formularios** (``*Form``): validación de entrada del panel Partner, reseñas y registro.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Annotated, Literal, TypeVar
from urllib.parse import urlparse

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

from rascal.data.enums import ColorKind, CreditRole, PlaybackMode, Role, SourceKind, SourceStatus

# ─────────────────────────────────────────────────────────────────────────────
# Tipos reutilizables
# ─────────────────────────────────────────────────────────────────────────────
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]


def _empty_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


OptionalText = Annotated[str | None, AfterValidator(_empty_to_none)]


def _validate_media_ref(value: str | None) -> str | None:
    """URL http(s) o ruta relativa dentro del almacenamiento de la app (sin ``..``)."""
    value = _empty_to_none(value)
    if value is None:
        return None
    if len(value) > 500:
        raise ValueError("La ruta/URL no puede superar 500 caracteres")
    parsed = urlparse(value)
    if parsed.scheme in ("http", "https"):
        if not parsed.netloc:
            raise ValueError("URL inválida")
        return value
    if parsed.scheme:
        raise ValueError("Sólo se admiten URLs http(s) o rutas relativas")
    if value.startswith(("/", "\\")) or ".." in value.replace("\\", "/").split("/"):
        raise ValueError("Ruta relativa inválida")
    return value


MediaRef = Annotated[str | None, AfterValidator(_validate_media_ref)]


def _validate_http_url(value: str | None) -> str | None:
    value = _empty_to_none(value)
    if value is None:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Debe ser una URL http(s)")
    if len(value) > 500:
        raise ValueError("URL demasiado larga")
    return value


HttpUrlStr = Annotated[str | None, AfterValidator(_validate_http_url)]


def _dedupe_casefold(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in values:
        value = raw.strip()
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            result.append(value)
    return result


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, from_attributes=True)


# ─────────────────────────────────────────────────────────────────────────────
# DTOs de lectura
# ─────────────────────────────────────────────────────────────────────────────
EntityKind = Literal["person", "company", "genre"]


class EntityRef(_Frozen):
    """Referencia navegable a una entidad (actor, director, productora, género)."""

    kind: EntityKind
    id: int
    slug: str
    name: str


class UserRef(_Frozen):
    id: int
    username: str
    display_name: str | None = None
    avatar_path: str | None = None


class CreditView(_Frozen):
    person: EntityRef
    role: CreditRole
    character_name: str | None = None
    billing_order: int = 0


class RatingBin(_Frozen):
    value: int  # 1..10 (medias estrellas)
    stars: float  # 0.5..5.0
    count: int


class RatingSummary(_Frozen):
    count: int
    average: float | None  # escala 0–10, 2 decimales (como antes)
    average_stars: float | None  # 0–5 redondeado a media estrella
    median: float | None
    stdev: float | None
    distribution: list[RatingBin]

    @property
    def present_bins(self) -> list[RatingBin]:
        """Sólo las barras con votos (equivalente a las etiquetas de la gráfica anterior)."""
        return [b for b in self.distribution if b.count > 0]


class MovieCard(_Frozen):
    id: int
    slug: str
    title: str
    release_year: int | None = None
    poster_path: str | None = None
    average: float | None = None
    average_stars: float | None = None
    review_count: int = 0
    watchlist_count: int = 0
    roles: list[CreditRole] = Field(default_factory=list)  # filmografía de personas
    characters: list[str] = Field(default_factory=list)


class TechSpecsView(_Frozen):
    aspect_ratio: str | None = None
    color: ColorKind | None = None
    sound_mix: str | None = None
    filmed_format: str | None = None
    original_language: str | None = None
    country: str | None = None
    budget_usd: int | None = None
    box_office_usd: int | None = None


class SourceView(_Frozen):
    id: int
    movie_id: int
    kind: SourceKind
    label: str
    info_hash: str
    display_name: str | None = None
    file_path: str | None = None
    file_size: int | None = None
    container: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    duration_s: float | None = None
    playback_mode: PlaybackMode
    status: SourceStatus
    status_message: str | None = None
    rights_statement: str
    created_at: datetime


class MovieDetail(_Frozen):
    id: int
    slug: str
    title: str
    original_title: str | None = None
    tagline: str | None = None
    synopsis: str | None = None
    release_year: int | None = None
    runtime_min: int | None = None
    poster_path: str | None = None
    backdrop_path: str | None = None
    trailer_url: str | None = None
    trailer_path: str | None = None
    imdb_id: str | None = None
    tmdb_id: int | None = None
    owner_id: int | None = None
    is_published: bool = True
    genres: list[EntityRef]
    companies: list[EntityRef]
    directors: list[EntityRef]
    cast: list[CreditView]
    crew: list[CreditView]
    tech: TechSpecsView | None = None
    sources: list[SourceView]
    rating: RatingSummary
    watchlist_count: int = 0


class ReviewView(_Frozen):
    id: int
    user: UserRef
    movie_id: int
    movie_slug: str
    movie_title: str
    rating: int | None
    stars: float | None
    star_label: str
    body: str | None
    watched_on: date | None
    is_rewatch: bool
    contains_spoilers: bool
    created_at: datetime
    votes_up: int = 0
    votes_down: int = 0


class EntityMetrics(_Frozen):
    films: int
    total_reviews: int
    average: float | None  # sobre todas las reseñas de la filmografía
    average_stars: float | None
    average_of_film_averages: float | None  # cada película pesa lo mismo
    distinct_reviewers: int
    watchlist_count: int
    reviews_last_30d: int
    popularity_score: float
    distribution: list[RatingBin]


class RoleCount(_Frozen):
    role: CreditRole
    films: int


class EntityProfile(_Frozen):
    ref: EntityRef
    description: str | None = None
    image_path: str | None = None
    country: str | None = None
    birth_date: date | None = None
    death_date: date | None = None
    roles: list[RoleCount] = Field(default_factory=list)
    metrics: EntityMetrics
    filmography: list[MovieCard]
    filmography_total: int


class UserPublicProfile(_Frozen):
    user: UserRef
    bio: str | None
    role: Role
    joined_at: datetime
    followers: int
    following: int
    is_following: bool
    is_self: bool
    films_logged: int
    average_given: float | None
    favorites: list[MovieCard]
    watchlist: list[MovieCard]
    recent_reviews: list[ReviewView]


class SearchHit(_Frozen):
    kind: Literal["movie", "person", "company", "genre", "user"]
    id: int
    slug: str
    title: str
    subtitle: str | None = None
    image_path: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Filtros de vistas dinámicas
# ─────────────────────────────────────────────────────────────────────────────
SortKey = Literal["popularity", "rating", "year_desc", "year_asc", "title"]


class FilmographyFilters(BaseModel):
    """Filtros de la vista de entidad (se mapean 1:1 a ``st.query_params``)."""

    model_config = ConfigDict(frozen=True)

    role: CreditRole | None = None
    genre_slug: str | None = None
    year_from: int | None = Field(default=None, ge=1870, le=2100)
    year_to: int | None = Field(default=None, ge=1870, le=2100)
    min_stars: float | None = Field(default=None, ge=0, le=5)
    sort: SortKey = "popularity"
    limit: int = Field(default=60, ge=1, le=500)
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _check_years(self) -> FilmographyFilters:
        if self.year_from and self.year_to and self.year_from > self.year_to:
            raise ValueError("year_from no puede ser mayor que year_to")
        return self


# ─────────────────────────────────────────────────────────────────────────────
# Formularios del panel Partner
# ─────────────────────────────────────────────────────────────────────────────
class PersonInput(BaseModel):
    """Persona por id existente o por nombre (se crea si no existe)."""

    id: int | None = Field(default=None, gt=0)
    name: Name


class CastInput(PersonInput):
    character_name: OptionalText = None

    @field_validator("character_name")
    @classmethod
    def _limit_character(cls, value: str | None) -> str | None:
        if value and len(value) > 200:
            raise ValueError("Nombre de personaje demasiado largo")
        return value


class CrewInput(PersonInput):
    role: CreditRole

    @field_validator("role")
    @classmethod
    def _no_cast_or_director(cls, value: CreditRole) -> CreditRole:
        if value in (CreditRole.CAST, CreditRole.DIRECTOR):
            raise ValueError("Usa las listas 'cast' y 'directors' para esos roles")
        return value


_ASPECT_RE = re.compile(r"^\d{1,2}(\.\d{1,3})?:\d{1,2}(\.\d{1,3})?$")
_LANG_RE = re.compile(r"^[a-z]{2,3}$")
_IMDB_RE = re.compile(r"^tt\d{7,10}$")


class TechSpecsForm(BaseModel):
    aspect_ratio: OptionalText = None
    color: ColorKind | None = None
    sound_mix: OptionalText = None
    filmed_format: OptionalText = None
    original_language: OptionalText = None
    country: OptionalText = None
    budget_usd: int | None = Field(default=None, ge=0)
    box_office_usd: int | None = Field(default=None, ge=0)

    @field_validator("aspect_ratio")
    @classmethod
    def _aspect(cls, value: str | None) -> str | None:
        if value is not None and not _ASPECT_RE.match(value):
            raise ValueError("Formato de relación de aspecto inválido (ej. 2.39:1)")
        return value

    @field_validator("original_language")
    @classmethod
    def _lang(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.lower()
            if not _LANG_RE.match(value):
                raise ValueError("Usa un código ISO 639-1/639-2 (ej. 'es', 'ja')")
        return value

    @field_validator("sound_mix", "filmed_format", "country")
    @classmethod
    def _short(cls, value: str | None) -> str | None:
        if value is not None and len(value) > 64:
            raise ValueError("Máximo 64 caracteres")
        return value

    def is_empty(self) -> bool:
        return all(value is None for value in self.model_dump().values())


def _current_year() -> int:
    return datetime.now(UTC).year


class MovieForm(BaseModel):
    """Todos los atributos editables de una película (panel Partner)."""

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
    original_title: OptionalText = None
    tagline: OptionalText = None
    synopsis: OptionalText = None
    release_year: int | None = Field(default=None, ge=1870)
    runtime_min: int | None = Field(default=None, ge=1, le=1000)
    poster_path: MediaRef = None
    backdrop_path: MediaRef = None
    trailer_url: HttpUrlStr = None
    trailer_path: MediaRef = None
    imdb_id: OptionalText = None
    tmdb_id: int | None = Field(default=None, gt=0)
    is_published: bool = True

    genres: list[Name] = Field(default_factory=list, max_length=12)
    companies: list[Name] = Field(default_factory=list, max_length=40)
    directors: list[PersonInput] = Field(default_factory=list, max_length=10)
    cast: list[CastInput] = Field(default_factory=list, max_length=300)
    crew: list[CrewInput] = Field(default_factory=list, max_length=300)
    tech: TechSpecsForm = Field(default_factory=TechSpecsForm)

    @field_validator("release_year")
    @classmethod
    def _year_not_far_future(cls, value: int | None) -> int | None:
        if value is not None and value > _current_year() + 5:
            raise ValueError("Año demasiado lejano en el futuro")
        return value

    @field_validator("synopsis")
    @classmethod
    def _synopsis_len(cls, value: str | None) -> str | None:
        if value is not None and len(value) > 5000:
            raise ValueError("La sinopsis no puede superar 5000 caracteres")
        return value

    @field_validator("original_title", "tagline")
    @classmethod
    def _title_len(cls, value: str | None) -> str | None:
        if value is not None and len(value) > 300:
            raise ValueError("Máximo 300 caracteres")
        return value

    @field_validator("imdb_id")
    @classmethod
    def _imdb(cls, value: str | None) -> str | None:
        if value is not None and not _IMDB_RE.match(value):
            raise ValueError("IMDb id inválido (ej. tt0114709)")
        return value

    @field_validator("genres", "companies")
    @classmethod
    def _dedupe_names(cls, values: list[str]) -> list[str]:
        return _dedupe_casefold(values)

    @model_validator(mode="after")
    def _dedupe_people(self) -> MovieForm:
        """Una persona aparece una sola vez por rol (lo exige ``uq_movie_credits_movie_person_role``)."""
        self.directors = _unique_people(self.directors)
        self.cast = _unique_people(self.cast)
        self.crew = _unique_people(self.crew, with_role=True)
        return self


_P = TypeVar("_P", bound=PersonInput)


def person_key(person: PersonInput) -> str:
    """Clave de identidad de una persona en un formulario: id si existe, si no el nombre."""
    return f"id:{person.id}" if person.id else f"name:{person.name.casefold()}"


def _unique_people(items: list[_P], *, with_role: bool = False) -> list[_P]:
    seen: set[str] = set()
    result: list[_P] = []
    for item in items:
        key = person_key(item) + (f"|{item.role}" if with_role and isinstance(item, CrewInput) else "")
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


class MagnetSourceForm(BaseModel):
    magnet_uri: Annotated[str, StringConstraints(strip_whitespace=True, min_length=20, max_length=8192)]
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)] = "Principal"
    rights_statement: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)]


class TorrentSourceForm(BaseModel):
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)] = "Principal"
    rights_statement: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)]


# ─────────────────────────────────────────────────────────────────────────────
# Reseñas, usuarios
# ─────────────────────────────────────────────────────────────────────────────
class ReviewForm(BaseModel):
    rating: int | None = Field(default=None, ge=0, le=10)  # 0 = sin estrellas (widget)
    body: OptionalText = None
    watched_on: date | None = None
    is_rewatch: bool = False
    contains_spoilers: bool = False

    @field_validator("rating")
    @classmethod
    def _zero_is_none(cls, value: int | None) -> int | None:
        return None if value == 0 else value

    @field_validator("body")
    @classmethod
    def _body_len(cls, value: str | None) -> str | None:
        if value is not None and len(value) > 10_000:
            raise ValueError("La reseña no puede superar 10 000 caracteres")
        return value

    @field_validator("watched_on")
    @classmethod
    def _not_future(cls, value: date | None) -> date | None:
        if value is not None and value > datetime.now(UTC).date():
            raise ValueError("La fecha de visionado no puede ser futura")
        return value

    @model_validator(mode="after")
    def _not_empty(self) -> ReviewForm:
        if self.rating is None and self.body is None:
            raise ValueError("Añade una calificación o un texto")
        return self


class RegisterForm(BaseModel):
    username: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9_.-]{3,30}$")]
    email: EmailStr | None = None
    password: SecretStr

    @field_validator("password")
    @classmethod
    def _password_strength(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if len(raw) < 10:
            raise ValueError("La contraseña debe tener al menos 10 caracteres")
        if len(raw) > 128:
            raise ValueError("La contraseña es demasiado larga")
        if raw.isdigit() or raw.isalpha():
            raise ValueError("Combina letras y números/símbolos")
        return value


class ProfileForm(BaseModel):
    display_name: OptionalText = None
    bio: OptionalText = None
    avatar_path: MediaRef = None
    favorite_movie_ids: list[int] = Field(default_factory=list, max_length=10)

    @field_validator("bio")
    @classmethod
    def _bio_len(cls, value: str | None) -> str | None:
        if value is not None and len(value) > 2000:
            raise ValueError("Biografía demasiado larga")
        return value
