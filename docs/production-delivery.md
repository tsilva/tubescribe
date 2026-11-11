# Production delivery

A successful `CI` completion for a push to `main` triggers `.github/workflows/production-delivery.yml` through `workflow_run`. After secret scanning, it calls the trusted workflow in public `tsilva/.github`, pinned to a reviewed full commit SHA. The privileged job checks out only the shared implementation, rechecks the same latest main commit's already successful CI, syncs its fixed Production allowlist, deploys that exact commit, and verifies the resulting READY deployment. Vercel's automatic Git deployments remain disabled so they cannot race synchronization.

Automatic delivery accepts only successful main-push CI from this repository,
for the same SHA as the delivery run and the current head of `main`. The CI
workflow path, immutable repository IDs, run ID and rerun attempt are checked
again against GitHub before secret access. Failed, cancelled, forked, stale or
superseded runs cannot deploy. Manual and secret-change relay dispatches remain
available, but fail immediately if the latest exact-commit CI is not already
successful. CI artifacts and caches are not imported into the trusted sync job.

Infisical remains the source of application secrets. Development uses the existing human CLI login. The shared GitHub reader has Viewer access to the assigned production vaults, and the completed rollout requires its server-side OIDC policy to match the exact approved reusable workflow revision, caller repository IDs, owner ID, main branch and Production context. The trusted `apps.json` selects the source vault and destination from the immutable caller repository ID; app files and inputs cannot change that mapping. The reader itself can access all assigned vaults inside the trusted job. Its token and fetched values never leave that job. The Vercel deployment credential remains in this repository's GitHub Production environment.

The pinned shared source and [trust contract](https://github.com/tsilva/.github/blob/c262be7b75fe4654c170eb5e13240808a70b9936/.github/actions/production-delivery/README.md) are authoritative. App repositories retain only the pinned caller workflow. Source/destination mappings, credential handling and boundary tests are maintained centrally; no app code executes in the privileged job.

The workflow does not run on pull requests or forks, export `.env` files, upload secret artifacts, delete secret variables, or change preview/development variables. All required source keys and destination targets are checked before writes. Failed CI or sync prevents deployment. Existing production remains live on failure.

Automatic secret-change delivery uses a standalone Cloudflare Worker with a private GitHub App, independent of application deployments. Each vault's signed General webhook is restricted to `prod`, `/`, and `secrets.modified`. The relay receives event metadata and never fetches application secrets. It verifies raw-body HMACs and timestamps, stores pending routing metadata durably, coalesces bursts and retries dispatches during GitHub outages. A per-repository installation token starts only the fixed entry workflow on `main`. The relay endpoint is `https://production-deployment-relay.tsilva-production-delivery.workers.dev/infisical`. Webhook activation and per-app live verification are required before calling the automatic path operational.

Use **Actions → Production delivery → Run workflow → main** to recover after fixing a failed workflow. A relay acknowledgement confirms durable acceptance, and a GitHub dispatch acknowledgement confirms only that the run was accepted. Neither confirms deployment success. There is no polling schedule. Deployment credentials expire after one year and need rotation before expiry.

Native Infisical syncs are disabled after this workflow has successfully replaced them. Updating a deployment credential in GitHub does not replace application keys in Infisical.

The GitHub Production environment, project-scoped deployment credential, existing application CI and disabled direct Vercel Git deployments remain required.

Neon Realtime uses the TubeScribe Infisical project's dedicated `/neon-realtime`
folder in `prod`. `NEON_REALTIME_SECRET` is mirrored as a write-only Secret and
`NEON_REALTIME_URL` as public Config, both in Vercel Production only. The currently
pinned shared allowlist still manages only `TUBESCRIBE_PUBLIC_ORIGIN`; it preserves
these separately provisioned variables but does not synchronize their rotation.
After changing the source credential, explicitly mirror these two values through
the authorized Production settings workflow before running Production delivery.
Do not expand shared OIDC trust or enable another sync as an application-release
workaround. See [the Realtime reader](guide.md#neon-realtime-reader) for its scope
and verification limits.

Production credentials must differ from Development and Preview; explicitly public configuration stays Config, while private credentials stay write-only Secret. Require Separate Values remains enabled.

Vercel Production domain assignment and required integration checks, including Clerk DNS verification, must succeed before release is reported live. A READY build alone is insufficient. Backend PostgreSQL, media/S3, local processing and protected administration remain governed by [the hosting guide](guide.md#vercel-hosting-with-local-processing).

The account-independent sign-in shell at `/` and bundled branding files can be
cached by Vercel for 60 seconds. Branding includes the fixed `/brand/` entries in
the static-asset map and their `/favicon.ico` alias, not arbitrary files.
Browser and downstream-CDN headers remain `no-store`. Only successful GET/HEAD
responses for those fixed public files are eligible, without query parameters,
an Authorization header, or a shared dashboard access token. The shell contains
public bootstrap configuration, never library records or account identity. Private
data, configuration endpoints, return-target URLs, scripts and styles continue to
bypass caches. Verify deployed shell and branding cache status and private-route
denial after changing this policy.

The sign-in HTML preloads Clerk's pinned browser scripts and its existing shared UI
chunks. Shared chunks use low fetch priority and the same request mode as the UI
loader; these hints do not execute code. The browser bootstrap retains its hints
for older cached documents, and matching requests are coalesced by the browser.

The optional [Workflow backend](guide.md#owner-selected-cloud-processing) preserves
this delivery contract. Its dedicated cloud Agent Vault bindings, maintenance
capability and immutable media image or clean snapshot are separately provisioned Production
configuration; the shared allowlist does not currently synchronize them. Keep
`TUBESCRIBE_PROCESSING_BACKEND` unset until those bindings and the cloud smoke test
pass. Deployment code never activates subscriptions or changes billing. A cloud
cutover additionally requires verified automatic discovery, manual-only admission,
saved-result recovery, and signed-in transcript and summary delivery before
retiring the Mac services.
