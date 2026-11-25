-- Лабораторна 4: черга задач обробки в PostgreSQL (міграція виконується Job migrate після init.sql).
-- Усі команди ідемпотентні: повторний запуск Job нічого не ламає.
CREATE TABLE IF NOT EXISTS tasks (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    record_id    BIGINT       NOT NULL REFERENCES admissions (id) ON DELETE CASCADE,
    status       VARCHAR(16)  NOT NULL DEFAULT 'queued',
    seed         BIGINT       NOT NULL,
    attempts     INT          NOT NULL DEFAULT 0,
    max_attempts INT          NOT NULL DEFAULT 3,
    token        BIGINT       NOT NULL DEFAULT 0,
    lease_until  TIMESTAMPTZ,
    worker_id    VARCHAR(128),
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp(),
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ,
    CONSTRAINT tasks_status_chk CHECK (status IN ('queued', 'running', 'done', 'failed'))
);
CREATE INDEX IF NOT EXISTS idx_tasks_status_created ON tasks (status, created_at);

CREATE TABLE IF NOT EXISTS task_results (
    task_id    BIGINT       NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    token      BIGINT       NOT NULL,
    worker_id  VARCHAR(128) NOT NULL,
    result     JSONB        NOT NULL,
    created_at TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT task_results_pkey PRIMARY KEY (task_id)
);

-- Журнал воркерів у базі: stdout контейнера губиться після кількох перезапусків
-- (kubectl logs --previous зберігає лише один попередній), а за цією таблицею
-- рахуються дублі, повернення в чергу і рядки про нуль оновлених рядків.
CREATE TABLE IF NOT EXISTS task_events (
    id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id   BIGINT,
    token     BIGINT,
    worker_id VARCHAR(128) NOT NULL,
    event     VARCHAR(32)  NOT NULL,
    info      JSONB,
    at        TIMESTAMPTZ  NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS idx_task_events_at ON task_events (at);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events (task_id);

-- Налаштування воркерів, які можна змінити посеред прогону без перезапуску подів
-- (ключі lease_s, heartbeat, t_poll_ms, batch, claim_mode, store_mode, fail_rate).
-- Значення зі змінних середовища діють, доки ключа немає в таблиці.
CREATE TABLE IF NOT EXISTS worker_config (
    key   VARCHAR(32) PRIMARY KEY,
    value VARCHAR(64) NOT NULL
);
