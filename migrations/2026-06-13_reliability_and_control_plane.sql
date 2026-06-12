-- Nydus reliability + control-plane migration (apply on the nydus database)
-- Author: backend pass 2026-06-13. Apply BEFORE/with the matching code deploy.
-- Safe to run once; each statement is additive.

-- ---------------------------------------------------------------------------
-- [REQUIRED for this pass] deployments.status needs 'unhealthy'.
-- A deploy whose pipeline fully succeeds but whose HTTP health check doesn't
-- return 200 is recorded as 'unhealthy' (site is live, not rolled back). Without
-- this, the status UPDATE is rejected by the enum and the row is left 'pending'.
-- Note: we intentionally do NOT add a 'deleted' status — deployments.subdomain is
-- UNIQUE, so removed/reclaimed deployments are hard-deleted (history lives in
-- deployment_logs, which has no FK to deployments).
-- ---------------------------------------------------------------------------
ALTER TABLE `deployments`
  MODIFY `status` enum('pending','active','failed','unhealthy') NOT NULL DEFAULT 'pending';

-- ---------------------------------------------------------------------------
-- [Phase 3 — DB remote users] per-user allowed host(s), default remote ('%').
-- Used for CREATE USER / GRANT / DROP / REVOKE so created users are remote-capable
-- and create/grant/drop/revoke all target the same 'user'@'host'.
-- ---------------------------------------------------------------------------
ALTER TABLE `database_users`
  ADD COLUMN `allowed_hosts` varchar(255) NOT NULL DEFAULT '%';

-- Optional remediation for users created BEFORE this fix (they were created
-- 'user'@'localhost' and can't connect remotely). RENAME USER preserves their grants.
-- Run per affected user, then reflect it in metadata:
--   RENAME USER 'theuser'@'localhost' TO 'theuser'@'%';
--   UPDATE database_users SET allowed_hosts = '%' WHERE username = 'theuser';
-- (List localhost users with: SELECT user FROM mysql.user WHERE host = 'localhost';)

-- ---------------------------------------------------------------------------
-- [Phase 1C — control plane / adoption] managed_services registry.
-- Adopted/external services that nydus monitors + controls but did NOT create via the
-- deploy pipeline (the main site arvo.team, nydus-ui, the nydus bot, nginx). Kept in a
-- SEPARATE table from `deployments` because those are subdomain-shaped
-- ({sub}.arvo.team, subdomain NOT NULL UNIQUE) and the apex arvo.team / the bot don't fit.
-- NOTE: schema below is PROPOSED — confirm before relying on it.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `managed_services` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `service_uuid` char(36) NOT NULL,
  `name` varchar(100) NOT NULL,                                    -- display name, e.g. "arvo.team"
  `service_type` enum('pm2','systemd','nginx','static') NOT NULL,  -- how it's controlled
  `pm2_name` varchar(64) DEFAULT NULL,                             -- for service_type='pm2'
  `systemd_unit` varchar(128) DEFAULT NULL,                        -- for service_type='systemd' (the bot)
  `fqdn` varchar(255) DEFAULT NULL,                                -- public host for health, e.g. arvo.team
  `health_url` varchar(512) DEFAULT NULL,                          -- explicit health URL (else https://fqdn)
  `deploy_path` varchar(512) DEFAULT NULL,                         -- working dir for pull/build maintenance
  `port` int(11) DEFAULT NULL,                                     -- local port if applicable
  `git_url` varchar(512) DEFAULT NULL,                             -- for pull-and-rebuild
  `branch` varchar(100) DEFAULT NULL,
  `enabled` tinyint(1) NOT NULL DEFAULT 1,                         -- watchdog monitors it
  `created_at` timestamp NOT NULL DEFAULT current_timestamp(),
  `updated_at` timestamp NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `service_uuid` (`service_uuid`),
  UNIQUE KEY `name` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

-- Optional: seed the core adopted services so they appear in the control plane immediately.
-- Adjust pm2_name / systemd_unit / deploy_path / fqdn to match the server. (Or register them
-- via POST /api/services / GET /api/server/discover.) UUIDs are placeholders — replace them.
-- INSERT INTO managed_services (service_uuid, name, service_type, pm2_name, fqdn, health_url, deploy_path, git_url, branch) VALUES
--   ('11111111-1111-4111-8111-111111111111', 'arvo.team',  'pm2',     'arvo.team', 'arvo.team',          'https://arvo.team',          '/var/www/arvo.team', NULL, 'main'),
--   ('22222222-2222-4222-8222-222222222222', 'nydus-ui',   'pm2',     'nydus-ui',  'nydus.arvo.team',    'https://nydus.arvo.team',    '/var/www/nydus',     NULL, 'main');
-- INSERT INTO managed_services (service_uuid, name, service_type, systemd_unit) VALUES
--   ('33333333-3333-4333-8333-333333333333', 'nydus-bot', 'systemd', 'nydus');
-- INSERT INTO managed_services (service_uuid, name, service_type) VALUES
--   ('44444444-4444-4444-8444-444444444444', 'nginx', 'nginx');
