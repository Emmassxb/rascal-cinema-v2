-- Esquema PostgreSQL generado desde rascal.data.models (SQLAlchemy 2.0) · v2.2 (migración 0003).
-- Fuente de verdad: los modelos ORM + migrations/ (Alembic). Este archivo es documentación/revisión.

CREATE TABLE companies (
	id SERIAL NOT NULL, 
	slug VARCHAR(120) NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	country VARCHAR(64), 
	logo_path VARCHAR(500), 
	CONSTRAINT pk_companies PRIMARY KEY (id), 
	CONSTRAINT uq_companies_slug UNIQUE (slug)
);
CREATE INDEX ix_companies_name ON companies (name);

CREATE TABLE genres (
	id SERIAL NOT NULL, 
	slug VARCHAR(80) NOT NULL, 
	name VARCHAR(80) NOT NULL, 
	CONSTRAINT pk_genres PRIMARY KEY (id), 
	CONSTRAINT uq_genres_slug UNIQUE (slug), 
	CONSTRAINT uq_genres_name UNIQUE (name)
);

CREATE TABLE people (
	id SERIAL NOT NULL, 
	slug VARCHAR(120) NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	bio TEXT, 
	birth_date DATE, 
	death_date DATE, 
	photo_path VARCHAR(500), 
	place_of_birth VARCHAR(200), 
	known_for_department VARCHAR(64), 
	tmdb_id INTEGER, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE, 
	CONSTRAINT pk_people PRIMARY KEY (id), 
	CONSTRAINT uq_people_slug UNIQUE (slug), 
	CONSTRAINT uq_people_tmdb_id UNIQUE (tmdb_id)
);
CREATE INDEX ix_people_name ON people (name);

CREATE TABLE users (
	id SERIAL NOT NULL, 
	username VARCHAR(40) NOT NULL, 
	email VARCHAR(254), 
	password_hash VARCHAR(255), 
	role VARCHAR(24) NOT NULL, 
	display_name VARCHAR(80), 
	bio TEXT, 
	avatar_path VARCHAR(500), 
	is_active BOOLEAN NOT NULL, 
	mfa_email_enabled BOOLEAN NOT NULL, 
	failed_login_count SMALLINT NOT NULL, 
	locked_until TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	last_login_at TIMESTAMP WITHOUT TIME ZONE, 
	legacy_id VARCHAR(64), 
	CONSTRAINT pk_users PRIMARY KEY (id), 
	CONSTRAINT ck_users_user_role CHECK (role IN ('admin', 'partner', 'user')), 
	CONSTRAINT uq_users_legacy_id UNIQUE (legacy_id)
);
CREATE INDEX ix_users_role ON users (role);
CREATE UNIQUE INDEX uq_users_email_lower ON users (lower(email));
CREATE UNIQUE INDEX uq_users_username_lower ON users (lower(username));

CREATE TABLE audit_log (
	id SERIAL NOT NULL, 
	actor_id INTEGER, 
	action VARCHAR(64) NOT NULL, 
	entity_type VARCHAR(32) NOT NULL, 
	entity_id INTEGER, 
	payload JSON, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	CONSTRAINT pk_audit_log PRIMARY KEY (id), 
	CONSTRAINT fk_audit_log_actor_id_users FOREIGN KEY(actor_id) REFERENCES users (id) ON DELETE SET NULL
);
CREATE INDEX ix_audit_log_created ON audit_log (created_at);
CREATE INDEX ix_audit_log_entity ON audit_log (entity_type, entity_id);

