-- Схема БД SyncScribe после alembic upgrade head (ревизия 0033).
-- Получено командой: pg_dump --schema-only --no-owner --no-privileges.
-- Источник истины — миграции в alembic/versions; файл обновляется вместе с ними.

--
-- PostgreSQL database dump
--

\restrict e6anAg2TyyN2EKtmAgRMwYapTeVTfdeFdDMwgZ6h9g4FQfeK3QDNBLanrvyBS3Y

-- Dumped from database version 16.15
-- Dumped by pg_dump version 16.15 (Ubuntu 16.15-1.pgdg24.04+2)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: pg_trgm; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;


--
-- Name: EXTENSION pg_trgm; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pg_trgm IS 'text similarity measurement and index searching based on trigrams';


--
-- Name: analysis_job_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.analysis_job_status AS ENUM (
    'pending',
    'processing',
    'success',
    'failed',
    'cancelled',
    'partial_success',
    'dispatched'
);


--
-- Name: audit_action; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.audit_action AS ENUM (
    'accept',
    'reject',
    'download',
    'bulk_accept',
    'finalize',
    'reopen',
    'bulk_reject',
    'finalize_review',
    'reset'
);


--
-- Name: block_type_enum; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.block_type_enum AS ENUM (
    'heading',
    'paragraph',
    'list_item',
    'code',
    'table',
    'other'
);


--
-- Name: change_type; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.change_type AS ENUM (
    'add',
    'modify',
    'delete'
);


--
-- Name: document_format; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.document_format AS ENUM (
    'doc',
    'docx',
    'txt',
    'markdown'
);


--
-- Name: document_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.document_status AS ENUM (
    'draft',
    'in_progress',
    'awaiting_approval',
    'ready'
);


--
-- Name: source_scope; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.source_scope AS ENUM (
    'project',
    'document'
);


--
-- Name: source_type; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.source_type AS ENUM (
    'file',
    'url'
);


--
-- Name: suggestion_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.suggestion_status AS ENUM (
    'pending',
    'accepted',
    'rejected'
);


--
-- Name: user_role; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.user_role AS ENUM (
    'admin',
    'user',
    'editor',
    'viewer'
);


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);


--
-- Name: analysis_job_sources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.analysis_job_sources (
    id uuid NOT NULL,
    job_id uuid NOT NULL,
    source_id uuid NOT NULL,
    status character varying(20) DEFAULT 'pending'::character varying NOT NULL,
    suggestions_count integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    locked_at timestamp with time zone,
    lock_token uuid
);


--
-- Name: analysis_jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.analysis_jobs (
    id uuid NOT NULL,
    document_id uuid NOT NULL,
    status public.analysis_job_status DEFAULT 'pending'::public.analysis_job_status NOT NULL,
    celery_task_id character varying(255),
    error_code character varying(100),
    error_message text,
    retry_count integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    idempotency_key character varying(128),
    partial_success boolean DEFAULT false NOT NULL,
    request_id character varying(128),
    pipeline_meta jsonb,
    checked_blocks integer,
    failed_blocks integer
);


--
-- Name: audit_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_logs (
    id uuid NOT NULL,
    suggestion_id uuid,
    user_id uuid,
    action public.audit_action NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    document_id uuid,
    details jsonb,
    CONSTRAINT ck_audit_logs_ref_not_null CHECK (((suggestion_id IS NOT NULL) OR (document_id IS NOT NULL))),
    CONSTRAINT ck_audit_logs_target CHECK ((((action = ANY (ARRAY['download'::public.audit_action, 'finalize'::public.audit_action, 'finalize_review'::public.audit_action])) AND (document_id IS NOT NULL) AND (suggestion_id IS NULL)) OR ((action <> ALL (ARRAY['download'::public.audit_action, 'finalize'::public.audit_action, 'finalize_review'::public.audit_action])) AND (suggestion_id IS NOT NULL))))
);


--
-- Name: dashboard_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dashboard_snapshots (
    id uuid NOT NULL,
    owner_id uuid NOT NULL,
    snapshot_date date NOT NULL,
    total_count integer NOT NULL,
    awaiting_count integer NOT NULL,
    relevance_percent double precision NOT NULL
);


