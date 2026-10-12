# Gradtwin Certificate Management System

Registration, approval, generation and distribution of Gradtwin internship certificates
(Offer Letter, Experience / Internship Certificate, Project Completion Certificate, Letter of Recommendation).

**Template policy:** only official Gradtwin templates, uploaded and approved inside the application, may be used
for production certificates. Until they are provided, the system works with files marked
`TEST TEMPLATE – NOT FOR ISSUE`, and production issuance stays disabled.

## Status

| Phase | State |
|---|---|
| 1. Inspection and plan | Done |
| 2. Foundation (schema, auth, roles, audit, config, tests) | Done |
| 3. QR registration campaigns and student form | Done |
| 3.1 PostgreSQL + browser verification, shared rate limits | Done |
| 4–10 | Not started |

## Local development (Windows, PowerShell)

```powershell
cd C:\Users\user\gradtwin-certificates
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
$env:DJANGO_DEBUG = "true"          # uses SQLite in db.sqlite3 unless DATABASE_URL is set
.venv\Scripts\python manage.py migrate
.venv\Scripts\python manage.py sync_roles
.venv\Scripts\python manage.py create_admin hr@example.com --role "HR Admin"   # prompts for a password
.venv\Scripts\python manage.py runserver
```

`$env:DJANGO_DEBUG` applies only to the current PowerShell window; set it again in a new window.

### Trying the Phase 3 flow

1. Open http://127.0.0.1:8000/accounts/login/ and sign in with the account created above.
2. Go to **Campaigns** (http://127.0.0.1:8000/hr/campaigns/) → **New campaign**. Choose opening/closing times
   (India Standard Time) and an optional default domain.
3. The campaign page shows the registration link, the QR code (PNG/SVG download) and the submissions table.
   Copy the link (`http://127.0.0.1:8000/register/<token>/`) into a browser, or scan the QR from a phone on the
   same network (see below).
4. Submit the student form. You should see:
   *“Thank you for your response. Your details have been submitted successfully.”*
5. Back on the campaign page, the submission appears as **Submitted** (pending HR review), with any
   possible-duplicate flags and review notes.
6. **Domains** (http://127.0.0.1:8000/hr/domains/) lists student-requested domains as pending; approve, reject,
   rename, activate or deactivate them there.

To test the QR code from a phone, run the server on your LAN address and tell the app which URL to encode:

```powershell
$env:PUBLIC_BASE_URL = "http://<your-PC-LAN-IP>:8000"
$env:DJANGO_ALLOWED_HOSTS = "<your-PC-LAN-IP>,127.0.0.1,localhost"
.venv\Scripts\python manage.py runserver 0.0.0.0:8000
```

### Registration policy settings

Defaults live in `apps/core/policy.py` and can be overridden per key in the `Setting` table
(e.g. `registration.gmail_only = false`). Current defaults: titles Mr./Ms./Mrs.; registered email must be Gmail;
alternative email optional; feedback optional, max 2,000 characters; internship period at most 366 days.

Duplicate rules: the same university registration number (ignoring case, spaces and `-` `/` `.` `_`) cannot be
submitted twice in one campaign (enforced by a database constraint). A matching registration number, email or
mobile number in another campaign is **flagged for HR review**, never rejected automatically.

## Tests

```powershell
cd C:\Users\user\gradtwin-certificates
.venv\Scripts\python -m pytest                       # default suite (browser tests excluded)
.venv\Scripts\python -m pytest tests\test_validation.py tests\test_registration.py tests\test_campaigns_hr.py   # Phase 3 only
```

Seven tests run only on PostgreSQL (audit-log trigger, truly concurrent submissions and rate-limit
hits) and are skipped on SQLite. **Run the full suite on PostgreSQL before every release.**
A throwaway container (no volume; removed when stopped; bound to localhost only):

```powershell
docker run -d --rm --name gradtwin-test-pg -e POSTGRES_USER=gradtwin_test -e POSTGRES_PASSWORD=<random> `
  -e POSTGRES_DB=gradtwin_test -p 127.0.0.1:55432:5432 postgres:17
$env:DATABASE_URL = "postgres://gradtwin_test:<random>@127.0.0.1:55432/gradtwin_test"
.venv\Scripts\python -m pytest --create-db -m "browser or not browser"   # everything, incl. browser tests
docker stop gradtwin-test-pg
Remove-Item Env:DATABASE_URL
```

pytest-django creates and drops its own `test_gradtwin_test` database; nothing else is touched.

### Browser tests

Real-browser tests (Playwright driving the installed Microsoft Edge, desktop and phone viewports) live in
`tests/browser/` and are excluded from the default run:

```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt     # once; no browser download needed
.venv\Scripts\python -m pytest -m browser
$env:PLAYWRIGHT_CHANNEL = "chrome"; .venv\Scripts\python -m pytest -m browser   # use Chrome instead
```

## Production (Docker)

```bash
cp .env.example .env        # fill in secrets; add POSTGRES_PASSWORD
docker compose up -d --build
docker compose exec web python manage.py migrate
docker compose exec web python manage.py sync_roles
docker compose exec web python manage.py create_admin hr@example.com --role "Super Admin"
```

Put a TLS-terminating reverse proxy in front of port 8000 and set `TRUSTED_PROXY_COUNT` to the number of
proxies that append to `X-Forwarded-For` (one nginx/Caddy => `1`). This must be exact: with `0` behind a proxy,
every student shares the proxy's IP address for rate limits; with a number that is too high, clients can spoof
their IP. Also set `PUBLIC_BASE_URL` so QR codes encode the public address.

### Rate limiting

Login lockout (5 failures per email+IP per 15 minutes) and the public form limit (30 POSTs per IP per hour) are
stored in the database table `core_ratelimitcounter`, not in process memory, so all gunicorn workers and
containers share them and they survive restarts. Identifiers are stored as SHA-256 hashes. Expired windows are
purged automatically. Limits are fixed windows, so a client can make up to twice the limit across a window boundary.

## Roles

Defined in `apps/accounts/roles.py`. The backend checks permissions, not role names.

- **HR Admin** — campaigns, domains, review, verification, certificate approval, generation, email, print, handover,
  template upload/mapping, audit view, export.
- **Super Admin** — everything above plus user management, system settings, **template activation**,
  reissue authorization, revocation and sensitive audit events.

## Layout

```
config/            settings (all secrets from environment), urls
apps/accounts      staff users (email login, Argon2), roles, login throttling
apps/core          append-only AuditLog, Setting, Job queue table
apps/registry      Domain, Campaign, Student, StudentEmail, Submission, DuplicateCandidate;
                   validation.py (field rules), forms.py, services.py (submission + duplicate flags),
                   views.py (public form, HR campaigns, QR, domains)
apps/internships   Internship, AttendancePeriod, Recommendation
apps/doctemplates  CertificateTemplate, TemplateVersion, TemplateField
apps/certificates  CertificateRequest, Issuance, GeneratedFile, EmailJob, EmailAttempt, PrintEvent, Handover
docs/data-model.md ER diagram and database-enforced rules
```
