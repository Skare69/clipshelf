# Clipshelf authentication and account recovery research

Researched 2026-09-11 against primary documentation, source code, and official
surveys. Framework and recovery direction approved by the user on 2026-09-11;
implementation remains unapproved. No framework installed, no server migration
performed, and no authentication flow exercised in a running Clipshelf.
The [consolidated server plan](server-plan.md) incorporates the multi-user
interview decisions and proposed implementation sequence.

## Chosen direction

**Use Django plus django-allauth for application accounts, with verified-email
password reset. Keep SQLite. Do not build a password-reset protocol.** Prefer the
current patch of Django 5.2 LTS for its longer supported lifetime; use a current,
compatible, security-patched allauth release. Django's release table currently
lists 5.2.17, supported through April 2028. Allauth 65.19.3 includes fixes for
concurrent rate-limit updates, MFA races, and headless JWT validation following
out-of-band password changes. These are maintenance requirements, not reasons to
write an alternative authentication system. [Django releases][django-releases],
[allauth releases][allauth-releases].

This changes the earlier stdlib-only direction: local accounts, verified email,
recovery, sessions, mobile authentication, and user administration now make a
maintained framework less application code, not unnecessary infrastructure.
Reuse the existing acquisition and interpretation logic where its contracts
still fit; replace the localhost HTTP/authentication boundary rather than
building an authentication service beside it. Collections and durable delivery
remain Clipshelf responsibilities.

**Chosen email recovery supersedes my earlier preference for saved codes.**
Saved account-recovery codes are legitimate, but the libraries' advertised
"recovery codes" often recover the second factor, not a forgotten password.
Verified-email reset is the directly supported password-recovery path here.
**Deployment prerequisite: a working SMTP server or relay must be configured
and delivery verified before account onboarding and email recovery are usable.**
It can be an existing or external SMTP service; Clipshelf does not need to host
a mail server. Ordinary password login for already-verified accounts does not
depend on SMTP availability. [Allauth configuration][allauth-config].

## Framework comparison

Fit assessments below are recommendations, not benchmark results. The candidate
libraries and providers already implement password recovery; the question is
how much integration and operational responsibility remains for Clipshelf.

| Candidate | Existing capabilities | Clipshelf assessment |
|---|---|---|
| Django core | Local users, password hashers, browser sessions, CSRF, reset views, ORM/migrations, SQLite | Valid base, but not the whole account lifecycle. Verified-email management, login throttling, and mobile auth need additional integration. |
| **Django + django-allauth** | Adds email verification, password-reset flows, enumeration protection, per-action rate limits, headless mobile API; optional MFA/passkeys and user-session management | **Best default for this Python application.** No separate identity service. Invite admission, limited app administration, and collection authorization remain application work. |
| Flask + Flask-Security-Too | Maintained package with session/token authentication, password recovery, confirmation, roles, MFA/passkeys, and JSON endpoints | Credible runner-up. More assembly for persistence/migrations, rate limiting, and administrative UI. Enable generic responses explicitly; its `SECURITY_RETURN_GENERIC_RESPONSES` default is false. Flask itself is not rejected. |
| FastAPI + FastAPI Users | Register/login/reset/verify routes and pluggable auth/database strategies | The **FastAPI Users package**, not FastAPI itself, is in maintenance mode: security/dependency maintenance continues, new features do not. Not my first foundation for this new multi-user application. |
| authentik via OIDC | Central account/recovery flows, MFA, and native expiring/single-use invitation links that can be copied or emailed | Strong alternative when a household identity service is already wanted. Its current reference deployment has server, worker, and PostgreSQL services: additional operations solely for Clipshelf. |
| Keycloak via OIDC | Central accounts, password reset, MFA, required-action onboarding, and native-client OIDC | Capable alternative, particularly for an existing deployment. Needs a supported production database; SQLite is not supported. PostgreSQL is one choice, not a mandatory vendor. Its development database is not a production shortcut. |

Sources: [Django auth][django-auth], [allauth configuration][allauth-config],
[headless session tokens][allauth-session-tokens], [allauth MFA][allauth-mfa],
[Flask-Security documentation][flask-security], [Flask-Security configuration][flask-security-config],
[FastAPI Users maintenance notice][fastapi-users], [authentik invitations][authentik-invitations],
[authentik reference Compose][authentik-compose], [Keycloak administration][keycloak-admin],
[Keycloak databases][keycloak-db].

