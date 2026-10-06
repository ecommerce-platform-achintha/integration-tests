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

- **Test:** `tests/test_stage1_registration.py::test_application_limit_follows_platform_setting` (`slow`)
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

- **N1-1:** refresh-token reuse (`RefreshTokenService.rotate`) and logout (`revokeFamilyOf`) revoke only the
  refresh-token family. Access tokens already issued stay valid for up to 10 minutes, because `tv` is not
  incremented. The tests assert this current behaviour. Say if reuse is supposed to sign the user out at once
  (`revokeAccessTokens`).
- **N1-2:** merchant approval does not revoke the merchant's tokens. The old token stays valid with the frozen
  `status: PENDING_APPROVAL` claim until the merchant logs in again, as the task describes. `/api/users/me` already
  shows `ACTIVE`.
- **N1-3:** registration validates the phone before normalising it, so `+94 77 123 4567` is refused (400) even
  though `UserAccountFactory.normalizePhone` would strip the spaces. By code reading, `PUT /api/users/me` behaves
  the same (`@Pattern` on `UpdateProfileRequest.phone`); not tested.
  Probably intended. Noted because the normalisation code can never take effect on registration.
- **N1-4:** the login IP key depends on how `localhost` resolves (`ip:127.0.0.1` vs `ip:0:0:0:0:0:0:0:1` rows
  in `login_throttle`). Both appear in this environment.
