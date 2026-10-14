# Employee onboarding: ServiceDesk Plus -> Microsoft Entra (Dagu)

```
Fetch Ticket -> Validate Ticket -> Create User -> Assign License -> Add to user groups
  -> Assign rule bundles -> Assign Intune apps -> Generate report -> Close ticket
```

Each box is one Dagu step (`dags/onboard-employee.yaml`, `type: chain`). A step that
fails exits non-zero, so Dagu does not run the remaining steps. The failure handler then
writes a failure report and adds a technician-only note to the ticket. The ticket stays open.

## Layout

| Path | Purpose |
|---|---|
| `onboard.py` | CLI, one sub-command per step (`onboard.py <step> <ticket_id>`) |
| `workflow.py` | Step implementations and the step runner |
| `clients/graph_client.py` | Microsoft Graph (users, licenses, groups, Intune apps) |
| `clients/servicedesk_client.py` | ServiceDesk Plus on-prem API v3 (get, note, resolution, close) |
| `helpers/transformation.py` | SDP field extraction, e-mail/UPN generation, Graph payload |
| `helpers/validation.py` | Required fields and business rules (offline) |
| `helpers/directory_map.py` | Local map of Entra groups / Intune apps and ticket-value resolution |
| `helpers/report.py` | Verification against Entra and the report (Markdown / JSON / SDP note) |
| `helpers/state.py` | Per-ticket state file passed between steps (never holds secrets) |
| `configs/onboarding.yaml` | Business rules and settings |
| `configs/servicedesk-entra-mapping.yaml` | SDP field paths |
| `dags/onboard-employee.yaml` | Dagu workflow |

## What each step does

1. **fetch**: `GET /api/v3/requests/{id}` and extract the fields listed in the mapping file.
   Multi-select fields (`user_groups`, `rule_bundles`, `intune_apps`, `license_sku`) become lists.
2. **validate**: checks required fields, approval status, request type, formats and Entra length limits.
   It then runs directory pre-flight checks before anything is created. These check for a
   duplicate staff number, whether the manager exists and is enabled, whether the license SKU
   exists and has free seats, and whether every group, rule bundle and app resolves to exactly
   one group whose membership can be managed. A typo therefore never leaves a half-onboarded user.
3. **create-user**: loads every UPN, mail and SMTP proxy address in the tenant (plus soft-deleted
   users when configured). Tries `firstname.lastname@domain`. If that address is taken anywhere,
   it uses `firstname.lastname<staff_no>@domain`. If both are taken it fails rather than guessing.
   It then creates the user and sets the manager.
4. **assign-license**: `POST /users/{id}/assignLicense` with the SKU(s) from the ticket.
   Friendly names map to SKUs through `license.sku_aliases`.
5. **add-groups** / 6. **assign-rule-bundles** / 7. **assign-intune-apps**: these three use the
   same mechanism. All Entra groups are loaded once into a local map, cached for
   `directory_cache.ttl_minutes`. Each ticket value is matched by alias, then by exact display
   name, then by `name_prefix` + value. A miss refreshes the map once before failing. Requests for
   role-assignable, dynamic, on-prem-synced, mail-enabled or (optionally) out-of-prefix groups
   are refused. With `intune_apps.resolve_via: intune_app`, the ticket can name the Intune app
   itself and the user is added to the group the app is assigned to.
8. **generate-report**: reads the user back from Entra (UPN, enabled, manager, licenses, group
   memberships), retrying briefly for replication. It writes `reports/onboarding-<ticket>-<ts>.md/.json`,
   prints the report into the Dagu log and adds a summary note to the ticket. A mismatch fails the
   step, so the ticket is not closed.
9. **close-ticket**: sets the resolution (plus any mandatory fields in `close.extra_request_fields`),
   then `PUT /api/v3/requests/{id}/close`.

**Re-runs are safe.** Running the DAG again for the same ticket skips work that is already done:
an existing user (recorded in state), licenses already assigned, existing memberships, and a ticket
that is already closed. Fix the cause and start the run again.

## Setup

1. Entra app registration (client credentials) with **application** permissions and admin consent:
   `User.ReadWrite.All`, `GroupMember.ReadWrite.All`, `Group.Read.All`,
   `LicenseAssignment.ReadWrite.All`, and `DeviceManagementApps.Read.All` (only for `resolve_via: intune_app`).
