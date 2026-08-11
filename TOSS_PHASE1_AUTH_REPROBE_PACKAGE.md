# Toss Phase 1 Auth and Reprobe Package

Status: `DEFER_TOSS_AND_KEEP_YFINANCE_CANONICAL`

This package is report-only. It does not authorize a Toss request, credential
change, Drive mutation, runner change, or canonical-source replacement.

## Diagnosis

The first natural schedule probe (`31438279843`, commit `1845cff626875f840ea4f3e60d3897565904d49e`) made one OAuth request and no market-data
request. It returned HTTP 4xx and was stored as
`TOSS_AUTH_OR_ENTITLEMENT_BLOCKED`. The producer then labeled every OAuth 403 as
`oauth_ip_not_allowed` without retaining the provider error code.

The official Toss contract documents OAuth client credentials and two possible
403 codes: `edge-blocked` and `forbidden`. It does not define HTTP 403 alone as
proof of an IP allow-list failure. The single primary defect is therefore:

- `PRODUCER_ERROR_CLASSIFICATION_DEFECT`

The unresolved operational subtype remains:

- `OAUTH_ERROR_SUBTYPE_UNVERIFIED`

Client inactivity, missing entitlement, and outbound-IP restriction are possible
causes, but none is proven by the preserved artifact. A new schedule alone cannot
resolve this because the sticky artifact suppresses later Toss requests.

Official references:

- Toss Open API: <https://developers.tossinvest.com/docs>
- GitHub-hosted runner networking: <https://docs.github.com/en/actions/reference/runners/github-hosted-runners>
- GitHub larger runners: <https://docs.github.com/en/actions/concepts/runners/larger-runners>

## Credential and Request Contract

- GitHub Actions contains `TOSS_CLIENT_ID` and `TOSS_CLIENT_SECRET` secret names;
  their values were not read or printed.
- `.github/workflows/main.yml` passes both only to the server-side Harvester step.
- The OAuth request uses `POST /oauth2/token`, client credentials, and
  `application/x-www-form-urlencoded`.
- No `VITE_*` Toss secret wiring exists in this producer path.
- `X-Tossinvest-Account` and account/order/conditional-order endpoints are absent.
- The canonical OHLCV source remains yfinance/Google Drive.

Credential wiring is present, but client registration and entitlement cannot be
verified from secret-name existence. Toss account administration or a written
provider response must confirm those facts before a reprobe.

## Execution Topology Decision

| Option | Fixed egress | Secret custody | Cost and operations | Decision |
|---|---|---|---|---|
| Standard GitHub-hosted `ubuntu-latest` | No; ranges are broad and change | GitHub Actions secrets | Lowest effort | Keep only while Toss remains deferred |
| Static-egress proxy | Yes | GitHub secrets plus proxy policy | New service, monitoring, and failure mode | Not justified without proven IP requirement |
| Fixed-IP self-hosted runner | Yes if host network is static | Runner host and GitHub | Patching, isolation, uptime, incident response | Reject for this public repository at present |
| Local/server-side scheduler | Depends on local ISP/network | Local secret store | Uptime, Drive handoff, alerting, recovery | Not justified until the OAuth subtype is known |
| Defer Toss; retain yfinance canonical | Not applicable | No new custody | Zero infrastructure change | **Recommended** |

GitHub states that standard hosted runner ranges are numerous and not recommended
for allow-lists; static IP is available through larger or self-hosted runners.
That makes static egress a valid future remedy only after Toss confirms it is a
registration requirement. It is not evidence that IP caused this 403.

## Sticky Artifact Inventory

Canonical Drive artifact:

- name: `System_Identity_Maps/TOSS_READ_ONLY_CAPABILITY.json`
- file ID: `1GqXInpPeKygTa5WkMxSoOSVBINWzahvk`
- parent ID: `1GyHFXrV63rVei9AJy4Op-xTgudmebZ8l`
- created: `2026-08-10T22:27:00.323Z`
- modified: `2026-08-10T22:27:03.378Z`
- size: `1558` bytes
- Drive byte SHA-256: `REQUIRED_BEFORE_MUTATION`

Google Drive metadata did not expose a content checksum. The downloaded workflow
artifact copy is 1437 bytes with SHA-256
`7124a2b1d424285e06c2f04823ce31bd6493baeb9de3a74aa5efcf4ee31b0ce9`;
because its size differs, that digest must not be asserted as the Drive object's
digest.

`harvester.py` reuses any matching schema-v1 artifact with `oneShotReservedAt`,
sets `runtimeAction=REUSED_ONE_SHOT_RESULT`, and records zero requests. The current
blocked evidence must remain untouched until every reprobe precondition passes.

## Reprobe Procedure (Not Yet Authorized)

Preconditions:

1. Confirm in Toss administration or in writing that the client registration is
   active, the market-data entitlement is enabled, and whether outbound IP
   allow-listing is required.
2. If IP allow-listing is required, provision and review one fixed-egress topology;
   do not infer this requirement from HTTP 403.
3. Confirm both GitHub secret names remain present without reading their values.
4. Confirm no Harvester run is active and reserve exactly one natural schedule.
5. Download the current Drive object once, calculate its byte SHA-256, and verify
   file ID, modified time, size, and hash before any metadata change.

Archive/reset:

1. Rename the existing Drive object in place to
   `TOSS_READ_ONLY_CAPABILITY.blocked.<timestamp>.<sha12>.json`. This preserves its
   file ID, bytes, `oneShotReservedAt`, and audit history; do not delete it.
2. Verify the archived object has the pre-change size and SHA-256.
3. Let the next eligible natural schedule create a new canonical reservation.
4. Permit at most one OAuth request and, only after OAuth success, one adjusted
   candle request.
5. Do not run `workflow_dispatch`, rerun, force, or a second probe.

Post-verification:

- `requestCounts.oauth <= 1`
- `requestCounts.marketData <= 1`
- `accountHeaderUsed=false`
- `orderEndpointUsed=false`
- no secret, raw response, account, or order evidence in artifacts
- yfinance/Google Drive remains canonical
- the new result records `oauthErrorCode` when a bounded provider code is present
- success still requires the candle schema, timestamps, pagination, hash, and
  rate-limit contract to pass

Rollback:

1. Preserve the new canonical result under a timestamped name.
2. Rename the archived blocked artifact back to
   `TOSS_READ_ONLY_CAPABILITY.json`.
3. Verify its original file ID, size, and captured SHA-256.
4. Leave Toss shadow integration disabled and yfinance canonical.

## Approval Gate

The following phrase is valid only after all preconditions, including the Drive
byte SHA-256 and registration/network confirmation, are recorded:

`AUTHORIZE TOSS PHASE1 READ-ONLY REPROBE — archive existing capability artifact, one OAuth request and at most one adjusted candle request, server-side Harvester only, no account header, no account/order endpoint, no canonical-source replacement, no broker/sidecar mutation`

Current verdict is not `AUTH_NETWORK_READY_FOR_REPROBE`. No request or artifact
mutation should occur until the unresolved OAuth subtype is independently confirmed.
