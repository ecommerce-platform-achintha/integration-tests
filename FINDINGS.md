# Findings

Failing or suspicious cases found by the suite. Each entry gives the stage, the test, the request (without
secrets), the actual response, the expected behaviour, where in the code the cause probably is, and a verdict
(test mistake or service bug). Test mistakes are fixed in the suite. Service bugs are left failing.

## Stage 0

No failures. Notes:

- **N0-1 (observation, not a bug).** user-service `LoginThrottleService.recordFailure(ipKey, ...)` counts failed
  logins per client IP (`auth.ip-max-failed-logins`, default 20 per 15-minute window), and a successful login does not
  reset that counter (`AuthService.login` only calls `resetUser`). Every wrong-password or unknown-email test adds to
  the per-IP budget of the machine running the suite. Stage 1 keeps its failed-login tests under that budget.

## Stage 1 (user-service)

### S1-1: a forced password change is only enforced by user-service (service bug)

- **Tests:** `tests/test_stage1_auth.py::test_password_change_required_in_other_services[/api/admin/banks]`,
  `[/api/admin/categories]`, `[/api/admin/orders]`
- **Request:** a newly created admin logs in with the temporary password (`mustChangePassword: true`), then
  `GET http://localhost:8084/api/admin/banks`, `GET http://localhost:8082/api/admin/categories`,
  `GET http://localhost:8083/api/admin/orders` with that access token.
- **Actual:** `200` with data from all three (for example store-service returns the bank page
  `{"content":[{"code":"IT_BANK",...}],"page":0,"size":50,...}`). user-service itself correctly answers
  `403 PASSWORD_CHANGE_REQUIRED` (`/api/users/me`, `/api/admin/users`, ...).
- **Expected:** design 3.3 and user-service README: "until changed, every other endpoint returns 403 with code
  `PASSWORD_CHANGE_REQUIRED`". The same applies to assistants on their temporary password (merchant routes in
  product/order/store-service).
- **Where:** `user-service/.../security/UserSecurityStateFilter.check` is the only place that checks the flag. The
  access token has no `mustChangePassword` claim (`JwtService.generateAccessToken`), and the other services'
  cached state is `SecurityState(long tokenVersion, UserStatus status)` (for example
  `store-service/.../security/SecurityState.java` and `SecurityStateFilter`), so they cannot know about it.
  Possible fixes: a claim in the token (cleared by the token change-password issues), or the flag in the
  security-state endpoint/event.
- **Verdict:** service bug (cross-service). The tests are left failing.

### S1-2: unknown enum query parameter answers 500 (service bug)

- **Test:** `tests/test_stage1_admin.py::test_admin_user_search_unknown_enum_is_400`
- **Request:** `GET http://localhost:8081/api/admin/users?role=ROLE_NOPE` (admin token)
- **Actual:** `500 {"code":"INTERNAL_ERROR","message":"Unexpected error","path":"/api/admin/users"}`, plus an
  ERROR log line with a stack trace.
- **Expected:** `400` (`BAD_REQUEST` or `VALIDATION_FAILED`): a bad query value is a client error.
- **Where:** `user-service/.../exception/GlobalExceptionHandler.handleOther`. `MethodArgumentTypeMismatchException`
  is not an `ErrorResponse`, so it falls into the 500 branch. None of the four services has a handler for
  `MethodArgumentTypeMismatchException`, so other enum/number query parameters probably behave the same (checked
  per service in later stages).
- **Verdict:** service bug. Left failing.

### S1-3: platform settings for user-service values have no effect (service bug / design gap)

- **Test:** `tests/test_stage1_destructive.py::test_application_limit_follows_platform_setting` (`stage1_destructive`, `slow`)
- **Request:** super admin `PUT http://localhost:8084/api/super-admin/settings/merchant.max-application-attempts`
  `{"value":"2","reason":"integration-tests"}` (200), then a new merchant
  `GET http://localhost:8081/api/merchant/application`. The value is restored to the original in a finalizer
  (verified in the setting history: 3 -> 2 -> 3).
- **Actual:** `maxAttempts` stays `3` for the whole 15 s wait.
- **Expected:** store-service's `SettingKey` defines `merchant.max-application-attempts`, `auth.access-token-minutes`,
  `auth.refresh-days`, `auth.max-failed-logins`, `auth.lockout-minutes` and `timers.ban-grace-days` as
  editable platform settings (design section 7), so changing them should change user-service's behaviour.
- **Where:** user-service reads all of them only from static config: `MerchantApplicationService` constructor
  (`@Value("${merchant.max-application-attempts:3}")`), `AccountStatusService` (`timers.ban-grace-days`), and
  `AuthProperties` (the auth values). user-service has no Kafka listener and never calls store-service's
  `/internal/settings`. Changing these settings in the admin UI is therefore silently ignored.
- **Verdict:** service bug or design gap. Left failing.

### Observations (no test failure)

- **N1-1 (accepted, not a finding):** refresh-token reuse and logout revoke only the refresh-token family. Access
  tokens already issued live up to 10 minutes. Confirmed as matching the design. Hardening idea in the README.
- **N1-2:** merchant approval does not revoke the merchant's tokens. The old token stays valid with the frozen
  `status: PENDING_APPROVAL` claim until the merchant logs in again, as the task describes. `/api/users/me` already
  shows `ACTIVE`.
- **N1-3:** registration validates the phone before normalising it, so `+94 77 123 4567` is refused (400) even
  though `UserAccountFactory.normalizePhone` would strip the spaces. By code reading, `PUT /api/users/me` behaves
  the same (`@Pattern` on `UpdateProfileRequest.phone`); not tested.
  Probably intended. Noted because the normalisation code can never take effect on registration.
