-- سجل المنظومة. متوافق مع SQLite محليًا ومع Postgres في Supabase.
-- كل جدول يحمل asset_class لتقبل المنظومة العملات لاحقًا دون تعديل.

CREATE TABLE IF NOT EXISTS opinions (
    id              INTEGER PRIMARY KEY,
    created_at      TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    asset_class     TEXT NOT NULL DEFAULT 'equity',
    source          TEXT NOT NULL,          -- jake / tradermonty / lens:<name> / anthropic:<skill>
    role            TEXT NOT NULL,          -- primary / secondary / lens
    direction       TEXT NOT NULL,
    entry           REAL,
    stop            REAL,
    target1         REAL,
    horizon_days    INTEGER,
    confidence      REAL,
    method          TEXT,
    payload_json    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verifications (
    id                    INTEGER PRIMARY KEY,
    created_at            TEXT NOT NULL,
    symbol                TEXT NOT NULL,
    asset_class           TEXT NOT NULL DEFAULT 'equity',
    decision              TEXT NOT NULL,
    worst_severity        TEXT NOT NULL,
    worst_size            TEXT NOT NULL,
    consensus_confidence  REAL,
    c_entry               REAL,
    c_stop                REAL,
    c_target              REAL,
    reward_risk           REAL,
    payload_json          TEXT NOT NULL
);

-- كل اقتراح عُرض على المالك
CREATE TABLE IF NOT EXISTS suggestions (
    id               INTEGER PRIMARY KEY,
    created_at       TEXT NOT NULL,
    verification_id  INTEGER REFERENCES verifications(id),
    symbol           TEXT NOT NULL,
    asset_class      TEXT NOT NULL DEFAULT 'equity',
    direction        TEXT NOT NULL,
    entry            REAL NOT NULL,
    stop             REAL NOT NULL,
    target           REAL NOT NULL,
    confidence       REAL NOT NULL,
    horizon_days     INTEGER NOT NULL,
    shares           REAL,
    amount_usd       REAL,
    max_loss_usd     REAL,
    status           TEXT NOT NULL DEFAULT 'open',  -- open / hit_target / hit_stop / expired / cancelled
    resolved_at      TEXT,
    resolved_price   REAL,
    notes            TEXT
);

-- ما نفّذه المالك فعلًا، مرتبط بالاقتراح إن وُجد
CREATE TABLE IF NOT EXISTS executions (
    id             INTEGER PRIMARY KEY,
    suggestion_id  INTEGER REFERENCES suggestions(id),
    symbol         TEXT NOT NULL,
    asset_class    TEXT NOT NULL DEFAULT 'equity',
    side           TEXT NOT NULL,      -- buy / sell
    qty            REAL NOT NULL,
    price          REAL NOT NULL,
    fees           REAL NOT NULL DEFAULT 0,
    executed_at    TEXT NOT NULL,
    notes          TEXT
);

CREATE INDEX IF NOT EXISTS idx_opinions_symbol ON opinions(symbol, created_at);
CREATE INDEX IF NOT EXISTS idx_suggestions_status ON suggestions(status);
CREATE INDEX IF NOT EXISTS idx_executions_symbol ON executions(symbol);

-- إشارات الارتداد اللحظي على المؤشرات (skills/reversal-detector): صفقة محاكاة أو ورقية أو فعلية لكل إشارة
CREATE TABLE IF NOT EXISTS intraday_signals (
    id                  INTEGER PRIMARY KEY,
    created_at          TEXT NOT NULL,      -- وقت الدخول
    symbol              TEXT NOT NULL,
    asset_class         TEXT NOT NULL DEFAULT 'index',
    side                TEXT NOT NULL,      -- call / put
    zone                TEXT,
    entry               REAL NOT NULL,
    result_pts          REAL,
    result_r            REAL,
    option_pnl_usd_est  REAL,
    closed_at           TEXT,
    mode                TEXT NOT NULL,      -- replay / paper / live
    pattern             TEXT,               -- failed_breakdown / failed_breakout / higher_low / lower_high
    contract_mode       TEXT NOT NULL DEFAULT 'standard',   -- standard / cheap
    payload_json        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intraday_mode ON intraday_signals(mode, created_at);
