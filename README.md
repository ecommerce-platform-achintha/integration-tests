# integration-tests

Black-box HTTP tests (pytest + httpx) for the marketplace: user-service, store-service, product-service,
order-service and the api-gateway. The tests never start or stop services: the whole stack must already be running.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # optional: every setting has a local default
```

## Configure

Settings come from environment variables (or `.env`, which is git-ignored). Defaults are in `tests/config.py`.

| Variable | Default | Purpose |
|---|---|---|
| `USER_URL` / `PRODUCT_URL` / `ORDER_URL` / `STORE_URL` | `http://localhost:8081` / `8082` / `8083` / `8084` | Service ports (direct mode) |
| `GATEWAY_URL` | `http://localhost:8080` | api-gateway |
| `EUREKA_URL` | `http://localhost:8761` | Eureka (smoke test only) |
| `BASE_MODE` | `direct` | `direct`: each call goes to the owning service. `gateway`: every call goes to `GATEWAY_URL` |
| `SUPER_ADMIN_EMAIL` | `superadmin@example.com` | Must match user-service's bootstrap value |
| `SUPER_ADMIN_INITIAL_PASSWORD` | local dev value | user-service's `SUPER_ADMIN_INITIAL_PASSWORD` |
| `SUPER_ADMIN_NEW_PASSWORD` | local dev value | What the suite changes the super admin password to on its first run |
| `GATEWAY_CONFIG_PATH` | `../api-gateway/src/main/resources/application.yml` | Used to check `tests/routing.py` against the gateway routes |
| `HTTP_TIMEOUT_SECONDS` | `15` | Per-request timeout |

The super admin fixture logs in with the NEW password first and falls back to the INITIAL password plus
`/api/auth/change-password` only on a 401. Once the password has been changed, a run costs no failed login. Only the
very first run costs one, which counts towards the per-user lockout (5 per 15 min) and the per-IP lockout (20 per
15 min). If neither password works, the fixture stops after that second attempt: fix the variables rather than
re-running, or the account locks.

## Run

```bash
.venv/bin/pytest -m stage0 -v        # smoke: health, JWKS, Eureka, super admin login, helpers
.venv/bin/pytest -m stage1 -v        # user-service
.venv/bin/pytest -m stage1_destructive -v   # user-service tests on throwaway merchants (bans, lockouts)
.venv/bin/pytest -m stage2 -v        # store-service
.venv/bin/pytest -m stage3 -v        # product-service
.venv/bin/pytest -m stage4 -v        # order-service end to end
.venv/bin/pytest -m "not slow" -v    # skip event-driven / long scenarios
```

Reports are written to `reports/junit.xml` and `reports/report.html` on every run.

### Through the gateway

```bash
BASE_MODE=gateway .venv/bin/pytest -m gateway -v
```

Run the gateway stage on its own. Its `auth` rate-limit tier allows **10 requests per minute per client IP**, shared
by login, refresh and every registration. A normal stage run creates several actors, each one a registration plus a
login, so running the full suite with `BASE_MODE=gateway` would hit 429s.

> **Note:** through the gateway, user-service sees every request coming from the gateway's address. The user-service
> IP lockout (`auth.ip-max-failed-logins`, 20 failures per 15 min, or the last `X-Forwarded-For` entry when
> `AUTH_TRUST_FORWARDED_FOR=true`) and the gateway's `auth` bucket (10/min) are both keyed on IP. Failed-login tests
> run through the gateway can therefore lock out logins for every client behind the same address, for 15 minutes.

## Markers

| Marker | Meaning |
|---|---|
| `stage0` … `stage5` | The stage a test belongs to (0 smoke, 1 user, 2 store, 3 product, 4 order, 5 gateway) |
| `stage1_destructive` | Bans and lockouts on dedicated throwaway accounts. Never touches the shared fixtures |
| `gateway` | Goes through the api-gateway. Stays under the auth-tier budget |
| `slow` | Waits for Kafka events or runs long multi-step flows |
| `skip_time_based(reason=...)` | Needs a timer that cannot be waited out (24 h, 72 h, 7 d, 14 d). Always skipped, with the reason shown |

## Layout

```
tests/
  config.py       settings from env / .env
  routing.py      path -> owning service (copy of the gateway's route patterns, checked by a stage 0 test)
  api.py          ApiClient / ApiResponse: routing, bearer token, Idempotency-Key, expect(status, code=...)
  actors.py       Actor: email/password/tokens, claims from the JWT, login/refresh, re-login near expiry
  data.py         unique emails, NICs, phones, store names, SKUs, slugs, codes per run
  factories.py    register/approve/create actors, master data (idempotent), merchant store set-up steps
  waits.py        wait_until(predicate, timeout=30, interval=1) for Kafka-driven state
  pagination.py   PageResponse shape and page/size/cap checks
  conftest.py     session fixtures: super_admin, admin, customer, customer2, merchant, merchant2, assistant,
                  bank, courier, category
  test_stage*_*.py
FINDINGS.md       failing or suspicious cases, with the suspected code location
```

### Test data

Every run uses a new run id (`tests/data.py`), so emails, NICs, phones, store names, SKUs and slugs never collide
with earlier runs or manual data, and no test assumes an empty database. The shared master data (bank `IT_BANK`,
courier `IT_COURIER`, category `it-general`) is created idempotently: a 409 "already exists" is accepted, and the
record is put back to the expected active state if a previous run changed it.

### Fixtures

- `super_admin`: see *Configure*.
- `admin`: created by the super admin. Its temporary password is already changed.
- `customer`, `customer2`: registered and logged in.
- `merchant`, `merchant2`: registered, approved by `admin`, then logged in again, because the token's `status` claim
  is set at login and product-service reads it. Each has a complete store profile (published), one bank account, the
  `IT_COURIER` courier with a FLAT shipping template, and COD on. Every step is a separate function in
  `factories.py` (`put_store_profile`, `add_bank_account`, `set_store_couriers`, `add_flat_shipping_template`,
  `set_cod`), so stage 2 can test each one.
- `assistant`: an assistant of `merchant` with every permission. Its temporary password is already changed.