--
-- Name: document_blocks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.document_blocks (
    id uuid NOT NULL,
    document_id uuid NOT NULL,
    "position" integer NOT NULL,
    block_type public.block_type_enum NOT NULL,
    content text NOT NULL,
    heading_level integer,
    block_ref character varying(255) NOT NULL,
    CONSTRAINT ck_document_blocks_heading_level CHECK (((block_type = 'heading'::public.block_type_enum) OR (heading_level IS NULL)))
);


--
-- Name: document_opens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.document_opens (
    user_id uuid NOT NULL,
    document_id uuid NOT NULL,
    last_opened_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: document_sources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.document_sources (
    document_id uuid NOT NULL,
    source_id uuid NOT NULL
);


--
-- Name: documents; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.documents (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    name character varying(512) NOT NULL,
    format public.document_format NOT NULL,
    storage_key character varying(1024) NOT NULL,
    status public.document_status DEFAULT 'draft'::public.document_status NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    review_version integer DEFAULT 0 NOT NULL,
    original_storage_key character varying(1024),
    size_bytes integer DEFAULT 0 NOT NULL,
    uploaded_at timestamp with time zone DEFAULT now() NOT NULL,
    exported_storage_key character varying(1024),
    current_analysis_job_id uuid,
    CONSTRAINT ck_documents_size_bytes_non_negative CHECK ((size_bytes >= 0))
);


--
-- Name: projects; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.projects (
    id uuid NOT NULL,
    name character varying(255) NOT NULL,
    owner_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    description character varying(2000),
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    color character varying(6),
    icon character varying(64)
);


--
-- Name: sources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sources (
    id uuid NOT NULL,
    project_id uuid NOT NULL,
    name character varying(255) NOT NULL,
    type public.source_type NOT NULL,
    storage_key character varying(1024),
    url character varying(2048),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    scope public.source_scope DEFAULT 'project'::public.source_scope NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_sources_type_field_consistency CHECK ((((type = 'file'::public.source_type) AND (storage_key IS NOT NULL)) OR ((type = 'url'::public.source_type) AND (url IS NOT NULL))))
);


--
-- Name: suggestion_sources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.suggestion_sources (
    suggestion_id uuid NOT NULL,
    source_id uuid NOT NULL
);


--
-- Name: suggestions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.suggestions (
    id uuid NOT NULL,
    analysis_job_id uuid NOT NULL,
    section_ref character varying(500) NOT NULL,
    change_type public.change_type NOT NULL,
    original_text text,
    suggested_text text,
    status public.suggestion_status DEFAULT 'pending'::public.suggestion_status NOT NULL,
    confidence_score numeric(4,3),
    rationale text,
    decided_by uuid,
    decided_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    block_id character varying(255),
    start_offset integer,
    end_offset integer,
    document_id uuid NOT NULL,
    edited_text text,
    "position" integer DEFAULT 0 NOT NULL,
    CONSTRAINT ck_suggestions_confidence_range CHECK (((confidence_score IS NULL) OR ((confidence_score >= (0)::numeric) AND (confidence_score <= (1)::numeric))))
);


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id uuid NOT NULL,
    email character varying(255) NOT NULL,
    password_hash character varying(255) NOT NULL,
    role public.user_role DEFAULT 'user'::public.user_role NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: analysis_job_sources analysis_job_sources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_job_sources
    ADD CONSTRAINT analysis_job_sources_pkey PRIMARY KEY (id);


--
-- Name: analysis_jobs analysis_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_jobs
    ADD CONSTRAINT analysis_jobs_pkey PRIMARY KEY (id);


--
-- Name: audit_logs audit_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_pkey PRIMARY KEY (id);


--
-- Name: dashboard_snapshots dashboard_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dashboard_snapshots
    ADD CONSTRAINT dashboard_snapshots_pkey PRIMARY KEY (id);


--
-- Name: document_blocks document_blocks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_blocks
    ADD CONSTRAINT document_blocks_pkey PRIMARY KEY (id);


--
-- Name: document_opens document_opens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_opens
    ADD CONSTRAINT document_opens_pkey PRIMARY KEY (user_id, document_id);


--
-- Name: document_sources document_sources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_sources
    ADD CONSTRAINT document_sources_pkey PRIMARY KEY (document_id, source_id);


--
-- Name: documents documents_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_pkey PRIMARY KEY (id);


--
-- Name: projects projects_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_pkey PRIMARY KEY (id);


--
-- Name: sources sources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sources
    ADD CONSTRAINT sources_pkey PRIMARY KEY (id);


--
-- Name: suggestion_sources suggestion_sources_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.suggestion_sources
    ADD CONSTRAINT suggestion_sources_pkey PRIMARY KEY (suggestion_id, source_id);


--
-- Name: suggestions suggestions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.suggestions
    ADD CONSTRAINT suggestions_pkey PRIMARY KEY (id);


--
-- Name: analysis_job_sources uq_analysis_job_sources_job_source; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_job_sources
    ADD CONSTRAINT uq_analysis_job_sources_job_source UNIQUE (job_id, source_id);


--
-- Name: analysis_jobs uq_analysis_jobs_doc_idem_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_jobs
    ADD CONSTRAINT uq_analysis_jobs_doc_idem_key UNIQUE (document_id, idempotency_key);


--
-- Name: dashboard_snapshots uq_dashboard_snapshot_owner_date; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dashboard_snapshots
    ADD CONSTRAINT uq_dashboard_snapshot_owner_date UNIQUE (owner_id, snapshot_date);


--
-- Name: document_blocks uq_document_blocks_doc_ref; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_blocks
    ADD CONSTRAINT uq_document_blocks_doc_ref UNIQUE (document_id, block_ref);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: ix_analysis_job_sources_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_analysis_job_sources_job_id ON public.analysis_job_sources USING btree (job_id);


--
-- Name: ix_analysis_jobs_doc_latest; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_analysis_jobs_doc_latest ON public.analysis_jobs USING btree (document_id, created_at);


--
-- Name: ix_analysis_jobs_document_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_analysis_jobs_document_id ON public.analysis_jobs USING btree (document_id);


--
-- Name: ix_audit_logs_document_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_logs_document_created ON public.audit_logs USING btree (document_id, created_at);


--
-- Name: ix_audit_logs_suggestion_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_logs_suggestion_id ON public.audit_logs USING btree (suggestion_id);


--
-- Name: ix_audit_logs_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_logs_user_id ON public.audit_logs USING btree (user_id);


--
-- Name: ix_dashboard_snapshots_owner_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dashboard_snapshots_owner_id ON public.dashboard_snapshots USING btree (owner_id);


--
-- Name: ix_document_opens_user_opened; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_document_opens_user_opened ON public.document_opens USING btree (user_id, last_opened_at);


--
-- Name: ix_documents_current_analysis_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_documents_current_analysis_job_id ON public.documents USING btree (current_analysis_job_id) WHERE (current_analysis_job_id IS NOT NULL);


--
-- Name: ix_documents_name_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_documents_name_trgm ON public.documents USING gin (name public.gin_trgm_ops);


--
-- Name: ix_documents_project_created_at_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_documents_project_created_at_id ON public.documents USING btree (project_id, created_at DESC, id DESC);


--
-- Name: ix_documents_project_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_documents_project_id ON public.documents USING btree (project_id);


--
-- Name: ix_projects_owner_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_projects_owner_id ON public.projects USING btree (owner_id);


--
-- Name: ix_sources_project_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_sources_project_id ON public.sources USING btree (project_id);


--
-- Name: ix_sources_scope; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_sources_scope ON public.sources USING btree (scope);


--
-- Name: ix_suggestion_sources_source_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_suggestion_sources_source_id ON public.suggestion_sources USING btree (source_id);


--
-- Name: ix_suggestions_decided_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_suggestions_decided_by ON public.suggestions USING btree (decided_by) WHERE (decided_by IS NOT NULL);


--
-- Name: ix_suggestions_document_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_suggestions_document_status ON public.suggestions USING btree (document_id, status);


--
-- Name: ix_suggestions_job_order; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_suggestions_job_order ON public.suggestions USING btree (analysis_job_id, created_at, "position", id);


--
-- Name: ix_suggestions_job_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_suggestions_job_status ON public.suggestions USING btree (analysis_job_id, status);


--
-- Name: ix_users_email; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_users_email ON public.users USING btree (email);


--
-- Name: uq_analysis_jobs_one_active_per_document; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_analysis_jobs_one_active_per_document ON public.analysis_jobs USING btree (document_id) WHERE (status = ANY (ARRAY['pending'::public.analysis_job_status, 'dispatched'::public.analysis_job_status, 'processing'::public.analysis_job_status]));


--
-- Name: uq_document_blocks_doc_position; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_document_blocks_doc_position ON public.document_blocks USING btree (document_id, "position");


--
-- Name: analysis_job_sources analysis_job_sources_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_job_sources
    ADD CONSTRAINT analysis_job_sources_job_id_fkey FOREIGN KEY (job_id) REFERENCES public.analysis_jobs(id) ON DELETE CASCADE;


--
-- Name: analysis_jobs analysis_jobs_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.analysis_jobs
    ADD CONSTRAINT analysis_jobs_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;


--
-- Name: audit_logs audit_logs_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;


--
-- Name: audit_logs audit_logs_suggestion_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_suggestion_id_fkey FOREIGN KEY (suggestion_id) REFERENCES public.suggestions(id) ON DELETE CASCADE;


--
-- Name: audit_logs audit_logs_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: dashboard_snapshots dashboard_snapshots_owner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dashboard_snapshots
    ADD CONSTRAINT dashboard_snapshots_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: document_blocks document_blocks_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_blocks
    ADD CONSTRAINT document_blocks_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;


--
-- Name: document_opens document_opens_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_opens
    ADD CONSTRAINT document_opens_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;


--
-- Name: document_opens document_opens_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_opens
    ADD CONSTRAINT document_opens_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: document_sources document_sources_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_sources
    ADD CONSTRAINT document_sources_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;


--
-- Name: document_sources document_sources_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document_sources
    ADD CONSTRAINT document_sources_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.sources(id) ON DELETE CASCADE;


--
-- Name: documents documents_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: documents fk_documents_current_analysis_job_id; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT fk_documents_current_analysis_job_id FOREIGN KEY (current_analysis_job_id) REFERENCES public.analysis_jobs(id) ON DELETE SET NULL;


--
-- Name: projects projects_owner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.projects
    ADD CONSTRAINT projects_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: sources sources_project_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sources
    ADD CONSTRAINT sources_project_id_fkey FOREIGN KEY (project_id) REFERENCES public.projects(id) ON DELETE CASCADE;


--
-- Name: suggestion_sources suggestion_sources_source_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.suggestion_sources
    ADD CONSTRAINT suggestion_sources_source_id_fkey FOREIGN KEY (source_id) REFERENCES public.sources(id) ON DELETE CASCADE;


--
-- Name: suggestion_sources suggestion_sources_suggestion_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.suggestion_sources
    ADD CONSTRAINT suggestion_sources_suggestion_id_fkey FOREIGN KEY (suggestion_id) REFERENCES public.suggestions(id) ON DELETE CASCADE;


--
-- Name: suggestions suggestions_analysis_job_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.suggestions
    ADD CONSTRAINT suggestions_analysis_job_id_fkey FOREIGN KEY (analysis_job_id) REFERENCES public.analysis_jobs(id) ON DELETE CASCADE;


--
-- Name: suggestions suggestions_decided_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.suggestions
    ADD CONSTRAINT suggestions_decided_by_fkey FOREIGN KEY (decided_by) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: suggestions suggestions_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.suggestions
    ADD CONSTRAINT suggestions_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict e6anAg2TyyN2EKtmAgRMwYapTeVTfdeFdDMwgZ6h9g4FQfeK3QDNBLanrvyBS3Y

