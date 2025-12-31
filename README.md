# ManageEIPs Automation (AWS CLI + jq + SAM) — Strict JSONL Discipline

End-to-end AWS automation lab that is fully reproducible, teardown-first, CLI-auditable (JSONL), and `SAM-template` driven for serverless deployment (`Lambda` + `IAM` + optional `EventBridge schedule`).

## What this repository demonstrates
- CLI-built disposable test harness (`VPC` + `EC2` + `EIPs`) used only to generate test conditions (1 attached `EIP`, 2 unassociated `EIPs`).
- `SAM-deployed` automation layer (`CloudFormation-managed`):
  - `Lambda` function that detects and releases `unassociated Elastic IPs`.
  - `IAM role` and `permissions` declared in `template.yaml`.
  - Optional `EventBridge schedule` declared in `template.yaml`.
- Operational safeguards and production-like hygiene:
  - `Dry-run` safety mode.
  - Structured `CloudWatch Logs` (JSON).
  - `Custom CloudWatch metrics` for `EIP` scanning/release outcomes.
  - `Multi-Region deployment pattern` (same `SAM` app deployed per Region).

## Non-negotiable engineering rules (portfolio standards)
- `CLI-only` (`AWS CLI` + `jq`, plus `SAM CLI` for deploy).
- `No hardcoded AWS IDs` (`VPC/Subnet/SG/EIP allocation IDs` discovered dynamically).
- Strict JSON Lines output for all “verify” steps (1 JSON object per line).
- No nested arrays in output; tags are flattened with 1 JSON object per tag.
- When AWS returns AWS-created names like `GroupName` or `RuleName`, they must remain different from the resource `Name` tag value (`NameTag`).

## Repository contents
- `ManageEIPs_Automation-SAM.md`: the authoritative, step-by-step runbook (teardown → rebuild → deploy → verify).
- `template.yaml`: SAM template defining `Lambda/IAM/optional schedule/observability` resources.
- `lambda_function.py`: `Lambda` source code.
- `*.jq`: `reusable jq filters` used across verification commands (e.g., `flatten_tags.jq`, `tag_helpers.jq`, `rules_names.jq`).
- `manage-eips-policy.json`, `manage-eips-trust.json`: `IAM policy/trust` JSON used in the lab workflow (do not commit secrets).

## Prerequisites
- `AWS CLI v2`, configured with a profile that can create the resources used in this lab.
- `jq` installed.
- `SAM CLI` installed (recommended: `pipx install aws-sam-cli`).
- A working shell environment (`WSL/Ubuntu` is used in the runbook).

## Quick start (high level)
1. Read and follow `ManageEIPs_Automation-SAM.md` from section **0** onward (it sets the global conventions and variables).
2. Export the required environment variables (examples used in the runbook):
   - `AWS_PROFILE`, `AWS_REGION`, `SAM_STACK_NAME`.
   - Tag defaults: `TAG_PROJECT`, `TAG_ENV`, `TAG_OWNER`, `TAG_MANAGEDBY`, `TAG_COSTCENTER`.
3. Create the `reusable jq filters` once (see the runbook section **0.7 Reusable jq filters**).
4. Validate/build/deploy the `SAM stack` (see runbook section **1.4** and **1.5**):
   - `sam validate`
   - `sam build`
   - `sam deploy --guided --stack-name "$SAM_STACK_NAME" --region "$AWS_REGION" --capabilities CAPABILITY_NAMED_IAM --s3-bucket "$SAM_ARTIFACTS_BUCKET"`
5. Run the disposable `network/EC2/EIP` harness to create test conditions (runbook section **2**).
6. Verify behavior end-to-end (runbook section **1.6**):
   - `Stack outputs` (JSONL).
   - `Lambda alias` + `IAM role/policies` (JSONL).
   - `EventBridge rule` + `targets` + `schedule expression` (JSONL).
   - `EIP before/after state` and `CloudWatch log evidence` (JSONL).

## Safety / cost notes
- This is a portfolio lab; teardown is part of the design.
- Prefer deleting the `SAM stack` to tear down the automation layer, and delete the CLI harness resources when done testing.
- Keep `schedules` disabled until `dry-run` and manual validations are successful (see runbook “Advanced Capabilities” section).

## Release/versioning guidance
- Use pre-release tags for WIP documentation milestones (e.g., `v0.0.1`, `v0.0.2`).
- Create the first “portfolio” release tag only when the runbook and `README` are final (typically `v1.0.0` or later).

## License
- Add a license if you want this repository to be reusable by others (MIT is common for portfolio labs).
