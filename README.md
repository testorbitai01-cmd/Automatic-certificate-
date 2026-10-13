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
| 4. HR review, corrections, duplicates, completion and per-certificate approval | Done |
| 5. Certificate template management and preview | Done |
| 6. Personalisation, certificate generation and PDF conversion | Done (awaiting review) |
| 7–10 | Not started |

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

### Phase 4 — HR review and approval workflow

Nothing in this phase creates, downloads or sends a certificate. Production issuance stays disabled.

1. **Submissions** (http://127.0.0.1:8000/hr/submissions/): search by name, registration number, email, mobile
   or college; filter by campaign, domain, status and duplicate-review state.
2. **Submission page**: full details (plus the values exactly as typed), validator notes, internal HR notes and
   the full history (from the append-only audit log). Decisions:
   * *Mark as under review* → *Approve details* (optional note) or *Reject* (reason required).
   * *Request correction*: tick the fields the student must fix and explain why. A private link is shown
     **once** (only its SHA-256 hash is stored). It expires after 7 days (`registration.correction_link_days`),
     works once, lets the student change only the ticked fields, and runs the same validation as registration.
     HR can create a new link (the old one stops working) or cancel the request. After the student resubmits,
     the submission returns to *Submitted* and the before/after values are shown on the submission page.
   * *Reopen* (reason required) moves a verified or rejected submission back to review.
   * Approval is blocked while any possible-duplicate flag is *pending* or *requires further review*, or while
     the requested domain is still pending.
3. **Possible duplicates**: record *Confirmed duplicate*, *Not a duplicate* or *Requires further review* with a
   reason. Records are never merged or deleted.
4. **Internship page** (link from the submission): separate decisions, each with reviewer and time:
   * *Completion* — verified actual dates, or *Not completed* (note required). Registration alone is never
     treated as completion.
   * *Project* — completed, or not accepted (note required). Title changes go through a student correction.
   * *Attendance history* — online/offline periods for the record only; never used for eligibility or delivery.
   * *LOR recommendation* — recommend (mentor feedback and performance evaluation required) or not.
   * *Certificate approvals* — one row per certificate type with its unmet requirements:

     | Certificate | Requires (in addition to verified registration, resolved duplicates, active domain) |
     |---|---|
     | Offer Letter | nothing else — may be approved before the internship starts |
     | Experience / Internship | completion verified |
     | Project Completion | project verified |
     | LOR | completion verified **and** a *recommended* decision |

     Approving one certificate never approves another. Reject / *needs correction* / reset need a reason.

     **LOR final authority: Super Admin only.** HR Admins record the separate *recommendation* decision, but
     every final LOR decision (approve, reject, needs correction, reset) requires the `certificates.approve_lor`
     permission, which only the Super Admin role has. It is enforced in the service layer (a crafted request
     from an HR Admin gets 403 and changes nothing). Automatic withdrawal still applies when HR changes the
     underlying facts. To list LOR approvals made before this rule by accounts without that permission
     (read-only; nothing is changed): `.venv\Scripts\python manage.py check_lor_approvals`.
5. **Automatic withdrawal**: if the facts behind an approval change, the approval returns to *Pending* with an
   audit entry — reopening or requesting corrections withdraws all four; a completion change withdraws
   Experience and LOR; a project change withdraws Project; a recommendation change withdraws LOR. Once a
   certificate has been created (Phase 6), these changes are refused and will need the reissue process.

Every transition locks the record and checks the status the reviewer saw, so double clicks and two reviewers
acting at once cannot both succeed. The audit log records decisions and reasons, but not passwords, tokens,
note text, mentor feedback or corrected personal values.

To try it: register a test student through a campaign link (see above), then sign in as HR and open
**Submissions**. Phase 4 tests only:

```powershell
.venv\Scripts\python -m pytest tests\test_review.py tests\test_corrections.py tests\test_approvals.py
.venv\Scripts\python -m pytest -m browser tests\browser\test_review_browser.py
```

### Phase 5 — Certificate templates

**Templates** (http://127.0.0.1:8000/hr/templates/) holds the four types: Offer Letter, Experience / Internship,
Project Completion and LOR. The system never writes certificate wording, logos, signatures or seals: all of
that comes from the uploaded Gradtwin Word file.

1. **Upload** (HR Admin or Super Admin): the original Gradtwin `.docx` with student-specific text replaced by
   approved placeholders, e.g. `{{ full_name }}`, `{{ start_month_year }}`. Tick *authentic Gradtwin template*
   only for official files; anything else is a **TEST** template that can never be used for production.
   Uploads are checked and refused with reasons if they are not a plain `.docx` (max 5 MB), are macro-enabled,
   encrypted, contain embedded objects, load external content (ordinary hyperlinks are fine), contain
   DOCTYPE/ENTITY declarations or look like a zip bomb; or if they use template logic (`{% %}`), filters,
   unknown placeholders, placeholders not allowed for that type, or no placeholders at all (a filled-in
   certificate rather than a template). Placeholders may appear only in the visible text of the body, headers
   and footers: template syntax in comments, footnotes/endnotes, field codes, tracked changes, document
   settings or image descriptions is refused (accept all tracked changes and delete comments before
   uploading). Damaged files are refused cleanly. The approved list is shown on the upload page
   (`apps/doctemplates/placeholders.py`). Files are stored privately under random names; there is no URL,
   downloads go through a permission-checked, audited view.
2. **Preview**: each version page shows the template text in document order with placeholders replaced by
   clearly labelled **synthetic** values, escaped as HTML. It is text-only (no fonts, layout or images) and
   creates, saves and sends nothing.
3. **Submit for review** (HR) → **Approve and activate** (Super Admin only, after confirming the version was
   compared with the original). **Separation of duties:** nobody can approve or activate a version they
   uploaded — enforced in the service layer (HTTP 403) and by a database constraint; a Super Admin who
   uploads a template needs a second Super Admin to activate it. (Rejecting or deactivating one's own
   upload is allowed.) Activation retires the previously active version in the same transaction, so
   only one version per type is ever active. Super Admins can also **reject** drafts/pending versions and
   **deactivate** the active one (reason required), or reactivate a retired version. A TEST template can never
   replace an active authentic template (deactivate the authentic one first, deliberately), and TEST templates
   cannot be activated at all once production issuance is enabled.
4. Production use still requires an *authentic* active version **and** `CERT_PRODUCTION_ISSUANCE_ENABLED=true`
   (off). Nothing in Phase 5 fills, prints, generates or sends a certificate.

Uploads, submissions, activations, retirements, rejections, deactivations and downloads are audited with
version ids, hashes and counts only — never filenames (the office's sample files are named after students)
or document content.

To try it with a synthetic test file (no Gradtwin content):

```powershell
.venv\Scripts\python manage.py make_test_template offer_letter test-offer.docx
```

Open questions before Phase 6 fills real templates: pronouns for students whose gender is *Others*; how the
salutation/LOR short name is entered; and whether a reference number is printed (`{{ reference_no }}`).

Phase 5 tests only:

```powershell
.venv\Scripts\python -m pytest tests	est_templates.py
.venv\Scripts\python -m pytest -m browser testsrowser	est_templates_browser.py
```

### Phase 6 — Personalisation and certificate generation

**Registration** now asks every student for **pronouns** (He/him, She/her, They/them — required, never inferred
from gender), an optional **preferred name** (used only for the Offer Letter salutation and the LOR short-name
slots; the full name is used when it is blank) and offers the titles **Mr., Ms., Mrs., Mx.** and **No title**
(`registration.titles`, `registration.allow_no_title`). HR corrects these through the existing correction link
(new groups *Preferred name* and *Pronouns*). Students registered earlier have no pronouns; generation then
stops and asks HR to collect them.

**Generation** (Internship page → *Certificate approvals* → *Generate draft PDF*, permission
`certificates.generate_certificate`):

1. Checks: the certificate type is approved (an LOR by a Super Admin), an approved template is active and its
   stored file matches its checksum, every placeholder the template uses has a value (pronouns, project title,
   etc.), the issue date is not in the future, and no live draft/issued certificate exists.
2. Fills the template (`apps/certificates/docxfill.py`): only approved `{{ placeholders }}`, located across
   Word's text runs; values are inserted as plain XML text (no template engine), so student input can never
   become markup or code. All other parts of the file are copied byte-for-byte.
3. Converts to PDF with the internal **Gotenberg/LibreOffice** service (`apps/certificates/pdf.py`), then checks
   the PDF: one A4 page, letterhead and signature/seal images present, fonts present, every filled value
   visible, no leftover placeholder text.
4. Stores the PDF privately with its SHA-256, the template version and a snapshot of the values used, as a
   **draft** (*not issued*). Audit entries hold ids and the hash only.

**Production lock.** While `CERT_PRODUCTION_ISSUANCE_ENABLED` is off (the default), authentic templates can only
produce **sample-data proofs** (template page → *Download PDF proof*, with he/she/they), never a real student's
certificate. TEST templates can produce drafts for testing that can never be issued. *Issue* is refused until
production is enabled and the draft came from the active authentic template. No email is sent in this phase.

**Who does what.** HR Admins (and Super Admins) generate and cancel drafts (`certificates.generate_certificate`)
and download them (`certificates.download_certificate`). **Only Super Admins officially issue certificates**
(`certificates.issue_certificate`, granted to the Super Admin role only); this is checked in the view and again
in the service before anything else, and issuance re-checks the production switch, draft state, active authentic
template and approval under row locks, so retries and concurrent requests cannot issue twice.

**Converter.** `docker-compose.yml` runs `gotenberg/gotenberg:8` on an internal network (no published port, no
internet access, read-only filesystem). Set `PDF_CONVERTER_URL` (compose sets `http://pdf:3000`). The image
has no Microsoft fonts: Times New Roman is rendered with the metric-compatible **Liberation Serif** (same line
breaks and positions, slightly different glyph shapes). Installing Microsoft's core fonts in a derived image
requires accepting Microsoft's EULA.

Local converter for development and the converter integration tests:

```powershell
docker run -d --rm --name gradtwin-pdf -p 127.0.0.1:3000:3000 gotenberg/gotenberg:8
$env:PDF_CONVERTER_URL = "http://127.0.0.1:3000"                 # for runserver
$env:GRADTWIN_TEST_PDF_CONVERTER_URL = "http://127.0.0.1:3000"   # enables tests\test_generation_converter.py
.venv\Scripts\python -m pytest tests\test_generation.py tests\test_personalisation.py tests\test_generation_converter.py
```

**Authentic templates.** The four Gradtwin templates were prepared outside this repository (they are office
documents, not source code) from the authentic Word files with the approved HR decisions, by
`build_templates.py` in `C:\Users\user\Documents\Gradtwin-templates\`. They still have to go through the normal
review: HR uploads each `.docx` from `output\` on the Templates page with *authentic Gradtwin template* ticked and
submits it; a **different** Super Admin downloads the PDF proofs (he/she/they), compares them with the original
certificates and approves. Until then, and while production issuance is off, no real certificate can be produced.

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

Limit request bodies at the proxy as well (template uploads are at most 5 MB; e.g. nginx
`client_max_body_size 6m;`). The app also refuses oversized uploads (HTTP 413) before parsing them.

Put a TLS-terminating reverse proxy in front of port 8000 and set `TRUSTED_PROXY_COUNT` to the number of
proxies that append to `X-Forwarded-For` (one nginx/Caddy => `1`). This must be exact: with `0` behind a proxy,
every student shares the proxy's IP address for rate limits; with a number that is too high, clients can spoof
their IP. Also set `PUBLIC_BASE_URL` so QR codes encode the public address.

### Keeping correction tokens out of logs

Correction links contain a secret token in the path (`/correct/<token>/`).

* **Django** (request warnings, development server): a logging filter replaces it with `/correct/[redacted]/`
  (`apps/core/logging.py`, enabled in `config/settings.py`). Tested.
* **Correction pages** send `Referrer-Policy: strict-origin` (other sites never see the path),
  `X-Robots-Tag: noindex, nofollow` and `Cache-Control: no-store`, and load no third-party resources.
* **Gunicorn**: its access log is off unless `--access-logfile` is given (the Dockerfile does not enable it).
  If you enable it, do not log the request path for `/correct/`.
* **Reverse proxy (required)**: nginx/Caddy log every request line by default. Disable access logging for the
  correction path, e.g. for nginx:

  ```nginx
  location /correct/ {
      access_log off;            # the path contains a one-time secret token
      proxy_pass http://web:8000;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto $scheme;
      proxy_set_header Host $host;
  }
  ```

  Caddy: put `log_skip` (or a `skip_log` matcher on older versions) on `path /correct/*`.
  Also check any CDN, load balancer or WAF in front of the proxy.

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
                   views.py (public form, HR campaigns, QR, domains); review.py + views_review.py
                   (HR review, duplicates, correction links)
apps/internships   Internship, AttendancePeriod, Recommendation; services.py (completion, project,
                   attendance, recommendation), views.py (HR internship page)
apps/doctemplates  CertificateTemplate, TemplateVersion, TemplateField; placeholders.py (approved list),
                   docx.py (safe .docx inspection + preview), services.py (upload/review/activation), views.py
apps/certificates  approvals.py (per-certificate prerequisites and decisions); values.py, docxfill.py, pdf.py,
                   generation.py (Phase 6 generation), views.py (downloads, issue/cancel); CertificateRequest, Issuance, GeneratedFile, EmailJob, EmailAttempt, PrintEvent, Handover
docs/data-model.md ER diagram and database-enforced rules
```
