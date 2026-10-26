-- =============================================================================
-- 电商销量预测系统 · SQLite 数据库建表脚本
-- -----------------------------------------------------------------------------
-- 版本      : V1.0
-- 依据文档  : 《开发技术文档 V1.0》第 3 章
-- 上游依据  : PRD V1.0 §3（FR-*）/ §4（NFR-*）；开发方案文档 V1.1 §5.2
-- 执行方式  : sqlite3 app.db < schema.sql
-- 注意事项  :
--   1. 所有时间字段统一为 TEXT，格式 'YYYY-MM-DD HH:MM:SS'（本地时区，不转 UTC）
--      依据 NFR-COMP-04「全部日期按本地时区处理，不引入 UTC 转换」
--   2. 不使用 SQLite 的 DATETIME 类型，避免驱动层隐式时区转换
--   3. 所有查询必须携带 user_id 过滤条件（NFR-SEC-02 数据隔离红线）
-- =============================================================================

PRAGMA foreign_keys = ON;

-- -----------------------------------------------------------------------------
-- T01 · users 用户表                                    （FR-USR-01 ~ FR-USR-07）
-- -----------------------------------------------------------------------------
-- token_version 的用途（TQ-1 决议，见《数据库设计文档 V1.0》§10.3）：
--   JWT 是无状态的，签发后无法从服务端「撤回」单个 Token。FR-USR-07 要求
--   「修改密码后原 Token 失效并强制重新登录」——若只有黑名单，就必须枚举该
--   用户已签发的所有 Token（不可能）。故在 users 上维护一个版本号：
--     · 登录/签发时：JWT 载荷写入 ver = users.token_version
--     · 校验时    ：比对载荷 ver 与库中 token_version，不等即 401（TOKEN_EXPIRED）
--     · 改密码时  ：token_version = token_version + 1，该用户全部旧 Token 立即失效
--   登出仍走内存 jti 黑名单（BE-19 方案 B），因为它只影响单个 Token 且需即时生效。
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT    NOT NULL,                       -- 登录名，全局唯一
    password_hash   TEXT    NOT NULL,                       -- bcrypt 哈希串，禁止明文（FR-USR-03）
    nickname        TEXT    DEFAULT NULL,                   -- 展示名，可为空
    status          INTEGER NOT NULL DEFAULT 1              -- 1 正常 / 0 停用（管理端可切换，CH-01）
                    CHECK (status IN (0, 1)),
    role            TEXT    NOT NULL DEFAULT 'user'         -- 角色：user 普通用户 / admin 管理员（CH-01）
                    CHECK (role IN ('user', 'admin')),
    fail_count      INTEGER NOT NULL DEFAULT 0,             -- 连续密码错误次数（FR-USR-06 限流）
    locked_until    TEXT    DEFAULT NULL,                   -- 冷却截止时间，NULL=未锁定
    last_login_at   TEXT    DEFAULT NULL,
    token_version   INTEGER NOT NULL DEFAULT 1              -- JWT 版本号；改密时 +1 使旧 Token 立即失效
                    CHECK (token_version >= 1),             -- （FR-USR-07 / TQ-1 决议）
    created_at      TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- email 唯一性：使用 NOCASE 保证 Alice@x.com 与 alice@x.com 视为同一账号
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_email ON users (email COLLATE NOCASE);

-- -----------------------------------------------------------------------------
-- T01a · 游客哨兵账号（id = 0）                     （FR-USR-08 / JT-10）
-- -----------------------------------------------------------------------------
-- 为什么需要这一行：
--   未登录用户要能上传数据并跑到结果页（FR-USR-08），因此 datasets.user_id /
--   tasks.user_id 必须能表达「不属于任何注册用户」。若只把列声明为 NULL，就
--   失去了 user_id 作为隔离第一道闸门的一致性（所有 DAO 都依赖它是整数）。
--   故约定 **user_id = 0 即游客**，并在 users 表落一条真实的哨兵行——因为
--   PRAGMA foreign_keys = ON 下，user_id = 0 若无对应 users.id 会被外键直接拒绝。
--
-- 为什么这是安全的：
--   1. password_hash 写 '!'，不是合法 bcrypt 串。bcrypt.checkpw 对它必然返回
--      False（且抛 ValueError），此账号**永远无法登录**。Django 的 unusable
--      password 采用同一手法。
--   2. email 用 RFC 2606 保留域 .invalid，DNS 永不解析，与真实邮箱不可能冲突。
--   3. id = 0 不会被 AUTOINCREMENT 分配（自增从 1 开始），不存在覆盖风险。
--   4. 本行**不参与业务**：历史列表按 user_id = 当前用户过滤，游客任务天然不可见。
-- -----------------------------------------------------------------------------
INSERT OR IGNORE INTO users (id, email, password_hash, nickname, status)
VALUES (0, '__guest__@local.invalid', '!', '游客（未登录）', 1);

