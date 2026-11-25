-- DocIntel database schema (PostgreSQL 18 + pgvector 0.8.7)
-- Exported with: pg_dump --schema-only from the running docintel-db container.
-- The app creates it itself (docintel db-init / on API start); this file is for reading.

-- PostgreSQL database dump

-- Name: pg_trgm; Type: EXTENSION; Schema: -; Owner: -

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;

-- Name: EXTENSION pg_trgm; Type: COMMENT; Schema: -; Owner: -

COMMENT ON EXTENSION pg_trgm IS 'text similarity measurement and index searching based on trigrams';

-- Name: vector; Type: EXTENSION; Schema: -; Owner: -

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;

-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: -

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';

-- Name: chunks; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.chunks (
    id bigint NOT NULL,
    document_id uuid NOT NULL,
    chunk_index integer NOT NULL,
    page_number integer NOT NULL,
    text text NOT NULL,
    search_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, text)) STORED NOT NULL,
    embedding public.vector(768),
    embedding_model character varying(64)
);

-- Name: chunks_id_seq; Type: SEQUENCE; Schema: public; Owner: -

CREATE SEQUENCE public.chunks_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

-- Name: chunks_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -

ALTER SEQUENCE public.chunks_id_seq OWNED BY public.chunks.id;

-- Name: documents; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.documents (
    id uuid NOT NULL,
    sha256 character varying(64) NOT NULL,
    filename character varying(512) NOT NULL,
    content_type character varying(128),
    size_bytes bigint NOT NULL,
    storage_path character varying(1024) NOT NULL,
    status character varying(20) NOT NULL,
    detected_type character varying(32),
    page_count integer NOT NULL,
    processing_status character varying(20),
    extraction_status character varying(20),
    validation_status character varying(20),
    needs_review boolean NOT NULL,
    invoice_count integer NOT NULL,
    search_status character varying(20) NOT NULL,
    error text,
    processing jsonb NOT NULL,
    result jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    processed_at timestamp with time zone
);

-- Name: invoices; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.invoices (
    id uuid NOT NULL,
    document_id uuid NOT NULL,
    index integer NOT NULL,
    document_type character varying(20) NOT NULL,
    invoice_number character varying(255),
    invoice_number_norm character varying(255),
    invoice_date date,
    supplier_name character varying(512),
    supplier_norm character varying(512),
    customer_name character varying(512),
    subtotal numeric(18,4),
    tax numeric(18,4),
    total numeric(18,4),
    currency character varying(3),
    method character varying(20) NOT NULL,
    confidence double precision NOT NULL,
    pages jsonb NOT NULL,
    source_label character varying(255),
    fields jsonb NOT NULL,
    extraction_notes jsonb NOT NULL,
    validation_status character varying(20) NOT NULL,
    needs_review boolean NOT NULL,
    checks jsonb NOT NULL,
    review_status character varying(20) NOT NULL,
    review_note text,
    reviewed_at timestamp with time zone,
    corrected_fields jsonb NOT NULL,
    summary_text text NOT NULL,
    search_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, COALESCE(summary_text, ''::text))) STORED NOT NULL,
    embedding public.vector(768),
    embedding_model character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    charges jsonb DEFAULT '[]'::jsonb NOT NULL
);

-- Name: jobs; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.jobs (
    id uuid NOT NULL,
    document_id uuid NOT NULL,
    kind character varying(20) NOT NULL,
    status character varying(20) NOT NULL,
    stage character varying(20),
    attempts integer NOT NULL,
    max_attempts integer NOT NULL,
    error text,
    worker_id character varying(128),
    run_after timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone,
    heartbeat_at timestamp with time zone,
    finished_at timestamp with time zone
);

-- Name: line_items; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.line_items (
    id bigint NOT NULL,
    invoice_id uuid NOT NULL,
    "position" integer NOT NULL,
    description text,
    quantity numeric(18,6),
    unit_price numeric(18,6),
    amount numeric(18,4),
    page integer,
    evidence text,
    source character varying(20),
    confidence double precision NOT NULL,
    issues jsonb NOT NULL
);