- **N1-4:** the login IP key depends on how `localhost` resolves (`ip:127.0.0.1` vs `ip:0:0:0:0:0:0:0:1` rows
  in `login_throttle`). Both appear in this environment.

## Stage 2 (store-service)

### S2-1: badly typed query parameters answer 500, also on public routes (service bug, same cause as S1-2)

- **Test:** `tests/test_stage2_store.py::test_badly_typed_query_parameter_is_400[public-size|public-page|admin-boolean|holiday-date]`
- **Requests:** `GET http://localhost:8084/api/public/stores?size=x` (no token),
  `GET /api/public/stores?page=abc` (no token), `GET /api/admin/banks?active=maybe` (admin),
  `GET /api/super-admin/holidays?from=not-a-date` (super admin)
- **Actual:** `500 {"code":"INTERNAL_ERROR","message":"Unexpected error",...}` for all four, with an ERROR log and
  stack trace each time. Anyone can trigger the public ones without a token, so they can fill the error logs and
  alerting.
- **Expected:** `400 BAD_REQUEST` (or `VALIDATION_FAILED`).
- **Where:** `store-service/.../exception/GlobalExceptionHandler.handleOther`, the same structure as user-service:
  no handler for `MethodArgumentTypeMismatchException` (or `TypeMismatchException`), which is not an
  `ErrorResponse`, so it gets the 500 branch.
- **Verdict:** service bug. Left failing.

### S1-2 / S2-1: shared root cause and fix

S1-2 and S2-1 stay two findings (one per service), but the fix is the same: one `@ExceptionHandler` for
`org.springframework.beans.TypeMismatchException` (the superclass of `MethodArgumentTypeMismatchException` and
`MethodArgumentConversionNotSupportedException`) in each service's `GlobalExceptionHandler`, answering 400
`BAD_REQUEST` with the parameter name. The pre-upgrade product-service had such a handler: product-service git history
shows `@ExceptionHandler(TypeMismatchException.class) handleTypeMismatch` in `GlobalExceptionHandler` at commits
`f2b9cd8` and `3450599`, and it is **gone in `b4081e6`** ("initial changes are done", the upgrade). The upgraded
product-service has no such handler either, so it is a regression there (stage 3 test
`test_badly_typed_public_query_is_400`).

### Observations (no test failure)

- **N2-1:** `MockImageStorageAdapter.exists` always returns `true`, so `StoreService.upsertProfile`'s
  "logoKey does not refer to an uploaded image" (400) can never happen with the mock adapter. Only the
  `STORAGE_KEY_REGEX` pattern check is testable.
- **N2-2 (intended, not a finding):** during `BAN_GRACE` the store leaves public search and its public detail
  answers 404 while the merchant's own view stays silent. This matches contract section 3.2.
  `test_banned_owner_store_stops_accepting_orders` asserts it.
- **N2-3:** the store-service README endpoint list (37 routes) matches the controllers. No mismatch found.

## Stage 3 (product-service)

### S3-1: badly typed query parameters answer 500 on the public product search (service bug, regression)

- **Test:** `tests/test_stage3_public.py::test_badly_typed_public_query_is_400[minPrice|inStock|page]`
- **Requests (no token):** `GET http://localhost:8082/api/public/products?minPrice=abc`, `?inStock=maybe`, `?page=x`
- **Actual:** `500 {"code":"INTERNAL_ERROR","message":"Unexpected error",...}` for all three. Anonymous callers can
  trigger it on the busiest public route.
- **Expected:** `400 BAD_REQUEST` (or `VALIDATION_FAILED`).
- **Where:** `product-service/.../exception/GlobalExceptionHandler` has no `TypeMismatchException` handler. It had
  one before the upgrade (`f2b9cd8`, `3450599`) and lost it in `b4081e6`. The fix is the same as for S1-2 / S2-1 (see
  above). order-service's `GlobalExceptionHandler` does handle it.
- **Verdict:** service bug (regression). Left failing.

### Observations (no test failure)

- **N3-1 (low severity, cross-service): six GET list endpoints return a plain JSON array, not a `PageResponse`.**
  The project rule is that every GET list endpoint supports pagination, with no exception for small lists.
  - `GET /api/public/categories`: `product-service/.../category/CategoryController.publicList`
  - `GET /api/admin/categories`: `CategoryController.adminList`
  - `GET /api/super-admin/settings`: `store-service/.../settings/SettingsController.list`
  - `GET /api/merchant/owner/couriers`: `store-service/.../courier/MerchantCourierController.list`
  - `GET /api/merchant/owner/couriers/available`: `MerchantCourierController.available`
  - `GET /api/merchant/owner/bank-accounts/banks`: `store-service/.../bank/BankAccountController.banks`

  Expected: `page`/`size`/`sort` and the `{content, page, size, totalElements, totalPages}` body. Converting them
  changes the response shape (an array becomes an object), so every client of these routes (frontend, the tests
  above) has to change at the same time. The suite pins today's shape
  (`test_stage3_categories.py::test_unpaged_list_endpoints_return_plain_arrays`) and will need updating with the fix.
- **N3-2:** the product-service README was rewritten in `b4081e6`. Its endpoint table (26 routes) matches the
  controllers, and the stage 3 behaviour tested here (status lifecycle, stock modes, discount rules, search filters,
  visibility) matches its descriptions. No README/code mismatch found in stage 3 scope.
