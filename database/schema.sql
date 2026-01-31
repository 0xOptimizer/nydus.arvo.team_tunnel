CREATE TABLE IF NOT EXISTS usage_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    cpu_percent REAL,
    ram_percent REAL,
    disk_percent REAL,
    active_connections INTEGER
);

CREATE TABLE IF NOT EXISTS deployment_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_name TEXT,
    status TEXT,
    triggered_by TEXT,
    output_log TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS system_stats (
    key TEXT PRIMARY KEY,
    value TEXT,
    last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS webhook_projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    webhook_uuid TEXT UNIQUE,
    project_name TEXT,
    github_repository_url TEXT,
    branch TEXT DEFAULT 'main',
    deploy_path TEXT,
    webhook_secret TEXT,
    last_deployed_at TIMESTAMP
);