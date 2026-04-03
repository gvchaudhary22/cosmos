-- COSMOS initial schema
-- Tables match app/db/models.py (14 tables)
-- PostgreSQL 16+

-- Enums
DO $$ BEGIN
    CREATE TYPE message_role AS ENUM ('user', 'assistant', 'system', 'tool');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE reasoning_phase AS ENUM ('reason', 'act', 'observe', 'evaluate', 'reflect');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE execution_status AS ENUM ('pending', 'running', 'success', 'failed', 'cancelled');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE risk_level AS ENUM ('low', 'medium', 'high', 'critical');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE approval_mode AS ENUM ('auto', 'manual', 'escalated');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE knowledge_category AS ENUM ('faq', 'policy', 'process', 'troubleshooting');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;


-- 1. Sessions
CREATE TABLE IF NOT EXISTS icrm_sessions (
    id CHAR(36) PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    company_id VARCHAR(255),
    channel VARCHAR(50) NOT NULL DEFAULT 'web',
    status VARCHAR(50) NOT NULL DEFAULT 'active',
    metadata JSON DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON icrm_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_company_id ON icrm_sessions(company_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user_status ON icrm_sessions(user_id, status);


-- 2. Messages
CREATE TABLE IF NOT EXISTS icrm_messages (
    id CHAR(36) PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES icrm_sessions(id) ON DELETE CASCADE,
    role message_role NOT NULL,
    content TEXT NOT NULL,
    token_count INTEGER,
    model VARCHAR(100),
    metadata JSON DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_messages_session_id ON icrm_messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_session_created ON icrm_messages(session_id, created_at);


-- 3. Conversation Context
CREATE TABLE IF NOT EXISTS icrm_conversation_context (
    id CHAR(36) PRIMARY KEY,
    session_id UUID NOT NULL UNIQUE REFERENCES icrm_sessions(id) ON DELETE CASCADE,
    intent VARCHAR(255),
    entities JSON DEFAULT '{}',
    tool_state JSON DEFAULT '{}',
    user_profile JSON DEFAULT '{}',
    conversation_summary TEXT,
    updated_at TIMESTAMP NOT NULL DEFAULT now()
);


-- 4. Reasoning Traces
CREATE TABLE IF NOT EXISTS icrm_reasoning_traces (
    id CHAR(36) PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES icrm_sessions(id) ON DELETE CASCADE,
    message_id UUID REFERENCES icrm_messages(id) ON DELETE SET NULL,
    phase reasoning_phase NOT NULL,
    content TEXT NOT NULL,
    duration_ms INTEGER,
    metadata JSON DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_traces_session_id ON icrm_reasoning_traces(session_id);
CREATE INDEX IF NOT EXISTS idx_traces_session_phase ON icrm_reasoning_traces(session_id, phase);


-- 5. Tool Executions
CREATE TABLE IF NOT EXISTS icrm_tool_executions (
    id CHAR(36) PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES icrm_sessions(id) ON DELETE CASCADE,
    tool_name VARCHAR(255) NOT NULL,
    input_params JSON DEFAULT '{}',
    output_result JSON DEFAULT '{}',
    status execution_status NOT NULL DEFAULT 'pending',
    duration_ms INTEGER,
    error_message TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tool_exec_session_id ON icrm_tool_executions(session_id);
CREATE INDEX IF NOT EXISTS idx_tool_exec_tool_name ON icrm_tool_executions(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_exec_session_status ON icrm_tool_executions(session_id, status);


-- 6. Action Approvals
CREATE TABLE IF NOT EXISTS icrm_action_approvals (
    id CHAR(36) PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES icrm_sessions(id) ON DELETE CASCADE,
    tool_execution_id UUID REFERENCES icrm_tool_executions(id) ON DELETE SET NULL,
    action_type VARCHAR(255) NOT NULL,
    risk_level risk_level NOT NULL DEFAULT 'low',
    approval_mode approval_mode NOT NULL DEFAULT 'auto',
    approved BOOLEAN,
    approved_by VARCHAR(255),
    reason TEXT,
    metadata JSON DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    resolved_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_approvals_session_id ON icrm_action_approvals(session_id);
CREATE INDEX IF NOT EXISTS idx_approvals_session_risk ON icrm_action_approvals(session_id, risk_level);


-- 7. Audit Log
CREATE TABLE IF NOT EXISTS icrm_audit_log (
    id CHAR(36) PRIMARY KEY,
    session_id UUID,
    user_id VARCHAR(255),
    action VARCHAR(255) NOT NULL,
    resource_type VARCHAR(255),
    resource_id VARCHAR(255),
    details JSON DEFAULT '{}',
    ip_address VARCHAR(45),
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_session_id ON icrm_audit_log(session_id);
CREATE INDEX IF NOT EXISTS idx_audit_user_id ON icrm_audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_action_created ON icrm_audit_log(action, created_at);


-- 8. Analytics
CREATE TABLE IF NOT EXISTS icrm_analytics (
    id CHAR(36) PRIMARY KEY,
    session_id UUID REFERENCES icrm_sessions(id) ON DELETE SET NULL,
    event_type VARCHAR(255) NOT NULL,
    event_data JSON DEFAULT '{}',
    user_id VARCHAR(255),
    company_id VARCHAR(255),
    duration_ms INTEGER,
    token_count INTEGER,
    model VARCHAR(100),
    cost_usd DOUBLE PRECISION,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_analytics_session_id ON icrm_analytics(session_id);
CREATE INDEX IF NOT EXISTS idx_analytics_event_type ON icrm_analytics(event_type);
CREATE INDEX IF NOT EXISTS idx_analytics_event_created ON icrm_analytics(event_type, created_at);


-- 9. Feedback
CREATE TABLE IF NOT EXISTS icrm_feedback (
    id CHAR(36) PRIMARY KEY,
    session_id UUID REFERENCES icrm_sessions(id) ON DELETE SET NULL,
    message_id UUID REFERENCES icrm_messages(id) ON DELETE SET NULL,
    user_id VARCHAR(255),
    rating INTEGER,
    comment TEXT,
    tags JSON DEFAULT '[]',
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_feedback_session_id ON icrm_feedback(session_id);
CREATE INDEX IF NOT EXISTS idx_feedback_session_rating ON icrm_feedback(session_id, rating);


-- 10. Tool Registry
CREATE TABLE IF NOT EXISTS icrm_tool_registry (
    id CHAR(36) PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    display_name VARCHAR(255),
    description TEXT,
    category VARCHAR(100),
    parameters JSON DEFAULT '{}',
    returns JSON DEFAULT '{}',
    requires_approval BOOLEAN DEFAULT FALSE,
    risk_level risk_level NOT NULL DEFAULT 'low',
    enabled BOOLEAN DEFAULT TRUE,
    version VARCHAR(20),
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tool_registry_category ON icrm_tool_registry(category);
CREATE INDEX IF NOT EXISTS idx_tool_registry_category_enabled ON icrm_tool_registry(category, enabled);


-- 11. Distillation Records
CREATE TABLE IF NOT EXISTS icrm_distillation_records (
    id CHAR(36) PRIMARY KEY,
    session_id UUID NOT NULL,
    user_query TEXT NOT NULL,
    intent VARCHAR(255),
    entity VARCHAR(255),
    tools_used JSON DEFAULT '[]',
    tool_results JSON DEFAULT '[]',
    llm_prompt TEXT,
    llm_response TEXT,
    final_response TEXT,
    confidence DOUBLE PRECISION,
    feedback_score INTEGER,
    feedback_text TEXT,
    model_used VARCHAR(100),
    token_count_input INTEGER DEFAULT 0,
    token_count_output INTEGER DEFAULT 0,
    cost_usd DOUBLE PRECISION DEFAULT 0.0,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_distillation_session ON icrm_distillation_records(session_id);
CREATE INDEX IF NOT EXISTS idx_distillation_confidence ON icrm_distillation_records(confidence);
CREATE INDEX IF NOT EXISTS idx_distillation_created ON icrm_distillation_records(created_at);


-- 12. Knowledge Entries
CREATE TABLE IF NOT EXISTS icrm_knowledge_entries (
    id CHAR(36) PRIMARY KEY,
    category knowledge_category NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    source VARCHAR(255),
    confidence DOUBLE PRECISION DEFAULT 1.0,
    usage_count INTEGER DEFAULT 0,
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    updated_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_knowledge_category ON icrm_knowledge_entries(category);
CREATE INDEX IF NOT EXISTS idx_knowledge_category_enabled ON icrm_knowledge_entries(category, enabled);


-- 13. Query Analytics
CREATE TABLE IF NOT EXISTS icrm_query_analytics (
    id CHAR(36) PRIMARY KEY,
    session_id UUID,
    intent VARCHAR(255),
    entity VARCHAR(255),
    confidence DOUBLE PRECISION,
    latency_ms DOUBLE PRECISION,
    tools_used JSON DEFAULT '[]',
    escalated BOOLEAN DEFAULT FALSE,
    model VARCHAR(100),
    cost_usd DOUBLE PRECISION DEFAULT 0.0,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_query_analytics_session ON icrm_query_analytics(session_id);
CREATE INDEX IF NOT EXISTS idx_query_analytics_intent ON icrm_query_analytics(intent);
CREATE INDEX IF NOT EXISTS idx_query_analytics_created ON icrm_query_analytics(created_at);
CREATE INDEX IF NOT EXISTS idx_query_analytics_model ON icrm_query_analytics(model);