-- Name: line_items_id_seq; Type: SEQUENCE; Schema: public; Owner: -

CREATE SEQUENCE public.line_items_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

-- Name: line_items_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -

ALTER SEQUENCE public.line_items_id_seq OWNED BY public.line_items.id;

-- Name: pages; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.pages (
    id bigint NOT NULL,
    document_id uuid NOT NULL,
    number integer NOT NULL,
    label character varying(255) NOT NULL,
    method character varying(20) NOT NULL,
    text text NOT NULL,
    char_count integer NOT NULL,
    ocr_confidence double precision,
    tables jsonb NOT NULL,
    issues jsonb NOT NULL
);

-- Name: pages_id_seq; Type: SEQUENCE; Schema: public; Owner: -

CREATE SEQUENCE public.pages_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

-- Name: pages_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -

ALTER SEQUENCE public.pages_id_seq OWNED BY public.pages.id;

-- Name: validation_issues; Type: TABLE; Schema: public; Owner: -

CREATE TABLE public.validation_issues (
    id bigint NOT NULL,
    document_id uuid NOT NULL,
    invoice_id uuid,
    code character varying(64) NOT NULL,
    severity character varying(10) NOT NULL,
    field character varying(128),
    message text NOT NULL,
    details jsonb NOT NULL
);

-- Name: validation_issues_id_seq; Type: SEQUENCE; Schema: public; Owner: -

CREATE SEQUENCE public.validation_issues_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

-- Name: validation_issues_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -

ALTER SEQUENCE public.validation_issues_id_seq OWNED BY public.validation_issues.id;

-- Name: chunks id; Type: DEFAULT; Schema: public; Owner: -

ALTER TABLE ONLY public.chunks ALTER COLUMN id SET DEFAULT nextval('public.chunks_id_seq'::regclass);

-- Name: line_items id; Type: DEFAULT; Schema: public; Owner: -

ALTER TABLE ONLY public.line_items ALTER COLUMN id SET DEFAULT nextval('public.line_items_id_seq'::regclass);

-- Name: pages id; Type: DEFAULT; Schema: public; Owner: -

ALTER TABLE ONLY public.pages ALTER COLUMN id SET DEFAULT nextval('public.pages_id_seq'::regclass);

-- Name: validation_issues id; Type: DEFAULT; Schema: public; Owner: -

ALTER TABLE ONLY public.validation_issues ALTER COLUMN id SET DEFAULT nextval('public.validation_issues_id_seq'::regclass);

-- Name: chunks chunks_document_id_chunk_index_key; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT chunks_document_id_chunk_index_key UNIQUE (document_id, chunk_index);

-- Name: chunks chunks_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT chunks_pkey PRIMARY KEY (id);

-- Name: documents documents_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_pkey PRIMARY KEY (id);

-- Name: documents documents_sha256_key; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.documents
    ADD CONSTRAINT documents_sha256_key UNIQUE (sha256);

-- Name: invoices invoices_document_id_index_key; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.invoices
    ADD CONSTRAINT invoices_document_id_index_key UNIQUE (document_id, index);

-- Name: invoices invoices_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.invoices
    ADD CONSTRAINT invoices_pkey PRIMARY KEY (id);

-- Name: jobs jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_pkey PRIMARY KEY (id);

-- Name: line_items line_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.line_items
    ADD CONSTRAINT line_items_pkey PRIMARY KEY (id);

-- Name: pages pages_document_id_number_key; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.pages
    ADD CONSTRAINT pages_document_id_number_key UNIQUE (document_id, number);

-- Name: pages pages_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.pages
    ADD CONSTRAINT pages_pkey PRIMARY KEY (id);

-- Name: validation_issues validation_issues_pkey; Type: CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.validation_issues
    ADD CONSTRAINT validation_issues_pkey PRIMARY KEY (id);

