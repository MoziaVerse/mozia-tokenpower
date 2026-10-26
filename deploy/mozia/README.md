# Mozia deployment

New API (upstream: QuantumNous/new-api) is the customer gateway; Sub2API
(upstream: Wei-Shaw/sub2api) is its internal subscription-account upstream.
Both applications run code built from their respective `mozia` branches.
`main` tracks the original upstream and contains no deployment customizations.

The sibling server checkouts are `/opt/mozia/mozia-newapi` and
`/opt/mozia/mozia-sub2api`. Runtime secrets are outside Git, in
`/opt/mozia/secrets/stack.env` (root, mode 0600). Do not commit or print this
file, resolved Compose configuration, API keys, or bootstrap credentials.

## Operate

Run from this directory:

```sh
docker compose --env-file /opt/mozia/secrets/stack.env ps
docker compose --env-file /opt/mozia/secrets/stack.env up -d --no-build
```

Images use the source commit as their tag and an OCI revision label. New API's
`VERSION` build argument also records the commit in its UI/API version field;
the upstream repository's `VERSION` file is empty outside release builds. Builds
target `linux/arm64`. Build the applications sequentially on the 2-core VPS.
PostgreSQL 18 and Redis are separate for each service, with persistent volumes
and private data networks. No database or Redis port is published.

Sub2API runs in standard mode. The ordinary user `Mozia-TokenPower` is the dedicated
New API upstream identity, with separate OpenAI and Anthropic model API keys.
It is restricted to `openai-default` and `anthropic-default`, starts with an
owner-authorized USD 1,000 internal balance, and has an aggregate concurrency
limit of 10. This is internal accounting credit, not a payment or customer funds.
Customer accounts and customer-facing charging remain in New API.

Standard mode checks and deducts the service user's balance; monitor and refill
it before it runs out. Keep upstream accounts assigned to the corresponding
groups. Public registration stays disabled. The service user's login and keys
are stored in `/opt/mozia/secrets/newapi-upstream.json` (root, mode 0600), outside
Git. The previous administrator-owned integration keys are retired after the
new credentials pass verification. Application ports remain bound to loopback;
Caddy publishes the two HTTPS domains on ports 80 and 443.

The public brand is configured through New API's native `SystemName=TokenPower`
and `Logo=/tokenpower-logo-v3.png` settings. The generated transparent logo is
served from the embedded frontend assets. Upstream attribution is retained.
The service user's login email is `mozia-tokenpower@example.com`; its generated
password is in the protected `newapi-upstream.json` file above.

```sh
ssh -N -L 13000:127.0.0.1:3000 -L 18080:127.0.0.1:8080 qiniu-sg
```

The local ports are available for diagnostics. Use the HTTPS domains for normal
sign-in, since New API session cookies are now marked Secure.
The public sites are `https://tokenpower.link` (New API) and
`https://sub.tokenpower.link` (Sub2API login and authenticated administration).
Runtime values are `NEWAPI_DOMAIN=tokenpower.link`,
`NEWAPI_ORIGIN=https://tokenpower.link`, `SUB2API_DOMAIN=sub.tokenpower.link`,
`SUB2API_ORIGIN=https://sub.tokenpower.link`, and `HTTPS_ENABLED=true`.
Start with `docker compose --env-file /opt/mozia/secrets/stack.env --profile https up -d --no-build`.
Caddy obtains and renews certificates using HTTP-01 and forwards streaming
responses without buffering. Port 80 must remain reachable for renewal.
Cloudflare DNS records must point at this VPS and its SSL mode must be Full (strict).
The trusted proxy ranges in Caddyfile must follow Cloudflare's published ranges.

New API's `ServerAddress` and Sub2API's `frontend_url` match their HTTPS origins.
Sub2API's `registration_enabled=false` and
`dingtalk_connect_bypass_registration=false` disable self-registration on the
server; Caddy redirects its home and registration pages to `/login`.
New API uses `http://sub2api:8080` internally, with distinct channel credentials
for OpenAI and Anthropic groups.

New API's `USDExchangeRate=7` and `Price=7` set the CNY display conversion and
the base CNY top-up price respectively. Sub2API's dormant subscription CNY
conversion is also 7; its payments remain disabled. Balances remain USD units;
these settings do not revalue existing balances or change model prices.

Import authorized upstream accounts in Sub2API before enabling customer traffic.
Verify each channel and its intended models and prices before accepting payment.
A healthy service or a working model listing does not prove upstream inference.

## Sync upstream and update

In a maintenance checkout of each fork (origin = MoziaVerse fork,
upstream = original repository), with a clean worktree:

```sh
git fetch origin
git fetch upstream
git switch main
git merge --ff-only origin/main
git merge --ff-only upstream/main
git push origin main
git switch mozia
git merge --ff-only origin/mozia
git merge main
# Resolve any conflicts, review and verify before pushing.
git push origin mozia
```

Never force-push `main` or overwrite the deployment branch with upstream.
On the server, fast-forward each `mozia` checkout, build fresh images tagged
with their commits, then update `NEWAPI_IMAGE`, `SUB2API_IMAGE` and
`NEWAPI_REVISION`, `SUB2API_REVISION` in the runtime env file and recreate the services.
Keep the previous images and take database backups before an upgrade.
Database migrations may prevent an image-only downgrade.

## Backup

Use `backup.sh` before upgrades. It writes PostgreSQL custom-format dumps and
a protected copy of the runtime secrets into `/opt/mozia/backups/<UTC timestamp>`.
Copy backups to separate storage for protection against loss of this VPS.
Backups and secrets must never enter Git. Redis is operational cache/state;
application-owned files live in the named data volumes and should also be
included in a full disaster-recovery backup.
