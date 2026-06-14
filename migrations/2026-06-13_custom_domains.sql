-- Nydus custom-domains migration (apply on the nydus database).
-- Author: backend pass 2026-06-13. Apply BEFORE/with the matching code deploy.
-- Safe to run once; every statement is additive or a nullability relaxation.
--
-- Adds per-deployment dns_mode + a canonical `fqdn` + the client Cloudflare zone id,
-- then backfills existing rows so the current {subdomain}.arvo.team behavior is unchanged.
--
-- dns_mode:
--   subdomain  = {subdomain}.arvo.team in the arvo.team zone (today's behavior, default).
--   cloudflare = a custom domain automated in the CLIENT's Cloudflare zone (same API token).
--   external   = a custom domain whose DNS the client runs elsewhere; nydus does nginx+certbot only.

-- 1) How DNS is managed for this deployment.
ALTER TABLE `deployments`
  ADD COLUMN `dns_mode` enum('subdomain','cloudflare','external')
    NOT NULL DEFAULT 'subdomain' AFTER `subdomain`;

-- 2) Canonical public hostname. nginx server_name, the nginx config filename, the certbot
--    cert-name, and the health check all key on this. Nullable at the DB layer so this runs
--    in one shot; create_deployment() always populates it going forward.
ALTER TABLE `deployments`
  ADD COLUMN `fqdn` varchar(255) NULL AFTER `dns_mode`;

-- 3) Which Cloudflare zone holds this deployment's A record. NULL for subdomain mode
--    (code falls back to CLOUDFLARE_ZONE_ID) and for external mode (no Cloudflare at all).
ALTER TABLE `deployments`
  ADD COLUMN `cf_zone_id` varchar(64) NULL AFTER `fqdn`;

-- 4) Custom domains have no subdomain, so allow NULL. The UNIQUE key is preserved; MySQL
--    permits multiple NULLs under a UNIQUE index, so many custom-domain rows coexist.
--    IMPORTANT: this MODIFY must match the live column's EXACT type/charset and change ONLY
--    nullability. Adjust varchar(24) if the live column differs.
ALTER TABLE `deployments`
  MODIFY `subdomain` varchar(24) NULL;

-- 5) Backfill existing rows to the unchanged subdomain behavior. Run BEFORE step 6 so the
--    new UNIQUE key won't trip over pre-existing NULL fqdn values.
UPDATE `deployments`
  SET `fqdn` = CONCAT(`subdomain`, '.arvo.team'),
      `dns_mode` = 'subdomain'
  WHERE `fqdn` IS NULL AND `subdomain` IS NOT NULL;

-- 6) Canonical-identity UNIQUE on fqdn — the real collision guard going forward (one host =
--    one live deployment, regardless of dns_mode).
ALTER TABLE `deployments`
  ADD UNIQUE KEY `uq_deployments_fqdn` (`fqdn`);

-- OPTIONAL hardening, run only AFTER confirming no row has a NULL fqdn:
--   ALTER TABLE `deployments` MODIFY `fqdn` varchar(255) NOT NULL;
