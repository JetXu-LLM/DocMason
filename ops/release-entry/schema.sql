CREATE TABLE IF NOT EXISTS release_current (
  distribution_channel TEXT PRIMARY KEY,
  latest_version TEXT NOT NULL,
  published_at TEXT NOT NULL,
  release_url TEXT NOT NULL,
  asset_url TEXT NOT NULL,
  asset_name TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_activity (
  event_day TEXT NOT NULL,
  installation_hash TEXT NOT NULL,
  distribution_channel TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  trigger TEXT NOT NULL,
  PRIMARY KEY (event_day, installation_hash, distribution_channel)
);
