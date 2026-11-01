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

| Permission | Level | Feature |
| --- | --- | --- |
| `administration` | `write` | Create and remove Grug's own enforcement rulesets |

Reading enforcement state needs `administration: read`. Without the permission
Grug reports `permission_denied` for enforcement and keeps reviewing, so the
runtime check above does not require it. It is the largest grant the App holds:
see [the least-privilege comparison](reviewer-attack-surface.md#github-app-permissions).

## Grant a permission

1. Open the GitHub App's settings page.
2. Select **Permissions and events**.
3. Under **Repository permissions**, find the permission and set its level to
   the level in the table above.
4. Select **Save changes**.
5. Accept the permission request for each existing installation. The new
   grant does not take effect for an installation until it is accepted.

The bot checks the permissions returned when it first mints an installation
token in each process. If a required permission is missing, it logs one
`app_permissions_missing` event for that installation. When `contents: write`
is missing, Elder's check summary says fixed threads are collapsed instead.