**Adoption evidence:** in the DSF/JetBrains Django Developers Survey 2025,
18% included django-allauth among their five favorite third-party packages.
The filtered survey had 4,655 responses, collected November 2024-January 2025.
This is a concrete established-use signal, not an authentication market-share
measurement or a security audit. [Official survey][django-survey].

**No compulsory external identity provider.** Reusing an existing, suitable IdP
could avoid duplicate account management, but adding one solely for this app is
not the minimum deployment. These providers are self-hostable; no cloud account
is inherently required. An IdP still cannot implement Clipshelf's collection
rules. Clipshelf app admins may receive the approved account-recovery authority,
not unrestricted IdP administration. Provider-password changes must not be assumed
to revoke application sessions or already-issued access tokens.

Authelia was considered because it supports SQLite, but that does not supply
Clipshelf's invitation and account-administration experience: its file-backed
users are configured separately. Its OIDC provider is documented as open beta.
Not a turnkey alternative for the requested lifecycle.
[Authelia storage][authelia-storage], [file-backed users][authelia-users],
[OIDC status][authelia-oidc].

If an OIDC route is selected instead, use a maintained client library and
Authorization Code + PKCE through the system browser for Android, not an embedded
password-collecting WebView or a bundled client secret. Refresh/revocation policy
still has to match the outbox requirements. This is a conditional alternative,
not a reason to introduce OAuth to the recommended same-application session flow.
[RFC 8252][rfc8252], [OAuth security BCP][rfc9700].

## Confirmed requirements, not new decisions

The following come from the user's interview answers, not framework defaults:

- Clipshelf has independent application accounts, administrator-invitation-only
  admission, and passwords chosen by recipients. Tailscale is transport only;
  LAN login and permissions cannot depend on Tailscale identity headers.
- Collections are the access boundary. Each user gets a permanently private
  Personal collection. Shared collections have explicit membership.
- An approved user can create a shared collection and manage its members as
  owner. This cannot grant power to create new application accounts.
- Ordinary members remove only their own contributions. Collection owners can
  moderate contributions in their collection. Removal is collection-local.
- Application administration does not automatically grant collection browsing
  access. **Exception approved by the user: Clipshelf app admins may restore
  account access from the admin UI when both password and verified-mailbox access
  are lost, using a Jellyfin-style administration model.** This recovery authority
  can enable account takeover; collection privacy is not protection against its
  misuse. NAS/database operators also remain trusted; this is not end-to-end
  encryption against the host operator.
- **Account administration supports reversible disabling only, not permanent
  account deletion** (approved 2026-09-12). Disabling blocks access and revokes
  browser/mobile sessions while retaining the account, Personal content, shared
  contributions, memberships, and collection ownership. Shared collections remain
  available to their other active members. Re-enabling permits login and access
  under current permissions; it does not revive revoked sessions.
- **App admins may transfer a disabled owner's shared collection to an existing
  active member** (approved 2026-09-12). This is an explicit ownership change,
  not an automatic consequence of disabling. It grants the acting admin neither
  membership nor access to saved content. Personal collections are not
  transferable.
- The existing single-user library migrates into its owner's private Personal
  collection, including links, prompts, and retained source material. Migration
  grants no new sharing or membership (approved 2026-09-12).
- Default capture destination is configurable, including a writable shared
  collection. An already queued share retains its original destination.
- Phone capture must persist locally and retry without reopening the app.
  Library browsing remains online-only. A failed destination falls back to
  Personal with a visible notice; if neither destination is available, retain
  the share in the outbox and notify the user. Authentication failure never
  permits bypassing login or rerouting to another account.

Framework adoption, verified-email recovery, the SMTP prerequisite,
application-admin-assisted exceptional recovery, disable-only account
administration, shared-collection succession, and private migration are approved.
This direction does not authorize implementation.

## Established recovery practice

### Password reset and MFA recovery are different operations

OWASP's forgotten-password guidance recommends a side-channel, single-use,
expiring reset link, uniform public responses, abuse controls, and notification
after a successful reset. It explicitly treats resetting MFA separately.
[OWASP forgotten password][owasp-reset].

