-- 韭菜称重 · 数据库结构 v1
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations(
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

-- 单管理员（只有一行）
CREATE TABLE IF NOT EXISTS admin(
  id INTEGER PRIMARY KEY CHECK (id = 1),
  username TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  token_version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

-- 家庭成员（统计对象，不是账号）
CREATE TABLE IF NOT EXISTS members(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  sex TEXT NOT NULL CHECK (sex IN ('male','female')),
  birth_year INTEGER NOT NULL,
  height_cm INTEGER NOT NULL,
  activity_level INTEGER NOT NULL DEFAULT 2,
  waist_cm INTEGER,
  color TEXT NOT NULL DEFAULT '#00A98F',
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

-- 测量：只存原始量，体成分实时算
CREATE TABLE IF NOT EXISTS measurements(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  member_id INTEGER REFERENCES members(id),
  device_mac TEXT NOT NULL,
  measured_at TEXT NOT NULL,
  weight_centi_kg INTEGER NOT NULL,
  impedances TEXT,
  impedance_used_ohm REAL NOT NULL DEFAULT 0,
  data_type INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'report',
  match_state TEXT NOT NULL DEFAULT 'auto',
  match_score REAL,
  excluded INTEGER NOT NULL DEFAULT 0,
  raw_frame_hex TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_m_time ON measurements(measured_at DESC);
CREATE INDEX IF NOT EXISTS idx_m_member ON measurements(member_id, measured_at DESC);
CREATE INDEX IF NOT EXISTS idx_m_state ON measurements(match_state);

-- 硬件上报按设备去重；补录/导入共用虚拟设备名，必须同时区分成员。
CREATE UNIQUE INDEX IF NOT EXISTS ux_m_hardware_dedup
  ON measurements(device_mac, measured_at, weight_centi_kg)
  WHERE device_mac NOT IN ('MANUAL','IMPORT');
CREATE UNIQUE INDEX IF NOT EXISTS ux_m_local_dedup
  ON measurements(device_mac, COALESCE(member_id,0), measured_at, weight_centi_kg)
  WHERE device_mac IN ('MANUAL','IMPORT');
-- 新索引建好后再移除旧索引，老库升级期间始终有去重约束。
DROP INDEX IF EXISTS ux_m_dedup;

CREATE TABLE IF NOT EXISTS goals(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  member_id INTEGER NOT NULL REFERENCES members(id),
  kind TEXT NOT NULL CHECK (kind IN ('weight','body_fat')),
  start_value REAL,
  target_value REAL NOT NULL,
  deadline TEXT,
  created_at TEXT NOT NULL,
  achieved_at TEXT,
  is_active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_goal_member ON goals(member_id, is_active);

CREATE TABLE IF NOT EXISTS device_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mac TEXT NOT NULL,
  kind TEXT NOT NULL,
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dev_at ON device_events(at DESC);

CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  action TEXT NOT NULL,
  target TEXT,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log(at DESC);

-- 设备名（用户可改，留空则按出现顺序显示「设备一 / 设备二」）
CREATE TABLE IF NOT EXISTS devices(
  mac TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  bind_mode TEXT NOT NULL DEFAULT 'all',   -- all=全部成员 / none=一个都不绑 / pick=指定
  hidden_at TEXT,                          -- 删除设备但保留数据时的隐藏时间点
  created_at TEXT NOT NULL
);

-- 设备与成员的绑定（多设备二级归档）：这台秤上通常是谁在称
CREATE TABLE IF NOT EXISTS device_members(
  device_mac TEXT NOT NULL,
  member_id INTEGER NOT NULL REFERENCES members(id),
  PRIMARY KEY (device_mac, member_id)
);

-- 默认设置
INSERT OR IGNORE INTO settings(key,value) VALUES
  ('grading','ace'),
  ('match_auto_kg','1'),
  ('match_ref_n','5'),
  ('backup_keep_days','30'),
  ('notify_enabled','0'),
  ('notify_events','measure.unassigned'),
  ('pushplus_token','');
