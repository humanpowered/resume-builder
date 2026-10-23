# Running the test site on Google Cloud

The tester site runs on Cloud Run at test.plumblineresume.com, with records in
Cloud SQL (Postgres). Cloud Run, Cloud SQL, secrets and logs live in GCP
project `plumbline-test`, region `us-east1`. Firebase sign-in is a separate
project, `plumbline-test-4d71a`. Keys go into Secret Manager and nowhere else:
not into the repository, the image, or a chat.

## Settings

| Setting | Where it comes from | Value |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | Secret `anthropic-api-key` | The test site's own key, in a workspace with a $150 monthly limit |
| `RB_SESSION_SECRET` | Secret `session-secret` | A long random string; changing it signs everyone out |
| `RB_DATABASE_URL` | Secret `database-url` | `postgresql://plumbline:PASSWORD@/plumbline?host=/cloudsql/plumbline-test:us-east1:plumbline-db` |
| `RB_FIREBASE_PROJECT` | Plain variable | `plumbline-test-4d71a` |
| `RB_FIREBASE_API_KEY` | Plain variable | The Firebase web API key (public by design) |
| `RB_INVITE_EMAILS` | Plain variable | Comma-separated tester emails |
| `RB_GA_ID` | Plain variable, optional | Google Analytics measurement ID for the test app's own data stream (`G-...`); leave unset to keep analytics off |
| `RB_CAP_TESTER_USD`, `RB_CAP_MONTH_USD`, `RB_CAP_CALLS_HOUR`, `RB_CAP_OVERRIDES` | Plain variables, optional | Defaults: 5, 150, 60, none |
| `RB_FREE_TAILORED` | Plain variable, optional | Samples written for a posting each tester gets; default 20 |
| `RB_LINKEDIN_REWRITES` | Plain variable, optional | LinkedIn profile rewrites each tester gets after the first profile; default 10 |

Use a database password of letters, digits and only the symbols `- _ . ~`,
so it can sit in the address without escaping.

## Cloud Run service

- Source: this GitHub repository, built from the `Dockerfile`, redeployed on
  each push to the test branch.
- Region `us-east1`, minimum instances 0, **maximum instances 2**.
- Allow unauthenticated access (the app has its own sign-in).
- Connections: add the Cloud SQL instance.
- Security: the service account needs the roles *Cloud SQL Client* and
  *Secret Manager Secret Accessor*.

## Domain

Map `test.plumblineresume.com` to the service (Cloud Run, Manage custom
domains). Add the record it shows in Cloudflare as **DNS only** (grey cloud),
then wait for the certificate. The address is already an Authorized domain in
Firebase.

## Usage events to BigQuery

The app writes one JSON line per usage event. In Logging, Log Router, create
a sink to a new BigQuery dataset `plumbline_usage` with the filter:

```
resource.type="cloud_run_revision" AND jsonPayload.rb_event:*
```

## Checking it works

Sign in at https://test.plumblineresume.com with an invited email, read the
"Before you start" notice, upload a resume, and make a sample resume. Then
check that a row reached BigQuery.

## Reports and resets

These read the same `RB_DATABASE_URL`. From Cloud Shell, start the Cloud SQL
Auth Proxy and point the address at it, then:

```bash
python -m web.spend_report                      # spend per tester and this month
python -m web.export_tester --list              # who has a record
python -m web.export_tester --all               # Word files under exports/
python -m web.wipe_test_data --yes              # empty the site between rounds
```