2. A ServiceDesk Plus technician API key for a technician who can view, add notes to and close requests.
3. `cp .env.example .env` and fill it in. Never commit `.env` or put secrets in YAML.
4. In `configs/servicedesk-entra-mapping.yaml`, replace the `request.udf_fields.*` paths with your
   real UDF keys (e.g. `udf_fields.udf_mslct_1805`). Save one real response with
   `GET /api/v3/requests/<id>` to check them. Also check `status`. In standard SDP the approval
   outcome is in `request.approval_status.name`, and `request.status.name` is Open, On Hold,
   Closed and so on. Point `status` at whichever field holds the values in `servicedesk.approved_statuses`.
5. In `configs/onboarding.yaml`, set the `sku_aliases`, group prefixes (`RB-`, `APP-`) and aliases
   to match your tenant.
6. `docker compose up -d --build`. The image adds Python and the scripts to the Dagu image.

The DAG uses current Dagu syntax (`run`, `params`, `${params.TICKET_ID}`, `handler_on`, `type: chain`).

## Running

From the Dagu UI start `onboard-employee` with `TICKET_ID=12345` (and `DRY_RUN=true` first), or:

```bash
dagu start onboard-employee -- TICKET_ID=12345 DRY_RUN=true
```

Locally:

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
export ONBOARDING_DATA_DIR=./data   # plus the ENTRA_* / SDP_* variables
python onboard.py fetch 12345 --input-file tests/sample-ticket.json
python onboard.py validate 12345 --dry-run false
python onboard.py run-all 12345 --dry-run true            # every step, no changes
```

Dry run reads from Graph and SDP but makes no changes. The report lists what would be done.

Exit codes: `0` ok, `2` validation, `3` configuration, `4` API, `5` step order, `6` verification, `1` other.

## Test with your M365 admin account (one user + Microsoft 365 F3)

This uses `configs/onboarding.test.yaml` and `tests/test-ticket-f3.json`. It needs no app
registration, no ServiceDesk Plus and no Dagu. You sign in once in the browser as your admin
account (MFA works). The account needs **User Administrator + License Administrator**, or
Global Administrator.

1. Edit `configs/onboarding.test.yaml`: set `defaults.domain` to a verified domain of your
   tenant (e.g. `yourcompany.onmicrosoft.com`).
2. Edit `tests/test-ticket-f3.json`: set `reporting_manager` to an existing user's UPN
   (your own account is fine).
3. Install and sign in (run everything from the repo root):

   ```bash
   python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
   export ENTRA_TENANT_ID=yourcompany.onmicrosoft.com     # or the tenant GUID
   export CFG=configs/onboarding.test.yaml
   python onboard.py login --config $CFG                  # open the URL, enter the code, sign in
   python onboard.py list-skus --config $CFG              # find F3: SPE_F1 (M365 F3) or DESKLESSPACK (O365 F3)
   ```

   The first sign-in asks you to consent to the Graph permissions. If it says admin approval
   is required, a Global Administrator must sign in once and tick "Consent on behalf of your
   organization".
   If `list-skus` shows `DESKLESSPACK` rather than `SPE_F1`, set `"license_sku": "Office 365 F3"`
   in the test ticket.

4. Dry run first, then the real run (each step is the same command Dagu runs):

   ```bash
   T=TEST-0001
   python onboard.py fetch $T --config $CFG --input-file tests/test-ticket-f3.json --dry-run true
   for s in validate create-user assign-license generate-report; do
     python onboard.py $s $T --config $CFG --dry-run true || break
   done
   # if the dry-run report in data-test/reports looks right:
   python onboard.py fetch $T --config $CFG --input-file tests/test-ticket-f3.json
   for s in validate create-user assign-license add-groups assign-rule-bundles assign-intune-apps generate-report; do
     python onboard.py $s $T --config $CFG || break
   done
   ```

   `create-user` prints the temporary password once in your terminal (`password.delivery: console`).
   `generate-report` reads the user back from Entra and confirms the F3 license is assigned.
   `close-ticket` is skipped because the ticket came from a file.

5. Clean up: delete the test user in the Entra admin center. This frees the F3 seat after the
   soft-delete. Then remove `data-test/` (state, reports and the cached sign-in).

The cached sign-in lives in `data-test/cache/msal_token_cache.json` (mode 0600). Treat it like
a password. Production Dagu runs should use `graph.auth_mode: app` with an app registration.

## Tests

```bash
python -m unittest discover -s tests -v      # or: pytest tests
```

The tests run the whole workflow against in-memory fakes of Graph and ServiceDesk Plus. They
cover e-mail fallback, pre-flight failures, stop-on-failure and re-run, dry run, Intune app
resolution, and verification blocking the close.

## Password handling

The generated password is never logged or written to state or reports. With the default
`password.delivery: none`, reset the password (or issue a Temporary Access Pass) when handing
over the account. `sdp_private_note` posts it once as a technician-only note on the ticket.