NIST SP 800-63B-4 recognizes saved recovery codes, issued codes, recovery
contacts, and repeated identity proofing. Saved codes must be unpredictable,
stored hashed by the verifier, throttled, invalidated after use, and replaced.
Issued email codes/links have a maximum 24-hour lifetime in that standard.
Email's prohibition as an out-of-band authentication factor explicitly excludes
email verification and recovery codes. This is security guidance for our
choices, **not a claim that Clipshelf meets a NIST assurance level**.
[NIST recovery][nist-recovery], [NIST authenticators][nist-authenticators].

Consequences:

- Do not relabel an MFA backup code as a password-reset credential.
- Do not reset or disable enrolled MFA merely because an email password reset
  succeeded. Full MFA loss needs its own supported recovery flow.
- Do not let recovery create a replacement account. Restore the same internal
  user identity, Personal collection, and memberships.
- Do not use security questions, email a password, or hand-roll reset JWTs.
- Loss of both password and mailbox access may be resolved by an explicit
  Clipshelf app-admin recovery action, as approved by the user. Failed email
  recovery must never automatically trigger that privileged path.

### Configuration still matters

| Area | Recommended Clipshelf behavior | Existing support / caveat |
|---|---|---|
| Reset request | Same public response for known, unknown, and ineligible accounts; avoid practical timing disclosure | Allauth enumeration prevention is on by default. Django core also returns generic results but documents email-delivery timing leakage. Verify the integrated flow. |
| Reset proof | Use maintained framework token generation and validation; one use, bounded lifetime | Allauth uses an email-aware Django reset-token generator. Django's default reset lifetime is three days; propose `PASSWORD_RESET_TIMEOUT = 3600`, a one-hour Clipshelf choice rather than a claimed universal standard. |
| Recovery address | Require and verify email before enabling account use; verify replacements before switching recovery address | `ACCOUNT_EMAIL_VERIFICATION = "mandatory"` plus required email signup field. Default verification is only optional. A manually copied invitation link is not proof of mailbox ownership. |
| Notifications | Inform user after password/security changes; never include passwords or reset secrets in ordinary logs | Enable `ACCOUNT_EMAIL_NOTIFICATIONS`; default is false. Keep email failure observable to the operator without exposing account existence publicly. |
| Abuse prevention | Throttle login, reset requests, and token submissions; do not permanently disable an account merely because reset requests arrive | Allauth supplies per-action limits. Cache capacity, sharing across web workers, and trusted-proxy IP handling determine whether those limits work in deployment. |
| Sensitive account changes | Require recent authentication before changing recovery email, password, or factors | Allauth offers `ACCOUNT_REAUTHENTICATION_REQUIRED`; default is false. This is Clipshelf-local authentication, not the separate Velvarr/Jellyfin re-login flow. |
| Password storage | Use Django's maintained password hashers, salts, and upgrades; never reversible encryption or a general-purpose fast hash | Django defaults to PBKDF2; its supported Argon2id hasher requires `argon2-cffi`. OWASP prefers Argon2id. Select/configure a supported hasher, not custom crypto, and calibrate cost on deployment hardware. |
| Password policy | Long passwords, password-manager/autofill/paste support, no arbitrary composition rules or periodic rotation | NIST's single-factor minimum is 15 characters and recommended permitted maximum is at least 64. Configure framework validators and common/compromised-password checks; framework presence alone does not establish this policy. |
| Sessions after recovery | Invalidate old browser and phone sessions, then require normal login; preserve the phone outbox | Django session auth hashes invalidate old sessions after password change. Allauth defaults to no automatic login after reset. Verify every selected mobile token strategy and API path; separate API tokens do not become revoked merely because a browser session did. |
| Transport and secret leakage | HTTPS on LAN and remote routes; secure cookies, CSRF, approved origin/host handling, trusted reset URL origin, no reset-page third-party resources | Use framework protections. Do not construct reset URLs from an unchecked Host header. Apply `Referrer-Policy: no-referrer` and redact credential-bearing URLs from access logs. |

Sources: [OWASP reset][owasp-reset], [OWASP storage][owasp-storage],
[NIST passwords][nist-authenticators], [Django auth][django-auth],
[Django password management][django-passwords], [Django reset default][django-reset-default],
[allauth configuration][allauth-config], [account rate limits][allauth-account-limits],
[rate-limit deployment requirements][allauth-limits],
[Django deployment checklist][django-deploy].

