-- Alerts feed (frontend-first notifications). Every monitoring/deploy event is recorded
-- here for the dashboard; Discord is secondary and only used for is_critical=1 events.

CREATE TABLE IF NOT EXISTS `alerts` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `alert_uuid` char(36) NOT NULL,
  `level` varchar(16) NOT NULL DEFAULT 'info',   -- info | success | warning | error | critical
  `source` varchar(64) DEFAULT NULL,             -- deploy | rebuild | watchdog | monitor | database | webhook
  `title` varchar(255) NOT NULL,
  `message` text DEFAULT NULL,
  `target` varchar(255) DEFAULT NULL,            -- affected subdomain / service / etc.
  `is_critical` tinyint(1) NOT NULL DEFAULT 0,   -- whether it was also sent to Discord
  `acknowledged_at` timestamp NULL DEFAULT NULL,
  `created_at` timestamp NOT NULL DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `alert_uuid` (`alert_uuid`),
  KEY `idx_created` (`created_at`),
  KEY `idx_ack` (`acknowledged_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
