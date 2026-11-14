-- What PostgreSQL's row-level security does to a login's reads, and what `row_security = off` and
-- `row_security_active()` say about it. On a throwaway server, as its superuser:
--
--   docker run -d --name probe-pg -e POSTGRES_PASSWORD=probe postgres:15-alpine
--   docker exec -i probe-pg psql -U postgres -At < probe_postgres_row_security.sql \
--       > postgres-row-security-15.txt 2>&1
\set ON_ERROR_STOP off
CREATE ROLE probe_owner LOGIN;
CREATE ROLE probe_bypass LOGIN BYPASSRLS;
CREATE ROLE probe_reader LOGIN;
CREATE SCHEMA probe AUTHORIZATION probe_owner;
GRANT USAGE ON SCHEMA probe TO probe_bypass, probe_reader;
SET ROLE probe_owner;
CREATE TABLE probe.t (id bigint PRIMARY KEY, value int);
INSERT INTO probe.t SELECT g, g FROM generate_series(1, 100) g;
GRANT SELECT ON probe.t TO probe_bypass, probe_reader;
ALTER TABLE probe.t ENABLE ROW LEVEL SECURITY;
CREATE POLICY few ON probe.t USING (id <= 10);
\echo '== the owner, the policy enabled and not forced'
SELECT 'rows', count(*) FROM probe.t;
SELECT 'row_security_active', row_security_active('probe.t');
ALTER TABLE probe.t FORCE ROW LEVEL SECURITY;
\echo '== the owner, the policy forced'
SELECT 'rows', count(*) FROM probe.t;
SELECT 'row_security_active', row_security_active('probe.t');
SET row_security = off;
\echo '== the owner, the policy forced, row_security off'
SELECT 'rows', count(*) FROM probe.t;
COPY (SELECT count(*) FROM probe.t) TO STDOUT;
INSERT INTO probe.t VALUES (5000, 1);
SELECT 'row_security_active', row_security_active('probe.t');
RESET row_security;
RESET ROLE;
SET ROLE probe_reader;
\echo '== a grantee the policy applies to'
SELECT 'rows', count(*) FROM probe.t;
SET row_security = off;
\echo '== the same, row_security off'
SELECT 'rows', count(*) FROM probe.t;
RESET row_security;
RESET ROLE;
SET ROLE probe_bypass;
\echo '== a role with BYPASSRLS'
SELECT 'rows', count(*) FROM probe.t;
SELECT 'row_security_active', row_security_active('probe.t');
SET row_security = off;
SELECT 'rows, row_security off', count(*) FROM probe.t;
RESET row_security;
RESET ROLE;
\echo '== the superuser'
SELECT 'rows', count(*) FROM probe.t;
SELECT 'row_security_active', row_security_active('probe.t');
SELECT version();
