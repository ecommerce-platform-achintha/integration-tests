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