-- Name: ix_chunks_document_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_chunks_document_id ON public.chunks USING btree (document_id);

-- Name: ix_chunks_embedding; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_chunks_embedding ON public.chunks USING hnsw (embedding public.vector_cosine_ops);

-- Name: ix_chunks_search_tsv; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_chunks_search_tsv ON public.chunks USING gin (search_tsv);

-- Name: ix_documents_needs_review; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_documents_needs_review ON public.documents USING btree (needs_review);

-- Name: ix_documents_status; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_documents_status ON public.documents USING btree (status);

-- Name: ix_documents_validation_status; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_documents_validation_status ON public.documents USING btree (validation_status);

-- Name: ix_invoices_document_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_invoices_document_id ON public.invoices USING btree (document_id);

-- Name: ix_invoices_embedding; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_invoices_embedding ON public.invoices USING hnsw (embedding public.vector_cosine_ops);

-- Name: ix_invoices_invoice_date; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_invoices_invoice_date ON public.invoices USING btree (invoice_date);

-- Name: ix_invoices_invoice_number_norm; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_invoices_invoice_number_norm ON public.invoices USING btree (invoice_number_norm);

-- Name: ix_invoices_needs_review; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_invoices_needs_review ON public.invoices USING btree (needs_review);

-- Name: ix_invoices_review_status; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_invoices_review_status ON public.invoices USING btree (review_status);

-- Name: ix_invoices_search_tsv; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_invoices_search_tsv ON public.invoices USING gin (search_tsv);

-- Name: ix_invoices_supplier_norm; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_invoices_supplier_norm ON public.invoices USING btree (supplier_norm);

-- Name: ix_invoices_supplier_trgm; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_invoices_supplier_trgm ON public.invoices USING gin (supplier_name public.gin_trgm_ops);

-- Name: ix_invoices_total; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_invoices_total ON public.invoices USING btree (total);

-- Name: ix_invoices_validation_status; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_invoices_validation_status ON public.invoices USING btree (validation_status);

-- Name: ix_jobs_claim; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_jobs_claim ON public.jobs USING btree (status, run_after);

-- Name: ix_jobs_document_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_jobs_document_id ON public.jobs USING btree (document_id);

-- Name: ix_line_items_invoice_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_line_items_invoice_id ON public.line_items USING btree (invoice_id);

-- Name: ix_validation_issues_code; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_validation_issues_code ON public.validation_issues USING btree (code);

-- Name: ix_validation_issues_document_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_validation_issues_document_id ON public.validation_issues USING btree (document_id);

-- Name: ix_validation_issues_invoice_id; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_validation_issues_invoice_id ON public.validation_issues USING btree (invoice_id);

-- Name: ix_validation_issues_severity; Type: INDEX; Schema: public; Owner: -

CREATE INDEX ix_validation_issues_severity ON public.validation_issues USING btree (severity);

-- Name: chunks chunks_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT chunks_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;

-- Name: invoices invoices_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.invoices
    ADD CONSTRAINT invoices_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;

-- Name: jobs jobs_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;

-- Name: line_items line_items_invoice_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.line_items
    ADD CONSTRAINT line_items_invoice_id_fkey FOREIGN KEY (invoice_id) REFERENCES public.invoices(id) ON DELETE CASCADE;

-- Name: pages pages_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.pages
    ADD CONSTRAINT pages_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;

-- Name: validation_issues validation_issues_document_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.validation_issues
    ADD CONSTRAINT validation_issues_document_id_fkey FOREIGN KEY (document_id) REFERENCES public.documents(id) ON DELETE CASCADE;

-- Name: validation_issues validation_issues_invoice_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -

ALTER TABLE ONLY public.validation_issues
    ADD CONSTRAINT validation_issues_invoice_id_fkey FOREIGN KEY (invoice_id) REFERENCES public.invoices(id) ON DELETE CASCADE;

-- PostgreSQL database dump complete

