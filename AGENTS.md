# Data Scrawler

Follow the repository-level instructions in `../AGENTS.md`.

- Preserve the official source URL, admission year, and collection metadata.
- Normalize source data before persistence. Validate required fields, duplicate records, JSON values, and numeric values.
- Keep crawler logic, PostgreSQL import logic, and Neo4j synchronization separate.
- Keep PostgreSQL imports idempotent and use existing upsert behavior where supported.
- Do not create, drop, recreate schemas, or delete existing PostgreSQL or Neo4j data.
- Synchronize Neo4j only from normalized PostgreSQL tables; PostgreSQL remains the source of truth.
- Never place aptitude tests in the regular high-school subject dictionary.
- Never expose credentials in source code or logs.
- Run tests with `python -m unittest discover -s tests`.
- Use the documented execution entry points: `python crawl_admissions.py`, `python import_to_postgres.py`, and `python sync_postgres_to_neo4j.py`. Use `--dry-run` where the command supports it before a write operation.
