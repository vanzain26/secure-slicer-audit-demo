# Audit Portal Full Dashboard

This package includes:
- Flask web app
- SQLite database initialization
- hashed passwords
- manufacturer and auditor roles
- dashboard landing page
- uploads for public batch JSON and audit bundles
- verifier aligned to the instrument_id leaf schema
- saved JSON reports

Demo accounts:
- manufacturer1 / demo123
- manufacturer2 / demo123
- auditor1 / demo123

Run:
```bash
pip install -r requirements.txt
python app.py
```

The database is created automatically in `instance/portal.db` on first run.
