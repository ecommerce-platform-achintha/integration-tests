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

## Stage 4 (order-service)

### S4-1: every checkout fails, because order-service sends store-service a locale-formatted date (service bug, blocker)

- **Test:** every stage 4 test that places an order, for example
  `tests/test_stage4_checkout.py::test_checkout_places_order`.
- **Request:** `POST http://localhost:8083/api/customer/checkout` (customer token, `Idempotency-Key`) with one available
  COD line of the shared merchant's store.
- **Actual:** `422`, group `FAILED` with `"code":"SERVICE_UNAVAILABLE"` and `"message":"Store service is unavailable
  (request failed): could not load the holiday calendar. Try again later."`. After a few failures the circuit
  breaker opens, and later checkouts fail with `... (circuit breaker is open): could not load the store`.
- **Expected:** `201`, order `AWAITING_MERCHANT` with a `MERCHANT_RESPONSE` deadline.
- **Cause (reproduced):**
  - Placing an order computes the merchant deadline (`OrderTimers` → `DeadlineCalculator` → `HolidayCalendar`).
  - That calls `StoreServiceClient.holidays(@RequestParam("from") LocalDate from, @RequestParam("to") LocalDate to)`
    (`order-service/.../client/StoreServiceClient.java`).
  - Spring Cloud OpenFeign formats `@RequestParam` values with a `DefaultFormattingConversionService`. Without
    `@DateTimeFormat(iso = DATE)` on the parameters, or a `FeignFormatterRegistrar` with ISO enabled
    (`FeignConfig` has neither), it uses the JVM locale's SHORT style.
  - The running order-service JVM is `en_US`, so it sends `?from=1/1/26&to=12/31/26`. With order-service's own
    classpath, `new DefaultFormattingConversionService().convert(LocalDate.of(2026,1,1), String.class)` gives
    `1/1/26`.
  - store-service's `InternalPlatformController.holidays` expects ISO dates. With a service token,
    `GET /internal/holidays?from=1/1/26&to=12/31/26` answers `500 INTERNAL_ERROR` (the S2-1 type-mismatch gap),
    while `?from=2026-01-01&to=2026-12-31` answers `200 []`.
  - `HolidayCalendar` has no cached year to fall back on, so the `ServiceUnavailableException` fails the checkout
    group.
- **Why the service's own tests miss it:** `order-service/src/test/.../support/Remote.java` stubs
  `urlPathEqualTo("/internal/holidays")`, which ignores the query string.
- **Fix:** `@DateTimeFormat(iso = DateTimeFormat.ISO.DATE)` on both `holidays` parameters (or ISO formatting for all
  Feign clients through a `FeignFormatterRegistrar`). S2-1's handler would turn the 500 into a 400 but not fix this.
  The quote, confirm, payment and ship deadlines use the same calendar, so all of stage 4 depends on this fix.
- **Verdict:** service bug (blocker). **Fixed in the working tree, verified by the stage 4 checkout tests**: the
  fix (`@DateTimeFormat(iso = ISO.DATE)` on both parameters) is uncommitted in the order-service working tree.
  After restarting order-service with it, all order-placing tests in `test_stage4_checkout.py` pass.

### S4-2: audit of inter-service calls for request parameters formatted without an explicit format (cross-service)

Read-only search of every repo (api-gateway, config-server, service-registry, user, store, product, order) for code
that calls another service: Feign interfaces (`@FeignClient`), `RestClient`, `WebClient`, `RestTemplate`. For each,
the `@RequestParam` / `@PathVariable` parameters of type `LocalDate`, `LocalDateTime`, `Instant`, an enum, `UUID` or
`BigDecimal` were checked for an explicit format.

How Spring Cloud OpenFeign renders them: it uses a `DefaultFormattingConversionService` unless a
`FeignFormatterRegistrar` changes it, and none of the three `FeignConfig` classes registers one. Checked with the
services' own classpath, JVM locale `en_US`:

| Type | Rendered as | Safe without a format? |
|---|---|---|
| `LocalDate` | `1/1/26` | **no**: depends on the JVM locale, not ISO |
| `LocalDateTime` | `1/1/26, 1:05 PM` | **no** |
| `Instant` | `2026-01-01T07:30:00Z` | yes (ISO) |
| `UUID` | `123e4567-e89b-12d3-a456-426614174000` | yes |
| enum | `COD` (`name()`) | yes |
| `BigDecimal` | `1000.00`, but `1E+3` for a value built with a negative scale | mostly (`toString()`) |

Every inter-service call with such a parameter:

| Caller | Method | Parameter | Type | Explicit format | Verdict |
|---|---|---|---|---|---|
| order-service `client/StoreServiceClient` | `holidays` `GET /internal/holidays` | `from`, `to` (`@RequestParam`) | `LocalDate` | none | **broken: S4-1** |
| order-service `client/StoreServiceClient` | `store`, `bankAccounts`, `couriers` `GET /internal/stores/{id}...` | `id` (`@PathVariable`) | `UUID` | none (not needed) | ok |
| order-service `client/UserServiceClient` | `securityState` `GET /internal/users/{id}/security-state` | `id` (`@PathVariable`) | `UUID` | none (not needed) | ok |
| product-service `client/UserServiceClient` | `securityState` (same route) | `id` | `UUID` | none (not needed) | ok |
| store-service `client/UserServiceClient` | `securityState` (same route) | `id` | `UUID` | none (not needed) | ok |

There are no other typed parameters. Every other inter-service method takes `String` path variables (`publicId`,
`orderRef`), a JSON `@RequestBody` (serialized by Jackson, not the conversion service), or headers only:
- order-service `ProductClient`: `quote`, `reserve`, `adjust`, `commit`, `release`
- order-service `StoreServiceClient`: `storeByPublicId`, `settings`
- order-service `UserServiceClient`: `serviceToken`, `myAddress`, `me`
- product-service and store-service `UserServiceClient`: `serviceToken`

user-service, config-server and service-registry make no calls to other services. The api-gateway's only client
call is the JWKS fetch in `auth/JwtConfig` (`WebClient`, no parameters).

- **Verdict:** one broken call (S4-1). No other call is affected today. Hardening, to stop it coming back: an
  explicit ISO `@DateTimeFormat` on any future date/time parameter, or a `FeignFormatterRegistrar` with ISO enabled
  in each service's `FeignConfig`. Also, the receiving controllers in store-service and user-service have no
  `TypeMismatchException` handler (S1-2 / S2-1), so a badly formatted parameter shows up as a 500 there, not a 400.
