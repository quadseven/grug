# GitHub App permissions

The app needs these repository permissions. `write` includes read access.

| Permission | Level | Feature |
| --- | --- | --- |
| `contents` | `write` | Read code and resolve fixed review threads |
| `pull_requests` | `write` | Read PRs, publish reviews, and manage review threads |
| `issues` | `write` | Read and update issues and issue comments |
| `checks` | `write` | Publish Elder and other persona check runs |
| `metadata` | `read` | Required repository metadata access |

GitHub's GraphQL reference describes `resolveReviewThread` but does not list
the required permission. The requirement above reflects the observed GitHub
authorization behavior: `pull_requests: write` alone is insufficient, while
`contents: write` is required. See [the GraphQL mutation reference](https://docs.github.com/en/graphql/reference/pulls#resolvereviewthread)
and [GitHub's permissions for Apps](https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps).

## Optional permission

`administration: write` lets Grug create and remove its own enforcement
rulesets, and reading enforcement state needs `administration: read`. Without it
Grug reports `permission_denied` for enforcement and keeps reviewing, so the
runtime check above does not require it. It is the largest grant the App holds:
see [the least-privilege comparison](reviewer-attack-surface.md#github-app-permissions).

## Purpose-scoped tokens

Each token is minted for one purpose and asks only for the granted
permissions that purpose needs:

- review (the default): no `administration`, `contents: read`, no `workflows`.
- ruleset: adds `administration: write`. Enforcement create, heal, remove and
  detection use it.
- commit: adds `contents: write`. `/grug fix`, `/grug docstrings` and thread
  resolution use it.

Writes are an allowlist. Every purpose may write `checks`, `issues`,
`pull_requests` and `statuses`; any other granted permission is capped at
`read`, and `secrets` and `workflows` are dropped.

A token never asks for a permission the installation lacks, so an installation
without `administration` still reviews.

## Grant a permission

1. Open the GitHub App's settings page.
2. Select **Permissions and events**.
3. Under **Repository permissions**, find the permission and set its level to
   the level in the table above.
4. Select **Save changes**.
5. Accept the permission request for each existing installation. The new
   grant does not take effect for an installation until it is accepted.

The bot reads each installation's grant (`GET /app/installations/{id}`) before
it mints a token, and re-reads it on a token refresh. If a required permission is missing, it logs one
`app_permissions_missing` event for that installation. When `contents: write`
is missing, Elder's check summary says fixed threads are collapsed instead.
