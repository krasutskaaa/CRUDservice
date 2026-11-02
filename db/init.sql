-- Схема накочується автоматично при першому старті порожнього тому
-- (docker-entrypoint-initdb.d). Повторно не виконується, доки том живий.
CREATE TABLE IF NOT EXISTS admissions (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    patient_code   VARCHAR(32)  NOT NULL,
    department     VARCHAR(64)  NOT NULL,
    room           VARCHAR(16)  NOT NULL,
    admission_date DATE         NOT NULL,
    discharge_date DATE,
    outcome        VARCHAR(32)  NOT NULL,
    CONSTRAINT admissions_dates_chk
        CHECK (discharge_date IS NULL OR discharge_date >= admission_date),
    CONSTRAINT admissions_outcome_chk
        CHECK (outcome IN ('на лікуванні', 'одужання', 'покращення',
                           'переведення', 'летальний наслідок')),
    CONSTRAINT admissions_nonempty_chk
        CHECK (patient_code <> '' AND department <> '' AND room <> '')
);

-- Фільтр за відділенням + стабільний порядок id для пагінації
CREATE INDEX IF NOT EXISTS idx_admissions_department_id
    ON admissions (department, id);
