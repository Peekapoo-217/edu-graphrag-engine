# THPT PDF extraction worker

The standalone `pdf-worker` service consumes pending extraction jobs created by the backend. It downloads the private PDF from Cloudflare R2, extracts embedded text or runs OCRmyPDF/Tesseract, validates the LLM response against the public `subjects` catalog, and writes review drafts. The worker never publishes to `subject_groups`.

## PostgreSQL contract

The backend Flyway migration V6 places workflow tables in the `admin` schema. The worker reads and writes `admin.extraction_jobs`, `admin.school_document_versions`, `admin.school_documents`, and `admin.subject_group_drafts`. The subject catalog remains in `public.subjects`.

Apply backend migrations through V6 before starting the worker. Use the same target PostgreSQL database in backend and worker configuration. The connection account must have access to both `admin` workflow tables and `public.subjects`.

## Configuration

Set R2 endpoint, bucket, access key ID, secret access key, `DATABASE_URL` (or backend datasource fallback), internal API URL, and shared internal service token in local environment files. Do not commit actual credentials. For OCR, the worker image installs OCRmyPDF and Tesseract with Vietnamese and English language data.

The worker is included in the monorepo's root `docker-compose.yml` as `pdf-worker`. It retries failed jobs up to `PDF_WORKER_MAX_ATTEMPTS`; operators should inspect the backend document/job status and logs for safe error codes.
