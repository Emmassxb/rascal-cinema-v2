-- Índices trigram para búsquedas por subcadena en PostgreSQL (ejecutar una vez como superusuario o dueño):
--   sudo -u postgres psql -d rascal -f scripts/postgres_trgm.sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS ix_movies_title_trgm ON movies USING gin (lower(title) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_people_name_trgm ON people USING gin (lower(name) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_companies_name_trgm ON companies USING gin (lower(name) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_users_username_trgm ON users USING gin (lower(username) gin_trgm_ops);
ANALYZE movies; ANALYZE people;
