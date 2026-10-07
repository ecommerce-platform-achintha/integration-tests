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
.venv/bin/pytest -m stage1_destructive -v   # merchant ban on throwaway merchants
.venv/bin/pytest -m lockout -v       # failed-login tests, opt-in (see "Failed-login tests" below)
.venv/bin/pytest -m stage2 -v        # store-service
.venv/bin/pytest -m stage3 -v        # product-service
.venv/bin/pytest -m stage4 -v        # order-service end to end
.venv/bin/pytest -m stage4_destructive -v   # bans, store blocks, COD suspension on throwaway actors
.venv/bin/pytest -m "not slow" -v    # skip event-driven / long scenarios
```

`pytest.ini` adds `-m "not lockout"` to every run. A `-m` given on the command line replaces it, so the lockout
tests carry only the `lockout` marker (not `stage1`), and `-m stage1` never selects them.

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

## Failed-login tests (`lockout`)

`tests/test_lockout.py` covers wrong password, unknown email (same 401 body, no user enumeration) and the account
lockout after 5 failures. It only uses throwaway customers, but every failed login also counts against the
**per-IP** counter of the machine running the suite:

- user-service locks an IP for 15 minutes once it reaches **20 failed logins** (`auth.ip-max-failed-logins`) within a
  15-minute window. The window starts at the first failure and is fixed, not sliding. A successful login does
  **not** reset the IP counter. While the IP is locked, every login from it, including the suite's own fixtures,
  gets `429 TOO_MANY_LOGIN_ATTEMPTS`.
- One run of `pytest -m lockout` costs **7** failed logins: 1 wrong password, 1 unknown email, 5 for the lockout.
- So **at most 2 runs per 15 minutes** are safe (14 failures). A third run would hit 20. The super admin
  fixture's very first run (password not yet changed) adds 1 more.

### Where the counters live and how to reset them

Both counters are in user-service's PostgreSQL database (`userdb`, container `user-service-postgres`, port
5432), not in memory, so restarting user-service does not clear them:

| What | Storage |
|---|---|
| per account (wrong password) | `users.failed_login_count`, `users.locked_until` |
| per unknown email | `login_throttle` row `email:<email>` |
| per client IP | `login_throttle` row `ip:<address>` (`ip:127.0.0.1` or `ip:0:0:0:0:0:0:0:1`, depending on how `localhost` resolves) |

To inspect and reset them on a **local** stack, without changing any service code:

```bash
# inspect
docker exec user-service-postgres psql -U userservice -d userdb \
  -c "select throttle_key, failed_count, window_started_at, locked_until from login_throttle order by updated_at desc;"

# reset IP and unknown-email counters
docker exec user-service-postgres psql -U userservice -d userdb -c "delete from login_throttle;"

# unlock accounts
docker exec user-service-postgres psql -U userservice -d userdb \
  -c "update users set failed_login_count = 0, locked_until = null where failed_login_count <> 0 or locked_until is not null;"
