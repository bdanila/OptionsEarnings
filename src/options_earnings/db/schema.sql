CREATE TABLE IF NOT EXISTS symbols (
    symbol         VARCHAR PRIMARY KEY,
    company_name   VARCHAR NOT NULL,
    sector         VARCHAR,
    market_cap     BIGINT,
    last_price     DOUBLE,
    next_earnings  DATE,
    earnings_when  VARCHAR,
    refreshed_at   TIMESTAMP NOT NULL
);
ALTER TABLE symbols ADD COLUMN IF NOT EXISTS iv_monitored BOOLEAN;

CREATE TABLE IF NOT EXISTS option_chain_jobs (
    job_id        UUID PRIMARY KEY,
    created_at    TIMESTAMP NOT NULL,
    symbols       VARCHAR[] NOT NULL,
    window_size   INTEGER NOT NULL,
    status        VARCHAR NOT NULL,
    error         VARCHAR,
    completed_at  TIMESTAMP
);

CREATE TABLE IF NOT EXISTS option_quotes (
    job_id        UUID NOT NULL,
    symbol        VARCHAR NOT NULL,
    snapshot_ts   TIMESTAMP NOT NULL,
    underlying    DOUBLE NOT NULL,
    expiry        DATE NOT NULL,
    strike        DOUBLE NOT NULL,
    cp            VARCHAR NOT NULL,
    bid           DOUBLE,
    ask           DOUBLE,
    last          DOUBLE,
    volume        INTEGER,
    open_interest INTEGER,
    iv_yahoo      DOUBLE,
    iv_computed   DOUBLE,
    PRIMARY KEY (job_id, symbol, expiry, strike, cp)
);

CREATE INDEX IF NOT EXISTS ix_quotes_symbol_ts ON option_quotes(symbol, snapshot_ts);
CREATE INDEX IF NOT EXISTS ix_quotes_symbol_expiry ON option_quotes(symbol, expiry);

CREATE TABLE IF NOT EXISTS earnings_moves (
    symbol           VARCHAR NOT NULL,
    earnings_date    DATE NOT NULL,
    ref_close        DOUBLE NOT NULL,
    max_up_3d_pct    DOUBLE NOT NULL,
    max_down_3d_pct  DOUBLE NOT NULL,
    computed_at      TIMESTAMP NOT NULL,
    PRIMARY KEY (symbol, earnings_date)
);
ALTER TABLE earnings_moves ADD COLUMN IF NOT EXISTS window_high_3d DOUBLE;
ALTER TABLE earnings_moves ADD COLUMN IF NOT EXISTS window_low_3d DOUBLE;
ALTER TABLE earnings_moves ADD COLUMN IF NOT EXISTS window_close_3d DOUBLE;
ALTER TABLE earnings_moves ADD COLUMN IF NOT EXISTS window_close_pct_3d DOUBLE;
CREATE INDEX IF NOT EXISTS ix_earnings_moves_symbol ON earnings_moves(symbol);

CREATE TABLE IF NOT EXISTS earnings_ohlc (
    symbol       VARCHAR NOT NULL,
    trading_day  DATE NOT NULL,
    open         DOUBLE,
    high         DOUBLE NOT NULL,
    low          DOUBLE NOT NULL,
    close        DOUBLE NOT NULL,
    PRIMARY KEY (symbol, trading_day)
);
CREATE INDEX IF NOT EXISTS ix_earnings_ohlc_symbol ON earnings_ohlc(symbol);

CREATE TABLE IF NOT EXISTS iv_rank_history (
    symbol       VARCHAR NOT NULL,
    snapshot_ts  TIMESTAMP NOT NULL,
    atm_iv       DOUBLE,
    iv_rank_2w   DOUBLE,
    PRIMARY KEY (symbol, snapshot_ts)
);
CREATE INDEX IF NOT EXISTS ix_iv_rank_history_symbol_ts ON iv_rank_history(symbol, snapshot_ts);
