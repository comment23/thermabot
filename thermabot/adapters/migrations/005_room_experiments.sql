CREATE TABLE room_experiments (
  id INTEGER PRIMARY KEY,
  room TEXT NOT NULL,
  name TEXT NOT NULL,
  change_day TEXT NOT NULL,
  created_at TEXT NOT NULL,
  settings_json TEXT NOT NULL
);
