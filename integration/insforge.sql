-- Optional schema for a NEW InsForge development project. This application uses SQLite.
-- Review and apply manually; this file is never executed by the local application.
BEGIN;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE public.support_companies (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), name text NOT NULL
);
CREATE TABLE public.support_memberships (
  company_id uuid NOT NULL REFERENCES public.support_companies(id),
  user_id uuid NOT NULL REFERENCES auth.users(id),
  role text NOT NULL CHECK (role IN ('customer','rep','admin')),
  PRIMARY KEY (company_id,user_id)
);
CREATE FUNCTION public.support_member(company uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM public.support_memberships m WHERE m.company_id=company AND m.user_id=auth.uid())
$$;
CREATE FUNCTION public.support_rep(company uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
  SELECT EXISTS (SELECT 1 FROM public.support_memberships m WHERE m.company_id=company AND m.user_id=auth.uid() AND m.role IN ('rep','admin'))
$$;
REVOKE ALL ON FUNCTION public.support_member(uuid),public.support_rep(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.support_member(uuid),public.support_rep(uuid) TO authenticated;

CREATE TABLE public.support_documents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), company_id uuid NOT NULL REFERENCES public.support_companies(id),
  title text NOT NULL, storage_key text, status text NOT NULL DEFAULT 'indexing', UNIQUE(company_id,id)
);
CREATE TABLE public.support_chunks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), company_id uuid NOT NULL, document_id uuid NOT NULL,
  page integer NOT NULL CHECK(page>0), kind text NOT NULL CHECK(kind IN ('text','image')),
  content text NOT NULL, image_key text, embedding vector(768) NOT NULL,
  FOREIGN KEY(company_id,document_id) REFERENCES public.support_documents(company_id,id)
);
CREATE INDEX ON public.support_chunks(company_id,kind);
CREATE INDEX ON public.support_chunks USING hnsw(embedding vector_cosine_ops);
CREATE TABLE public.support_conversations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), company_id uuid NOT NULL REFERENCES public.support_companies(id),
  user_id uuid NOT NULL DEFAULT auth.uid() REFERENCES auth.users(id), question text NOT NULL,
  answer text, citations jsonb NOT NULL DEFAULT '[]', status text NOT NULL DEFAULT 'processing', UNIQUE(company_id,id)
);
CREATE TABLE public.support_tickets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), company_id uuid NOT NULL, conversation_id uuid NOT NULL,
  question text NOT NULL, rejected jsonb NOT NULL DEFAULT '[]', reply text, status text NOT NULL DEFAULT 'open',
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY(company_id,conversation_id) REFERENCES public.support_conversations(company_id,id)
);
CREATE TABLE public.support_decisions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), company_id uuid NOT NULL, conversation_id uuid NOT NULL,
  attempt integer NOT NULL, outcome text NOT NULL, detail jsonb NOT NULL,
  FOREIGN KEY(company_id,conversation_id) REFERENCES public.support_conversations(company_id,id)
);
CREATE TABLE public.support_jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), company_id uuid NOT NULL REFERENCES public.support_companies(id),
  document_id uuid NOT NULL, completed integer NOT NULL DEFAULT 0,total integer NOT NULL DEFAULT 0,
  status text NOT NULL DEFAULT 'queued', error text,
  FOREIGN KEY(company_id,document_id) REFERENCES public.support_documents(company_id,id)
);
CREATE TABLE public.support_thresholds (
  company_id uuid PRIMARY KEY REFERENCES public.support_companies(id),
  text_relevance real NOT NULL DEFAULT .42 CHECK(text_relevance BETWEEN 0 AND 1),
  text_answerability real NOT NULL DEFAULT .55 CHECK(text_answerability BETWEEN 0 AND 1),
  image_relevance real NOT NULL DEFAULT .46 CHECK(image_relevance BETWEEN 0 AND 1),
  image_answerability real NOT NULL DEFAULT .60 CHECK(image_answerability BETWEEN 0 AND 1)
);

ALTER TABLE public.support_companies ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.support_memberships ENABLE ROW LEVEL SECURITY;
CREATE POLICY company_read ON public.support_companies FOR SELECT TO authenticated USING(public.support_member(id));
CREATE POLICY membership_read ON public.support_memberships FOR SELECT TO authenticated USING(user_id=auth.uid());
-- Memberships are provisioned by a trusted project administrator, never by a customer.
GRANT SELECT ON public.support_companies,public.support_memberships TO authenticated;

