-- Nydus webhook custom-domain support (apply on the nydus database, after/with
-- 2026-06-13_custom_domains.sql). Safe to run once; additive.
--
-- A GitHub webhook must resolve to the right live deployment. Custom-domain deployments
-- have subdomain = NULL, so the webhook project needs to carry the full fqdn to look the
-- deployment up. Backfill keeps existing (subdomain) webhooks working unchanged.

ALTER TABLE `webhook_projects`
  ADD COLUMN `fqdn` varchar(255) NULL AFTER `subdomain`;

UPDATE `webhook_projects`
  SET `fqdn` = CONCAT(`subdomain`, '.arvo.team')
  WHERE `fqdn` IS NULL AND `subdomain` IS NOT NULL;
