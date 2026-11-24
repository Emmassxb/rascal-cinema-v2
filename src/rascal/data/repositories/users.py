"""Repositorios de usuarios, desafíos 2FA y actividad social."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Select, delete, func, select
from sqlalchemy.orm import Session

from rascal.data.models import FavoriteMovie, Follow, LoginChallenge, Movie, User, WatchlistEntry


class UserRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, user_id: int) -> User | None:
        return self.session.get(User, user_id)

    def by_username(self, username: str) -> User | None:
        return self.session.scalars(select(User).where(func.lower(User.username) == username.casefold())).first()

    def by_email(self, email: str) -> User | None:
        return self.session.scalars(select(User).where(func.lower(User.email) == email.casefold())).first()

    def add(self, user: User) -> User:
        self.session.add(user)
        self.session.flush()
        return user

    def search(self, q: str, limit: int) -> list[User]:
        return list(
            self.session.scalars(
                select(User)
                .where(User.is_active.is_(True), User.username.icontains(q, autoescape=True))
                .order_by(User.username)
                .limit(limit)
            )
        )

    def list_all(self, limit: int = 500, offset: int = 0) -> list[User]:
        return list(self.session.scalars(select(User).order_by(User.id).limit(limit).offset(offset)))

    # ── 2FA ──────────────────────────────────────────────────────────────────
    def add_challenge(self, challenge: LoginChallenge) -> LoginChallenge:
        self.session.add(challenge)
        self.session.flush()
        return challenge

    def get_challenge(self, challenge_id: int) -> LoginChallenge | None:
        return self.session.get(LoginChallenge, challenge_id)

    def purge_challenges(self, user_id: int, older_than: datetime) -> None:
        self.session.execute(
            delete(LoginChallenge).where(LoginChallenge.user_id == user_id, LoginChallenge.created_at < older_than)
        )


class SocialRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ── Follows ──────────────────────────────────────────────────────────────
    def is_following(self, follower_id: int, followed_id: int) -> bool:
        return self.session.get(Follow, (follower_id, followed_id)) is not None

    def follow(self, follower_id: int, followed_id: int) -> bool:
        """Devuelve ``True`` si se creó la relación (idempotente)."""
        if self.is_following(follower_id, followed_id):
            return False
        self.session.add(Follow(follower_id=follower_id, followed_id=followed_id))
        self.session.flush()
        return True

    def unfollow(self, follower_id: int, followed_id: int) -> bool:
        result = self.session.execute(
            delete(Follow).where(Follow.follower_id == follower_id, Follow.followed_id == followed_id)
        )
        return bool(result.rowcount)

    def follow_counts(self, user_id: int) -> tuple[int, int]:
        followers = self.session.scalar(select(func.count()).where(Follow.followed_id == user_id)) or 0
        following = self.session.scalar(select(func.count()).where(Follow.follower_id == user_id)) or 0
        return int(followers), int(following)

    def followers(self, user_id: int, limit: int = 200) -> list[User]:
        return list(
            self.session.scalars(
                select(User).join(Follow, Follow.follower_id == User.id).where(Follow.followed_id == user_id).limit(limit)
            )
        )

    def following(self, user_id: int, limit: int = 200) -> list[User]:
        return list(
            self.session.scalars(
                select(User).join(Follow, Follow.followed_id == User.id).where(Follow.follower_id == user_id).limit(limit)
            )
        )

    # ── Watchlist ────────────────────────────────────────────────────────────
    def in_watchlist(self, user_id: int, movie_id: int) -> bool:
        return self.session.get(WatchlistEntry, (user_id, movie_id)) is not None

    def toggle_watchlist(self, user_id: int, movie_id: int) -> bool:
        """Devuelve ``True`` si quedó añadida, ``False`` si se quitó."""
        entry = self.session.get(WatchlistEntry, (user_id, movie_id))
        if entry is not None:
            self.session.delete(entry)
            self.session.flush()
            return False
        self.session.add(WatchlistEntry(user_id=user_id, movie_id=movie_id))
        self.session.flush()
        return True

    def watchlist_movie_ids(self, user_id: int) -> Select[Any]:
        return select(WatchlistEntry.movie_id).where(WatchlistEntry.user_id == user_id)

    def watchlist_count_for_movies(self, movie_ids: Select[Any]) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(WatchlistEntry)
                .join(Movie, Movie.id == WatchlistEntry.movie_id)
                .where(WatchlistEntry.movie_id.in_(movie_ids), Movie.is_published.is_(True))
            )
            or 0
        )

    def watchlist_count_for_movie(self, movie_id: int) -> int:
        return int(self.session.scalar(select(func.count()).where(WatchlistEntry.movie_id == movie_id)) or 0)

    # ── Favoritos ────────────────────────────────────────────────────────────
    def favorite_movie_ids(self, user_id: int) -> list[int]:
        return list(
            self.session.scalars(
                select(FavoriteMovie.movie_id).where(FavoriteMovie.user_id == user_id).order_by(FavoriteMovie.position)
            )
        )

    def set_favorites(self, user_id: int, movie_ids: Sequence[int]) -> None:
        self.session.execute(delete(FavoriteMovie).where(FavoriteMovie.user_id == user_id))
        seen: set[int] = set()
        position = 0
        for movie_id in movie_ids:
            if movie_id in seen:
                continue
            seen.add(movie_id)
            self.session.add(FavoriteMovie(user_id=user_id, movie_id=movie_id, position=position))
            position += 1
        self.session.flush()
