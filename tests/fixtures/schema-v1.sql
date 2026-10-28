-- Known previous schema with a lower balance and a recorded operator halt.
PRAGMA application_id = 1296126550;
PRAGMA user_version = 1;
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE account (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    initial_equity TEXT NOT NULL, equity TEXT NOT NULL, high_water TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE halts (code TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT, occurred_at TEXT NOT NULL,
    event TEXT NOT NULL, payload TEXT NOT NULL);
INSERT INTO metadata VALUES ('config_hash', 'CONFIG_HASH');
INSERT INTO metadata VALUES ('mode', 'paper');
INSERT INTO metadata VALUES ('created_at', '2026-10-22T00:00:00.000000+00:00');
INSERT INTO account VALUES (1, '50', '45', '50', '2026-10-22T00:00:00.000000+00:00', '2026-10-22T01:00:00.000000+00:00');
INSERT INTO halts VALUES ('operator', '2026-10-22T01:00:00.000000+00:00');
INSERT INTO journal (occurred_at, event, payload) VALUES (
    '2026-10-22T00:00:00.000000+00:00', 'initialized',
    '{"mode":"paper","starting_equity_usd":"50","config_hash":"CONFIG_HASH"}');
INSERT INTO journal (occurred_at, event, payload) VALUES (
    '2026-10-22T01:00:00.000000+00:00', 'paused', '{"reason":"operator","revision":1}');