## Where framework reuse ends

**Invitation-only is not an allauth feature toggle.** Its documentation says
invitation handling is not supplied, but provides account-adapter hooks for
integrating it. Admission must require an unexpired, unused, administrator-issued
invitation in both browser and headless signup. Consume it atomically with
account creation; never let changing an email bypass admission. Public signup
must fail even when its URL is called directly. A maintained invitation package
can be evaluated against these requirements; no package has been selected here.
[Allauth invitations][allauth-invitations].

Do not substitute Django's stock forgot-password view for first-time onboarding
of an unusable-password user: that view deliberately excludes such users.
Use the account-creation/set-password flow, keeping framework validation and
cryptographic primitives. [Django password reset][django-reset].

**Application admin must not mean Django superuser or unrestricted UserAdmin.**
Django's own documentation warns that unrestricted user editing can confer
superuser-equivalent powers, and its user admin can set other users' passwords.
Changing a user's recovery email can also enable takeover. **The user explicitly
approved app-admin-assisted recovery without the original mailbox, like
Jellyfin.** Reuse Django/allauth credential handling for that targeted admin
action, preserve the account identity, and invalidate old sessions. This grants
account-recovery authority, not unrestricted Django UserAdmin, privilege
escalation, or a general impersonation feature. Direct collection browsing still
requires membership; recovery authority is an accepted exception to protection
against administrator takeover.
[Django user administration][django-user-admin].

**Authentication is not collection authorization.** Apply membership checks to
search, detail, cached media, exports, findings, queue status, and mutations, not
only visible cards. Recheck permissions when a queued capture reaches the server.
Neither Django model permissions nor an identity-provider role automatically
implements collection membership and contributor-local removal.
[OWASP authorization][owasp-authorization].

## Browser, phone, and deployment consequences

- Use ordinary server-side Django sessions and secure, HttpOnly cookies for the
  same-origin web interface. Do not add JWTs simply because Android exists.
- Allauth headless explicitly supports mobile apps using `X-Session-Token`.
  Its default session-token strategy can also secure application APIs; packaged
  integrations exist for Django REST framework and Django Ninja. Selecting an
  API adapter is implementation work, not permission to invent token crypto.
  Browser cookies and mobile authentication must refer to the same account and
  enforce the same collection rules. [Headless API][allauth-headless],
  [session-token integration][allauth-session-tokens].
- Do not store the password for unattended retries. Store session credentials in
  app-private storage with platform-backed protection, separate from the outbox;
  ensure credential protection does not require user interaction on each retry.
  Logout, password reset, disabling, or expiry pauses authenticated delivery.
  Keep queued shares and require the appropriate account to sign in again.
- An expired credential is not a network outage or an unavailable destination.
  A phone cannot guarantee authenticated delivery indefinitely after revocation.
  The existing share-and-forget decision applies while delivery is authorized.
- Allauth's default limits use the Django cache. Do not use DummyCache, deploy
  process-local state across multiple web workers, or trust arbitrary forwarded
  IP headers. A single web process can use process-local state but loses limits
  on restart; a shared backend is necessary before adding web processes.
  Choose against the actual deployment, not an assumed Redis prerequisite.
  [Rate-limit deployment requirements][allauth-limits].
- Allauth's login limiter does **not** protect the separate Django admin login.
  Do not expose an unprotected alternative login; route it through protected
  authentication or keep that interface unavailable to normal app users.
  [Account rate limits][allauth-account-limits].
- Use an HTTPS application hostname that works on LAN without Tailscale and
  remains reachable remotely through the chosen network path. Reset-email links
  need server reachability; requesting a reset does not make a private service
  publicly accessible. Tailscale remains transport, never authentication.

## Proposed acceptance gates, not completed tests

Before deploying the selected authentication integration:

1. One invited user completes setup and email verification; non-invited browser
   and headless requests cannot create an account. Expired, replayed, and
   concurrent invitation redemption cannot create extra accounts.
2. Unknown/known-account reset requests have equivalent public outcomes; limits
   work behind the real proxy and across the chosen worker model.
