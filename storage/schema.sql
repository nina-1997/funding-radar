-- Funding Radar: схема DuckDB. Всё время — UTC.
-- Составные PRIMARY KEY = «без дублей»: повторная загрузка обновляет строку, а не добавляет новую.

CREATE TABLE IF NOT EXISTS instruments (
    exchange            VARCHAR NOT NULL,
    symbol              VARCHAR NOT NULL,   -- единое имя: BTC
    base                VARCHAR,
    quote               VARCHAR,
    exchange_symbol     VARCHAR,            -- как у биржи: BTCUSDT / BTC-USDT-SWAP
    funding_interval_h  DOUBLE,
    is_active           BOOLEAN,
    PRIMARY KEY (exchange, symbol)
);

CREATE TABLE IF NOT EXISTS funding (
    exchange         VARCHAR NOT NULL,
    symbol           VARCHAR NOT NULL,
    ts               TIMESTAMP NOT NULL,    -- 5-минутный слот сбора (или момент расчёта для истории)
    funding_rate     DOUBLE NOT NULL,       -- доля за 8 ч, нормализованная
    mark_price       DOUBLE,
    index_price      DOUBLE,
    next_funding_ts  TIMESTAMP,
    raw_rate         DOUBLE,                -- как отдала биржа
    raw_interval_h   DOUBLE,                -- интервал начисления у биржи
    PRIMARY KEY (exchange, symbol, ts)
);

CREATE TABLE IF NOT EXISTS open_interest (
    exchange      VARCHAR NOT NULL,
    symbol        VARCHAR NOT NULL,
    ts            TIMESTAMP NOT NULL,
    oi_contracts  DOUBLE,                   -- в базовой монете (BTC, ETH, ...)
    oi_usd        DOUBLE,
    PRIMARY KEY (exchange, symbol, ts)
);

CREATE TABLE IF NOT EXISTS klines (
    exchange      VARCHAR NOT NULL,
    symbol        VARCHAR NOT NULL,
    ts            TIMESTAMP NOT NULL,       -- время открытия свечи
    "interval"    VARCHAR NOT NULL,         -- '5m'
    open          DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
    volume        DOUBLE,                   -- в базовой монете
    quote_volume  DOUBLE,                   -- в USDT
    trades        BIGINT,                   -- есть только у Binance
    PRIMARY KEY (exchange, symbol, ts, "interval")
);

CREATE TABLE IF NOT EXISTS spreads (
    symbol         VARCHAR NOT NULL,
    ts             TIMESTAMP NOT NULL,
    max_rate       DOUBLE, min_rate DOUBLE,
    spread_bp      DOUBLE,                  -- (max - min) * 10 000
    exchange_hi    VARCHAR, exchange_lo VARCHAR,
    annualized_hi  DOUBLE, annualized_lo DOUBLE,   -- доля в год: rate * 3 * 365
    PRIMARY KEY (symbol, ts)
);

CREATE SEQUENCE IF NOT EXISTS seq_anomalies;
CREATE TABLE IF NOT EXISTS anomalies (
    id           BIGINT PRIMARY KEY DEFAULT nextval('seq_anomalies'),
    ts           TIMESTAMP NOT NULL,        -- начало эпизода
    symbol       VARCHAR NOT NULL,
    exchange     VARCHAR,
    type         VARCHAR NOT NULL,          -- volume / oi / spread
    value        DOUBLE,                    -- последнее значение метрики
    zscore       DOUBLE,
    window_min   INTEGER,                   -- окно расчёта, мин (в плане колонка называлась window — это ключевое слово SQL)
    last_seen_ts TIMESTAMP,
    resolved_ts  TIMESTAMP                  -- NULL = эпизод ещё идёт
);

CREATE SEQUENCE IF NOT EXISTS seq_alerts;
CREATE TABLE IF NOT EXISTS alerts (
    id              BIGINT PRIMARY KEY DEFAULT nextval('seq_alerts'),
    anomaly_id      BIGINT,
    kind            VARCHAR,                -- open / close
    sent_ts         TIMESTAMP,
    chat_id         VARCHAR,
    message         VARCHAR,
    ai_explanation  VARCHAR
);

CREATE SEQUENCE IF NOT EXISTS seq_runs;
CREATE TABLE IF NOT EXISTS collector_runs (
    id         BIGINT PRIMARY KEY DEFAULT nextval('seq_runs'),
    collector  VARCHAR,                     -- binance.funding, okx.klines, ...
    started    TIMESTAMP,
    finished   TIMESTAMP,
    status     VARCHAR,                     -- ok / error
    rows       INTEGER,
    error      VARCHAR
);

-- Служебные таблицы (сверх плана)
CREATE TABLE IF NOT EXISTS settings (       -- пороги, изменённые через /alerts; chat_id
    key VARCHAR PRIMARY KEY, value VARCHAR
);

CREATE TABLE IF NOT EXISTS ai_calls (       -- кэш ответов модели и лог стоимости
    hash        VARCHAR PRIMARY KEY,
    ts          TIMESTAMP,
    kind        VARCHAR,
    model       VARCHAR,
    response    VARCHAR,
    in_tokens   INTEGER, out_tokens INTEGER,
    cost_usd    DOUBLE
);

CREATE TABLE IF NOT EXISTS regime_log (     -- эксперимент: правило против модели
    ts TIMESTAMP PRIMARY KEY,
    rule_regime VARCHAR, llm_regime VARCHAR,
    avg_funding DOUBLE, oi_change_24h_pct DOUBLE, mean_corr DOUBLE
);