-- -----------------------------------------------------------------------------
-- T02 · datasets 数据集表                               （FR-UPL-01 ~ FR-UPL-15）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS datasets (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL,                   -- 归属，隔离红线；0 = 游客哨兵（FR-USR-08）
    file_name           TEXT    NOT NULL,                   -- 用户原始文件名（仅展示用）
    stored_name         TEXT    NOT NULL,                   -- 落盘文件名（重命名，NFR-SEC-07）
    file_path           TEXT    NOT NULL,                   -- 相对工作区根目录的路径
    file_size           INTEGER DEFAULT NULL,               -- 字节
    row_count           INTEGER DEFAULT NULL,               -- 有效数据行数
    sku_count           INTEGER DEFAULT NULL,               -- 去重商品数
    warehouse_count     INTEGER DEFAULT NULL,               -- 去重仓库数（仓库列必填）
    valid_series_count  INTEGER DEFAULT NULL,               -- 有效序列数（sku x warehouse 组合）
    date_start          TEXT    DEFAULT NULL,               -- 数据起始日期 YYYY-MM-DD
    date_end            TEXT    DEFAULT NULL,               -- 数据结束日期 YYYY-MM-DD
    gap_filled          INTEGER NOT NULL DEFAULT 0,         -- 是否执行过插值补齐 0/1（FR-UPL-13）
    gap_count           INTEGER NOT NULL DEFAULT 0,         -- 补齐的断档天数合计
    is_sample           INTEGER NOT NULL DEFAULT 0          -- 是否内置示例数据（FR-UPL-02）
                        CHECK (is_sample IN (0, 1)),
    status              INTEGER NOT NULL DEFAULT 1          -- 1 可用 / -1 已失效
                        CHECK (status IN (1, -1)),
    created_at          TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at          TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_datasets_user ON datasets (user_id, created_at DESC);

-- -----------------------------------------------------------------------------
-- T03 · tasks 预测任务表                              （FR-MOD-01 ~ FR-MOD-15）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tasks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL,                   -- 归属；0 = 游客哨兵（FR-USR-08）
    dataset_id          INTEGER NOT NULL,                   -- 输入数据集
    task_name           TEXT    DEFAULT NULL,               -- 用户可命名，空则后端生成
    granularity         TEXT    NOT NULL DEFAULT 'warehouse_sku'
                        CHECK (granularity IN ('warehouse_sku')),  -- 固定主粒度
    horizon             INTEGER NOT NULL                    -- 预测步长
                        CHECK (horizon IN (7, 14, 30)),
    use_holiday_feature INTEGER NOT NULL DEFAULT 1
                        CHECK (use_holiday_feature IN (0, 1)),
    status              TEXT    NOT NULL DEFAULT 'created'
                        CHECK (status IN ('created', 'running', 'succeeded', 'failed')),
    error_code          TEXT    DEFAULT NULL,               -- E_TIMEOUT / E_TOO_MANY_SERIES / E_INFER_FAIL
    error_msg           TEXT    DEFAULT NULL,
    completed_stage     TEXT    DEFAULT NULL,               -- 软失败时已完成阶段（FR-SYS-02 软失败）
    model_version       TEXT    NOT NULL,                   -- 必填：在线无「本次训练的模型」
    device              TEXT    DEFAULT NULL,               -- cuda / cpu，含回退后实际设备
    device_fallback     INTEGER NOT NULL DEFAULT 0,         -- 是否发生 CUDA->CPU 回退（FR-MOD-10）
    series_total        INTEGER DEFAULT NULL,               -- 待预测序列数
    l1_count            INTEGER DEFAULT NULL,               -- 覆盖率披露（FR-VIS-07）
    l2_count            INTEGER DEFAULT NULL,
    l3_count            INTEGER DEFAULT NULL,
    duration_ms         INTEGER DEFAULT NULL,               -- 端到端耗时，验证 <= 60s 硬指标
    started_at          TEXT    DEFAULT NULL,
    finished_at         TEXT    DEFAULT NULL,
    is_deleted          INTEGER NOT NULL DEFAULT 0          -- 软删除（FR-HIS-05）
                        CHECK (is_deleted IN (0, 1)),
    created_at          TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at          TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (user_id)    REFERENCES users (id)    ON DELETE CASCADE,
    FOREIGN KEY (dataset_id) REFERENCES datasets (id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS ix_tasks_user_list ON tasks (user_id, is_deleted, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_tasks_status    ON tasks (status);

-- -----------------------------------------------------------------------------
-- T04 · predictions 预测结果表                          （FR-VIS-01 ~ FR-VIS-05）
-- -----------------------------------------------------------------------------
-- 说明：一行 = 一条序列的一个预测日。113 序列 x 14 天 = 1,582 行（示例规模）
--       故本表写入必须批量 INSERT，禁止逐行 commit（NFR-REL-04）
CREATE TABLE IF NOT EXISTS predictions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id           INTEGER NOT NULL,
    sku_id            TEXT    NOT NULL,                     -- 商品编码
    warehouse_id      TEXT    NOT NULL,                     -- 仓库编码（PRD 必填，不可空）
    forecast_date     TEXT    NOT NULL,                     -- 预测目标日期 YYYY-MM-DD
    predicted_value   REAL    NOT NULL,                     -- 预测销量
    actual_value      REAL    DEFAULT NULL,                 -- 测试集回填真值（回测时才有）
    baseline_value    REAL    DEFAULT NULL,                 -- 移动平均基线值（同预测点）
    granularity_level TEXT    NOT NULL                      -- 建模层级，决定置信度
                      CHECK (granularity_level IN ('L1', 'L2', 'L3')),
    lower_bound       REAL    DEFAULT NULL,                 -- 置信下界（P2）
    upper_bound       REAL    DEFAULT NULL,                 -- 置信上界（P2）
    created_at        TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (task_id) REFERENCES tasks (id) ON DELETE CASCADE
);

-- 联合唯一索引：同一任务内 (仓库, 商品, 日期) 不得重复
CREATE UNIQUE INDEX IF NOT EXISTS ux_predictions_key
    ON predictions (task_id, warehouse_id, sku_id, forecast_date);
-- 结果页按序列聚合查询；导出按此顺序排序（FR-VIS-05 列序固定）
CREATE INDEX IF NOT EXISTS ix_predictions_task_sku
    ON predictions (task_id, sku_id, warehouse_id);

-- -----------------------------------------------------------------------------
-- T05 · metrics 任务指标表（与 tasks 一对一）              （FR-MOD-11 ~ FR-MOD-13）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS metrics (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             INTEGER NOT NULL UNIQUE,            -- 一对一
    -- 主指标（加权口径，PRD §3.4.3）
    mape                REAL DEFAULT NULL,                  -- 加权 MAPE = sum|e| / sum|y|，硬指标 <= 20
    mae                 REAL DEFAULT NULL,                  -- 加权 MAE
    rmse                REAL DEFAULT NULL,                  -- 加权 RMSE
    -- 移动平均基线（必须同预测点、同序列集合，FR-MOD-06）
    baseline_mape       REAL DEFAULT NULL,
    baseline_mae        REAL DEFAULT NULL,
    baseline_rmse       REAL DEFAULT NULL,
    -- 相对基线提升幅度
    mae_improve_pct     REAL DEFAULT NULL,                  -- 硬指标 >= 15
    rmse_improve_pct    REAL DEFAULT NULL,                  -- 硬指标 >= 15
    -- 达标序列占比（辅助指标 >= 60）
    series_evaluated    INTEGER DEFAULT NULL,               -- 参与评估序列数（分母）
    series_passed       INTEGER DEFAULT NULL,               -- 逐序列 MAPE <= 20 的序列数
    series_pass_rate    REAL    DEFAULT NULL,               -- 占比，百分数
    -- 分组指标：L1/L2 必须分开统计，不得混算后宣称达标
    l1_mape             REAL DEFAULT NULL,
    l2_mape             REAL DEFAULT NULL,
    -- 判定与披露
    is_over_threshold   INTEGER NOT NULL DEFAULT 0          -- 加权 MAPE 是否超 20%（FR-MOD-12）
                        CHECK (is_over_threshold IN (0, 1)),
    eval_note           TEXT    DEFAULT NULL,               -- 评估前提说明（FR-MOD-13）
    distribution_json   TEXT    DEFAULT NULL,               -- 逐序列 MAPE 分布（直方图用，JSON）
    timing_json         TEXT    DEFAULT NULL,               -- 分阶段耗时 JSON（进度页用）
    created_at          TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (task_id) REFERENCES tasks (id) ON DELETE CASCADE
);

-- -----------------------------------------------------------------------------
-- T06 · model_registry 模型版本表                        （FR-MOD-05 / FR-MOD-07）
-- -----------------------------------------------------------------------------
-- 在线推理完全依赖本表取权重，故 is_active = 1 的记录必须存在且唯一
CREATE TABLE IF NOT EXISTS model_registry (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    version           TEXT    NOT NULL UNIQUE,              -- 如 lstm_v1
    file_path         TEXT    NOT NULL,                     -- 权重 bundle 相对路径
    -- 超参数留存（与 app/algo/config.py 生产配置一致）
    window_size       INTEGER NOT NULL,
    hidden_size       INTEGER NOT NULL,
    num_layers        INTEGER NOT NULL,
    dropout           REAL    NOT NULL,
    learning_rate     REAL    NOT NULL,
    batch_size        INTEGER NOT NULL,
    -- 训练上下文
    train_data_range  TEXT    DEFAULT NULL,                 -- 训练数据时间范围
    train_row_count   INTEGER DEFAULT NULL,
    series_count      INTEGER DEFAULT NULL,                 -- 参与训练的序列数
    -- 验证集指标（加权口径）
    val_mae           REAL    DEFAULT NULL,
    val_rmse          REAL    DEFAULT NULL,
    val_mape          REAL    DEFAULT NULL,
    -- 环境与资源
    device            TEXT    DEFAULT NULL,                 -- cuda / cpu
    peak_memory_mb    REAL    DEFAULT NULL,                 -- 训练峰值显存
    param_count       INTEGER DEFAULT NULL,                 -- 参数量，便于判断可在线复现
    -- 状态
    is_active         INTEGER NOT NULL DEFAULT 0            -- 是否当前启用版本，全局唯一为 1
                      CHECK (is_active IN (0, 1)),
    trained_at        TEXT    DEFAULT NULL,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 部分唯一索引：保证有且仅有一个启用版本（FR-MOD-05 验收要点）
CREATE UNIQUE INDEX IF NOT EXISTS ux_model_active
    ON model_registry (is_active) WHERE is_active = 1;

-- -----------------------------------------------------------------------------
-- T07 · operation_logs 操作日志表                              （FR-SYS-03）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS operation_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER DEFAULT NULL,                       -- 未登录操作（如注册失败）可为 NULL
    action      TEXT    NOT NULL                            -- 8 类操作（FR-SYS-03 四类核心动作的超集：login/upload/predict/export；
                --  + register/logout/delete；+ admin = 管理端子动作，CH-01）
                --  管理端子动作（停用/启用用户、重置密码、强制删除任务、切换模型版本）
                --  统一记 action='admin'，细分动作写入 detail（QD-02 方案 A）
                CHECK (action IN ('login', 'register', 'logout', 'upload', 'predict', 'export', 'delete', 'admin')),
    target_id   TEXT    DEFAULT NULL,                       -- 关联对象 id（dataset_id / task_id）
    result      TEXT    NOT NULL DEFAULT 'success'
                CHECK (result IN ('success', 'fail')),
    detail      TEXT    DEFAULT NULL,                       -- 错误摘要等
    ip          TEXT    DEFAULT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS ix_oplogs_user_time ON operation_logs (user_id, created_at DESC);

-- -----------------------------------------------------------------------------
-- T08 · events 埋点事件表                                  （PRD §5.1 EV-*）
-- -----------------------------------------------------------------------------
-- 独立于 operation_logs：前者面向「排障审计」，后者面向「指标分析」，生命周期不同
-- 写入失败仅记日志不抛异常（PRD §5.1 埋点实现要求，不得阻塞主流程）
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_name  TEXT    NOT NULL,                           -- 如 EV-PRED-02 -> prediction_complete
    user_id     INTEGER DEFAULT NULL,
    task_id     INTEGER DEFAULT NULL,
    properties  TEXT    DEFAULT NULL,                       -- JSON 字符串
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS ix_events_name_time ON events (event_name, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_events_task      ON events (task_id);

-- -----------------------------------------------------------------------------
-- T09 · revoked_tokens Token 黑名单表                    （R-03 / BE-19，2026-10-26）
-- -----------------------------------------------------------------------------
-- 为什么从「进程内存」改成落库：
--   内存版在两种真实场景下会静默失效 ——
--     ① 进程重启 → 已登出的 Token 在重启后又"活"回来（直到 exp 自然过期）；
--     ② 多 worker / 多实例 → 在 A 进程登出，旧 Token 打到 B 进程照样放行。
--   两者都不会报错，只会让「登出」这个动作变得不可靠。
-- `jti` 作主键：同一 Token 重复登出天然幂等（INSERT OR IGNORE）。
-- `expires_at` 与 JWT 的 `exp` 同源（Unix 秒），过期行由启动时的清理任务删除，
--   故常驻规模上限 = 「同时段内登出的 Token 数」，不会无限增长。
CREATE TABLE IF NOT EXISTS revoked_tokens (
    jti         TEXT    PRIMARY KEY,                        -- JWT 的 jti（32 位 hex）
    user_id     INTEGER DEFAULT NULL,                       -- 归属用户，便于排障；游客不签发 Token 故不受影响
    expires_at  INTEGER NOT NULL,                           -- Token 原 exp（Unix 秒）：到期即可删行
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS ix_revoked_tokens_exp ON revoked_tokens (expires_at);

-- =============================================================================
-- 触发器：自动维护 updated_at
-- =============================================================================

CREATE TRIGGER IF NOT EXISTS trg_users_updated
AFTER UPDATE ON users FOR EACH ROW
BEGIN
    UPDATE users SET updated_at = datetime('now', 'localtime') WHERE id = OLD.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_datasets_updated
AFTER UPDATE ON datasets FOR EACH ROW
BEGIN
    UPDATE datasets SET updated_at = datetime('now', 'localtime') WHERE id = OLD.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_tasks_updated
AFTER UPDATE ON tasks FOR EACH ROW
BEGIN
    UPDATE tasks SET updated_at = datetime('now', 'localtime') WHERE id = OLD.id;
END;

-- =============================================================================
-- 视图：任务结果摘要（供历史列表 / 结果页头部复用，避免前端多次拼接）
-- =============================================================================

CREATE VIEW IF NOT EXISTS v_task_summary AS
SELECT
    t.id                AS task_id,
    t.user_id,
    t.task_name,
    t.granularity,
    t.horizon,
    t.status,
    t.error_code,
    t.device,
    t.device_fallback,
    t.series_total,
    t.l1_count,
    t.l2_count,
    t.l3_count,
    t.duration_ms,
    t.model_version,
    t.created_at,
    t.finished_at,
    d.id                AS dataset_id,
    d.file_name         AS dataset_name,
    d.valid_series_count,
    m.mape              AS weighted_mape,
    m.mae               AS weighted_mae,
    m.rmse              AS weighted_rmse,
    m.baseline_mae,
    m.mae_improve_pct,
    m.rmse_improve_pct,
    m.series_pass_rate,
    m.is_over_threshold
FROM tasks t
LEFT JOIN datasets d ON d.id = t.dataset_id
LEFT JOIN metrics  m ON m.task_id = t.id
WHERE t.is_deleted = 0;