3. Real SMTP mail arrives with the configured HTTPS origin. A valid reset changes
   the password once; expired, tampered, and replayed links fail. Mail outage
   cannot silently turn failed recovery into reported completion.
4. Recovery retains the original user ID, collection memberships, and content.
   Old browser/mobile credentials fail; ordinary login with the new password
   succeeds. Password reset does not remove enrolled MFA.
5. An application admin without membership cannot directly read another user's
   Personal content or serve its cache using their admin session. The explicit
   admin-recovery action can restore the same user's access without the original
   mailbox; non-admin callers cannot invoke it. Recovery does not silently grant
   the administrator collection membership or remove enrolled MFA.
6. On a real phone, a revoked or expired session keeps queued shares intact and
   shows the required authentication action; signing into the original account
   resumes delivery without duplication. Changing accounts cannot send another
   account's queued content.
7. LAN and remote authentication enforce identical permissions; forged Tailscale
   and forwarded-client headers cannot authenticate a user or evade limits.
8. Disabling an account rejects both new login and existing browser/mobile
   credentials without deleting its content or memberships. Other active members
   retain shared-collection access. Re-enabling allows fresh login under current
   permissions; revoked credentials stay invalid and the phone outbox is retained.
9. An app admin can transfer a disabled owner's shared collection to an existing
   active member without gaining collection membership or content access. Reject
   non-member or inactive recipients and any Personal-collection transfer.
   Re-enabling the previous owner does not undo the transfer.

The account-recovery and administration decisions above are recorded.
Implementation remains unapproved.

## Primary sources

[django-releases]: https://www.djangoproject.com/download/
[allauth-releases]: https://docs.allauth.org/en/latest/release-notes/recent.html
[allauth-config]: https://docs.allauth.org/en/latest/account/configuration.html
[owasp-reset]: https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html
[owasp-storage]: https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html
[nist-recovery]: https://pages.nist.gov/800-63-4/sp800-63b/events/#recovery
[nist-authenticators]: https://pages.nist.gov/800-63-4/sp800-63b/authenticators/#passwordver
[django-auth]: https://docs.djangoproject.com/en/5.2/topics/auth/default/
[django-passwords]: https://docs.djangoproject.com/en/5.2/topics/auth/passwords/
[django-reset-default]: https://github.com/django/django/blob/stable/5.2.x/django/conf/global_settings.py
[allauth-account-limits]: https://docs.allauth.org/en/latest/account/rate_limits.html
[allauth-limits]: https://docs.allauth.org/en/latest/common/rate_limits.html
[django-deploy]: https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/
[allauth-invitations]: https://docs.allauth.org/en/latest/account/advanced.html#invitations
[django-reset]: https://docs.djangoproject.com/en/5.2/topics/auth/default/#django.contrib.auth.views.PasswordResetView
[django-user-admin]: https://docs.djangoproject.com/en/5.2/topics/auth/default/#user-management-in-django-admin
[owasp-authorization]: https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html
[allauth-headless]: https://docs.allauth.org/en/latest/headless/api.html
[allauth-session-tokens]: https://docs.allauth.org/en/latest/headless/token-strategies/session-tokens.html
[django-survey]: https://lp.jetbrains.com/django-developer-survey-2025/
[allauth-mfa]: https://docs.allauth.org/en/latest/mfa/introduction.html
[flask-security]: https://flask-security-too.readthedocs.io/en/stable/
[flask-security-config]: https://flask-security-too.readthedocs.io/en/stable/configuration.html
[fastapi-users]: https://github.com/fastapi-users/fastapi-users#readme
[authentik-invitations]: https://docs.goauthentik.io/users-sources/user/invitations/
[authentik-compose]: https://docs.goauthentik.io/compose.yml
[keycloak-admin]: https://www.keycloak.org/docs/latest/server_admin/index.html
[keycloak-db]: https://www.keycloak.org/server/db
[authelia-storage]: https://www.authelia.com/configuration/storage/introduction/
[authelia-users]: https://www.authelia.com/configuration/first-factor/file/
[authelia-oidc]: https://www.authelia.com/configuration/identity-providers/openid-connect/provider/
[rfc8252]: https://www.rfc-editor.org/rfc/rfc8252.html
[rfc9700]: https://www.rfc-editor.org/rfc/rfc9700.html
