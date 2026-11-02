CREATE TABLE cm_sessions (
  scope text NOT NULL,
  id text NOT NULL CHECK (id ~ '^[a-f0-9]{64}$'),
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  PRIMARY KEY (scope, id)
);
CREATE TABLE cm_tasks (
  scope text NOT NULL,
  id text NOT NULL CHECK (id ~ '^[a-f0-9]{32}$'),
  session_id text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('source', 'search', 'compare')),
  status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'error')),
  payload jsonb NOT NULL,
  result jsonb,
  error text,
  pdf_key text,
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 3),
  worker_id text,
  lease_token text,
  lease_expires_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL DEFAULT now() + interval '24 hours',
  PRIMARY KEY (scope, id),
  FOREIGN KEY (scope, session_id) REFERENCES cm_sessions(scope, id)
);
CREATE INDEX cm_tasks_claim ON cm_tasks(scope, status, created_at);
CREATE INDEX cm_tasks_owner ON cm_tasks(scope, session_id, expires_at);
CREATE INDEX cm_tasks_rate ON cm_tasks(scope, session_id, kind, created_at);
CREATE TABLE cm_artifacts (
  scope text NOT NULL,
  id text NOT NULL CHECK (id ~ '^[a-f0-9]{32}$'),
  session_id text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('source', 'report')),
  value jsonb NOT NULL,
  pdf_key text,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL,
  PRIMARY KEY (scope, id),
  FOREIGN KEY (scope, session_id) REFERENCES cm_sessions(scope, id)
);
CREATE INDEX cm_artifacts_owner ON cm_artifacts(scope, session_id, expires_at);
CREATE TABLE cm_workers (
  scope text NOT NULL,
  id text NOT NULL,
  last_seen timestamptz NOT NULL,
  PRIMARY KEY (scope, id)
);
CREATE TABLE cm_pdf_gc (
  scope text NOT NULL,
  pdf_key text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  not_before timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (scope, pdf_key)
);
