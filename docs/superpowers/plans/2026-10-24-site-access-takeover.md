# Site access takeover implementation plan

**Goal:** Implement the user's approved option B: administrator-controlled denial of non-administrator page and business API access, with the approved three-second red/black takeover.

**Architecture:** Reuse SiteConfig and the current administrator setting screen. A small site-access middleware inside the existing CORS/logging stack enforces the switch; authentication and access-status endpoints stay reachable. A Vue route guard redirects visitors to an independent forbidden page; the Axios error handler covers requests from pages that were already open. PowerGlitch, GSAP and ScrambleText drive an inert, code-native page background and the takeover.

**Constraints:** Default off. Active administrators bypass the gate based on server-validated identity. Preserve administrator login/refresh/callback and legacy SQLAdmin login. No database schema changes, deployment changes, scheduler shutdown, live polling, request recovery, or automatic browser interactions. Existing open SSE connections are not forcibly disconnected; new API requests are checked.

## Tasks

- [x] Backend: reuse JWT validation; add an access configuration reader and middleware enforcement. Allow only necessary authentication/status paths; ordinary reads, writes and new streams return 403 `SITE_ACCESS_DISABLED`. Add the setting to existing configuration seed, list/update and public status. Verify with concise real API tests covering enable/disable, anonymous/ordinary/admin, invalid credentials and login availability.
- [x] Frontend: add the disabled-site setting metadata, a site status API, route gate and response marker handling. Keep authentication callbacks reachable and validate administrator status with the server when disabled. Use a standalone `/access-denied` page with administrator login and refresh actions.
- [x] Artwork: install pinned PowerGlitch/GSAP packages; adapt the existing three-second prototype into a Vue component and a small animation module. Render an inert branded page shell instead of mounting disabled business views. Cleanup library clones/animations when unmounted, respect reduced motion, and finish at “禁止访问”.
- [x] Verify: run relevant backend configuration/auth/middleware tests, existing frontend unit tests and production build; inspect screenshots only. Review the complete diff and report scope and verification. Do not enable the switch on a live system or publish without a separate request.

## Review focus

- An administrator with an expired token must be able to renew/login and disable the switch.
- Modifying cached browser role information must not bypass API enforcement.
- A missing configuration defaults to off and remains visible/editable in the administrator screen.
- Login callback/profile requests must not loop into the forbidden page.
- Forbidden route transitions must not mount business pages or continue the artwork after unmount.

## Execution notes

User approved B before implementation. Working on `feat/site-access-takeover` in the current checkout; tracked files were clean and unrelated untracked outputs are left in place. Native execution; no repeated plan approval requested.


## Review and integration notes

- Final review identified and fixed `/history/` origin bypass, legacy SQLAdmin session placement, and administrator public SSE/chart credentials.
- Reused the existing TradingStream decoder/renewal path for authenticated public EventSource consumers; no new recovery queue or credentials-in-URL scheme.
- Backend history responses are gated at origin. Previously cached immutable history files may still be served by the existing browser/Nginx caches; this feature does not purge public historical files or alter deployment/cache topology.
- Backend gate/config/auth/middleware tests and frontend route/transport tests cover the new access boundary. Screenshot fixtures exercise page loading only, not control interactions.


## Final verification

- `pytest tests/test_site_access.py tests/test_site_config_api.py tests/test_site_config.py tests/test_auth.py tests/test_http_middleware.py tests/test_configurable_economy.py -q`: 55 passed.
- `npm run test:unit`: 27 files, 271 passed.
- `npm run build`: passed (existing chunk-size/dynamic-import warnings).
- `node /tmp/sunyunbo/site-access-production-screenshots.mjs`: desktop, mobile and reduced-motion screenshots; no page errors or horizontal overflow; source page issued only access-status requests. Administrator configuration page remained accessible. Fixture-backed screenshots, no buttons clicked or forms submitted.
- `git diff --check`: clean. No live switch change, deployment, push, or merge performed.
