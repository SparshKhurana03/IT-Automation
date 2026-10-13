Employee lifecycle onboarding runner
Files
`employee_lifecycle.py`: ServiceDesk validation and Microsoft Entra provisioning runner.
`onboarding.yaml`: Workflow rules and Microsoft Graph settings.
`servicedesk-entra-mapping.yaml`: ServiceDesk field paths and Entra mappings.
Install
```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```
Required environment variables
```bash
export ENTRA_TENANT_ID="tenant-guid"
export ENTRA_CLIENT_ID="app-registration-client-guid"
export ENTRA_CLIENT_SECRET="retrieve-from-secret-store"
export SDP_USERNAME="service-account"
export SDP_PASSWORD="retrieve-from-secret-store"
```
Do not place secrets inside either YAML file.
Run a safe local validation
```bash
python employee_lifecycle.py   --input-file sample-ticket.json   --config onboarding.yaml   --dry-run
```
Process an approved ServiceDesk ticket
```bash
python employee_lifecycle.py   --ticket-id 12345   --config onboarding.yaml
```
Dagu step
```yaml
steps:
  - name: provision-entra-user
    run: >
      python /app/employee_lifecycle.py
      --ticket-id "${TICKET_ID}"
      --config /app/config/onboarding.yaml
```
Start with `--dry-run`. Confirm the exact ServiceDesk Plus API response paths and note payload format for your deployment before enabling production writes.


Current Workflow

Receive ServiceDesk ticket ID
        |
        v
Retrieve ticket from ServiceDesk Plus
        |
        v
Load servicedesk-entra-mapping.yaml
        |
        v
Extract logical fields from the SDP response
        |
        v
Validate approval and required fields
        |
        v
Generate display name, alias and UPN
        |
        v
Check duplicate employee ID
        |
        v
Check UPN availability
        |
        v
Resolve reporting manager
        |
        v
Create Microsoft Entra user
        |
        v
Assign reporting manager
        |
        v
Update ServiceDesk ticket