CREATE TABLE follows (
	follower_id INTEGER NOT NULL, 
	followed_id INTEGER NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	CONSTRAINT pk_follows PRIMARY KEY (follower_id, followed_id), 
	CONSTRAINT ck_follows_no_self_follow CHECK (follower_id <> followed_id), 
	CONSTRAINT fk_follows_follower_id_users FOREIGN KEY(follower_id) REFERENCES users (id) ON DELETE CASCADE, 
	CONSTRAINT fk_follows_followed_id_users FOREIGN KEY(followed_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE INDEX ix_follows_followed_id ON follows (followed_id);

CREATE TABLE login_challenges (
	id SERIAL NOT NULL, 
	user_id INTEGER NOT NULL, 
	code_hash VARCHAR(128) NOT NULL, 
	expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	attempts SMALLINT NOT NULL, 
	consumed_at TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	CONSTRAINT pk_login_challenges PRIMARY KEY (id), 
	CONSTRAINT fk_login_challenges_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE INDEX ix_login_challenges_user_created ON login_challenges (user_id, created_at);

CREATE TABLE movies (
	id SERIAL NOT NULL, 
	media_type VARCHAR(24) DEFAULT 'movie' NOT NULL, 
	slug VARCHAR(120) NOT NULL, 
	title VARCHAR(300) NOT NULL, 
	original_title VARCHAR(300), 
	tagline VARCHAR(300), 
	synopsis TEXT, 
	release_year SMALLINT, 
	runtime_min SMALLINT, 
	poster_path VARCHAR(500), 
	backdrop_path VARCHAR(500), 
	trailer_url VARCHAR(500), 
	trailer_path VARCHAR(500), 
	imdb_id VARCHAR(16), 
	tmdb_id INTEGER, 
	legacy_movie_id INTEGER, 
	owner_id INTEGER, 
	is_published BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	rating_avg FLOAT, 
	rating_count INTEGER DEFAULT '0' NOT NULL, 
	review_count INTEGER DEFAULT '0' NOT NULL, 
	watchlist_count INTEGER DEFAULT '0' NOT NULL, 
	popularity INTEGER DEFAULT '0' NOT NULL, 
	CONSTRAINT pk_movies PRIMARY KEY (id), 
	CONSTRAINT ck_movies_release_year_range CHECK (release_year IS NULL OR release_year BETWEEN 1870 AND 2100), 
	CONSTRAINT ck_movies_runtime_positive CHECK (runtime_min IS NULL OR runtime_min > 0), 
	CONSTRAINT uq_movies_media_type_tmdb_id UNIQUE (media_type, tmdb_id), 
	CONSTRAINT ck_movies_media_type CHECK (media_type IN ('movie', 'series')), 
	CONSTRAINT uq_movies_slug UNIQUE (slug), 
	CONSTRAINT uq_movies_imdb_id UNIQUE (imdb_id), 
	CONSTRAINT uq_movies_legacy_movie_id UNIQUE (legacy_movie_id), 
	CONSTRAINT fk_movies_owner_id_users FOREIGN KEY(owner_id) REFERENCES users (id) ON DELETE SET NULL
);
CREATE INDEX ix_movies_is_published ON movies (is_published);
CREATE INDEX ix_movies_owner_id ON movies (owner_id);
CREATE INDEX ix_movies_published_created ON movies (is_published, created_at);
CREATE INDEX ix_movies_published_popularity ON movies (is_published, popularity);
CREATE INDEX ix_movies_published_rating ON movies (is_published, rating_avg);
CREATE INDEX ix_movies_published_title ON movies (is_published, title, id);
CREATE INDEX ix_movies_release_year ON movies (release_year);
CREATE INDEX ix_movies_title ON movies (title);
CREATE INDEX ix_movies_tmdb_id ON movies (tmdb_id);
CREATE INDEX ix_movies_type_published_year ON movies (media_type, is_published, release_year);

CREATE TABLE partner_requests (
	id SERIAL NOT NULL, 
	user_id INTEGER NOT NULL, 
	status VARCHAR(24) NOT NULL, 
	display_name VARCHAR(80), 
	reason TEXT NOT NULL, 
	portfolio_url VARCHAR(500), 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	reviewed_at TIMESTAMP WITHOUT TIME ZONE, 
	reviewed_by INTEGER, 
	CONSTRAINT pk_partner_requests PRIMARY KEY (id), 
	CONSTRAINT fk_partner_requests_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE, 
	CONSTRAINT ck_partner_requests_partner_request_status CHECK (status IN ('pending', 'approved', 'rejected')), 
	CONSTRAINT fk_partner_requests_reviewed_by_users FOREIGN KEY(reviewed_by) REFERENCES users (id) ON DELETE SET NULL
);
CREATE INDEX ix_partner_requests_status ON partner_requests (status);
CREATE INDEX ix_partner_requests_user_id ON partner_requests (user_id);
CREATE UNIQUE INDEX uq_partner_requests_one_pending ON partner_requests (user_id) WHERE status = 'pending';

CREATE TABLE favorite_movies (
	user_id INTEGER NOT NULL, 
	movie_id INTEGER NOT NULL, 
	position SMALLINT NOT NULL, 
	CONSTRAINT pk_favorite_movies PRIMARY KEY (user_id, movie_id), 
	CONSTRAINT ck_favorite_movies_position_non_negative CHECK (position >= 0), 
	CONSTRAINT fk_favorite_movies_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE, 
	CONSTRAINT fk_favorite_movies_movie_id_movies FOREIGN KEY(movie_id) REFERENCES movies (id) ON DELETE CASCADE
);

CREATE TABLE movie_companies (
	movie_id INTEGER NOT NULL, 
	company_id INTEGER NOT NULL, 
	CONSTRAINT pk_movie_companies PRIMARY KEY (movie_id, company_id), 
	CONSTRAINT fk_movie_companies_movie_id_movies FOREIGN KEY(movie_id) REFERENCES movies (id) ON DELETE CASCADE, 
	CONSTRAINT fk_movie_companies_company_id_companies FOREIGN KEY(company_id) REFERENCES companies (id) ON DELETE CASCADE
);
CREATE INDEX ix_movie_companies_company_id ON movie_companies (company_id);

CREATE TABLE movie_credits (
	id SERIAL NOT NULL, 
	movie_id INTEGER NOT NULL, 
	person_id INTEGER NOT NULL, 
	role VARCHAR(24) NOT NULL, 
	character_name VARCHAR(200), 
	billing_order SMALLINT NOT NULL, 
	CONSTRAINT pk_movie_credits PRIMARY KEY (id), 
	CONSTRAINT uq_movie_credits_movie_person_role UNIQUE (movie_id, person_id, role), 
	CONSTRAINT fk_movie_credits_movie_id_movies FOREIGN KEY(movie_id) REFERENCES movies (id) ON DELETE CASCADE, 
	CONSTRAINT fk_movie_credits_person_id_people FOREIGN KEY(person_id) REFERENCES people (id) ON DELETE CASCADE, 
	CONSTRAINT ck_movie_credits_credit_role CHECK (role IN ('cast', 'director', 'writer', 'producer', 'cinematographer', 'composer', 'editor'))
);
CREATE INDEX ix_movie_credits_movie_role_order ON movie_credits (movie_id, role, billing_order);
CREATE INDEX ix_movie_credits_person_role ON movie_credits (person_id, role);

CREATE TABLE movie_genres (
	movie_id INTEGER NOT NULL, 
	genre_id INTEGER NOT NULL, 
	CONSTRAINT pk_movie_genres PRIMARY KEY (movie_id, genre_id), 
	CONSTRAINT fk_movie_genres_movie_id_movies FOREIGN KEY(movie_id) REFERENCES movies (id) ON DELETE CASCADE, 
	CONSTRAINT fk_movie_genres_genre_id_genres FOREIGN KEY(genre_id) REFERENCES genres (id) ON DELETE CASCADE
);
CREATE INDEX ix_movie_genres_genre_id ON movie_genres (genre_id);

CREATE TABLE movie_tech_specs (
	movie_id INTEGER NOT NULL, 
	aspect_ratio VARCHAR(16), 
	color VARCHAR(24), 
	sound_mix VARCHAR(64), 
	filmed_format VARCHAR(64), 
	original_language VARCHAR(8), 
	country VARCHAR(64), 
	budget_usd BIGINT, 
	box_office_usd BIGINT, 
	CONSTRAINT pk_movie_tech_specs PRIMARY KEY (movie_id), 
	CONSTRAINT ck_movie_tech_specs_budget_non_negative CHECK (budget_usd IS NULL OR budget_usd >= 0), 
	CONSTRAINT ck_movie_tech_specs_box_office_non_negative CHECK (box_office_usd IS NULL OR box_office_usd >= 0), 
	CONSTRAINT fk_movie_tech_specs_movie_id_movies FOREIGN KEY(movie_id) REFERENCES movies (id) ON DELETE CASCADE, 
	CONSTRAINT ck_movie_tech_specs_color_kind CHECK (color IN ('color', 'bw', 'mixed'))
);

CREATE TABLE reviews (
	id SERIAL NOT NULL, 
	user_id INTEGER NOT NULL, 
	movie_id INTEGER NOT NULL, 
	rating SMALLINT, 
	body TEXT, 
	watched_on DATE, 
	is_rewatch BOOLEAN NOT NULL, 
	contains_spoilers BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	legacy_id VARCHAR(64), 
	CONSTRAINT pk_reviews PRIMARY KEY (id), 
	CONSTRAINT ck_reviews_rating_half_stars CHECK (rating IS NULL OR rating BETWEEN 1 AND 10), 
	CONSTRAINT fk_reviews_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE, 
	CONSTRAINT fk_reviews_movie_id_movies FOREIGN KEY(movie_id) REFERENCES movies (id) ON DELETE CASCADE, 
	CONSTRAINT uq_reviews_legacy_id UNIQUE (legacy_id)
);
CREATE INDEX ix_reviews_created ON reviews (created_at);
CREATE INDEX ix_reviews_movie_created ON reviews (movie_id, created_at);
CREATE INDEX ix_reviews_movie_rating ON reviews (movie_id, rating);
CREATE INDEX ix_reviews_user_created ON reviews (user_id, created_at);

CREATE TABLE seasons (
	id SERIAL NOT NULL, 
	series_id INTEGER NOT NULL, 
	season_number SMALLINT NOT NULL, 
	title VARCHAR(300), 
	overview TEXT, 
	poster_path VARCHAR(500), 
	air_year SMALLINT, 
	tmdb_season_id INTEGER, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_seasons PRIMARY KEY (id), 
	CONSTRAINT uq_seasons_series_number UNIQUE (series_id, season_number), 
	CONSTRAINT ck_seasons_season_number_range CHECK (season_number BETWEEN 0 AND 999), 
	CONSTRAINT ck_seasons_air_year_range CHECK (air_year IS NULL OR air_year BETWEEN 1870 AND 2100), 
	CONSTRAINT fk_seasons_series_id_movies FOREIGN KEY(series_id) REFERENCES movies (id) ON DELETE CASCADE
);

CREATE TABLE watch_links (
	id SERIAL NOT NULL, 
	movie_id INTEGER NOT NULL, 
	platform_name VARCHAR(80) NOT NULL, 
	platform_slug VARCHAR(80) NOT NULL, 
	url VARCHAR(1000) NOT NULL, 
	offer_type VARCHAR(24) NOT NULL, 
	region VARCHAR(2) NOT NULL, 
	logo_path VARCHAR(500), 
	added_by_id INTEGER, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_watch_links PRIMARY KEY (id), 
	CONSTRAINT uq_watch_links_offer UNIQUE (movie_id, platform_slug, offer_type, region), 
	CONSTRAINT fk_watch_links_movie_id_movies FOREIGN KEY(movie_id) REFERENCES movies (id) ON DELETE CASCADE, 
	CONSTRAINT ck_watch_links_offer_type CHECK (offer_type IN ('subscription', 'rent', 'buy', 'free')), 
	CONSTRAINT fk_watch_links_added_by_id_users FOREIGN KEY(added_by_id) REFERENCES users (id) ON DELETE SET NULL
);
CREATE INDEX ix_watch_links_movie_id ON watch_links (movie_id);

CREATE TABLE watchlist (
	user_id INTEGER NOT NULL, 
	movie_id INTEGER NOT NULL, 
	added_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	CONSTRAINT pk_watchlist PRIMARY KEY (user_id, movie_id), 
	CONSTRAINT fk_watchlist_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE, 
	CONSTRAINT fk_watchlist_movie_id_movies FOREIGN KEY(movie_id) REFERENCES movies (id) ON DELETE CASCADE
);
CREATE INDEX ix_watchlist_movie_id ON watchlist (movie_id);

CREATE TABLE episodes (
	id SERIAL NOT NULL, 
	season_id INTEGER NOT NULL, 
	episode_number SMALLINT NOT NULL, 
	title VARCHAR(300), 
	synopsis TEXT, 
	runtime_min SMALLINT, 
	still_path VARCHAR(500), 
	air_date DATE, 
	tmdb_episode_id INTEGER, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	CONSTRAINT pk_episodes PRIMARY KEY (id), 
	CONSTRAINT uq_episodes_season_number UNIQUE (season_id, episode_number), 
	CONSTRAINT ck_episodes_episode_number_range CHECK (episode_number BETWEEN 0 AND 9999), 
	CONSTRAINT ck_episodes_episode_runtime_positive CHECK (runtime_min IS NULL OR runtime_min > 0), 
	CONSTRAINT fk_episodes_season_id_seasons FOREIGN KEY(season_id) REFERENCES seasons (id) ON DELETE CASCADE
);

CREATE TABLE review_votes (
	review_id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	value SMALLINT NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	CONSTRAINT pk_review_votes PRIMARY KEY (review_id, user_id), 
	CONSTRAINT ck_review_votes_vote_value CHECK (value IN (-1, 1)), 
	CONSTRAINT fk_review_votes_review_id_reviews FOREIGN KEY(review_id) REFERENCES reviews (id) ON DELETE CASCADE, 
	CONSTRAINT fk_review_votes_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE TABLE media_sources (
	id SERIAL NOT NULL, 
	movie_id INTEGER NOT NULL, 
	episode_id INTEGER, 
	kind VARCHAR(24) NOT NULL, 
	label VARCHAR(120) NOT NULL, 
	audio_language VARCHAR(40), 
	quality VARCHAR(20), 
	magnet_uri TEXT, 
	torrent_data BYTEA, 
	info_hash VARCHAR(64), 
	stream_url TEXT, 
	url_hash VARCHAR(64), 
	display_name VARCHAR(500), 
	total_size BIGINT, 
	file_index INTEGER, 
	file_path VARCHAR(1000), 
	file_size BIGINT, 
	container VARCHAR(16), 
	video_codec VARCHAR(32), 
	audio_codec VARCHAR(32), 
	duration_s FLOAT, 
	playback_mode VARCHAR(24) NOT NULL, 
	status VARCHAR(24) NOT NULL, 
	status_message VARCHAR(500), 
	rights_statement VARCHAR(500) NOT NULL, 
	added_by_id INTEGER, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	CONSTRAINT pk_media_sources PRIMARY KEY (id), 
	CONSTRAINT uq_media_sources_movie_info_hash UNIQUE (movie_id, info_hash), 
	CONSTRAINT uq_media_sources_movie_url_hash UNIQUE (movie_id, url_hash), 
	CONSTRAINT ck_media_sources_payload_matches_kind CHECK ((kind = 'magnet' AND magnet_uri IS NOT NULL AND info_hash IS NOT NULL) OR (kind = 'torrent_file' AND torrent_data IS NOT NULL AND info_hash IS NOT NULL) OR (kind IN ('direct_url', 'hls') AND stream_url IS NOT NULL AND url_hash IS NOT NULL)), 
	CONSTRAINT fk_media_sources_movie_id_movies FOREIGN KEY(movie_id) REFERENCES movies (id) ON DELETE CASCADE, 
	CONSTRAINT fk_media_sources_episode_id_episodes FOREIGN KEY(episode_id) REFERENCES episodes (id) ON DELETE CASCADE, 
	CONSTRAINT ck_media_sources_source_kind CHECK (kind IN ('magnet', 'torrent_file', 'direct_url', 'hls')), 
	CONSTRAINT ck_media_sources_playback_mode CHECK (playback_mode IN ('unknown', 'direct', 'remux', 'remux_audio', 'transcode', 'unsupported')), 
	CONSTRAINT ck_media_sources_source_status CHECK (status IN ('pending', 'downloading', 'complete', 'error', 'disabled')), 
	CONSTRAINT fk_media_sources_added_by_id_users FOREIGN KEY(added_by_id) REFERENCES users (id) ON DELETE SET NULL
);
CREATE INDEX ix_media_sources_episode_id ON media_sources (episode_id);
CREATE INDEX ix_media_sources_info_hash ON media_sources (info_hash);
CREATE INDEX ix_media_sources_movie_id ON media_sources (movie_id);
CREATE INDEX ix_media_sources_status ON media_sources (status);

-- Búsqueda con ILIKE '%texto%' en PostgreSQL: SQLAlchemy genera lower(col) LIKE lower(:q), así que el
-- índice trigram debe ser sobre lower(col). Sin pg_trgm la búsqueda recorre la tabla (≈50 ms con 86 000 títulos).
-- Ubuntu/Debian: el paquete postgresql-contrib trae la extensión.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS ix_movies_title_trgm ON movies USING gin (lower(title) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_people_name_trgm ON people USING gin (lower(name) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_companies_name_trgm ON companies USING gin (lower(name) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_users_username_trgm ON users USING gin (lower(username) gin_trgm_ops);