DO $$ DECLARE table_name text; BEGIN
  FOREACH table_name IN ARRAY ARRAY['support_documents','support_chunks','support_thresholds'] LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY',table_name);
    EXECUTE format('CREATE POLICY member_read ON public.%I FOR SELECT TO authenticated USING(public.support_member(company_id))',table_name);
    EXECUTE format('CREATE POLICY rep_insert ON public.%I FOR INSERT TO authenticated WITH CHECK(public.support_rep(company_id))',table_name);
    EXECUTE format('CREATE POLICY rep_update ON public.%I FOR UPDATE TO authenticated USING(public.support_rep(company_id)) WITH CHECK(public.support_rep(company_id))',table_name);
    EXECUTE format('GRANT SELECT,INSERT,UPDATE ON public.%I TO authenticated',table_name);
  END LOOP;
  FOREACH table_name IN ARRAY ARRAY['support_tickets','support_decisions','support_jobs'] LOOP
    EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY',table_name);
    EXECUTE format('CREATE POLICY rep_access ON public.%I FOR ALL TO authenticated USING(public.support_rep(company_id)) WITH CHECK(public.support_rep(company_id))',table_name);
    EXECUTE format('GRANT SELECT,INSERT,UPDATE ON public.%I TO authenticated',table_name);
  END LOOP;
END $$;
ALTER TABLE public.support_conversations ENABLE ROW LEVEL SECURITY;
CREATE POLICY conversation_read ON public.support_conversations FOR SELECT TO authenticated
  USING(public.support_member(company_id) AND (user_id=auth.uid() OR public.support_rep(company_id)));
CREATE POLICY conversation_create ON public.support_conversations FOR INSERT TO authenticated
  WITH CHECK(public.support_member(company_id) AND user_id=auth.uid() AND status='processing' AND answer IS NULL AND citations='[]'::jsonb);
CREATE POLICY conversation_write ON public.support_conversations FOR UPDATE TO authenticated
  USING(public.support_rep(company_id)) WITH CHECK(public.support_rep(company_id));
GRANT SELECT,INSERT,UPDATE ON public.support_conversations TO authenticated;

CREATE FUNCTION public.match_support_chunks(query_embedding vector(768), requested_company uuid, requested_kind text, result_limit integer DEFAULT 8)
RETURNS TABLE(id uuid,document_id uuid,page integer,kind text,content text,image_key text,similarity double precision)
LANGUAGE sql STABLE SECURITY INVOKER SET search_path=public,pg_temp AS $$
  SELECT c.id,c.document_id,c.page,c.kind,c.content,c.image_key,1-(c.embedding <=> query_embedding)
  FROM public.support_chunks c JOIN public.support_documents d ON d.id=c.document_id AND d.company_id=c.company_id
  WHERE c.company_id=requested_company AND public.support_member(requested_company)
    AND c.kind=requested_kind AND d.status='complete'
  ORDER BY c.embedding <=> query_embedding LIMIT LEAST(GREATEST(result_limit,1),32)
$$;
REVOKE ALL ON FUNCTION public.match_support_chunks(vector,uuid,text,integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.match_support_chunks(vector,uuid,text,integer) TO authenticated;

INSERT INTO storage.buckets(name,public) VALUES('support-documents',false);
-- Company UUID is the first storage-key segment. Restrictive guard composes with
-- InsForge's existing permissive owner policies, preventing cross-company access.
CREATE FUNCTION public.support_storage_company(object_key text) RETURNS uuid
LANGUAGE plpgsql IMMUTABLE AS $$ BEGIN
  RETURN split_part(object_key,'/',1)::uuid;
EXCEPTION WHEN invalid_text_representation THEN RETURN NULL; END $$;
CREATE POLICY support_storage_guard ON storage.objects AS RESTRICTIVE FOR ALL TO authenticated
  USING(bucket!='support-documents' OR public.support_member(public.support_storage_company(key)))
  WITH CHECK(bucket!='support-documents' OR public.support_rep(public.support_storage_company(key)));
CREATE POLICY support_storage_read ON storage.objects FOR SELECT TO authenticated
  USING(bucket='support-documents' AND public.support_member(public.support_storage_company(key)));
CREATE POLICY support_storage_insert ON storage.objects FOR INSERT TO authenticated
  WITH CHECK(bucket='support-documents' AND public.support_rep(public.support_storage_company(key)));
COMMIT;