```

user-service reads both on every login (no cache), so the reset takes effect immediately. The user name is
user-service's `DB_USERNAME` (local default `userservice`). Inside the container `psql` connects over the local
socket, so no password is needed. Otherwise wait 15 minutes.

## Not covered / notes

- The super admin's forced password change happens only on the suite's very first run against a fresh database, so it
  cannot be tested repeatedly. The same rule is covered through new admins and assistants.
- Stage 4 time-based cases (`tests/test_stage4_timers.py`, always skipped): merchant response timeout, customer
  confirmation timeout, `DEADLINE_PASSED` for a late user action, the late-shipment flag, auto-complete, the 24 h
  idempotency-key expiry, payment submission timeout, the late-verification flag, the complaint window (30 days),
  the COD objection window (7 days) and open orders needing admin resolution at the end of a merchant's ban grace
  (14 days). They need a test clock or settings in seconds.
- Duplicate payment references are unique per (normalised reference, destination bank) across all stores. Each test
  uses a fresh `ITREF...` / `ITDUP...` reference, so earlier runs never collide.
- Optional hardening idea (not a finding): refresh-token reuse revokes the token family, but access tokens already
  issued stay valid for up to 10 minutes. Bumping the token version (`tv`) on reuse would sign the user out at once.

## Markers

| Marker | Meaning |
|---|---|
| `stage0` … `stage5` | The stage a test belongs to (0 smoke, 1 user, 2 store, 3 product, 4 order, 5 gateway) |
| `stage1_destructive` | Merchant ban on dedicated throwaway merchants, and a test that changes a global platform setting for about 15 s (restored in a finalizer). Never touches the shared fixtures |
| `lockout` | Failed-login tests. Excluded by default, opt in with `-m lockout` (at most 2 runs per 15 min) |
| `gateway` | Goes through the api-gateway. Stays under the auth-tier budget |
| `slow` | Waits for Kafka events or runs long multi-step flows |
| `stage4_destructive` | Customer ban (including payment), store block, COD suspension (and lifting it by an upheld objection) and merchant ban, each on throwaway customers/merchants created inside the test. Never touches the shared fixtures |
| `skip_time_based(reason=...)` | Needs a timer that cannot be waited out (24 h, 72 h, 7 d, 14 d). Always skipped, with the reason shown |

## Layout

```
tests/
  config.py       settings from env / .env
  routing.py      path -> owning service (copy of the gateway's route patterns, checked by a stage 0 test)
  api.py          ApiClient / ApiResponse: routing, bearer token, Idempotency-Key, expect(status, code=...)
  actors.py       Actor: email/password/tokens, claims from the JWT, login/refresh, re-login near expiry
  data.py         unique emails, NICs, phones, store names, SKUs, slugs, codes per run
  factories.py    register/approve/create actors, master data (idempotent), merchant store set-up steps,
                  product payloads/create/activate, stock adjust
  waits.py        wait_until(predicate, timeout=30, interval=1) for Kafka-driven state
  pagination.py   PageResponse shape and page/size/cap checks
  conftest.py     session fixtures: super_admin, admin, customer, customer2, merchant, merchant2, assistant,
                  bank, courier, courier_no_cod, category, product
  test_stage*_*.py
FINDINGS.md       failing or suspicious cases, with the suspected code location
```

### Test data

Every run uses a new run id (`tests/data.py`), so emails, NICs, phones, store names, SKUs and slugs never collide
with earlier runs or manual data, and no test assumes an empty database. The shared master data (bank `IT_BANK`,
couriers `IT_COURIER` (COD) and `IT_COURIER_NC` (no COD), category `it-general`) is created idempotently: a 409 "already exists" is accepted, and the
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
- `bank`, `courier`, `courier_no_cod`, `category`: the shared master data above.
- `product`: an ACTIVE simple product of `merchant` in `it-general` (1000.00, stock 50, COD allowed), shared per
  session. Tests that change price, stock or status create their own product (`factories.create_active_product`).
- `factories.new_approved_merchant(client, admin, store="complete"|"incomplete"|None)`: a throwaway merchant, for
  tests that change store state, so the shared `merchant`/`merchant2` stay as described.
- Stage 4 (orders): `buyer` is a fresh customer with one address per test, so its cart, open-order limit (10) and
  score start from zero. `item`/`item2` are fresh ACTIVE products (stock 50) of `merchant`/`merchant2` per test;
  order tests never use the shared `product`, so its stock is not drained. `factories.new_shop(...)` makes a
  throwaway merchant with a ready store and a visible product, for exact store metrics, COD off or a second courier.
  Order steps are helpers too: `place_order`, `quote`, `confirm`, `ship`, `order_to_ready`, `order_to_shipped`.

Test data is never deleted: users, stores, throwaway banks (`ITB_...`) and couriers (`ITC_...`) stay in the databases.
For a clean slate, reset **all** databases together (user, store, product, order) and restart the services. Resetting
only some of them leaves references across services that point at nothing.

Tests that change global state restore it in a finalizer: platform settings go back to their previous value, and
holidays created on random far-future dates are deleted.
