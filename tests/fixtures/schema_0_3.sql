-- Schema of a real 0.3.0 database (migrated from 0.1/0.2), fixed DDL for migration tests.
CREATE TABLE samples (
    ts INTEGER NOT NULL,
    device_id TEXT NOT NULL,
    channel INTEGER NOT NULL,
    is_on INTEGER NOT NULL,
    power_w REAL NULL,
    online INTEGER NOT NULL,
    energy_wh REAL NULL
, home TEXT NOT NULL DEFAULT 'Home A');
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX idx_samples_home_dev_ch_ts ON samples (home, device_id, channel, ts);
CREATE TABLE devices (
    home TEXT NOT NULL,
    device_id TEXT NOT NULL,
    channel INTEGER NOT NULL DEFAULT 0,
    name TEXT,
    model TEXT,
    gen TEXT,
    room_id INTEGER,
    room TEXT,
    last_seen INTEGER,
    PRIMARY KEY (home, device_id, channel)
);
CREATE TABLE homes (
    key_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    server TEXT,
    unit_id INTEGER,
    discovered_name TEXT,
    name_override TEXT,
    updated INTEGER
);
INSERT INTO samples (ts, device_id, channel, is_on, power_w, online, energy_wh) VALUES
    (1000, 'aa', 0, 1, NULL, 1, NULL), (1060, 'aa', 0, 0, NULL, 1, NULL), (1000, 'bb', 1, 1, 5.0, 1, 12.5);
INSERT INTO samples (ts, device_id, channel, is_on, power_w, online, energy_wh, home) VALUES
    (1000, 'cc', 0, 1, NULL, 1, NULL, 'Borgo');
INSERT INTO devices VALUES ('Home A', 'aa', 0, 'Boiler', 'S3SW-001X8EU', 'G2+', 1, 'Cucina', 1060),
                           ('Borgo', 'cc', 0, NULL, 'SHSW-1', 'G1', NULL, NULL, 1000);
INSERT INTO homes VALUES ('0123456789ab', 'Home A', 's', 1234567, 'Home A', NULL, 1);
INSERT INTO meta VALUES ('lists_refreshed_at:0123456789ab', '1000');
