-- COSMOS initial schema
-- Tables match app/db/models.py (14 tables)
-- MySQL 8.0+

-- 1. Sessions
CREATE TABLE IF NOT EXISTS icrm_sessions (
    id CHAR(36) PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    company_id VARCHAR(255),
    channel VARCHAR(50) NOT NULL DEFAULT 'web',
    status VARCHAR(50) NOT NULL DEFAULT 'active',
    metadata JSON,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE INDEX idx_sessions_user_id ON icrm_sessions(user_id);
CREATE INDEX idx_sessions_company_id ON icrm_sessions(company_id);
CREATE INDEX idx_sessions_user_status ON icrm_sessions(user_id, status);


-- 2. Messages
CREATE TABLE IF NOT EXISTS icrm_messages (
    id CHAR(36) PRIMARY KEY,
    session_id CHAR(36) NOT NULL,
    role ENUM('user','assistant','system','tool') NOT NULL,
    content TEXT NOT NULL,
    token_count INTEGER,
    model VARCHAR(100),
    metadata JSON,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_messages_session FOREIGN KEY (session_id) REFERENCES icrm_sessions(id) ON DELETE CASCADE
);

CREATE INDEX idx_messages_session_id ON icrm_messages(session_id);
CREATE INDEX idx_messages_session_created ON icrm_messages(session_id, created_at);


-- 3. Conversation Context
CREATE TABLE IF NOT EXISTS icrm_conversation_context (
    id CHAR(36) PRIMARY KEY,
    session_id CHAR(36) NOT NULL,
    intent VARCHAR(255),
    entities JSON,
    tool_state JSON,
    user_profile JSON,
    conversation_summary TEXT,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CONSTRAINT uq_conv_context_session UNIQUE (session_id),
    CONSTRAINT fk_conv_context_session FOREIGN KEY (session_id) REFERENCES icrm_sessions(id) ON DELETE CASCADE
);


-- 4. Reasoning Traces
CREATE TABLE IF NOT EXISTS icrm_reasoning_traces (
    id CHAR(36) PRIMARY KEY,
    session_id CHAR(36) NOT NULL,
    message_id CHAR(36),
    phase ENUM('reason','act','observe','evaluate','reflect') NOT NULL,
    content TEXT NOT NULL,
    duration_ms INTEGER,
    metadata JSON,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_traces_session FOREIGN KEY (session_id) REFERENCES icrm_sessions(id) ON DELETE CASCADE,
    CONSTRAINT fk_traces_message FOREIGN KEY (message_id) REFERENCES icrm_messages(id) ON DELETE SET NULL
);

CREATE INDEX idx_traces_session_id ON icrm_reasoning_traces(session_id);
CREATE INDEX idx_traces_session_phase ON icrm_reasoning_traces(session_id, phase);


-- 5. Tool Executions
CREATE TABLE IF NOT EXISTS icrm_tool_executions (
    id CHAR(36) PRIMARY KEY,
    session_id CHAR(36) NOT NULL,
    tool_name VARCHAR(255) NOT NULL,
    input_params JSON,
    output_result JSON,
    status ENUM('pending','running','success','failed','cancelled') NOT NULL DEFAULT 'pending',
    duration_ms INTEGER,
    error_message TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_tool_exec_session FOREIGN KEY (session_id) REFERENCES icrm_sessions(id) ON DELETE CASCADE
);

CREATE INDEX idx_tool_exec_session_id ON icrm_tool_executions(session_id);
CREATE INDEX idx_tool_exec_tool_name ON icrm_tool_executions(tool_name);
CREATE INDEX idx_tool_exec_session_status ON icrm_tool_executions(session_id, status);


-- 6. Action Approvals
CREATE TABLE IF NOT EXISTS icrm_action_approvals (
    id CHAR(36) PRIMARY KEY,
    session_id CHAR(36) NOT NULL,
    tool_execution_id CHAR(36),
    action_type VARCHAR(255) NOT NULL,
    risk_level ENUM('low','medium','high','critical') NOT NULL DEFAULT 'low',
    approval_mode ENUM('auto','manual','escalated') NOT NULL DEFAULT 'auto',
    approved TINYINT(1),
    approved_by VARCHAR(255),
    reason TEXT,
    metadata JSON,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP NULL,
    CONSTRAINT fk_approvals_session FOREIGN KEY (session_id) REFERENCES icrm_sessions(id) ON DELETE CASCADE,
    CONSTRAINT fk_approvals_tool_exec FOREIGN KEY (tool_execution_id) REFERENCES icrm_tool_executions(id) ON DELETE SET NULL
);

CREATE INDEX idx_approvals_session_id ON icrm_action_approvals(session_id);
CREATE INDEX idx_approvals_session_risk ON icrm_action_approvals(session_id, risk_level);


-- 7. Audit Log
CREATE TABLE IF NOT EXISTS icrm_audit_log (
    id CHAR(36) PRIMARY KEY,
    session_id CHAR(36),
    user_id VARCHAR(255),
    action VARCHAR(255) NOT NULL,
    resource_type VARCHAR(255),
    resource_id VARCHAR(255),
    details JSON,
    ip_address VARCHAR(45),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_audit_session_id ON icrm_audit_log(session_id);
CREATE INDEX idx_audit_user_id ON icrm_audit_log(user_id);
CREATE INDEX idx_audit_action_created ON icrm_audit_log(action, created_at);


-- 8. Analytics
CREATE TABLE IF NOT EXISTS icrm_analytics (
    id CHAR(36) PRIMARY KEY,
    session_id CHAR(36),
    event_type VARCHAR(255) NOT NULL,
    event_data JSON,
    user_id VARCHAR(255),
    company_id VARCHAR(255),
    duration_ms INTEGER,
    token_count INTEGER,
    model VARCHAR(100),
    cost_usd DOUBLE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_analytics_session FOREIGN KEY (session_id) REFERENCES icrm_sessions(id) ON DELETE SET NULL
);

CREATE INDEX idx_analytics_session_id ON icrm_analytics(session_id);
CREATE INDEX idx_analytics_event_type ON icrm_analytics(event_type);
CREATE INDEX idx_analytics_event_created ON icrm_analytics(event_type, created_at);


-- 9. Feedback
CREATE TABLE IF NOT EXISTS icrm_feedback (
    id CHAR(36) PRIMARY KEY,
    session_id CHAR(36),
    message_id CHAR(36),
    user_id VARCHAR(255),
    rating INTEGER,
    comment TEXT,
    tags JSON,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_feedback_session FOREIGN KEY (session_id) REFERENCES icrm_sessions(id) ON DELETE SET NULL,
    CONSTRAINT fk_feedback_message FOREIGN KEY (message_id) REFERENCES icrm_messages(id) ON DELETE SET NULL
);

CREATE INDEX idx_feedback_session_id ON icrm_feedback(session_id);
CREATE INDEX idx_feedback_session_rating ON icrm_feedback(session_id, rating);


-- 10. Tool Registry
CREATE TABLE IF NOT EXISTS icrm_tool_registry (
    id CHAR(36) PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    display_name VARCHAR(255),
    description TEXT,
    category VARCHAR(100),
    parameters JSON,
    returns JSON,
    requires_approval TINYINT(1) DEFAULT 0,
    risk_level ENUM('low','medium','high','critical') NOT NULL DEFAULT 'low',
    enabled TINYINT(1) DEFAULT 1,
    version VARCHAR(20),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE INDEX idx_tool_registry_category ON icrm_tool_registry(category);
CREATE INDEX idx_tool_registry_category_enabled ON icrm_tool_registry(category, enabled);


-- 11. Distillation Records
CREATE TABLE IF NOT EXISTS icrm_distillation_records (
    id CHAR(36) PRIMARY KEY,
    session_id CHAR(36) NOT NULL,
    user_query TEXT NOT NULL,
    intent VARCHAR(255),
    entity VARCHAR(255),
    tools_used JSON,
    tool_results JSON,
    llm_prompt TEXT,
    llm_response TEXT,
    final_response TEXT,
    confidence DOUBLE,
    feedback_score INTEGER,
    feedback_text TEXT,
    model_used VARCHAR(100),
    token_count_input INTEGER DEFAULT 0,
    token_count_output INTEGER DEFAULT 0,
    cost_usd DOUBLE DEFAULT 0.0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_distillation_session ON icrm_distillation_records(session_id);
CREATE INDEX idx_distillation_confidence ON icrm_distillation_records(confidence);
CREATE INDEX idx_distillation_created ON icrm_distillation_records(created_at);


-- 12. Knowledge Entries
CREATE TABLE IF NOT EXISTS icrm_knowledge_entries (
    id CHAR(36) PRIMARY KEY,
    category ENUM('faq','policy','process','troubleshooting') NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    source VARCHAR(255),
    confidence DOUBLE DEFAULT 1.0,
    usage_count INTEGER DEFAULT 0,
    enabled TINYINT(1) DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE INDEX idx_knowledge_category ON icrm_knowledge_entries(category);
CREATE INDEX idx_knowledge_category_enabled ON icrm_knowledge_entries(category, enabled);


-- 13. Query Analytics
CREATE TABLE IF NOT EXISTS icrm_query_analytics (
    id CHAR(36) PRIMARY KEY,
    session_id CHAR(36),
    intent VARCHAR(255),
    entity VARCHAR(255),
    confidence DOUBLE,
    latency_ms DOUBLE,
    tools_used JSON,
    escalated TINYINT(1) DEFAULT 0,
    model VARCHAR(100),
    cost_usd DOUBLE DEFAULT 0.0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_query_analytics_session ON icrm_query_analytics(session_id);
CREATE INDEX idx_query_analytics_intent ON icrm_query_analytics(intent);
CREATE INDEX idx_query_analytics_created ON icrm_query_analytics(created_at);
CREATE INDEX idx_query_analytics_model ON icrm_query_analytics(model);


-- 14. Settings Cache (single-row config store)
CREATE TABLE IF NOT EXISTS cosmos_settings_cache (
    id INTEGER PRIMARY KEY,
    settings JSON,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);


-- 15. Cosmos Tools — DB-driven tool definitions (seeded by training pipeline from P11 YAMLs)
--     Replaces hardcoded Python tool classes. Every tool is a row; adding a tool = add YAML + run cosmos:train.
CREATE TABLE IF NOT EXISTS cosmos_tools (
    id              VARCHAR(128)    PRIMARY KEY,            -- e.g. "orders_create"
    name            VARCHAR(128)    NOT NULL,
    display_name    VARCHAR(256),
    description     TEXT            NOT NULL,
    pillar          VARCHAR(32),                            -- "P11"
    entity          VARCHAR(64),                            -- "orders", "shipment"
    intent          VARCHAR(32),                            -- "act" | "lookup" | "diagnose"

    -- HTTP execution metadata (what the training pipeline extracts from P11 YAML)
    http_method     VARCHAR(8),                             -- GET | POST | PUT | DELETE
    endpoint_path   VARCHAR(512),                           -- "/v1/orders/create/adhoc"
    base_url_key    VARCHAR(64),                            -- config key: "MCAPI_BASE_URL"
    auth_type       VARCHAR(32),                            -- "seller_token" | "company_token" | "none"
    request_schema  JSON,                                   -- Anthropic input_schema format
    response_schema JSON,                                   -- expected response shape (for synthesis)

    -- Governance
    risk_level      VARCHAR(16)     NOT NULL DEFAULT 'low', -- low | medium | high | critical
    approval_mode   VARCHAR(16)     NOT NULL DEFAULT 'auto',-- auto | manual | always
    allowed_roles   JSON,                                   -- ["operator", "seller"]
    rate_limit_rpm  INT             DEFAULT 60,

    -- KB linkage (for graph edges + context injection)
    kb_doc_id       VARCHAR(256),                           -- "pillar_11_tools/orders_create"
    action_contract VARCHAR(256),                           -- "pillar_6_action_contracts/.../create_order"

    -- Quality / freshness
    trust_score     FLOAT           DEFAULT 0.9,
    training_ready  TINYINT(1)      DEFAULT 1,
    content_hash    VARCHAR(64),                            -- content-hash skip on re-train
    enabled         TINYINT(1)      DEFAULT 1,
    created_at      TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

CREATE INDEX idx_cosmos_tools_entity   ON cosmos_tools(entity);
CREATE INDEX idx_cosmos_tools_intent   ON cosmos_tools(intent);
CREATE INDEX idx_cosmos_tools_risk     ON cosmos_tools(risk_level);
CREATE INDEX idx_cosmos_tools_enabled  ON cosmos_tools(enabled);

-- Seed: orders_create (mirrors knowledge_base/.../pillar_11_tools/orders_create.yaml)
INSERT IGNORE INTO cosmos_tools
    (id, name, display_name, description, pillar, entity, intent,
     http_method, endpoint_path, base_url_key, auth_type,
     request_schema,
     risk_level, approval_mode, allowed_roles,
     kb_doc_id, action_contract, trust_score)
VALUES
    ('orders_create', 'orders_create', 'Create Order',
     'Create a new shipping order in Shiprocket. Validates address, calculates freight, assigns courier, and triggers RTO prediction.',
     'P11', 'orders', 'act',
     'POST', '/api/v1/app/orders/create', 'MCAPI_BASE_URL', 'seller_token',
     JSON_OBJECT(
       'type', 'object',
       'required', JSON_ARRAY('order_id','order_date','billing_customer_name',
                               'billing_address','billing_city','billing_pincode',
                               'billing_state','billing_country','billing_phone',
                               'order_items','payment_method','sub_total',
                               'length','breadth','height','weight'),
       'properties', JSON_OBJECT(
         'order_id',              JSON_OBJECT('type','string',  'description','Seller reference order ID'),
         'order_date',            JSON_OBJECT('type','string',  'description','Order date YYYY-MM-DD'),
         'billing_customer_name', JSON_OBJECT('type','string',  'description','Customer full name'),
         'billing_address',       JSON_OBJECT('type','string',  'description','Full billing address line'),
         'billing_city',          JSON_OBJECT('type','string'),
         'billing_pincode',       JSON_OBJECT('type','string',  'description','6-digit pincode'),
         'billing_state',         JSON_OBJECT('type','string'),
         'billing_country',       JSON_OBJECT('type','string',  'description','Default: India'),
         'billing_phone',         JSON_OBJECT('type','string',  'description','10-digit mobile number'),
         'shipping_is_billing',   JSON_OBJECT('type','boolean', 'description','Use billing address for shipping'),
         'payment_method',        JSON_OBJECT('type','string',  'enum',JSON_ARRAY('prepaid','cod')),
         'sub_total',             JSON_OBJECT('type','number',  'description','Order value in INR'),
         'length',                JSON_OBJECT('type','number',  'description','Package length cm'),
         'breadth',               JSON_OBJECT('type','number',  'description','Package breadth cm'),
         'height',                JSON_OBJECT('type','number',  'description','Package height cm'),
         'weight',                JSON_OBJECT('type','number',  'description','Package weight kg'),
         'channel_id',            JSON_OBJECT('type','integer', 'description','Optional channel ID')
       )
     ),
     'high', 'manual', JSON_ARRAY('operator','seller'),
     'pillar_11_tools/orders_create',
     'pillar_6_action_contracts/domains/orders/create_order',
     0.9),

    -- orders_get: read order details by ID
    ('orders_get', 'orders_get', 'Get Order',
     'Fetch a single order by order_id or shipment_id from Shiprocket.',
     'P11', 'orders', 'lookup',
     'GET', '/api/v1/orders/show', 'MCAPI_BASE_URL', 'seller_token',
     JSON_OBJECT(
       'type', 'object',
       'required', JSON_ARRAY('order_id'),
       'properties', JSON_OBJECT(
         'order_id', JSON_OBJECT('type','integer','description','Shiprocket order ID')
       )
     ),
     'low', 'auto', JSON_ARRAY('operator','seller','viewer'),
     'pillar_11_tools/orders_get', NULL, 0.9),

    -- orders_cancel: cancel a pre-pickup order
    ('orders_cancel', 'orders_cancel', 'Cancel Order',
     'Cancel one or more Shiprocket orders before AWB generation / courier pickup.',
     'P11', 'orders', 'act',
     'POST', '/api/v1/orders/cancel', 'MCAPI_BASE_URL', 'seller_token',
     JSON_OBJECT(
       'type', 'object',
       'required', JSON_ARRAY('ids'),
       'properties', JSON_OBJECT(
         'ids', JSON_OBJECT('type','array','items',JSON_OBJECT('type','integer'),
                            'description','List of order IDs to cancel')
       )
     ),
     'high', 'manual', JSON_ARRAY('operator','seller'),
     'pillar_11_tools/orders_cancel', NULL, 0.9),

    -- track_shipment: real-time shipment tracking
    ('track_shipment', 'track_shipment', 'Track Shipment',
     'Get real-time tracking status and event timeline for a shipment by AWB number.',
     'P11', 'shipment', 'lookup',
     'GET', '/api/v1/courier/track/awb', 'MCAPI_BASE_URL', 'seller_token',
     JSON_OBJECT(
       'type', 'object',
       'required', JSON_ARRAY('awb'),
       'properties', JSON_OBJECT(
         'awb', JSON_OBJECT('type','string','description','AWB / tracking number')
       )
     ),
     'low', 'auto', JSON_ARRAY('operator','seller','viewer'),
     'pillar_11_tools/track_shipment', NULL, 0.9);


-- 16. Kafka Embedding Queue Tracker
-- Tracks which KB docs have been published to Kafka for async embedding
-- and whether the primary (small) embedding has completed in Qdrant.
-- PrimaryEmbeddingConsumer writes to this table; /pipeline/status reads it.
-- Note: entity_id limited to 191 chars in unique key (MySQL utf8mb4 3072-byte PK limit).
CREATE TABLE IF NOT EXISTS cosmos_embedding_queue_tracker (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    repo_id       VARCHAR(255)    NOT NULL DEFAULT '',
    entity_type   VARCHAR(255)    NOT NULL DEFAULT '',
    entity_id     VARCHAR(500)    NOT NULL DEFAULT '',
    content_hash  VARCHAR(64)     NOT NULL DEFAULT '',
    published_at  TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    small_done    TINYINT(1)      NOT NULL DEFAULT 0,
    large_done    TINYINT(1)      NOT NULL DEFAULT 0,
    voyage_done   TINYINT(1)      NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    UNIQUE KEY uq_tracker_entity (repo_id(100), entity_type(100), entity_id(191))
);

-- Table 17: Pattern cache for fast-path query resolution
CREATE TABLE IF NOT EXISTS cosmos_pattern_cache (
    id            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    pattern_key   VARCHAR(191)    NOT NULL,
    intent        VARCHAR(100)    NOT NULL DEFAULT '',
    entity_type   VARCHAR(100)    NOT NULL DEFAULT '',
    tool_sequence JSON            NOT NULL,
    agent_name    VARCHAR(255)    NOT NULL DEFAULT '',
    confidence    FLOAT           NOT NULL DEFAULT 0,
    success_count INT             NOT NULL DEFAULT 0,
    total_count   INT             NOT NULL DEFAULT 0,
    avg_latency_ms FLOAT          NOT NULL DEFAULT 0,
    kb_version    VARCHAR(255)    NOT NULL DEFAULT '',
    last_used_at  TIMESTAMP       NULL,
    created_at    TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_pattern_key (pattern_key)
);
