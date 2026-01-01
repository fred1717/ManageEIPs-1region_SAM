# ManageEIPs Automation – AWS CLI + jq + SAM (Strict JSONL Discipline)

End-to-end infrastructure automation exercise designed to be:
- fully reproducible
- teardown-first / rebuild-from-scratch
- auditable via structured CLI output
- free of hardcoded AWS resource IDs
- SAM-template driven for serverless deployment (Lambda + IAM + optional schedule)

## Scope
This project demonstrates how to:
- Create a complete VPC-based environment (AWS CLI):
  - `VPC`, `subnets`, `route tables`, `Internet Gateway`
  - `security groups`
  - `EC2 instance` (test harness / `target`)
- Allocate and manage `Elastic IPs`:
  - 1 attached `EIP`
  - 2 intentionally unused `EIPs` (clean up `targets`)
- Deploy `serverless automation` with `SAM` (CloudFormation-managed):
  - `Lambda` function that detects and releases unused `Elastic IPs`
  - `IAM role` + `permissions` declared in the `SAM` template
  - `EventBridge schedule` declared in the `SAM` template
- Implement operational safeguards:
  - `Dry-Run` safety mode
  - structured `CloudWatch  logging` (JSON)
  - `CloudWatch metrics` for released `EIPs`

## Engineering principles enforced throughout
All steps in this document follow strict operational rules:

- **CLI-only**
  - `AWS CLI` + `jq` for infrastructure and verification
  - `SAM CLI` for build/package/deploy of the `serverless stack`
  - no Console, no SDK-driven provisioning
- **Dynamic resource discovery**
  - no hardcoded `VPC IDs`, `subnet IDs`, `SG IDs`, `allocation IDs`, etc.
- **Strict JSON Lines (JSONL) output**
  - exactly **1 JSON object per line**
  - no human-formatted tables
- **No nested arrays in output**
  - tag data is fully flattened
  - **1 JSON object per tag**
- **Reusable jq filters**
  - defined once, reused everywhere
  - consistent output contracts for every “verify” step
- **Separation of concerns**
  - imperative commands (create / delete)
  - declarative commands (describe / verify)
  - serverless resources declared in SAM templates, validated via CLI

## Workflow
1. **Clean existing resources** to start from a known empty state
2. **Rebuild the entire environment from scratch**
3. **Build and deploy the SAM stack** (`ManageEIPs Lambda` + `IAM` + `schedule`)
4. **Verify behavior** via strict JSONL CLI output, structured logs, and metrics

The cleanup phase is intentionally documented first to allow:
- repeatable testing
- safe re-runs
- forensic debugging when things go wrong



## 0. Global environment and conventions
This section defines the **global execution context** for the entire project.
All subsequent commands assume these settings are in place.

### 0.1 Shell safety (recommended)
**Use strict mode, so failures stop the run immediately (repeatability + debugging).**
```bash
set -euo pipefail
```


### 0.2 AWS CLI identity and credentials (CLI-only verification)
**Verify which profile/region we are using**
```bash
aws configure list --no-cli-pager
```

**Verify the AWS account (this is the definitive check)**
```bash
aws sts get-caller-identity --no-cli-pager | jq -c '{Account:.Account,Arn:.Arn,UserId:.UserId}'
```
**Expected output**
```json
{"Account":"1802********72","Arn":"arn:aws:iam::1802********72:user/<redacted>","UserId":"AIDA***************"}
```

**To view credentials locally (without committing this file)**
```bash
cat ~/.aws/credentials
```


### 0.3 Tooling baseline (jq + AWS CLI pager discipline)
**Install jq (WSL/Ubuntu), if needed**
```bash
sudo apt update && sudo apt install -y jq
```

**Disable AWS CLI pager globally (`no pager`, output always on `stdout`)**
```bash
aws configure set cli_pager ""
```

**Enforce pager-off per-session as well**
```bash
export AWS_PAGER=""
```


### 0.4 SAM CLI (required for this SAM version of the project)
#### 0.4.1 Standardize SAM CLI to pipx (portfolio-reproducible)
Our system can have SAM CLI installed by different methods (system-wide vs pipx).

**If upgrading fails, identify which installation we are using.**
```bash
command -v sam && sam --version
```

If `SAM` is under `/usr/local/bin/sam`, it is not managed by `pipx` by default.
**Confirm whether it is an `APT` package or a `pip` install**
```bash
dpkg -S "$(readlink -f "$(command -v sam)")" 2>/dev/null || echo "Not from apt/dpkg"
python3 -m pip show aws-sam-cli 2>/dev/null | sed -n '1,12p' || echo "Not from pip"
```
**Example output**
```text
Not from apt/dpkg
Not from pip
```

If `sam` is installed under `/usr/local/bin`, it is not owned by `apt/dpkg` and is typically not pipx-managed.
**To avoid upgrade/ownership confusion, manage SAM CLI with `pipx`**
```bash
sudo apt update && sudo apt install -y pipx
pipx ensurepath
pipx install aws-sam-cli
```

**In a new terminal**
```bash
command -v sam && sam --version
```
**Example output**
```text
/home/mfh/.local/bin/sam  
SAM CLI, version 1.151.0
```

#### 0.4.2 Fast-fail template validation (run from the folder containing `template.yaml`)
```bash
sam validate
```
If `template.yaml` is empty, `sam validate` fails.

**Minimal fix creates a “Dummy” Lambda that must be removed later**
```bash
cat > template.yaml <<'YAML'
AWSTemplateFormatVersion: "2010-09-09"
Transform: AWS::Serverless-2016-10-31
Description: ManageEIPs-1region_SAM (skeleton)

Resources:
  Dummy:
    Type: AWS::Serverless::Function
    Properties:
      Runtime: python3.12
      Handler: index.handler
      InlineCode: |
        def handler(event, context):
            return {"ok": True}
YAML
```
Remove/replace the Dummy function when the real `ManageEIPs` `SAM` resources are added.

**Re-run `sam validate`**
```bash
sam validate
```
**Example output**
```text
Template is valid.
```


### 0.5 Global environment variables (single-region + reproducible naming)
**Initialize variables for the current shell session**
```bash
export AWS_PROFILE=default
export AWS_REGION=us-east-1

ACCOUNT_ID=$(aws sts get-caller-identity --no-cli-pager | jq -r '.Account')

AWS_AZ="${AWS_REGION}a"

SNS_TOPIC_ARN="arn:aws:sns:${AWS_REGION}:${ACCOUNT_ID}:ManageEIPs-Alarms"

export TAG_PROJECT="ManageEIPs"
export TAG_ENV="dev"
export TAG_OWNER="Malik"
export TAG_MANAGEDBY="CLI"
export TAG_COSTCENTER="Portfolio"

export SAM_STACK_NAME="ManageEIPs-1region-SAM"
```

**Tag keys used consistently across all resources**
- Name
- Project
- Component
- Environment
- Owner
- ManagedBy
- CostCenter


### 0.6 Output discipline (applies to every “verify” command)
- Default: AWS CLI output is piped to `jq -c` and printed as JSONL (1 JSON object per line).
- Exception: if a JSON field contains colons (or otherwise harms readability), use a multi-line JSON object, 
  but still keep **exactly 1 JSON object per block**.
- No human-formatted tables.
- No nested arrays in output: tags are flattened with **1 JSON object per tag**.
- When AWS returns fields like `GroupName` / `RuleName` (AWS-created names), they must stay different from `NameTag`.


### 0.7 Reusable jq filters (one-time setup)
Create reusable filters once, then reuse them everywhere.

#### 0.7.1 Create `flatten_tags.jq` (tag-array normalization helper)
```bash
cat > flatten_tags.jq <<'JQ'
def flatten_tags($tags): ($tags // [] | if length>0 then . else [] end);
JQ
```

#### 0.7.2 Create `tag_helpers.jq` (safe Name tag extraction)
```bash
cat > tag_helpers.jq <<'JQ'
def _tags($o): ($o.Tags // $o.TagSet // []);
def tag_value($o; $k): (_tags($o) | first(.[]? | select(.Key==$k) | .Value) // null);
def tag_name($o): tag_value($o; "Name");
def must_tag_name($o): (tag_name($o) // "MISSING-NAME-TAG");
JQ
```

These filters will be reused in all subsequent inspection commands involving tags.



## 1. Teardown and deploy workflow (SAM-first)
In the SAM version, teardown is primarily done by deleting the CloudFormation stack created by `sam deploy`. 
Manual CLI cleanup by service is kept as a fallback.

### 1.1 SAM scope decision (templated vs CLI-built)
#### 1.1.1 Option A — Template networking/EC2/EIPs in SAM (everything as IaC)
**Pros**
- Single source of truth; minimal drift
- One “deploy” recreates the whole environment
- Best for reproducibility and review

**Cons**
- Bigger template, more parameters/outputs
- Debugging CloudFormation failures can be slower than CLI
- EC2/EIP scaffolding increases cost unless aggressively gated/disabled

#### 1.1.2 Option B — Template automation in SAM; CLI-build networking/EC2/EIPs test harness (recommended)
**Pros**
- SAM template stays focused on the “product”: automation + observability
- CLI harness is flexible for experiments (attach/detach EIPs, recreate `instances` quickly)
- Easy to delete harness completely while keeping the `SAM stack` intact

**Cons**
- Two sources of truth (template + CLI steps)
- Must be disciplined about teardown to avoid leftovers
- Reviewers must read both the SAM template and the CLI harness section

#### 1.1.3 Decision for this repository
This repository templates the automation layer in SAM for repeatable deployment. 
The VPC/EC2/EIP resources are a disposable, CLI-built test harness used only to create unassociated EIPs for validation.
The harness is intentionally torn down after tests to minimize cost.


### 1.2 Teardown via SAM (preferred)
#### 1.2.1 Confirm active stack name and region
**Check whether your shell variables are empty or wrong before querying CloudFormation**
```bash
jq -c -n --arg p "${AWS_PROFILE:-}" --arg r "${AWS_REGION:-}" --arg s "${SAM_STACK_NAME:-}" '{AWS_PROFILE:$p,AWS_REGION:$r,SAM_STACK_NAME:$s}'
```
**Expected output**
```json
{"AWS_PROFILE":"default","AWS_REGION":"us-east-1","SAM_STACK_NAME":"ManageEIPs-1region-SAM"}
```

**List all non-deleted stacks in the region (to find the real stack name)**
```bash
aws cloudformation list-stacks --region "$AWS_REGION" --no-cli-pager | jq -c '.StackSummaries[]? | select(.StackStatus!="DELETE_COMPLETE") | {StackName:.StackName,StackStatus:.StackStatus}'
```
**Example output**
```json
{"StackName":"BillingBucketParserStack","StackStatus":"CREATE_COMPLETE"}
{"StackName":"LambdaEC2DailySnapshot","StackStatus":"UPDATE_COMPLETE"}
{"StackName":"aws-sam-cli-managed-default","StackStatus":"CREATE_COMPLETE"}
{"StackName":"Infra-ECS-Cluster-my-fargate-cluster-c0518a8c","StackStatus":"CREATE_COMPLETE"}
```

#### 1.2.2 Delete the SAM stack (CloudFormation)
Our SAM stack does not exist in the region, so there is nothing to delete.
```bash
jq -c -n --arg s "$SAM_STACK_NAME" --arg r "$AWS_REGION" '{StackName:$s,Region:$r,Exists:false,Action:"Skip sam delete"}'
```
**Expected output**
```json
{"StackName":"ManageEIPs-1region-SAM","Region":"us-east-1","Exists":false,"Action":"Skip sam delete"}
```

#### 1.2.3 Verify stack deletion completed
As our SAM stack does not exist in the region, the verification does not apply.


### 1.3 Teardown via AWS CLI (fallback for leftovers)
Use this section only when the SAM stack does not exist (as in our case). 
The goal is to find and delete any leftover regional resources from earlier runs, using tags and name patterns, with JSONL verification before deletion.

#### 1.3.1 List remaining resources by service (JSONL inventory)
**Purpose**
- Each command below is read-only and emits JSONL (1 object per resource).
- Some services are name-filtered because tags are not consistently available (e.g., EventBridge rules).

**EventBridge rules (name-based; rules may not be consistently tagged)**
```bash
aws events list-rules --region "$AWS_REGION" --no-cli-pager | jq -c --arg p "$TAG_PROJECT" '.Rules[]? | select((.Name|tostring|contains($p)) or (.Name|tostring|contains("ManageEIPs"))) | {RuleName:.Name,State:.State,ScheduleExpression:(.ScheduleExpression//null)}'
```
**Expected output**
```json
{"RuleName":"CheckAndReleaseUnassociatedEIPs-Monthly","State":"ENABLED","ScheduleExpression":"cron(0 21 30 * ? *)"}
```

**Lambda functions (name-based; then confirm exact function name(s) before deleting)**
```bash
aws lambda list-functions --region "$AWS_REGION" --no-cli-pager | jq -c '.Functions[]? | select((.FunctionName|tostring|contains("ManageEIPs"))) | {FunctionName:.FunctionName,Runtime:.Runtime,LastModified:.LastModified}'
```
**Expected output**
```json
{"FunctionName":"ManageEIPs","Runtime":"python3.12","LastModified":"2025-12-24T04:03:47.000+0000"}
```

**CloudWatch alarms (name-based)**
```bash
aws cloudwatch describe-alarms --region "$AWS_REGION" --no-cli-pager | jq -c '.MetricAlarms[]? | select((.AlarmName|tostring|contains("ManageEIPs"))) | {AlarmName:.AlarmName,StateValue:.StateValue,Namespace:.Namespace,MetricName:.MetricName}'
```
**Expected output**
```json
{"AlarmName":"ManageEIPs-DurationHigh","StateValue":"OK","Namespace":"AWS/Lambda","MetricName":"Duration"}
{"AlarmName":"ManageEIPs-Errors","StateValue":"OK","Namespace":"AWS/Lambda","MetricName":"Errors"}
{"AlarmName":"ManageEIPs-Throttles","StateValue":"OK","Namespace":"AWS/Lambda","MetricName":"Throttles"}
```

**CloudWatch dashboards (name-based)**
```bash
aws cloudwatch list-dashboards --region "$AWS_REGION" --no-cli-pager | jq -c '.DashboardEntries[]? | select((.DashboardName|tostring|contains("ManageEIPs"))) | {DashboardName:.DashboardName,LastModified:.LastModified}'
```
```json
{"DashboardName":"ManageEIPs-Dashboard","LastModified":"2025-12-28T00:20:10+00:00"}
```

**SNS topics (name/ARN contains ManageEIPs)**
```bash
aws sns list-topics --region "$AWS_REGION" --no-cli-pager | jq -c '.Topics[]? | select((.TopicArn|tostring|contains("ManageEIPs"))) | {TopicArn:.TopicArn}'
```
```json
{"TopicArn":"arn:aws:sns:us-east-1:180294215772:ManageEIPs-Alarms"}
```

**Elastic IPs (tag-based if tags exist; falls back to Name tag extraction via tag_helpers)**
```bash
aws ec2 describe-addresses --region "$AWS_REGION" --no-cli-pager | jq -L . -c 'include "tag_helpers"; .Addresses[]? | select((tag_value(.;"Project")=="ManageEIPs") or (tag_name(.)=="EIP_Attached_ManageEIPs") or (tag_name(.)=="EIP_Unused_ManageEIPs")) | {AllocationId:.AllocationId,AssociationId:(.AssociationId//null),InstanceId:(.InstanceId//null),PublicIp:(.PublicIp//null),NameTag:(must_tag_name(.))}'
```
**Expected output**
```json
{"AllocationId":"eipalloc-0da06ada08055dcb5","AssociationId":"eipassoc-02a07f8bc7b6d1adf","InstanceId":"i-0c69cfce5294ffdd4","PublicIp":"100.50.117.194","NameTag":"EIP_Attached_ManageEIPs"}
```

#### 1.3.2 Release unused / unassociated Elastic IPs
This `targets` only EIPs that are unassociated (AssociationId is null) and match our ManageEIPs tag/name conventions.

**List unassociated candidate AllocationIds (shell plumbing)**
```bash
aws ec2 describe-addresses --region "$AWS_REGION" --no-cli-pager | jq -L . -r 'include "tag_helpers"; .Addresses[]? | select(.AssociationId==null) | select((tag_value(.;"Project")=="ManageEIPs") or (tag_name(.)|startswith("EIP_"))) | .AllocationId'
```
**Expected output**
```json

```

**Release each AllocationId (JSONL result per EIP)**
```bash
aws ec2 describe-addresses --region "$AWS_REGION" --no-cli-pager | jq -L . -r 'include "tag_helpers"; .Addresses[]? | select(.AssociationId==null) | select((tag_value(.;"Project")=="ManageEIPs") or (tag_name(.)|startswith("EIP_"))) | .AllocationId' | while read -r ALLOC; 
do OUT=$(aws ec2 release-address --allocation-id "$ALLOC" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null); 
RC=$?; 
if [ $RC -eq 0 ]; 
then jq -c -n --arg a "$ALLOC" '{AllocationId:$a,Released:true}'; 
else jq -c -n --arg a "$ALLOC" --arg err "$OUT" '{AllocationId:$a,Released:false,Error:$err}'; 
fi; 
done
```
No output.

#### 1.3.3 Delete networking resources (dependency order)
**Purpose**
- Remove only ManageEIPs networking resources (VPC + attached dependencies) when no SAM/CloudFormation stack exists.
- Dependency order: instances/LB/endpoints/NAT/ENIs/SG/subnets/route tables/IGW/VPC.

##### 1.3.3.1 Scope gate: list candidate ManageEIPs VPCs
**Purpose**
Prevents accidental deletion of unrelated networking.

```bash
export AWS_REGION="us-east-1"
aws ec2 describe-vpcs --region "$AWS_REGION" --no-cli-pager | jq -L . -c 'include "tag_helpers"; .Vpcs[]? | (must_tag_name(.) as $Name | tag_value(.;"Project") as $Proj | select(($Proj=="ManageEIPs") or ($Name|contains("ManageEIPs")) or ($Name|contains("VPC_ManageEIPs"))) | {VpcId:.VpcId,VpcName:$Name,Project:($Proj//"null")})'
```
**Expected output**
```json
{"VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs","Project":"ManageEIPs"}
```

##### 1.3.3.2 Terminate ManageEIPs EC2 instances
**Why**
Instances must go first because they create/attach ENIs and block subnet deletion.
```bash
aws ec2 describe-instances --region "$AWS_REGION" --no-cli-pager | jq -L . -c -r 'include "tag_helpers"; .Reservations[].Instances[]? | (must_tag_name(.) as $NameTag | tag_value(.;"Project") as $Proj | select(($Proj=="ManageEIPs") or ($NameTag|contains("ManageEIPs"))) | "\(.InstanceId)\t\($NameTag)")' | while IFS=$'\t' read -r IID INSTANCE_NAME; 
do OUT=$(aws ec2 terminate-instances --instance-ids "$IID" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null); 
RC=$?; 
if [ $RC -eq 0 ]; 
then jq -c -n --arg i "$IID" --arg n "$INSTANCE_NAME" '{InstanceId:$i,InstanceName:$n,Terminated:true}'; 
else jq -c -n --arg i "$IID" --arg n "$INSTANCE_NAME" --arg err "$OUT" '{InstanceId:$i,InstanceName:$n,Terminated:false,Error:$err}'; 
fi; 
done
```
**Expected output**
```json
{"InstanceId":"i-0c69cfce5294ffdd4", "InstanceName": "ManageEIPs-ec2", "Terminated":true}
```

##### 1.3.3.3 Delete VPC endpoints (if any)
**Why**
Endpoints can block subnet deletion.
```bash
aws ec2 describe-vpc-endpoints --region "$AWS_REGION" --no-cli-pager | jq -L . -r 'include "tag_helpers"; 
.VpcEndpoints[]? | select((tag_value(.;"Project")=="ManageEIPs") or (must_tag_name(.)|contains("ManageEIPs"))) | .VpcEndpointId' | while read -r VPCE; do OUT=$(aws ec2 delete-vpc-endpoints --vpc-endpoint-ids "$VPCE" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null); 
RC=$?; 
if [ $RC -eq 0 ]; then jq -c -n --arg v "$VPCE" '{VpcEndpointId:$v,Deleted:true}'; 
else jq -c -n --arg v "$VPCE" --arg err "$OUT" '{VpcEndpointId:$v,Deleted:false,Error:$err}'; 
fi; 
done
```
**Expected output**
```json
{"VpcEndpointId":"vpce-04a9cae3727e1e9b9","Deleted":true}
{"VpcEndpointId":"vpce-049d68eff853c959f","Deleted":true}
{"VpcEndpointId":"vpce-08eac81e86e6b75de","Deleted":true}
```

##### 1.3.3.4 Delete NAT Gateways (if any)
**Why**
NAT Gateways create ENIs and block subnets; deletion is asynchronous.
```bash
(aws ec2 describe-nat-gateways --region "$AWS_REGION" --no-cli-pager; aws ec2 describe-subnets --region "$AWS_REGION" --no-cli-pager; aws ec2 describe-vpcs --region "$AWS_REGION" --no-cli-pager) | jq -L . -c -r -s 'include "tag_helpers"; (.[0].NatGateways//[]) as $NG | (.[1].Subnets//[]) as $S | (.[2].Vpcs//[]) as $V | $NG[]? | (must_tag_name(.) as $NameTag | (tag_value(.;"Project")//"") as $Proj | select(($Proj=="ManageEIPs") or ($NameTag|contains("ManageEIPs"))) | (.VpcId as $Vid | .SubnetId as $Sid | (($V|map(select(.VpcId==$Vid))|.[0])//{}) as $Vpc | (($S|map(select(.SubnetId==$Sid))|.[0])//{}) as $Sub | "\(.NatGatewayId)\t\($Vid)\t\(must_tag_name($Vpc))\t\($Sid)\t\(must_tag_name($Sub))\t\(.State)\t\($NameTag)"))' | while IFS=$'\t' read -r NGW_ID VPC_ID VPC_NAME SUBNET_ID SUBNET_NAME STATE NAME_TAG; 
do OUT=$(aws ec2 delete-nat-gateway --nat-gateway-id "$NGW_ID" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null); 
RC=$?; 
if [ $RC -eq 0 ]; 
then jq -c -n --arg id "$NGW_ID" --arg v "$VPC_ID" --arg vn "$VPC_NAME" --arg s "$SUBNET_ID" --arg sn "$SUBNET_NAME" --arg st "$STATE" --arg nt "$NAME_TAG" '{NatGatewayId:$id,VpcId:$v,VpcName:$vn,SubnetId:$s,SubnetName:$sn,State:$st,NameTag:$nt,DeleteRequested:true}'; 
else jq -c -n --arg id "$NGW_ID" --arg v "$VPC_ID" --arg vn "$VPC_NAME" --arg s "$SUBNET_ID" --arg sn "$SUBNET_NAME" --arg st "$STATE" --arg nt "$NAME_TAG" --arg err "$OUT" '{NatGatewayId:$id,VpcId:$v,VpcName:$vn,SubnetId:$s,SubnetName:$sn,State:$st,NameTag:$nt,DeleteRequested:false,Error:$err}'; 
fi; 
done
```
No output.

##### 1.3.3.5 Delete Load Balancers (if any) named ManageEIPs*
**Why**
Load balancers attach to subnets and can block deletion.
```bash
aws elbv2 describe-load-balancers --region "$AWS_REGION" --no-cli-pager | jq -r '.LoadBalancers[]? | select((.LoadBalancerName|tostring|contains("ManageEIPs"))) | .LoadBalancerArn' | while read -r LBA; 
do OUT=$(aws elbv2 delete-load-balancer --load-balancer-arn "$LBA" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null); 
RC=$?; 
if [ $RC -eq 0 ]; 
then jq -c -n --arg a "$LBA" '{LoadBalancerArn:$a,Deleted:true}'; 
else jq -c -n --arg a "$LBA" --arg err "$OUT" '{LoadBalancerArn:$a,Deleted:false,Error:$err}'; 
fi; 
done
```
No output.

##### 1.3.3.6 Delete available ENIs (if any) in ManageEIPs scope
**Note**
Only ENIs in available state can be deleted directly.
```bash
aws ec2 describe-network-interfaces --region "$AWS_REGION" --no-cli-pager | jq -L . -r 'include "tag_helpers"; .NetworkInterfaces[]? | select((.Status//"")=="available") | select((tag_value(.;"Project")=="ManageEIPs") or (must_tag_name(.)|contains("ManageEIPs"))) | .NetworkInterfaceId' | while read -r ENI; 
do OUT=$(aws ec2 delete-network-interface --network-interface-id "$ENI" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null);
RC=$?; 
if [ $RC -eq 0 ]; then jq -c -n --arg e "$ENI" '{NetworkInterfaceId:$e,Deleted:true}'; 
else jq -c -n --arg e "$ENI" --arg err "$OUT" '{NetworkInterfaceId:$e,Deleted:false,Error:$err}'; 
fi; 
done
```
No output.

##### 1.3.3.7 Delete non-default security groups in ManageEIPs scope
**Note**
A security group must not be referenced by any ENI to be deleted.
```bash
aws ec2 describe-security-groups --region "$AWS_REGION" --no-cli-pager | jq -L . -c -r 'include "tag_helpers"; .SecurityGroups[]? | select(.GroupName!="default") | (must_tag_name(.) as $NameTag | .GroupName as $GroupName | .GroupId as $GroupId | tag_value(.;"Project") as $Proj | select(($Proj=="ManageEIPs") or ($NameTag|contains("ManageEIPs"))) | "\($GroupId)\t\($GroupName)\t\($NameTag)")' | while IFS=$'\t' read -r SG_ID GROUP_NAME NAME_TAG; 
do USED=$(aws ec2 describe-network-interfaces --filters "Name=group-id,Values=$SG_ID" --region "$AWS_REGION" --no-cli-pager | jq -r '.NetworkInterfaces|length'); 
if [ "$USED" -eq 0 ]; 
then OUT=$(aws ec2 delete-security-group --group-id "$SG_ID" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null); 
RC=$?; 
if [ $RC -eq 0 ]; 
then jq -c -n --arg id "$SG_ID" --arg gn "$GROUP_NAME" --arg nt "$NAME_TAG" '{GroupId:$id,GroupName:$gn,NameTag:$nt,Deleted:true}'; 
else jq -c -n --arg id "$SG_ID" --arg gn "$GROUP_NAME" --arg nt "$NAME_TAG" --arg err "$OUT" '{GroupId:$id,GroupName:$gn,NameTag:$nt,Deleted:false,Error:$err}'; 
fi; 
else jq -c -n --arg id "$SG_ID" --arg gn "$GROUP_NAME" --arg nt "$NAME_TAG" --argjson used "$USED" '{GroupId:$id,GroupName:$gn,NameTag:$nt,Deleted:false,Reason:"InUseByENIs",EniCount:$used}'; 
fi; 
done
```
**Expected output**
```json
{"GroupId":"sg-02864ee1dea47d873","GroupName":"ManageEIPs-SG-App","NameTag":"SG_ManageEIPs_App","Deleted":true}
{"GroupId":"sg-0370469d180f27a70","GroupName":"ManageEIPs-SG-Endpoints","NameTag":"SG_ManageEIPs_Endpoints","Deleted":true}
```

##### 1.3.3.8 Delete subnets in ManageEIPs scope
**Note**
Subnets can be removed once dependencies are gone.
```bash
aws ec2 describe-subnets --region "$AWS_REGION" --no-cli-pager | jq -L . -c -r 'include "tag_helpers"; .Subnets[]? | (must_tag_name(.) as $NameTag | tag_value(.;"Project") as $Proj | select(($Proj=="ManageEIPs") or ($NameTag|contains("ManageEIPs")) or ($NameTag|contains("Subnet_ManageEIPs"))) | "\(.SubnetId)\t\($NameTag)")' | while IFS=$'\t' read -r SUBNET_ID SUBNET_NAME; 
do OUT=$(aws ec2 delete-subnet --subnet-id "$SUBNET_ID" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null); 
RC=$?; 
if [ $RC -eq 0 ]; then jq -c -n --arg s "$SUBNET_ID" --arg sn "$SUBNET_NAME" '{SubnetId:$s,SubnetName:$sn,Deleted:true}'; 
else jq -c -n --arg s "$SUBNET_ID" --arg sn "$SUBNET_NAME" --arg err "$OUT" '{SubnetId:$s,SubnetName:$sn,Deleted:false,Error:$err}'; 
fi; 
done
```
**Expected output**
```json
{"SubnetId":"subnet-03949955e141deebb","SubnetName":"Subnet_ManageEIPs_AZ1","Deleted":true}
```

##### 1.3.3.9 Delete non-main route tables in ManageEIPs scope
**Why**
Non-main route tables must be disassociated before deletion.

**Disassociate non-main `route table associations` (include `RouteTableName`)**
```bash
aws ec2 describe-route-tables --region "$AWS_REGION" --no-cli-pager | jq -L . -c -r 'include "tag_helpers"; .RouteTables[]? | (must_tag_name(.) as $RTName | (tag_value(.;"Project")//"") as $Proj | select(($Proj=="ManageEIPs") or ($RTName|contains("ManageEIPs"))) | (.RouteTableId as $RTB | (.Associations//[])[]? | select((.Main//false)==false) | "\(.RouteTableAssociationId)\t\($RTB)\t\($RTName)"))' | while IFS=$'\t' read -r ASSOC RTB_ID RTB_NAME; 
do OUT=$(aws ec2 disassociate-route-table --association-id "$ASSOC" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null);
RC=$?; 
if [ $RC -eq 0 ]; then jq -c -n --arg a "$ASSOC" --arg r "$RTB_ID" --arg rn "$RTB_NAME" '{RouteTableAssociationId:$a,RouteTableId:$r,RouteTableName:$rn,Disassociated:true}'; 
else jq -c -n --arg a "$ASSOC" --arg r "$RTB_ID" --arg rn "$RTB_NAME" --arg err "$OUT" '{RouteTableAssociationId:$a,RouteTableId:$r,RouteTableName:$rn,Disassociated:false,Error:$err}'; 
fi; 
done
```

**Delete non-main `route tables` (include `RouteTableName`)**
```bash
aws ec2 describe-route-tables --region "$AWS_REGION" --no-cli-pager | jq -L . -c -r 'include "tag_helpers"; .RouteTables[]? | (must_tag_name(.) as $RTName | (tag_value(.;"Project")//"") as $Proj | select(($Proj=="ManageEIPs") or ($RTName|contains("ManageEIPs"))) | select(((.Associations//[])|any(.Main==true))|not) | "\(.RouteTableId)\t\($RTName)")' | while IFS=$'\t' read -r RTB_ID RTB_NAME; 
do OUT=$(aws ec2 delete-route-table --route-table-id "$RTB_ID" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null); 
RC=$?; 
if [ $RC -eq 0 ]; then jq -c -n --arg r "$RTB_ID" --arg rn "$RTB_NAME" '{RouteTableId:$r,RouteTableName:$rn,Deleted:true}'; 
else jq -c -n --arg r "$RTB_ID" --arg rn "$RTB_NAME" --arg err "$OUT" '{RouteTableId:$r,RouteTableName:$rn,Deleted:false,Error:$err}'; 
fi; 
done
```
**Expected output**
```json
{"RouteTableId":"rtb-0435ffce50cebdffb","RouteTableName":"RTB_ManageEIPs","Deleted":true}
```

##### 1.3.3.10 Detach and delete Internet Gateway in ManageEIPs scope
**Purpose**
Detach each in-scope Internet Gateway from its VPC (if attached), then delete it.
```bash
(aws ec2 describe-internet-gateways --region "$AWS_REGION" --no-cli-pager; aws ec2 describe-vpcs --region "$AWS_REGION" --no-cli-pager) | jq -L . -c -r -s 'include "tag_helpers"; (.[0].InternetGateways//[]) as $IGW | (.[1].Vpcs//[]) as $V | $IGW[]? | (must_tag_name(.) as $IgwName | (tag_value(.;"Project")//"") as $Proj | select(($Proj=="ManageEIPs") or ($IgwName|contains("ManageEIPs"))) | (.InternetGatewayId as $I | (((.Attachments//[])[]? | .VpcId) // "") as $Vid | (($V|map(select(.VpcId==$Vid))|.[0])//{}) as $Vpc | "\($I)\t\($IgwName)\t\($Vid)\t\(must_tag_name($Vpc))"))' | while IFS=$'\t' read -r IGW_ID IGW_NAME VPC_ID VPC_NAME; 
do [ -n "$VPC_ID" ] && aws ec2 detach-internet-gateway --internet-gateway-id "$IGW_ID" --vpc-id "$VPC_ID" --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1; 
OUT=$(aws ec2 delete-internet-gateway --internet-gateway-id "$IGW_ID" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null); 
RC=$?; 
if [ $RC -eq 0 ]; 
then jq -c -n --arg g "$IGW_ID" --arg gn "$IGW_NAME" --arg v "$VPC_ID" --arg vn "$VPC_NAME" '{InternetGatewayId:$g,InternetGatewayName:$gn,VpcId:($v|select(length>0)//"null"),VpcName:($vn|select(length>0)//"null"),Deleted:true}'; 
else jq -c -n --arg g "$IGW_ID" --arg gn "$IGW_NAME" --arg v "$VPC_ID" --arg vn "$VPC_NAME" --arg err "$OUT" '{InternetGatewayId:$g,InternetGatewayName:$gn,VpcId:($v|select(length>0)//"null"),VpcName:($vn|select(length>0)//"null"),Deleted:false,Error:$err}'; 
fi; 
done
```
**Expected output**
```json
{"InternetGatewayId":"igw-0b3e1619bb8b88318","InternetGatewayName":"IGW_ManageEIPs","VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs","Deleted":true}
```

##### 1.3.3.11 Delete ManageEIPs VPC(s)
**Purpose**
Delete each non-default VPC in-scope.

```bash
aws ec2 describe-vpcs --region "$AWS_REGION" --no-cli-pager | jq -L . -c -r 'include "tag_helpers"; .Vpcs[]? | select(.IsDefault|not) | (must_tag_name(.) as $VpcName | (tag_value(.;"Project")//"") as $Proj | select(($Proj=="ManageEIPs") or ($VpcName|contains("ManageEIPs")) or ($VpcName|contains("VPC_ManageEIPs"))) | "\(.VpcId)\t\($VpcName)")' | while IFS=$'\t' read -r VPC_ID VPC_NAME; 
do OUT=$(aws ec2 delete-vpc --vpc-id "$VPC_ID" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null); 
RC=$?; 
if [ $RC -eq 0 ]; 
then jq -c -n --arg v "$VPC_ID" --arg vn "$VPC_NAME" '{VpcId:$v,VpcName:$vn,Deleted:true}'; 
else jq -c -n --arg v "$VPC_ID" --arg vn "$VPC_NAME" --arg err "$OUT" '{VpcId:$v,VpcName:$vn,Deleted:false,Error:$err}'; 
fi; 
done
```
**Expected output**
```json
{"VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs","Deleted":true}
```


### 1.4 Build and validate (local)
#### 1.4.1 Validate template (run after template changes)
**Purpose**
Validate `template.yaml` locally.
Re-run when `template.yaml` changes.

**Show required variables (JSONL, no blanks)**
```bash
jq -c -n --arg AWS_PROFILE "${AWS_PROFILE:-}" --arg AWS_REGION "${AWS_REGION:-}" --arg SAM_STACK_NAME "${SAM_STACK_NAME:-}" '{AWS_PROFILE:$AWS_PROFILE,AWS_REGION:$AWS_REGION,SAM_STACK_NAME:$SAM_STACK_NAME}'
```
**Example output**
```json
{"AWS_PROFILE":"default","AWS_REGION":"us-east-1","SAM_STACK_NAME":"ManageEIPs-1region-SAM"}
```

**Validate the SAM template**
```bash
sam validate
```
Template is valid.

#### 1.4.2 Build (run after code/template changes)
**Purpose**
Validate the `SAM` application locally.
Re-run when Lambda code or `template.yaml` changes.

##### 1.4.2.1 Pre-build packaging sanity check (source vs `function.zip`)
**Purpose**
Confirm whether we are looking at source code or only a packaged artifact (`function.zip`).
If `lambda_function.py` is not in our working directory, it may be inside `function.zip`, named differently, or only produced under `.aws-sam/build/` after `sam build`.

**Show what exists in our working directory (a few files, one of them being `function.zip`)**
```bash
ls -la
```

**Check whether `lambda_function.py` exists inside `function.zip`**
```bash
unzip -l function.zip | grep -F 'lambda_function.py' || echo 'NOT_FOUND_IN_ZIP'
```
**Expected output**
```text
8973  2025-12-14 21:21   lambda_function.py
```
**Conclusion**
`lambda_function.py` is present inside function.zip (size 8973 bytes), so the source file exists but is packaged.

**List Python files inside `function.zip` (identify the actual module name)**
```bash
unzip -l function.zip | awk '{print $4}' | grep -E '\.py$' || echo 'NO_PY_FILES_IN_ZIP'
```
**Expected output**
```text
lambda_function.py
```
**Conclusion**
The zip contains at least one Python module, and the only `.py` surfaced by this filter is `lambda_function.py`.

**Extract `function.zip` for inspection**
```bash
rm -rf ./function && mkdir -p ./function && unzip -o function.zip -d ./function
```
**Example output**
```text
Archive:  function.zip
  inflating: ./function/lambda_function.py  
```
**Conclusion**
Extraction confirms the module is physically present and can be materialized as a normal file at `./function/lambda_function.py`.

**Locate the handler function in extracted code**
```bash
grep -RIn --include='*.py' -E 'def[[:space:]]+lambda_handler' ./function || echo 'NO_lambda_handler_FOUND'
```
**Example output**
```text
./function/lambda_function.py:76:def lambda_handler(event, context):
```
**Conclusion**
The handler function is defined in `lambda_function.py` at line 76, and its function name is `lambda_handler`.

**Verify `template.yaml` Handler (must match `<module>.<function>`)**
```bash
grep -nE '^[[:space:]]*Handler:' template.yaml || echo 'NO_Handler_IN_template.yaml'
```
**Example output**
```text
106:      Handler: lambda_function.lambda_handler
```
**Conclusion**
Our `SAM` handler is configured as `lambda_function.lambda_handler`, which means: module `lambda_function` (file `lambda_function.py`) and function `lambda_handler`.

##### 1.4.2.2 Run the build
```bash
sam build
```

#### 1.4.3 Build `artifacts` location (`SAM` output)
**Purpose**
Confirm where `sam build` wrote the generated `artifacts` (`.aws-sam/build`) and list them for inspection.

```bash
ls -la .aws-sam/
```
**Example output**
```text
total 0
drwxrwxrwx 1 mfh mfh 512 Dec 29 02:33 .
drwxrwxrwx 1 mfh mfh 512 Dec 28 21:37 ..
drwxrwxrwx 1 mfh mfh 512 Dec 29 02:33 build
-rwxrwxrwx 1 mfh mfh 112 Dec 29 02:33 build.toml
```

```bash
find .aws-sam/build -maxdepth 3 -type f -print
```
**Example output**
```text
.aws-sam/build/ManageEIPsFunction/.vs/slnx.sqlite
.aws-sam/build/ManageEIPsFunction/.vs/VSWorkspaceState.json
.aws-sam/build/ManageEIPsFunction/event.json
.aws-sam/build/ManageEIPsFunction/flatten_tags.jq
.aws-sam/build/ManageEIPsFunction/function.zip
.aws-sam/build/ManageEIPsFunction/lambda_function.py
.aws-sam/build/ManageEIPsFunction/manage-eips-policy.json
.aws-sam/build/ManageEIPsFunction/manage-eips-trust.json
.aws-sam/build/ManageEIPsFunction/ManageEIPs_Automation-SAM.md
.aws-sam/build/ManageEIPsFunction/README.md
.aws-sam/build/ManageEIPsFunction/response.1766551137.json
.aws-sam/build/ManageEIPsFunction/response.json
.aws-sam/build/ManageEIPsFunction/rules_names.jq
.aws-sam/build/ManageEIPsFunction/tag_helpers.jq
.aws-sam/build/ManageEIPsFunction/template.yaml
.aws-sam/build/template.yaml
```


### 1.5 Deploy (create/update `stack`)
#### 1.5.1 Reset failed `stack state` (only if `StackStatus` is `REVIEW_IN_PROGRESS`)
**Purpose**
- A failed SAM changeset can leave the stack in `REVIEW_IN_PROGRESS`.
- We must delete that stack before re-deploying.

**Check current `stack status` (JSONL)**
```bash
aws cloudformation describe-stacks --stack-name "$SAM_STACK_NAME" --region "$AWS_REGION" --no-cli-pager 2>/dev/null | jq -c '{StackName:.Stacks[0].StackName,StackStatus:.Stacks[0].StackStatus,Region:"'"$AWS_REGION"'"}' || jq -c -n '{StackName:"'"$SAM_STACK_NAME"'",Region:"'"$AWS_REGION"'",Exists:false}'
```

**Delete the `stack` (if it exists)**
```bash
aws cloudformation delete-stack --stack-name "$SAM_STACK_NAME" --region "$AWS_REGION" --no-cli-pager
```

**Verify `stack` is gone (JSONL)**
```bash
aws cloudformation describe-stacks --stack-name "$SAM_STACK_NAME" --region "$AWS_REGION" --no-cli-pager 2>/dev/null | jq -c '{StackName:.Stacks[0].StackName,StackStatus:.Stacks[0].StackStatus,Region:"'"$AWS_REGION"'",Exists:true}' || jq -c -n '{StackName:"'"$SAM_STACK_NAME"'",Region:"'"$AWS_REGION"'",Exists:false}'
```

#### 1.5.2 Prepare `SAM artifacts` `bucket` (stable name, jq -c JSONL)
**Purpose**
- Set a deterministic `S3 bucket` name for SAM packaging artifacts (no auto-generated `bucket`).
- Verify the `bucket` exists (create it if missing).

##### 1.5.2.1 Get AccountId (with jq -c output) and set `SAM_ARTIFACTS_BUCKET`
```bash
ID_JSON=$(aws sts get-caller-identity --no-cli-pager | jq -c '{Account:.Account,Arn:.Arn,UserId:.UserId}'); echo "$ID_JSON";
AWS_ACCOUNT_ID=$(echo "$ID_JSON" | jq -r '.Account'); 
export ACCOUNT_ID="$AWS_ACCOUNT_ID"; 
export SAM_ARTIFACTS_BUCKET="manageeips-sam-artifacts-${AWS_ACCOUNT_ID}-${AWS_REGION}"; jq -c -n --arg Bucket "$SAM_ARTIFACTS_BUCKET" --arg Region "$AWS_REGION" '{Action:"SetArtifactsBucket",Bucket:$Bucket,Region:$Region}'
```
**Example output**
```json
{"Action":"SetArtifactsBucket","Bucket":"manageeips-sam-artifacts-180294215772-us-east-1","Region":"us-east-1"}
```

##### 1.5.2.2 Verify `bucket` exists (or create it) (jq -c status output)
```bash
aws s3api head-bucket --bucket "$SAM_ARTIFACTS_BUCKET" --no-cli-pager 2>/dev/null && jq -c -n --arg Bucket "$SAM_ARTIFACTS_BUCKET" '{Action:"HeadBucket",Bucket:$Bucket,Exists:true}' || aws s3api create-bucket --bucket "$SAM_ARTIFACTS_BUCKET" --region "$AWS_REGION" --no-cli-pager | jq -c --arg Bucket "$SAM_ARTIFACTS_BUCKET" --arg Region "$AWS_REGION" '{Action:"CreateBucket",Bucket:$Bucket,Region:$Region,Created:true,Location:(.Location//null)}'
```
**Example output**
```json
{
    "BucketArn": "arn:aws:s3:::manageeips-sam-artifacts-180294215772-us-east-1",
    "BucketRegion": "us-east-1",
    "AccessPointAlias": false
}
{"Action":"HeadBucket","Bucket":"manageeips-sam-artifacts-180294215772-us-east-1","Exists":true}
```

#### 1.5.3 Deploy (`sam deploy`)
**Purpose**
Create or update the `CloudFormation stack` defined by `template.yaml` (`SAM` packages to the stable `artifacts` `bucket`).

```bash
sam deploy --guided --stack-name "$SAM_STACK_NAME" --region "$AWS_REGION" --capabilities CAPABILITY_NAMED_IAM --s3-bucket "$SAM_ARTIFACTS_BUCKET"
```
**Conclusion**
Successfully created/updated `stack` - `ManageEIPs-1region-SAM` in us-east-1

#### 1.5.4 Capture `stack status` + outputs (single AWS call, JSONL)
**Purpose**
- Emit 1 JSON object per line: 1 `RecordType=Stack`.
- Then 0+ `RecordType=Output`.

```bash
aws cloudformation describe-stacks --stack-name "$SAM_STACK_NAME" --region "$AWS_REGION" --no-cli-pager | jq -c --arg region "$AWS_REGION" '.Stacks[0] as $S | {RecordType:"Stack",StackName:$S.StackName,StackStatus:$S.StackStatus,Region:$region,CreationTime:$S.CreationTime,LastUpdatedTime:($S.LastUpdatedTime//null)}, ($S.Outputs[]? | {RecordType:"Output",StackName:$S.StackName,OutputKey:.OutputKey,OutputValue:.OutputValue,Description:(.Description//"")})'
```
**Example output**
```json
{"RecordType":"Stack","StackName":"ManageEIPs-1region-SAM","StackStatus":"UPDATE_COMPLETE","Region":"us-east-1","CreationTime":"2025-12-29T02:52:05.450000+00:00","LastUpdatedTime":"2025-12-29T03:49:38.240000+00:00"}
{"RecordType":"Output","StackName":"ManageEIPs-1region-SAM","OutputKey":"DashboardName","OutputValue":"ManageEIPs-1region-SAM-Dashboard","Description":""}
{"RecordType":"Output","StackName":"ManageEIPs-1region-SAM","OutputKey":"ScheduleRuleName","OutputValue":"ManageEIPs-1region-SAM-ScheduleMonthly","Description":""}
{"RecordType":"Output","StackName":"ManageEIPs-1region-SAM","OutputKey":"LambdaRoleName","OutputValue":"ManageEIPs-1region-SAM-LambdaRole","Description":""}
{"RecordType":"Output","StackName":"ManageEIPs-1region-SAM","OutputKey":"LambdaFunctionName","OutputValue":"ManageEIPs-1region-SAM-Lambda","Description":""}
{"RecordType":"Output","StackName":"ManageEIPs-1region-SAM","OutputKey":"AlarmsTopicArn","OutputValue":"arn:aws:sns:us-east-1:180294215772:ManageEIPs-1region-SAM-Alarms","Description":""}
```


### 1.6 Post-deploy verification (CLI + JSONL discipline)
#### 1.6.1 Verify `Lambda` function exists and is tagged
##### 1.6.1.1 Verify `Lambda` function exists (configuration summary, JSONL)
**Purpose**
Confirm the stable function name exists and print key properties (no tags yet).

```bash
LAMBDA_NAME="${SAM_STACK_NAME}-Lambda"; 
aws lambda get-function --function-name "$LAMBDA_NAME" --region "$AWS_REGION" --no-cli-pager | jq -c '{ResourceType:"LambdaFunction",FunctionName:.Configuration.FunctionName,FunctionArn:.Configuration.FunctionArn,Runtime:.Configuration.Runtime,RoleArn:.Configuration.Role,LastModified:.Configuration.LastModified}'
```
**Example output**
```json
{"ResourceType":"LambdaFunction","FunctionName":"ManageEIPs-1region-SAM-Lambda","FunctionArn":"arn:aws:lambda:us-east-1:180294215772:function:ManageEIPs-1region-SAM-Lambda","Runtime":"python3.12","RoleArn":"arn:aws:iam::180294215772:role/ManageEIPs-1region-SAM-LambdaRole","LastModified":"2025-12-29T03:49:59.000+0000"}
```

##### 1.6.1.2 List function tags (1 JSON object per tag)
```bash
aws lambda list-tags --resource "arn:aws:lambda:${AWS_REGION}:${ACCOUNT_ID}:function:${LAMBDA_NAME}" --region "$AWS_REGION" --no-cli-pager | jq -c '(.Tags//{}) | to_entries[]? | {ResourceType:"LambdaFunctionTag",FunctionName:"'"$LAMBDA_NAME"'",TagKey:.key,TagValue:.value}'
```
**Example output**
```json
{"ResourceType":"LambdaFunctionTag","FunctionName":"ManageEIPs-1region-SAM-Lambda","TagKey":"Component","TagValue":"Lambda"}
{"ResourceType":"LambdaFunctionTag","FunctionName":"ManageEIPs-1region-SAM-Lambda","TagKey":"CostCenter","TagValue":"Portfolio"}
{"ResourceType":"LambdaFunctionTag","FunctionName":"ManageEIPs-1region-SAM-Lambda","TagKey":"Environment","TagValue":"dev"}
{"ResourceType":"LambdaFunctionTag","FunctionName":"ManageEIPs-1region-SAM-Lambda","TagKey":"ManagedBy","TagValue":"SAM"}
{"ResourceType":"LambdaFunctionTag","FunctionName":"ManageEIPs-1region-SAM-Lambda","TagKey":"Name","TagValue":"Lambda_ManageEIPs"}
{"ResourceType":"LambdaFunctionTag","FunctionName":"ManageEIPs-1region-SAM-Lambda","TagKey":"Owner","TagValue":"Malik"}
{"ResourceType":"LambdaFunctionTag","FunctionName":"ManageEIPs-1region-SAM-Lambda","TagKey":"Project","TagValue":"ManageEIPs"}
{"ResourceType":"LambdaFunctionTag","FunctionName":"ManageEIPs-1region-SAM-Lambda","TagKey":"aws:cloudformation:logical-id","TagValue":"ManageEIPsFunction"}
{"ResourceType":"LambdaFunctionTag","FunctionName":"ManageEIPs-1region-SAM-Lambda","TagKey":"aws:cloudformation:stack-id","TagValue":"arn:aws:cloudformation:us-east-1:180294215772:stack/ManageEIPs-1region-SAM/5be17bf0-e461-11f0-b029-0e364670ebf5"}
{"ResourceType":"LambdaFunctionTag","FunctionName":"ManageEIPs-1region-SAM-Lambda","TagKey":"aws:cloudformation:stack-name","TagValue":"ManageEIPs-1region-SAM"}
{"ResourceType":"LambdaFunctionTag","FunctionName":"ManageEIPs-1region-SAM-Lambda","TagKey":"lambda:createdBy","TagValue":"SAM"}
```

##### 1.6.1.3 Verify `Lambda alias` exists (live) (JSONL)
**Purpose**
Prove the `live` alias exists for the deployed Lambda function (no Console).

```bash
FN=$(aws cloudformation describe-stacks --stack-name "$SAM_STACK_NAME" --region "$AWS_REGION" --no-cli-pager | jq -r '.Stacks[0].Outputs[]? | select(.OutputKey=="LambdaFunctionName") | .OutputValue'); aws lambda get-alias --function-name "$FN" --name live --region "$AWS_REGION" --no-cli-pager 2>/dev/null | jq -c --arg fn "$FN" '{RecordType:"LambdaAlias",FunctionName:$fn,AliasName:.Name,FunctionVersion:.FunctionVersion,AliasArn:.AliasArn,Exists:true}' || jq -c -n --arg fn "$FN" --arg r "$AWS_REGION" '{RecordType:"LambdaAlias",FunctionName:$fn,AliasName:"live",Region:$r,Exists:false}'
```
**Example output**
```json
{"RecordType":"LambdaAlias","FunctionName":"ManageEIPs-1region-SAM-Lambda","AliasName":"live","FunctionVersion":"1","AliasArn":"arn:aws:lambda:us-east-1:180294215772:function:ManageEIPs-1region-SAM-Lambda:live","Exists":true}
```

#### 1.6.2 Verify `IAM role/policies` attached
**Purpose**
- With `RoleName: ${StackName}-LambdaRole`, the role name is stable.
- Show managed policies and inline policies separately (both matter).

##### 1.6.2.1 List `managed policies` attached to the `role` (JSONL)
```bash
ROLE_NAME="${SAM_STACK_NAME}-LambdaRole"; 
aws iam list-attached-role-policies --role-name "$ROLE_NAME" --no-cli-pager | jq -c --arg rn "$ROLE_NAME" '.AttachedPolicies[]? | {RoleName:$rn,PolicyName:.PolicyName,PolicyArn:.PolicyArn}'
```
**Example output**
```json
{"RoleName":"ManageEIPs-1region-SAM-LambdaRole","PolicyName":"AWSLambdaBasicExecutionRole","PolicyArn":"arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"}
```

##### 1.6.2.2 List `inline policy` names on the `role` (JSONL)
```bash
ROLE_NAME="${SAM_STACK_NAME}-LambdaRole"; 
aws iam list-role-policies --role-name "$ROLE_NAME" --no-cli-pager | jq -c --arg rn "$ROLE_NAME" '.PolicyNames[]? | {RoleName:$rn,InlinePolicyName:.}'
```
**Example output**
```json
{"RoleName":"ManageEIPs-1region-SAM-LambdaRole","InlinePolicyName":"ManageEIPs-1region-SAM-ManageEIPsPolicy"}
```

#### 1.6.3 Verify `EventBridge rule/permission` (if included)
**Purpose**
- If you add an `EventBridge schedule` in the template, give it a stable Name too (example: `${StackName}-ScheduleMonthly`).
- Verify the `rule` exists, then verify Lambda’s resource policy allows `events.amazonaws.com`.
- Confirm the `rule` is actually wired to a `target` (the `Lambda`).

##### 1.6.3.1 Describe the `rule` (JSONL)
```bash
RULE_NAME="${SAM_STACK_NAME}-ScheduleMonthly"; 
aws events describe-rule --name "$RULE_NAME" --region "$AWS_REGION" --no-cli-pager | jq -c '{ResourceType:"EventBridgeRule",RuleName:.Name,RuleArn:.Arn,State:.State,ScheduleExpression:(.ScheduleExpression//null)}'
```
**Example output**
```json
{"ResourceType":"EventBridgeRule","RuleName":"ManageEIPs-1region-SAM-ScheduleMonthly","RuleArn":"arn:aws:events:us-east-1:180294215772:rule/ManageEIPs-1region-SAM-ScheduleMonthly","State":"ENABLED","ScheduleExpression":"cron(0 21 30 * ? *)"}
```

##### 1.6.3.2 Show `Lambda resource-policy` statements (JSONL)
**Purpose**
Emit 1 JSON object per Lambda policy statement, so we can confirm `events.amazonaws.com` is allowed (the rule ARN constraint is present if we used one).

```bash
LAMBDA_NAME="${SAM_STACK_NAME}-Lambda"; 
aws lambda get-policy --function-name "$LAMBDA_NAME" --region "$AWS_REGION" --no-cli-pager | jq -c '(.Policy | fromjson | .Statement[]? | {ResourceType:"LambdaPermission",Sid:.Sid,Effect:.Effect,Action:.Action,Principal:.Principal,Condition:(.Condition//null)})'
```
**Example output**
```json
{"ResourceType":"LambdaPermission","Sid":"ManageEIPs-1region-SAM-ManageEIPsAllowEventBridgeInvoke-tJDI5NgeCegw","Effect":"Allow","Action":"lambda:InvokeFunction","Principal":{"Service":"events.amazonaws.com"},"Condition":{"ArnLike":{"AWS:SourceArn":"arn:aws:events:us-east-1:180294215772:rule/ManageEIPs-1region-SAM-ScheduleMonthly"}}}
```

##### 1.6.3.3 List `rule targets` (JSONL)
**Purpose**
Confirm the rule is actually wired to a `target` (the `Lambda`). If this emits 0 lines, the rule has no `targets`.

**List rule `targets` (uses CloudFormation Output → avoids empty `RULE_NAME`; JSONL)**
```bash
RULE_NAME=$(aws cloudformation describe-stacks --stack-name "$SAM_STACK_NAME" --region "$AWS_REGION" --no-cli-pager | jq -r '.Stacks[0].Outputs[]? | select(.OutputKey=="ScheduleRuleName") | .OutputValue' | head -n 1); aws events list-targets-by-rule --rule "$RULE_NAME" --region "$AWS_REGION" --no-cli-pager | jq -c --arg rn "$RULE_NAME" '.Targets[]? | {RuleName:$rn,TargetId:.Id,TargetArn:.Arn,LambdaFunctionName:(try (.Arn|capture("function:(?<fn>[^:]+)").fn) catch "")} | with_entries(select(.value!=""))'
```
**Example output**
```json
{"RuleName":"ManageEIPs-1region-SAM-ScheduleMonthly","TargetId":"LambdaTarget","TargetArn":"arn:aws:lambda:us-east-1:180294215772:function:ManageEIPs-1region-SAM-Lambda","LambdaFunctionName":"ManageEIPs-1region-SAM-Lambda"}
```
**Conclusion**
The `EventBridge rule` `ManageEIPs-1region-SAM-ScheduleMonthly` has exactly one `target` (`TargetId=LambdaTarget`).
It is our `Lambda function` `ManageEIPs-1region-SAM-Lambda` (`ARN` shown), so the schedule is correctly wired to invoke the Lambda.

##### 1.6.3.4 Verify the deployed `schedule expression` matches our intended `cron` (JSONL)
```bash
aws events describe-rule --name "$RULE_NAME" --region "$AWS_REGION" --no-cli-pager | jq -c '{RuleName:.Name,ScheduleExpression:(.ScheduleExpression//""),State:(.State//"")}'
```
**Example output**
```json
{"RuleName":"ManageEIPs-1region-SAM-ScheduleMonthly","ScheduleExpression":"cron(0 21 30 * ? *)","State":"ENABLED"}
```
**Conclusion**
After re-running `sam deploy --guided` (see 1.5.3), the deployed schedule expression now matches our intended `cron`.

#### 1.6.4 Verify `EIP` behavior (create 3 EIPs: 1 attached, 2 unused)
**Purpose**
- Run the CLI harness to generate test conditions (1 associated EIP, 2 unassociated).
- Keep verification output as JSONL (include `NameTag`).

#### 1.6.5 Verify scheduled `invocation` outcome (`CloudWatch Logs` + `EIP state`)
**Purpose**
- Confirm the cron actually invoked the `Lambda`.
- Confirm the expected `EIP` outcome (2 unattached released, 1 attached remains).

##### 1.6.5.1 Capture “before” `EIP state` (JSONL)
```bash
aws ec2 describe-addresses --region "$AWS_REGION" --no-cli-pager --filters "Name=tag:GroupName,Values=NW-EC2-EIP-Test" | jq -c '.Addresses[]? | ((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0]//"MISSING-EIP-NAME") as $NameTag | {AllocationId:(.AllocationId//""),NameTag:$NameTag,PublicIp:(.PublicIp//""),AssociationId:(.AssociationId//""),InstanceId:(.InstanceId//""),NetworkInterfaceId:(.NetworkInterfaceId//"")} | with_entries(select(.value!=""))'
```
**Example output**
```json
{"AllocationId":"eipalloc-0e281a4134e91af72","NameTag":"EIP_Unattached_1_NW-EC2-EIP-Test","PublicIp":"100.52.32.249"}
{"AllocationId":"eipalloc-0b8425b8a82aee46d","NameTag":"EIP_Unattached_2_NW-EC2-EIP-Test","PublicIp":"34.205.35.133"}
{"AllocationId":"eipalloc-0bff9015be09e6951","NameTag":"EIP_Attached_NW-EC2-EIP-Test","PublicIp":"98.91.43.181","AssociationId":"eipassoc-0bf30df6985210344","InstanceId":"i-08a6f769b30f80543","NetworkInterfaceId":"eni-0ae73da1a6fcbf2bd"}
```
**Conclusion**
Record the expected baseline:
3 EIPs total → 1 attached (has `AssociationId` + `InstanceId`) and 2 unattached (no `AssociationId`/`InstanceId`).

##### 1.6.5.2 Verify `Lambda` ran around the `schedule` (latest `log stream`, JSONL)
```bash
LAMBDA_NAME=$(aws cloudformation describe-stacks --stack-name "$SAM_STACK_NAME" --region "$AWS_REGION" --no-cli-pager | jq -r '.Stacks[0].Outputs[]? | select(.OutputKey=="LambdaFunctionName") | .OutputValue' | head -n 1); 
STREAM=$(aws logs describe-log-streams --log-group-name "/aws/lambda/$LAMBDA_NAME" --order-by LastEventTime --descending --limit 1 --region "$AWS_REGION" --no-cli-pager | jq -r '.logStreams[0].logStreamName'); aws logs get-log-events --log-group-name "/aws/lambda/$LAMBDA_NAME" --log-stream-name "$STREAM" --limit 200 --region "$AWS_REGION" --no-cli-pager | jq -c --arg Lambda "$LAMBDA_NAME" --arg Stream "$STREAM" '.events[]? | {Lambda:$Lambda,LogStream:$Stream,TimestampMs:.timestamp,Message:(.message|rtrimstr("\n"))}'
```
**Example output**
```json
{"Lambda":"ManageEIPs-1region-SAM-Lambda","LogStream":"2025/12/30/[$LATEST]41972b372dcc43b08a2594e45e84e3bd","TimestampMs":1767128439691,"Message":"INIT_START Runtime Version: python:3.12.v101\tRuntime Version ARN: arn:aws:lambda:us-east-1::runtime:994aac32248ecf4d69d9f5e9a3a57aba3ccea19d94170a61d5ecf978927e1b0f"}     
{"Lambda":"ManageEIPs-1region-SAM-Lambda","LogStream":"2025/12/30/[$LATEST]41972b372dcc43b08a2594e45e84e3bd","TimestampMs":1767128439972,"Message":"START RequestId: f678488e-719f-42a4-b770-bb99cc43afeb Version: $LATEST"}
{"Lambda":"ManageEIPs-1region-SAM-Lambda","LogStream":"2025/12/30/[$LATEST]41972b372dcc43b08a2594e45e84e3bd","TimestampMs":1767128439972,"Message":"{\"level\": \"INFO\", \"message\": \"ManageEIPs start\", \"dry_run\": false, \"function_name\": \"ManageEIPs-1region-SAM-Lambda\", \"request_id\": \"f678488e-719f-42a4-b770-bb99cc43afeb\", \"managed_tag_key\": \"ManagedBy\", \"managed_tag_value\": \"ManageEIPs\", \"protect_tag_key\": \"Protection\", \"protect_tag_value\": \"DoNotRelease\", \"metrics_enabled\": true, \"metrics_namespace\": \"Custom/FinOps\"}"}
{"Lambda":"ManageEIPs-1region-SAM-Lambda","LogStream":"2025/12/30/[$LATEST]41972b372dcc43b08a2594e45e84e3bd","TimestampMs":1767128444279,"Message":"{\"level\": \"INFO\", \"message\": \"Releasing unassociated Elastic IP\", \"allocation_id\": \"eipalloc-0e281a4134e91af72\", \"action\": \"release\", \"dry_run\": false}"} 
{"Lambda":"ManageEIPs-1region-SAM-Lambda","LogStream":"2025/12/30/[$LATEST]41972b372dcc43b08a2594e45e84e3bd","TimestampMs":1767128444600,"Message":"{\"level\": \"INFO\", \"message\": \"Releasing unassociated Elastic IP\", \"allocation_id\": \"eipalloc-0b8425b8a82aee46d\", \"action\": \"release\", \"dry_run\": false}"} 
{"Lambda":"ManageEIPs-1region-SAM-Lambda","LogStream":"2025/12/30/[$LATEST]41972b372dcc43b08a2594e45e84e3bd","TimestampMs":1767128445094,"Message":"{\"level\": \"INFO\", \"message\": \"ManageEIPs completed\", \"dry_run\": false, \"scanned\": 3, \"skipped_not_managed\": 0, \"skipped_protected\": 0, \"associated\": 1, \"would_release\": 0, \"released\": 2, \"per_eip_errors\": 0}"}
{"Lambda":"ManageEIPs-1region-SAM-Lambda","LogStream":"2025/12/30/[$LATEST]41972b372dcc43b08a2594e45e84e3bd","TimestampMs":1767128445094,"Message":"{\"_aws\": {\"Timestamp\": 1767128445094, \"CloudWatchMetrics\": [{\"Namespace\": \"Custom/FinOps\", \"Dimensions\": [[\"FunctionName\"]], \"Metrics\": [{\"Name\": \"EIPsScanned\", \"Unit\": \"Count\"}, {\"Name\": \"EIPsSkippedNotManaged\", \"Unit\": \"Count\"}, {\"Name\": \"EIPsSkippedProtected\", \"Unit\": \"Count\"}, {\"Name\": \"EIPsAssociated\", \"Unit\": \"Count\"}, {\"Name\": \"EIPsWouldRelease\", \"Unit\": \"Count\"}, {\"Name\": \"EIPsReleased\", \"Unit\": \"Count\"}, {\"Name\": \"EIPsPerEipErrors\", \"Unit\": \"Count\"}]}]}, \"FunctionName\": \"ManageEIPs-1region-SAM-Lambda\", \"EIPsScanned\": 3, \"EIPsSkippedNotManaged\": 0, \"EIPsSkippedProtected\": 0, \"EIPsAssociated\": 1, \"EIPsWouldRelease\": 0, \"EIPsReleased\": 2, \"EIPsPerEipErrors\": 0}"}
{"Lambda":"ManageEIPs-1region-SAM-Lambda","LogStream":"2025/12/30/[$LATEST]41972b372dcc43b08a2594e45e84e3bd","TimestampMs":1767128445107,"Message":"END RequestId: f678488e-719f-42a4-b770-bb99cc43afeb"}
{"Lambda":"ManageEIPs-1region-SAM-Lambda","LogStream":"2025/12/30/[$LATEST]41972b372dcc43b08a2594e45e84e3bd","TimestampMs":1767128445107,"Message":"repositoryRT RequestId: f678488e-719f-42a4-b770-bb99cc43afeb\tDuration: 5134.50 ms\tBilled Duration: 5412 ms\tMemory Size: 128 MB\tMax Memory Used: 97 MB\tInit Duration: 276.94 ms\t\nXRAY TraceId: 1-69543d77-5042a8600c5c4a2665029f8e\tSegmentId: 0d2f9bcad75e6cf5\tSampled: true\t"}
```
**Conclusion (logs)**
Confirm there is an invocation at the scheduled time.
Note any `released EIP` / `skipped attached EIP` messages or errors.


##### 1.6.5.3 Capture “after” `EIP state` (JSONL)
```bash
aws ec2 describe-addresses --region "$AWS_REGION" --no-cli-pager --filters "Name=tag:GroupName,Values=NW-EC2-EIP-Test" | jq -c '.Addresses[]? | ((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0]//"MISSING-EIP-NAME") as $NameTag | {AllocationId:(.AllocationId//""),NameTag:$NameTag,PublicIp:(.PublicIp//""),AssociationId:(.AssociationId//""),InstanceId:(.InstanceId//""),NetworkInterfaceId:(.NetworkInterfaceId//"")} | with_entries(select(.value!=""))'
```
**Example output**
```json
{"AllocationId":"eipalloc-0bff9015be09e6951","NameTag":"EIP_Attached_NW-EC2-EIP-Test","PublicIp":"98.91.43.181","AssociationId":"eipassoc-0bf30df6985210344","InstanceId":"i-08a6f769b30f80543","NetworkInterfaceId":"eni-0ae73da1a6fcbf2bd"}
```
**Conclusion (logs)**
Success criteria: only the `attached EIP` remains.
The 2 `EIP_Unattached_*_NW-EC2-EIP-Test` entries are gone (released).



## 2. `Network & EC2` (for `EIP` test)
This section creates a minimal, reproducible network and a disposable `EC2 instance` used as a safe `target` to validate `Elastic IP` attachment/detachment behaviors (and later, any automation that depends on those behaviors).
The goal is to test `EIP` workflows in isolation, without touching production-like resources, and with tagging that makes test assets easy to identify and clean up.

### 2.1 Purpose and scope
#### 2.1.1 What this section builds
- 1 dedicated test VPC (no approved `VPC` for `EIP` testing yet).
- 1 `public subnet` and 1 `private subnet` (enough to support typical “public entry / private compute” patterns, 
  even if the `instance` ends up in only 1 `subnet` for the test).
- Internet access primitives (`IGW` + `route table`) for the `public subnet`, and `VPC interface endpoints` for `SSM`.
- Security groups sized to the test: least-privilege egress, 
  and only the minimum ingress required for SSM endpoint traffic (if used).
- 1 EC2 test `instance` with `SSM` connectivity, tagged clearly as a test `target` for `EIP association`.
- 1 test `Elastic IP allocation` for attach/detach verification (allocated only when needed; released during cleanup).

#### 2.1.2 What this section does not cover
- No `Lambda/SAM` build or deployment steps (those remain in Section 1 and later sections).
- No production hardening: no autoscaling, no multi-AZ resilience requirements, no `HTTPS/ALB`, and no `RDS`.
- No permanent EIP retention: `EIPs` are treated as disposable test assets unless explicitly stated otherwise.
- No cross-region or DR behavior in this section; it is single-region, test-only baseline infrastructure.
- No manual Console-only steps as the primary path; later subsections should use `AWS CLI` + `jq -c` JSONL outputs.

### 2.2 Region and tagging variables (required)
#### 2.2.1 Export and show required variables (JSONL, no blanks)
**Purpose**
- Set the region/profile once, plus a consistent tagging baseline used by every resource created in Section 2.
- Unless readability requires otherwise, all later CLI outputs will be JSONL (1 object per line), fully flattened, no arrays.

**Export required variables (single command)**
```bash
export AWS_PROFILE="${AWS_PROFILE:-default}" AWS_REGION="${AWS_REGION:-us-east-1}" TAG_PROJECT="${TAG_PROJECT:-ManageEIPs-1region-SAM}" TAG_ENV="${TAG_ENV:-Dev}" TAG_OWNER="${TAG_OWNER:-mfh}" TAG_MANAGEDBY="${TAG_MANAGEDBY:-CLI}" TAG_COSTCENTER="${TAG_COSTCENTER:-Lab}" TAG_COMPONENT="${TAG_COMPONENT:-NW-EC2-EIP-Test}"
```
```bash
SAM_STACK_NAME=$(aws cloudformation list-stacks --region "$AWS_REGION" --no-cli-pager --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE UPDATE_ROLLBACK_COMPLETE | jq -r '.StackSummaries[]? | select(.StackName|test("ManageEIPs-1region-SAM";"i")) | .StackName' | head -n 1); 
[ -n "$SAM_STACK_NAME" ] || SAM_STACK_NAME=$(aws cloudformation list-stacks --region "$AWS_REGION" --no-cli-pager --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE UPDATE_ROLLBACK_COMPLETE | jq -r '.StackSummaries[]?.StackName' | head -n 1); 
export SAM_STACK_NAME; 
jq -c -n --arg SAM_STACK_NAME "$SAM_STACK_NAME" '{SAM_STACK_NAME:$SAM_STACK_NAME}'
```

**Show required variables (JSONL, no blanks)**
```bash
jq -c -n --arg AWS_PROFILE "$AWS_PROFILE" --arg AWS_REGION "$AWS_REGION" --arg SAM_STACK_NAME "$SAM_STACK_NAME" --arg TAG_PROJECT "$TAG_PROJECT" --arg TAG_ENV "$TAG_ENV" --arg TAG_OWNER "$TAG_OWNER" --arg TAG_MANAGEDBY "$TAG_MANAGEDBY" --arg TAG_COSTCENTER "$TAG_COSTCENTER" --arg TAG_COMPONENT "$TAG_COMPONENT" '{AWS_PROFILE:$AWS_PROFILE,AWS_REGION:$AWS_REGION,SAM_STACK_NAME:$SAM_STACK_NAME,TAG_PROJECT:$TAG_PROJECT,TAG_ENV:$TAG_ENV,TAG_OWNER:$TAG_OWNER,TAG_MANAGEDBY:$TAG_MANAGEDBY,TAG_COSTCENTER:$TAG_COSTCENTER,TAG_COMPONENT:$TAG_COMPONENT}'
```
**Expected output**
```json
{"AWS_PROFILE":"default","AWS_REGION":"us-east-1","SAM_STACK_NAME":"ManageEIPs-1region-SAM","TAG_PROJECT":"ManageEIPs-1region-SAM","TAG_ENV":"Dev","TAG_OWNER":"mfh","TAG_MANAGEDBY":"CLI","TAG_COSTCENTER":"Lab","TAG_COMPONENT":"NW-EC2-EIP-Test"}
```

#### 2.2.2 Tagging standard (NameTag vs AWS-created names; `GroupName/RuleName` rule)
**Purpose**
Standardize tags so resources are:
- discoverable by CLI queries,
- protected from accidental cleanup,
- unambiguous when AWS auto-generates names.

**Rules**
- `Name` is the AWS tag key used for console-friendly naming (human readable).
- `NameTag` is a derived/display field in your JSONL outputs that should mirror the resource `Name` tag value when present, 
  and must never be populated with any AWS-created/generated name (those go to a separate field such as AwsGeneratedName).
- `GroupName` and `RuleName` must never reuse the same value as NameTag when the value is AWS-created/generated; 
  keep them semantically specific (grouping vs rule identity vs display name).

**`Reusable jq filter` (named module `flatten_tags`; writes `./flatten_tags.jq` and overwrites if it already exists)**
```bash
printf '%s\n' 'def tv($k):((.Tags//[])|map(select(.Key==$k)|.Value)|.[0]//""); def nz:(if .==null then "" else tostring end);
def flatten_tags($rid): {ResourceId:($rid|nz),NameTag:(tv("Name")|nz),Project:(tv("Project")|nz),Environment:(tv("Environment")|nz),Owner:(tv("Owner")|nz),ManagedBy:(tv("ManagedBy")|nz),CostCenter:(tv("CostCenter")|nz),Component:(tv("Component")|nz),GroupName:(tv("GroupName")|nz),RuleName:(tv("RuleName")|nz),AwsGeneratedName:(tv("aws:cloudformation:logical-id")|nz)} | with_entries(select(.value!=""));' > flatten_tags.jq
```

**`Reusable jq filter` (named module `rules_names`; writes `./rules_names.jq` and overwrites if it already exists)**
```bash
printf '%s\n' 'def nz:(if .==null then "" else tostring end); def rules_names($nameTag;$awsName): {NameTag:($nameTag|nz),AwsGeneratedName:($awsName|nz)} | with_entries(select(.value!=""));' > rules_names.jq
```

**`Reusable jq filter` (named module `tag_helpers`; writes `./tag_helpers.jq` and overwrites if it already exists)**
```bash
printf '%s\n' 'def tv($tags;$k):(($tags//[])|map(select(.Key==$k)|.Value)|.[0]//""); 
def nz:(if .==null then "" else tostring end); 
def tagv($k):tv(.Tags;$k); 
def name_tag: (tagv("Name")|nz); 
def aws_generated_name: ((tagv("aws:cloudformation:logical-id"))|nz); def group_name: (tagv("GroupName")|nz); 
def rule_name: (tagv("RuleName")|nz); 
def project: (tagv("Project")|nz); def environment: (tagv("Environment")|nz); def owner: (tagv("Owner")|nz); 
def managed_by: (tagv("ManagedBy")|nz); 
def cost_center: (tagv("CostCenter")|nz); 
def component: (tagv("Component")|nz);' > tag_helpers.jq
```

**Verify the 3 `jq` modules exist (JSONL)**
```bash
ls -la ./flatten_tags.jq ./rules_names.jq ./tag_helpers.jq | jq -c -R 'capture("^(?<Perms>\\S+)\\s+\\d+\\s+\\S+\\s+\\S+\\s+(?<Bytes>\\d+)\\s+\\S+\\s+\\S+\\s+\\S+\\s+(?<File>.+)$") | {File:.File,Bytes:(.Bytes|tonumber),Perms:.Perms}'
```
**Expected output**
```json
{"File":"./flatten_tags.jq","Bytes":518,"Perms":"-rwxrwxrwx"}
{"File":"./rules_names.jq","Bytes":175,"Perms":"-rwxrwxrwx"}
{"File":"./tag_helpers.jq","Bytes":564,"Perms":"-rwxrwxrwx"}
```


### 2.3 `VPC` (test `network baseline`)
#### 2.3.1 Verify `VPC` exists (JSONL)
**Purpose**
Find the test `VPC` by `Name` tag (preferred) and show `VpcId` + `VpcName` together (no naked IDs).

**List matching VPCs (JSONL, `VpcId` + `VpcName` adjacent; uses jq include "flatten_tags" from ./flatten_tags.jq)**
```bash
aws ec2 describe-vpcs --region "$AWS_REGION" --no-cli-pager | jq -c -L . 'include "flatten_tags"; .Vpcs[] | (flatten_tags(.VpcId) + {VpcId:.VpcId,VpcName:(((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"MISSING-VPC-NAME")}) | select(.VpcName=="VPC_NW-EC2-EIP-Test") | {VpcId,VpcName,Project,Environment,Owner,ManagedBy,CostCenter,Component,GroupName,RuleName,AwsGeneratedName} | with_entries(select(.value!=""))'
```
No output.

#### 2.3.2 Create `VPC` (if missing)
**Create VPC (idempotent-ish; sets `VPC_ID`; uses `jq include` `flatten_tags` from `./flatten_tags.jq`)**
```bash
VPC_ID=$(aws ec2 describe-vpcs --region "$AWS_REGION" --no-cli-pager | jq -r -L . 'include "flatten_tags"; .Vpcs[] | (flatten_tags(.VpcId) + {VpcId:.VpcId,VpcName:(((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"")}) | select(.VpcName=="VPC_NW-EC2-EIP-Test") | .VpcId' | head -n 1); 
[ -n "$VPC_ID" ] || VPC_ID=$(aws ec2 create-vpc --cidr-block "10.20.0.0/16" --region "$AWS_REGION" --no-cli-pager | jq -r '.Vpc.VpcId'); 
jq -c -n --arg VpcId "$VPC_ID" --arg VpcName "VPC_NW-EC2-EIP-Test" '{VpcId:$VpcId,VpcName:$VpcName}'
```
**Expected output**
```json
{"VpcId":"vpc-0757dfccfbd169bd8","VpcName":"VPC_NW-EC2-EIP-Test"}
```
However, the VPC name does not appear in the AWS console yet.

#### 2.3.3 Tag `VPC` (JSONL verify)
**Tag the VPC (standard tags; `GroupName`/`RuleName` distinct from `NameTag`)**
```bash
aws ec2 create-tags --resources "$VPC_ID" --tags Key=Name,Value="VPC_NW-EC2-EIP-Test" Key=Project,Value="$TAG_PROJECT" Key=Environment,Value="$TAG_ENV" Key=Owner,Value="$TAG_OWNER" Key=ManagedBy,Value="$TAG_MANAGEDBY" Key=CostCenter,Value="$TAG_COSTCENTER" Key=Component,Value="$TAG_COMPONENT" Key=GroupName,Value="NW-EC2-EIP-Test" Key=RuleName,Value="VPC" --region "$AWS_REGION" --no-cli-pager
```
**Conclusion**
The VPC name now appears in the AWS console.

#### 2.3.4 Verify `VPC` by `ID + Name` (JSONL, no naked `IDs`)
**Verify VPC (JSONL; uses `jq include` `flatten_tags` from `./flatten_tags.jq`)**
```bash
aws ec2 describe-vpcs --vpc-ids "$VPC_ID" --region "$AWS_REGION" --no-cli-pager | jq -c -L . 'include "flatten_tags"; 
.Vpcs[] | (flatten_tags(.VpcId) + {VpcId:.VpcId,VpcName:(((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"MISSING-VPC-NAME"),CidrBlock:.CidrBlock,IsDefault:.IsDefault}) | {VpcId,VpcName,CidrBlock,IsDefault,Project,Environment,Owner,ManagedBy,CostCenter,Component,GroupName,RuleName,AwsGeneratedName} | with_entries(select(.value!=""))'
```
**Expected output**
```json
{"VpcId":"vpc-0757dfccfbd169bd8","VpcName":"VPC_NW-EC2-EIP-Test","CidrBlock":"10.20.0.0/16","IsDefault":false,"Project":"ManageEIPs","Environment":"dev","Owner":"Malik","ManagedBy":"CLI","CostCenter":"Portfolio","Component":"NW-EC2-EIP-Test","GroupName":"NW-EC2-EIP-Test","RuleName":"VPC","AwsGeneratedName":null}
```


### 2.4 `Subnets`
#### 2.4.1 Create `public subnet`
**Purpose**
We create a public subnet to host the `ENIs` for the `SSM interface endpoints` (`SSM`/`EC2Messages`/`SSMMessages`), 
enabling private-subnet `instances` to use `Session Manager` without a `NAT Gateway`.

**Create `public subnet`(idempotent-ish; sets `PUB_SUBNET_ID`; uses `jq include` `flatten_tags` from `./flatten_tags.jq`)**
```bash
PUB_SUBNET_ID=$(aws ec2 describe-subnets --region "$AWS_REGION" --no-cli-pager --filters "Name=vpc-id,Values=$VPC_ID" | jq -r -L . 'include "flatten_tags"; 
.Subnets[] | (flatten_tags(.SubnetId) + {SubnetId:.SubnetId,SubnetName:(((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"")}) | select(.SubnetName=="Subnet_Public_AZ1_NW-EC2-EIP-Test") | .SubnetId' | head -n 1); [ -n "$PUB_SUBNET_ID" ] || PUB_SUBNET_ID=$(aws ec2 create-subnet --vpc-id "$VPC_ID" --cidr-block "10.20.1.0/24" --availability-zone "${AWS_REGION}a" --region "$AWS_REGION" --no-cli-pager | jq -r '.Subnet.SubnetId'); 
jq -c -n --arg SubnetId "$PUB_SUBNET_ID" --arg SubnetName "Subnet_Public_AZ1_NW-EC2-EIP-Test" '{SubnetId:$SubnetId,SubnetName:$SubnetName}'
```
**Expected output**
```json
{"SubnetId":"subnet-074c3e8e1490cbb60","SubnetName":"Subnet_Public_AZ1_NW-EC2-EIP-Test"}
```

#### 2.4.2 Create `private subnet`
**Create private subnet(idempotent-ish; sets `PRIV_SUBNET_ID`; uses `jq include` `flatten_tags` from `./flatten_tags.jq`)**
```bash
PRIV_SUBNET_ID=$(aws ec2 describe-subnets --region "$AWS_REGION" --no-cli-pager --filters "Name=vpc-id,Values=$VPC_ID" | jq -r -L . 'include "flatten_tags"; 
.Subnets[] | (flatten_tags(.SubnetId) + {SubnetId:.SubnetId,SubnetName:(((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"")}) | select(.SubnetName=="Subnet_Private_AZ1_NW-EC2-EIP-Test") | .SubnetId' | head -n 1); [ -n "$PRIV_SUBNET_ID" ] || PRIV_SUBNET_ID=$(aws ec2 create-subnet --vpc-id "$VPC_ID" --cidr-block "10.20.11.0/24" --availability-zone "${AWS_REGION}a" --region "$AWS_REGION" --no-cli-pager | jq -r '.Subnet.SubnetId'); 
jq -c -n --arg SubnetId "$PRIV_SUBNET_ID" --arg SubnetName "Subnet_Private_AZ1_NW-EC2-EIP-Test" '{SubnetId:$SubnetId,SubnetName:$SubnetName}'
```
**Expected output**
```json
{"SubnetId":"subnet-013030e88f7ac56be","SubnetName":"Subnet_Private_AZ1_NW-EC2-EIP-Test"}
```

#### 2.4.3 Tag `subnets` (JSONL verify)
**Tag public + private subnets (standard tags; `GroupName`/`RuleName` distinct from `NameTag`)**

**Apply `baseline tags` to both `subnets` (shared tags)**
```bash
aws ec2 create-tags --resources "$PUB_SUBNET_ID" "$PRIV_SUBNET_ID" --tags Key=Project,Value="$TAG_PROJECT" Key=Environment,Value="$TAG_ENV" Key=Owner,Value="$TAG_OWNER" Key=ManagedBy,Value="$TAG_MANAGEDBY" Key=CostCenter,Value="$TAG_COSTCENTER" Key=Component,Value="$TAG_COMPONENT" Key=GroupName,Value="NW-EC2-EIP-Test" Key=RuleName,Value="Subnets" --region "$AWS_REGION" --no-cli-pager
```

**Set `Name` tag for `public subnet` (unique per subnet)**
```bash
aws ec2 create-tags --resources "$PUB_SUBNET_ID" --tags Key=Name,Value="Subnet_Public_AZ1_NW-EC2-EIP-Test" --region "$AWS_REGION" --no-cli-pager
```

**Set `Name` tag for `private subnet` (unique per subnet)**
```bash
aws ec2 create-tags --resources "$PRIV_SUBNET_ID" --tags Key=Name,Value="Subnet_Private_AZ1_NW-EC2-EIP-Test" --region "$AWS_REGION" --no-cli-pager
```

**Verify `subnets` by `ID` + `Name` (JSONL; uses `jq include` `flatten_tags` from `./flatten_tags.jq`)**
```bash
aws ec2 describe-subnets --subnet-ids "$PUB_SUBNET_ID" "$PRIV_SUBNET_ID" --region "$AWS_REGION" --no-cli-pager | jq -c -L . 'include "flatten_tags"; 
.Subnets[] | (flatten_tags(.SubnetId) + {SubnetId:.SubnetId,SubnetName:(((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"MISSING-SUBNET-NAME"),VpcId:.VpcId,CidrBlock:.CidrBlock,AZ:.AvailabilityZone}) | {SubnetId,SubnetName,VpcId,CidrBlock,AZ,Project,Environment,Owner,ManagedBy,CostCenter,Component,GroupName,RuleName,AwsGeneratedName} | with_entries(select(.value!=""))'
```
**Expected output**
```json
{"SubnetId":"subnet-074c3e8e1490cbb60","SubnetName":"Subnet_Public_AZ1_NW-EC2-EIP-Test","VpcId":"vpc-0757dfccfbd169bd8","CidrBlock":"10.20.1.0/24","AZ":"us-east-1a","Project":"ManageEIPs","Environment":"dev","Owner":"Malik","ManagedBy":"CLI","CostCenter":"Portfolio","Component":"NW-EC2-EIP-Test","GroupName":"NW-EC2-EIP-Test","RuleName":"Subnets","AwsGeneratedName":null}
{"SubnetId":"subnet-013030e88f7ac56be","SubnetName":"Subnet_Private_AZ1_NW-EC2-EIP-Test","VpcId":"vpc-0757dfccfbd169bd8","CidrBlock":"10.20.11.0/24","AZ":"us-east-1a","Project":"ManageEIPs","Environment":"dev","Owner":"Malik","ManagedBy":"CLI","CostCenter":"Portfolio","Component":"NW-EC2-EIP-Test","GroupName":"NW-EC2-EIP-Test","RuleName":"Subnets","AwsGeneratedName":null}
```
**Conclusion**
The subnets names now appear in the AWS console.


### 2.5 Internet access (`IGW` + `routing`)
#### 2.5.1 Create and attach `IGW`
**Purpose**
Create an Internet Gateway (IGW) and attach it to the test VPC, so the public subnet can reach the internet (required for any public-subnet routing and later endpoint-related dependencies).

**Create `IGW` (idempotent-ish; sets `IGW_ID`; uses `jq include` `flatten_tags` from `./flatten_tags.jq`)**
```bash
IGW_ID=$(aws ec2 describe-internet-gateways --region "$AWS_REGION" --no-cli-pager | jq -r -L . 'include "flatten_tags"; 
.InternetGateways[] | (flatten_tags(.InternetGatewayId) + {InternetGatewayId:.InternetGatewayId,IGWName:(((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"")}) | select(.IGWName=="IGW_NW-EC2-EIP-Test") | .InternetGatewayId' | head -n 1); 
[ -n "$IGW_ID" ] || IGW_ID=$(aws ec2 create-internet-gateway --region "$AWS_REGION" --no-cli-pager | jq -r '.InternetGateway.InternetGatewayId'); 
jq -c -n --arg InternetGatewayId "$IGW_ID" --arg IGWName "IGW_NW-EC2-EIP-Test" '{InternetGatewayId:$InternetGatewayId,IGWName:$IGWName}'
```
**Expected output**
```json
{"InternetGatewayId":"igw-04a4c28ec13304061","IGWName":"IGW_NW-EC2-EIP-Test"}
```

**Tag `IGW` (standard tags)**
```bash
aws ec2 create-tags --resources "$IGW_ID" --tags Key=Name,Value="IGW_NW-EC2-EIP-Test" Key=Project,Value="$TAG_PROJECT" Key=Environment,Value="$TAG_ENV" Key=Owner,Value="$TAG_OWNER" Key=ManagedBy,Value="$TAG_MANAGEDBY" Key=CostCenter,Value="$TAG_COSTCENTER" Key=Component,Value="$TAG_COMPONENT" Key=GroupName,Value="NW-EC2-EIP-Test" Key=RuleName,Value="IGW" --region "$AWS_REGION" --no-cli-pager
```
**Conclusion**
The `IGW name` now appears in the AWS console.

**Attach `IGW` to `VPC`**
```bash
aws ec2 attach-internet-gateway --internet-gateway-id "$IGW_ID" --vpc-id "$VPC_ID" --region "$AWS_REGION" --no-cli-pager
```
**Conclusion**
The `IGW state` now appears as "Attached" in the AWS console.

#### 2.5.2 Create/associate `route table` for `public subnet`
**Purpose**
- Create a dedicated `public route table`, 
- add a default route to the `IGW`, 
- associate it with the `public subnet`.

**Create `public route table` (idempotent-ish; sets `RT_PUBLIC_ID`;uses `jq include` `flatten_tags` from `./flatten_tags.jq`)**
```bash
RT_PUBLIC_ID=$(aws ec2 describe-route-tables --region "$AWS_REGION" --no-cli-pager --filters "Name=vpc-id,Values=$VPC_ID" | jq -r -L . 'include "flatten_tags"; .RouteTables[] | (flatten_tags(.RouteTableId) + {RouteTableId:.RouteTableId,RTName:(((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"")}) | select(.RTName=="RT_Public_NW-EC2-EIP-Test") | .RouteTableId' | head -n 1);
[ -n "$RT_PUBLIC_ID" ] || RT_PUBLIC_ID=$(aws ec2 create-route-table --vpc-id "$VPC_ID" --region "$AWS_REGION" --no-cli-pager | jq -r '.RouteTable.RouteTableId'); 
jq -c -n --arg RouteTableId "$RT_PUBLIC_ID" --arg RTName "RT_Public_NW-EC2-EIP-Test" '{RouteTableId:$RouteTableId,RTName:$RTName}'
```
**Expected output**
```json
{"RouteTableId":"rtb-01b855a7a6a13b4a7","RTName":"RT_Public_NW-EC2-EIP-Test"}
```

**Tag `public route table` (standard tags)**
```bash
aws ec2 create-tags --resources "$RT_PUBLIC_ID" --tags Key=Name,Value="RT_Public_NW-EC2-EIP-Test" Key=Project,Value="$TAG_PROJECT" Key=Environment,Value="$TAG_ENV" Key=Owner,Value="$TAG_OWNER" Key=ManagedBy,Value="$TAG_MANAGEDBY" Key=CostCenter,Value="$TAG_COSTCENTER" Key=Component,Value="$TAG_COMPONENT" Key=GroupName,Value="NW-EC2-EIP-Test" Key=RuleName,Value="RouteTablePublic" --region "$AWS_REGION" --no-cli-pager
```
**Conclusion**
The `public Route Table` name now appears in the AWS console.

**Create default route to IGW (ignore error if it already exists)**
```bash
aws ec2 create-route --route-table-id "$RT_PUBLIC_ID" --destination-cidr-block "0.0.0.0/0" --gateway-id "$IGW_ID" --region "$AWS_REGION" --no-cli-pager 2>/dev/null || true
```
**Expected output**
```bash
{
    "Return": true
}
```

**Associate `public route table` with `public subnet` (sets `RT_PUBLIC_ASSOC_ID` if created)**
```bash
RT_PUBLIC_ASSOC_ID=$(aws ec2 associate-route-table --route-table-id "$RT_PUBLIC_ID" --subnet-id "$PUB_SUBNET_ID" --region "$AWS_REGION" --no-cli-pager | jq -r '.AssociationId' 2>/dev/null || echo "ALREADY-ASSOCIATED"); 
jq -c -n --arg RouteTableId "$RT_PUBLIC_ID" --arg SubnetId "$PUB_SUBNET_ID" --arg AssociationId "$RT_PUBLIC_ASSOC_ID" '{RouteTableId:$RouteTableId,SubnetId:$SubnetId,AssociationId:$AssociationId}'
```
**Expected output**
```bash
{"RouteTableId":"rtb-01b855a7a6a13b4a7","SubnetId":"subnet-074c3e8e1490cbb60","AssociationId":"rtbassoc-0f8ad59ee8b80fcca"}
```
**Conclusion**
The association is also visible in the AWS console:
Explicit `subnet associations`: "subnet-074c3e8e1490cbb60 / Subnet_Public_AZ1_NW-EC2-EIP-Test"

#### 2.5.3 Verify `routes` (JSONL)
**Verify `public route table` (JSONL; includes ID + Name; uses `jq include` `flatten_tags` from `./flatten_tags.jq`)**
```bash
aws ec2 describe-route-tables --route-table-ids "$RT_PUBLIC_ID" --region "$AWS_REGION" --no-cli-pager | jq -c -L . 'include "flatten_tags"; .RouteTables[] | (flatten_tags(.RouteTableId) + {RouteTableId:.RouteTableId,RTName:(((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"MISSING-RT-NAME"),VpcId:.VpcId,RoutesDefaultToIGW:([.Routes[]? | select(.DestinationCidrBlock=="0.0.0.0/0" and (.GatewayId//"")==("'"$IGW_ID"'"))] | length>0),AssociatedSubnetId:((.Associations[]? | select(.Main!=true) | .SubnetId)//"")}) | {RouteTableId,RTName,VpcId,AssociatedSubnetId,RoutesDefaultToIGW,Project,Environment,Owner,ManagedBy,CostCenter,Component,GroupName,RuleName,AwsGeneratedName} | with_entries(select(.value!=""))'
```
**Expected output**
```bash
{"RouteTableId":"rtb-01b855a7a6a13b4a7","RTName":"RT_Public_NW-EC2-EIP-Test","VpcId":"vpc-0757dfccfbd169bd8","AssociatedSubnetId":"subnet-074c3e8e1490cbb60","RoutesDefaultToIGW":true,"Project":"ManageEIPs","Environment":"dev","Owner":"Malik","ManagedBy":"CLI","CostCenter":"Portfolio","Component":"NW-EC2-EIP-Test","GroupName":"NW-EC2-EIP-Test","RuleName":"RouteTablePublic","AwsGeneratedName":null}
```


### 2.6 `Security groups` (minimum for `EIP/SSM` test)
#### 2.6.1 Create `EC2 SG` (`egress only`)
**Create `EC2 SG` (idempotent-ish; sets `SG_EC2_ID`; uses `jq include` `flatten_tags` from `./flatten_tags.jq`)**
```bash
SG_EC2_ID=$(aws ec2 describe-security-groups --region "$AWS_REGION" --no-cli-pager --filters "Name=vpc-id,Values=$VPC_ID" | jq -r -L . 'include "flatten_tags"; .SecurityGroups[] | (flatten_tags(.GroupId) + {GroupId:.GroupId,GroupName:.GroupName,SGNameTag:(((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"")}) | select(.SGNameTag=="SG_EC2_NW-EC2-EIP-Test") | .GroupId' | head -n 1); 
[ -n "$SG_EC2_ID" ] || SG_EC2_ID=$(aws ec2 create-security-group --group-name "SG_EC2_NW_EIP_Test" --description "EC2 test SG (egress only) for EIP/SSM validation" --vpc-id "$VPC_ID" --region "$AWS_REGION" --no-cli-pager | jq -r '.GroupId'); 
jq -c -n --arg GroupId "$SG_EC2_ID" --arg NameTag "SG_EC2_NW-EC2-EIP-Test" '{GroupId:$GroupId,NameTag:$NameTag}'
```
**Expected output**
```bash
{"GroupId":"sg-097e258d5cdbdc0e0","NameTag":"SG_EC2_NW-EC2-EIP-Test"}
```

**Tag `EC2 SG` (standard tags; `GroupName`/`RuleName` distinct from `NameTag`)**
```bash
aws ec2 create-tags --resources "$SG_EC2_ID" --tags Key=Name,Value="SG_EC2_NW-EC2-EIP-Test" Key=Project,Value="$TAG_PROJECT" Key=Environment,Value="$TAG_ENV" Key=Owner,Value="$TAG_OWNER" Key=ManagedBy,Value="$TAG_MANAGEDBY" Key=CostCenter,Value="$TAG_COSTCENTER" Key=Component,Value="$TAG_COMPONENT" Key=GroupName,Value="NW-EC2-EIP-Test" Key=RuleName,Value="EC2SG" --region "$AWS_REGION" --no-cli-pager
```

**Remove default `allow all egress` (emit JSON `Removed` vs `NotPresent/Failed`)**
```bash
aws ec2 revoke-security-group-egress --group-id "$SG_EC2_ID" --ip-permissions "$(jq -c -n '{IpProtocol:"-1",IpRanges:[{CidrIp:"0.0.0.0/0"}]}')" --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg GroupId "$SG_EC2_ID" --arg NameTag "SG_EC2_NW-EC2-EIP-Test" '{Action:"RevokeDefaultEgress",GroupId:$GroupId,NameTag:$NameTag,Result:"REMOVED"}' || jq -c -n --arg GroupId "$SG_EC2_ID" --arg NameTag "SG_EC2_NW-EC2-EIP-Test" '{Action:"RevokeDefaultEgress",GroupId:$GroupId,NameTag:$NameTag,Result:"NOT_PRESENT_OR_FAILED"}'
```
**Example output**
```json
{
    "Return": true,
    "RevokedSecurityGroupRules": [
        {
            "SecurityGroupRuleId": "sgr-0c6f13879370a3a96",
            "GroupId": "sg-097e258d5cdbdc0e0",
            "IsEgress": true,
            "IpProtocol": "-1",
            "FromPort": -1,
            "ToPort": -1,
            "CidrIpv4": "0.0.0.0/0"
        }
    ]
}
```

**Verify `EC2 SG egress rule count` (JSONL; no arrays; no naked IDs)**
```bash
aws ec2 describe-security-groups --group-ids "$SG_EC2_ID" --region "$AWS_REGION" --no-cli-pager | jq -c '.SecurityGroups[0] as $g | (($g.Tags//[])|map(select(.Key=="Name")|.Value)|.[0]//"MISSING-SG-NAME") as $NameTag | {GroupId:$g.GroupId,NameTag:$NameTag,EgressRulesCount:(($g.IpPermissionsEgress//[])|length)}'
```
**Example output**
```json
{"GroupId":"sg-097e258d5cdbdc0e0","NameTag":"SG_EC2_NW-EC2-EIP-Test","EgressRulesCount":0}
```
**Conclusion**
The SG has zero outbound rules, as expected after removing the default “allow all egress” and before adding the explicit TCP 443 egress to the endpoints SG.

#### 2.6.2 Create `endpoint/SSM SG` (`VPC endpoints`; mandatory)
**Purpose**
- Create the security group attached to the `SSM interface endpoints`’ `ENIs`
- Allow inbound TCP 443 from the `EC2 SG`
- then (now that `SG_EP_ID` exists) add the `EC2 SG` outbound TCP 443 rule to this `endpoints SG`.

##### 2.6.2.1 Create `endpoint/SSM SG` (idempotent-ish; sets `SG_EP_ID`; uses `jq include` `flatten_tags` from `./flatten_tags.jq`)
```bash
SG_EP_ID=$(aws ec2 describe-security-groups --region "$AWS_REGION" --no-cli-pager --filters "Name=vpc-id,Values=$VPC_ID" | jq -r -L . 'include "flatten_tags"; .SecurityGroups[] | (flatten_tags(.GroupId) + {GroupId:.GroupId,NameTag:(((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"")}) | select(.NameTag=="SG_Endpoints_SSM_NW-EC2-EIP-Test") | .GroupId' | head -n 1); 
[ -n "$SG_EP_ID" ] || SG_EP_ID=$(aws ec2 create-security-group --group-name "SG_Endpoints_SSM_NW_EIP_Test" --description "Interface endpoints SG for SSM (ingress from EC2 SG on 443)" --vpc-id "$VPC_ID" --region "$AWS_REGION" --no-cli-pager | jq -r '.GroupId'); 
jq -c -n --arg GroupId "$SG_EP_ID" --arg NameTag "SG_Endpoints_SSM_NW-EC2-EIP-Test" '{GroupId:$GroupId,NameTag:$NameTag}'
```
**Example output**
```json
{"GroupId":"sg-05e8e4c3756c6044a","NameTag":"SG_Endpoints_SSM_NW-EC2-EIP-Test"}
```
**Conclusion**
`Security group` `sg-05e8e4c3756c6044a` created and then tagged.

##### 2.6.2.2 Tag `endpoint/SSM SG` (standard tags; `GroupName`/`RuleName` distinct from `NameTag`)
```bash
aws ec2 create-tags --resources "$SG_EP_ID" --tags Key=Name,Value="SG_Endpoints_SSM_NW-EC2-EIP-Test" Key=Project,Value="$TAG_PROJECT" Key=Environment,Value="$TAG_ENV" Key=Owner,Value="$TAG_OWNER" Key=ManagedBy,Value="$TAG_MANAGEDBY" Key=CostCenter,Value="$TAG_COSTCENTER" Key=Component,Value="$TAG_COMPONENT" Key=GroupName,Value="NW-EC2-EIP-Test" Key=RuleName,Value="EndpointsSG" --region "$AWS_REGION" --no-cli-pager
```

##### 2.6.2.3 Verify `endpoint/SSM SG tags` (JSONL; conclusion for the tagging command; uses `jq include` `flatten_tags` from `./flatten_tags.jq`)
```bash
aws ec2 describe-security-groups --group-ids "$SG_EP_ID" --region "$AWS_REGION" --no-cli-pager | jq -c -L . 'include "flatten_tags"; .SecurityGroups[] | (flatten_tags(.GroupId) + {GroupId:.GroupId,NameTag:(((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"MISSING-SG-NAME")}) | {GroupId,NameTag,Project,Environment,Owner,ManagedBy,CostCenter,Component,GroupName,RuleName,AwsGeneratedName} | with_entries(select(.value!=""))'
```
**Example output**
```json
{"GroupId":"sg-05e8e4c3756c6044a","NameTag":"SG_Endpoints_SSM_NW-EC2-EIP-Test","Project":"ManageEIPs","Environment":"dev","Owner":"Malik","ManagedBy":"CLI","CostCenter":"Portfolio","Component":"NW-EC2-EIP-Test","GroupName":"NW-EC2-EIP-Test","RuleName":"EndpointsSG","AwsGeneratedName":null}
```
**Conclusion**
The `endpoint/SSM` `security group` exists and our tagging succeeded.

##### 2.6.2.4 Allow `endpoint/SSM SG ingress` from `EC2 SG` on TCP 443 (emit `JSONL` `Created` vs `Exists/Failed`)
```bash
aws ec2 authorize-security-group-ingress --group-id "$SG_EP_ID" --ip-permissions "$(jq -c -n --arg SG "$SG_EC2_ID" '{IpProtocol:"tcp",FromPort:443,ToPort:443,UserIdGroupPairs:[{GroupId:$SG}]}')" --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg GroupId "$SG_EP_ID" --arg NameTag "SG_Endpoints_SSM_NW-EC2-EIP-Test" --arg SourceGroupId "$SG_EC2_ID" '{Action:"AuthorizeIngress",GroupId:$GroupId,NameTag:$NameTag,FromPort:443,ToPort:443,Protocol:"tcp",SourceGroupId:$SourceGroupId,Result:"CREATED"}' || jq -c -n --arg GroupId "$SG_EP_ID" --arg NameTag "SG_Endpoints_SSM_NW-EC2-EIP-Test" --arg SourceGroupId "$SG_EC2_ID" '{Action:"AuthorizeIngress",GroupId:$GroupId,NameTag:$NameTag,FromPort:443,ToPort:443,Protocol:"tcp",SourceGroupId:$SourceGroupId,Result:"EXISTS_OR_FAILED"}'
```
**Example output**
```json
{"Action":"AuthorizeIngress","GroupId":"sg-05e8e4c3756c6044a","NameTag":"SG_Endpoints_SSM_NW-EC2-EIP-Test","FromPort":443,"ToPort":443,"Protocol":"tcp","SourceGroupId":"sg-097e258d5cdbdc0e0","Result":"CREATED"}
```
**Conclusion**
The inbound rule was successfully added to the `endpoint/SSM security group` `sg-05e8e4c3756c6044a` 
(`NameTag` = `SG_Endpoints_SSM_NW-EC2-EIP-Test`): it now allows TCP 443 traffic from the `EC2 security group sg-097e258d5cdbdc0e0`.

##### 2.6.2.5 Allow `EC2 SG egress` to `endpoint/SSM SG` on TCP 443 (emit `JSONL` `Created` vs `Exists/Failed`)
```bash
aws ec2 authorize-security-group-egress --group-id "$SG_EC2_ID" --ip-permissions "$(jq -c -n --arg SG "$SG_EP_ID" '{IpProtocol:"tcp",FromPort:443,ToPort:443,UserIdGroupPairs:[{GroupId:$SG}]}')" --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg GroupId "$SG_EC2_ID" --arg NameTag "SG_EC2_NW-EC2-EIP-Test" --arg TargetGroupId "$SG_EP_ID" '{Action:"AuthorizeEgress",GroupId:$GroupId,NameTag:$NameTag,FromPort:443,ToPort:443,Protocol:"tcp",TargetGroupId:$TargetGroupId,Result:"CREATED"}' || jq -c -n --arg GroupId "$SG_EC2_ID" --arg NameTag "SG_EC2_NW-EC2-EIP-Test" --arg TargetGroupId "$SG_EP_ID" '{Action:"AuthorizeEgress",GroupId:$GroupId,NameTag:$NameTag,FromPort:443,ToPort:443,Protocol:"tcp",TargetGroupId:$TargetGroupId,Result:"EXISTS_OR_FAILED"}'
```
**Example output**
```json
{"Action":"AuthorizeEgress","GroupId":"sg-097e258d5cdbdc0e0","NameTag":"SG_EC2_NW-EC2-EIP-Test","FromPort":443,"ToPort":443,"Protocol":"tcp","TargetGroupId":"sg-05e8e4c3756c6044a","Result":"CREATED"}
```
**Conclusion**
The outbound rule was successfully added to the `EC2 security group sg-097e258d5cdbdc0e0` (`NameTag = SG_EC2_NW-EC2-EIP-Test`): it now allows TCP 443 traffic to the `endpoints/SSM security group sg-05e8e4c3756c6044a`.


#### 2.6.3 Verify `SG rules` (JSONL)
##### 2.6.3.1 Verify `EC2 SG` now has the expected `egress rule count` (JSONL; no arrays; no naked IDs)
```bash
aws ec2 describe-security-groups --group-ids "$SG_EC2_ID" --region "$AWS_REGION" --no-cli-pager | jq -c '.SecurityGroups[0] as $g | (($g.Tags//[])|map(select(.Key=="Name")|.Value)|.[0]//"MISSING-SG-NAME") as $NameTag | {GroupId:$g.GroupId,NameTag:$NameTag,EgressRulesCount:(($g.IpPermissionsEgress//[])|length)}'
```
**Example output**
```json
{"GroupId":"sg-097e258d5cdbdc0e0","NameTag":"SG_EC2_NW-EC2-EIP-Test","EgressRulesCount":1}
```
**Conclusion**
The `EC2 security group sg-097e258d5cdbdc0e0` currently has exactly 1 outbound rule: the `TCP 443 SG-to-SG egress rule` to the `endpoints/SSM SG` we just created.

##### 2.6.3.2 Verify `endpoint/SSM SG rule counts` (JSONL; no arrays; no naked `IDs`)
```bash
aws ec2 describe-security-groups --group-ids "$SG_EP_ID" --region "$AWS_REGION" --no-cli-pager | jq -c '.SecurityGroups[0] as $g | (($g.Tags//[])|map(select(.Key=="Name")|.Value)|.[0]//"MISSING-SG-NAME") as $NameTag | {GroupId:$g.GroupId,NameTag:$NameTag,IngressRulesCount:(($g.IpPermissions//[])|length),EgressRulesCount:(($g.IpPermissionsEgress//[])|length)}'
```
**Example output**
```json
{"GroupId":"sg-05e8e4c3756c6044a","NameTag":"SG_Endpoints_SSM_NW-EC2-EIP-Test","IngressRulesCount":1,"EgressRulesCount":1}
```
**Conclusion**
The `endpoints/SSM security group sg-05e8e4c3756c6044a` has exactly 1 inbound rule and 1 outbound rule.


### 2.7 `VPC endpoints` (no `NAT`; `SSM` connectivity)
#### 2.7.1 Enable `VPC` `DNS` attributes (required for --private-dns-enabled)
**Resolve `VpcName` for JSONL outputs (sets `VPC_NAME`)**
```bash
VPC_NAME=$(aws ec2 describe-vpcs --vpc-ids "$VPC_ID" --region "$AWS_REGION" --no-cli-pager | jq -r '.Vpcs[0].Tags//[] | map(select(.Key=="Name")|.Value) | .[0] // "MISSING-VPC-NAME"'); jq -c -n --arg VpcId "$VPC_ID" --arg VpcName "$VPC_NAME" '{VpcId:$VpcId,VpcName:$VpcName}'
```
**Example output**
```json
{"VpcId":"vpc-0757dfccfbd169bd8","VpcName":"VPC_NW-EC2-EIP-Test"}
```
**Conclusion**
The command looked up the Name tag for the `VPC ID vpc-0757dfccfbd169bd8`, stored it in `VPC_NAME`, then printed a JSONL line confirming the pairing: `VpcName` is `VPC_NW-EC2-EIP-Test`.
The VPC already had that `Name` tag. 
This step just retrieves it into `VPC_NAME`, so later JSONL outputs never show a naked `VpcId` without a `VpcName`.

##### 2.7.1.1 Enable `enableDnsSupport=true` (required for `--private-dns-enabled`)
```bash
aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-support "{\"Value\":true}" --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg VpcId "$VPC_ID" --arg VpcName "$VPC_NAME" '{Action:"EnableDnsSupport",VpcId:$VpcId,VpcName:$VpcName,Value:true,Result:"APPLIED"}' || jq -c -n --arg VpcId "$VPC_ID" --arg VpcName "$VPC_NAME" '{Action:"EnableDnsSupport",VpcId:$VpcId,VpcName:$VpcName,Value:true,Result:"FAILED"}'
```
**Example output**
```json
{"Action":"EnableDnsSupport","VpcId":"vpc-0757dfccfbd169bd8","VpcName":"VPC_NW-EC2-EIP-Test","Value":true,"Result":"APPLIED"}
```
**Conclusion**
The VPC attribute `enableDnsSupport` was successfully set to `true` for `vpc-0757dfccfbd169bd8` (`VpcName` = `VPC_NW-EC2-EIP-Test`).
The JSONL confirms the change was applied.

##### 2.7.1.2 Enable `enableDnsHostnames=true` (required for `--private-dns-enabled`)
```bash
aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-hostnames "{\"Value\":true}" --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg VpcId "$VPC_ID" --arg VpcName "$VPC_NAME" '{Action:"EnableDnsHostnames",VpcId:$VpcId,VpcName:$VpcName,Value:true,Result:"APPLIED"}' || jq -c -n --arg VpcId "$VPC_ID" --arg VpcName "$VPC_NAME" '{Action:"EnableDnsHostnames",VpcId:$VpcId,VpcName:$VpcName,Value:true,Result:"FAILED"}'
```
**Example output**
```json
{"Action":"EnableDnsHostnames","VpcId":"vpc-0757dfccfbd169bd8","VpcName":"VPC_NW-EC2-EIP-Test","Value":true,"Result":"APPLIED"}
```
**Conclusion**
The VPC attribute `enableDnsHostnames` was successfully set to `true` for `vpc-0757dfccfbd169bd8` (`VpcName` = `VPC_NW-EC2-EIP-Test`).
Together with `enableDnsSupport=true`, this allows us to create `interface endpoints` with `--private-dns-enabled`.

##### 2.7.1.3 Verify both attributes are now `true` (JSONL; no arrays)
```bash
DNS_SUPPORT=$(aws ec2 describe-vpc-attribute --vpc-id "$VPC_ID" --attribute enableDnsSupport --region "$AWS_REGION" --no-cli-pager | jq -r '.EnableDnsSupport.Value'); 
DNS_HOSTNAMES=$(aws ec2 describe-vpc-attribute --vpc-id "$VPC_ID" --attribute enableDnsHostnames --region "$AWS_REGION" --no-cli-pager | jq -r '.EnableDnsHostnames.Value'); 
jq -c -n --arg VpcId "$VPC_ID" --arg VpcName "$VPC_NAME" --argjson EnableDnsSupport "$DNS_SUPPORT" --argjson EnableDnsHostnames "$DNS_HOSTNAMES" '{VpcId:$VpcId,VpcName:$VpcName,EnableDnsSupport:$EnableDnsSupport,EnableDnsHostnames:$EnableDnsHostnames}'
```
**Example output**
```json
{"VpcId":"vpc-0757dfccfbd169bd8","VpcName":"VPC_NW-EC2-EIP-Test","EnableDnsSupport":true,"EnableDnsHostnames":true}
```
**Conclusion**
The verification succeeded: the `VPC vpc-0757dfccfbd169bd8` (`VpcName` = `VPC_NW-EC2-EIP-Test`) now has both `EnableDnsSupport:true` and `EnableDnsHostnames:true`, so `--private-dns-enabled` will no longer be rejected when creating the `interface endpoints`.

#### 2.7.2 Create `interface endpoints` (`SSM`, `EC2Messages`, `SSMMessages`)
**Purpose**
Create the 3 mandatory `interface endpoints` in the `test VPC`, so the `private EC2 instance` can use `Session Manager` without a `NAT Gateway`.

##### 2.7.2.1 Create `com.amazonaws.$AWS_REGION.ssm endpoint` (idempotent-ish; sets `VPCE_SSM_ID`; uses `endpoint SG` + `private subnet`)
```bash
VPCE_SSM_ID=$(aws ec2 describe-vpc-endpoints --region "$AWS_REGION" --no-cli-pager --filters "Name=vpc-id,Values=$VPC_ID" "Name=service-name,Values=com.amazonaws.$AWS_REGION.ssm" | jq -r '.VpcEndpoints[0].VpcEndpointId // ""'); 
[ -n "$VPCE_SSM_ID" ] || VPCE_SSM_ID=$(aws ec2 create-vpc-endpoint --vpc-id "$VPC_ID" --vpc-endpoint-type Interface --service-name "com.amazonaws.$AWS_REGION.ssm" --subnet-ids "$PUB_SUBNET_ID" --security-group-ids "$SG_EP_ID" --private-dns-enabled --region "$AWS_REGION" --no-cli-pager | jq -r '.VpcEndpoint.VpcEndpointId'); 
jq -c -n --arg VpcEndpointId "$VPCE_SSM_ID" --arg NameTag "VPCE_SSM_NW-EC2-EIP-Test" '{VpcEndpointId:$VpcEndpointId,NameTag:$NameTag,ServiceName:"com.amazonaws.'"$AWS_REGION"'.ssm"}'
```
**Example output**
```json
{"VpcEndpointId":"vpce-032a7f26278e58657","NameTag":"VPCE_SSM_NW-EC2-EIP-Test","ServiceName":"com.amazonaws.us-east-1.ssm"}
```
**Conclusion**
- The `SSM interface VPC endpoint` now exists: AWS returned `VpcEndpointId:"vpce-032a7f26278e58657"` for `service com.amazonaws.us-east-1.ssm`.
- The JSONL line confirms that `VPCE_SSM_ID` is set to that `endpoint ID` (the `NameTag` shown is the name we intend to apply when we tag it).

##### 2.7.2.2 Create `com.amazonaws.$AWS_REGION.ec2messages` (idempotent-ish; sets `VPCE_EC2MSG_ID`; uses `endpoint SG` + `private subnet`)
```bash
VPCE_EC2MSG_ID=$(aws ec2 describe-vpc-endpoints --region "$AWS_REGION" --no-cli-pager --filters "Name=vpc-id,Values=$VPC_ID" "Name=service-name,Values=com.amazonaws.$AWS_REGION.ec2messages" | jq -r '.VpcEndpoints[0].VpcEndpointId // ""'); 
[ -n "$VPCE_EC2MSG_ID" ] || VPCE_EC2MSG_ID=$(aws ec2 create-vpc-endpoint --vpc-id "$VPC_ID" --vpc-endpoint-type Interface --service-name "com.amazonaws.$AWS_REGION.ec2messages" --subnet-ids "$PUB_SUBNET_ID" --security-group-ids "$SG_EP_ID" --private-dns-enabled --region "$AWS_REGION" --no-cli-pager | jq -r '.VpcEndpoint.VpcEndpointId'); 
jq -c -n --arg VpcEndpointId "$VPCE_EC2MSG_ID" --arg NameTag "VPCE_EC2Messages_NW-EC2-EIP-Test" '{VpcEndpointId:$VpcEndpointId,NameTag:$NameTag,ServiceName:"com.amazonaws.'"$AWS_REGION"'.ec2messages"}'
```
**Example output**
```json
{"VpcEndpointId":"vpce-07a790f816f2e056f","NameTag":"VPCE_EC2Messages_NW-EC2-EIP-Test","ServiceName":"com.amazonaws.us-east-1.ec2messages"}
```

##### 2.7.2.3 Create `com.amazonaws.$AWS_REGION.ssmmessages` (idempotent-ish; sets `VPCE_SSMM_ID`; uses `endpoint SG` + `private subnet`)
```bash
VPCE_SSMM_ID=$(aws ec2 describe-vpc-endpoints --region "$AWS_REGION" --no-cli-pager --filters "Name=vpc-id,Values=$VPC_ID" "Name=service-name,Values=com.amazonaws.$AWS_REGION.ssmmessages" | jq -r '.VpcEndpoints[0].VpcEndpointId // ""'); 
[ -n "$VPCE_SSMM_ID" ] || VPCE_SSMM_ID=$(aws ec2 create-vpc-endpoint --vpc-id "$VPC_ID" --vpc-endpoint-type Interface --service-name "com.amazonaws.$AWS_REGION.ssmmessages" --subnet-ids "$PUB_SUBNET_ID" --security-group-ids "$SG_EP_ID" --private-dns-enabled --region "$AWS_REGION" --no-cli-pager | jq -r '.VpcEndpoint.VpcEndpointId'); 
jq -c -n --arg VpcEndpointId "$VPCE_SSMM_ID" --arg NameTag "VPCE_SSMMessages_NW-EC2-EIP-Test" '{VpcEndpointId:$VpcEndpointId,NameTag:$NameTag,ServiceName:"com.amazonaws.'"$AWS_REGION"'.ssmmessages"}'
```
**Example output**
```json
{"VpcEndpointId":"vpce-00bcd0544364db667","NameTag":"VPCE_SSMMessages_NW-EC2-EIP-Test","ServiceName":"com.amazonaws.us-east-1.ssmmessages"}
```

#### 2.7.3 Tag all 3 `endpoints` (standard tags + deterministic `Name`)
##### 2.7.3.1 Apply baseline tags to all endpoints (shared tags)
```bash
aws ec2 create-tags --resources "$VPCE_SSM_ID" "$VPCE_EC2MSG_ID" "$VPCE_SSMM_ID" --tags Key=Project,Value="$TAG_PROJECT" Key=Environment,Value="$TAG_ENV" Key=Owner,Value="$TAG_OWNER" Key=ManagedBy,Value="$TAG_MANAGEDBY" Key=CostCenter,Value="$TAG_COSTCENTER" Key=Component,Value="$TAG_COMPONENT" Key=GroupName,Value="NW-EC2-EIP-Test" Key=RuleName,Value="VPCEndpoints" --region "$AWS_REGION" --no-cli-pager
```
**Conclusion**
No output but in the AWS console, each of the endpoints have the expected 8 tags.

##### 2.7.3.2 Set `Name` tag for `SSM endpoint` (unique `Name`)
```bash
aws ec2 create-tags --resources "$VPCE_SSM_ID" --tags Key=Name,Value="VPCE_SSM_NW-EC2-EIP-Test" --region "$AWS_REGION" --no-cli-pager
```

##### 2.7.3.3 Set `Name` tag for `EC2Messages` (unique `Name`)
```bash
aws ec2 create-tags --resources "$VPCE_EC2MSG_ID" --tags Key=Name,Value="VPCE_EC2Messages_NW-EC2-EIP-Test" --region "$AWS_REGION" --no-cli-pager
```

##### 2.7.3.4 Set `Name` tag for `SSMMessages` (unique `Name`)
```bash
aws ec2 create-tags --resources "$VPCE_SSMM_ID" --tags Key=Name,Value="VPCE_SSMMessages_NW-EC2-EIP-Test" --region "$AWS_REGION" --no-cli-pager
```

#### 2.7.4 Attach `endpoint SG` and restrict `ingress` (`SG-to-SG`)
**Purpose**
Confirm the endpoints use `SG_EP_ID` and that the only required `inbound` is TCP 443 from the `EC2 SG` (already configured in 2.6.2).

##### 2.7.4.1 Verify `endpoint SG` attachment (JSONL; no arrays)
```bash
aws ec2 describe-vpc-endpoints --vpc-endpoint-ids "$VPCE_SSM_ID" "$VPCE_EC2MSG_ID" "$VPCE_SSMM_ID" --region "$AWS_REGION" --no-cli-pager | jq -c '.VpcEndpoints[] | ((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0]//"MISSING-VPCE-NAME") as $NameTag | {VpcEndpointId:.VpcEndpointId,NameTag:$NameTag,ServiceName:.ServiceName,VpcId:.VpcId,SubnetIds:((.SubnetIds//[])|join(",")),SecurityGroupIds:((.Groups//[])|map(.GroupId)|join(",")),PrivateDnsEnabled:.PrivateDnsEnabled,State:.State}'
```
**Example output**
```json
{"VpcEndpointId":"vpce-032a7f26278e58657","NameTag":"VPCE_SSM_NW-EC2-EIP-Test","ServiceName":"com.amazonaws.us-east-1.ssm","VpcId":"vpc-0757dfccfbd169bd8","SubnetIds":"subnet-074c3e8e1490cbb60","SecurityGroupIds":"sg-05e8e4c3756c6044a","PrivateDnsEnabled":true,"State":"available"}
{"VpcEndpointId":"vpce-07a790f816f2e056f","NameTag":"VPCE_EC2Messages_NW-EC2-EIP-Test","ServiceName":"com.amazonaws.us-east-1.ec2messages","VpcId":"vpc-0757dfccfbd169bd8","SubnetIds":"subnet-074c3e8e1490cbb60","SecurityGroupIds":"sg-05e8e4c3756c6044a","PrivateDnsEnabled":true,"State":"available"}
{"VpcEndpointId":"vpce-00bcd0544364db667","NameTag":"VPCE_SSMMessages_NW-EC2-EIP-Test","ServiceName":"com.amazonaws.us-east-1.ssmmessages","VpcId":"vpc-0757dfccfbd169bd8","SubnetIds":"subnet-074c3e8e1490cbb60","SecurityGroupIds":"sg-05e8e4c3756c6044a","PrivateDnsEnabled":true,"State":"available"}
```
**Conclusion**
SG attachment is confirmed.

##### 2.7.4.2 Verify `endpoint SG ingress` is ONLY `TCP 443` from `EC2 SG` (JSONL; no arrays)
```bash
aws ec2 describe-security-groups --group-ids "$SG_EP_ID" --region "$AWS_REGION" --no-cli-pager | jq -c --arg EC2SG "$SG_EC2_ID" '.SecurityGroups[0] as $g | (($g.Tags//[])|map(select(.Key=="Name")|.Value)|.[0]//"MISSING-SG-NAME") as $NameTag | ($g.IpPermissions//[]) as $in | ($in|length) as $InCount | ($in|map(select(.IpProtocol=="tcp" and .FromPort==443 and .ToPort==443 and ((.UserIdGroupPairs//[])|map(.GroupId)|index($EC2SG))!=null))|length) as $MatchCount | (($InCount==1) and ($MatchCount==1)) as $Only443FromEC2 | {GroupId:$g.GroupId,NameTag:$NameTag,IngressRulesCount:$InCount,Matching443FromEc2RulesCount:$MatchCount,OnlyTcp443FromEc2Sg:$Only443FromEC2,Ec2GroupId:$EC2SG}'
```
**Example output**
```json
{"GroupId":"sg-05e8e4c3756c6044a","NameTag":"SG_Endpoints_SSM_NW-EC2-EIP-Test","IngressRulesCount":1,"Matching443FromEc2RulesCount":1,"OnlyTcp443FromEc2Sg":true,"Ec2GroupId":"sg-097e258d5cdbdc0e0"}
```
**Conclusion**
- The `endpoint/SSM SG sg-05e8e4c3756c6044a` (`NameTag`=`SG_Endpoints_SSM_NW-EC2-EIP-Test`) has exactly one inbound rule.
- That rule is `only TCP 443` from the `EC2 SG sg-097e258d5cdbdc0e0` (`OnlyTcp443FromEc2Sg:true`).

#### 2.7.5 Verify `endpoints` (JSONL)
**Verify endpoints are `available` (JSONL; no arrays; no naked IDs)**
```bash
aws ec2 describe-vpc-endpoints --vpc-endpoint-ids "$VPCE_SSM_ID" "$VPCE_EC2MSG_ID" "$VPCE_SSMM_ID" --region "$AWS_REGION" --no-cli-pager | jq -c '.VpcEndpoints[] | ((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0]//"MISSING-VPCE-NAME") as $NameTag | {VpcEndpointId:.VpcEndpointId,NameTag:$NameTag,ServiceName:.ServiceName,State:.State,PrivateDnsEnabled:.PrivateDnsEnabled}'
```
**Example output**
```json
{"VpcEndpointId":"vpce-032a7f26278e58657","NameTag":"VPCE_SSM_NW-EC2-EIP-Test","ServiceName":"com.amazonaws.us-east-1.ssm","State":"available","PrivateDnsEnabled":true}
{"VpcEndpointId":"vpce-07a790f816f2e056f","NameTag":"VPCE_EC2Messages_NW-EC2-EIP-Test","ServiceName":"com.amazonaws.us-east-1.ec2messages","State":"available","PrivateDnsEnabled":true}
{"VpcEndpointId":"vpce-00bcd0544364db667","NameTag":"VPCE_SSMMessages_NW-EC2-EIP-Test","ServiceName":"com.amazonaws.us-east-1.ssmmessages","State":"available","PrivateDnsEnabled":true}
```
**Conclusion**
- All 3 required `interface endpoints` (ssm`` , `ec2messages`, `ssmmessages`) exist, are in State:"available".
- They have `PrivateDnsEnabled:true` with the expected `NameTag` values.
So the `VPC endpoints` layer for `SSM` connectivity is ready.


### 2.8 `EC2 instance` (test `target` for `EIP association`)
#### 2.8.1 `IAM instance profile` (`SSM`)
##### 2.8.1.1 Resolve `target names` (deterministic; JSONL)
```bash
EC2_ROLE_NAME="NW-EC2-EIP-Test-EC2SSMRole"; 
EC2_PROFILE_NAME="NW-EC2-EIP-Test-InstanceProfile"; 
jq -c -n --arg RoleName "$EC2_ROLE_NAME" --arg InstanceProfileName "$EC2_PROFILE_NAME" '{RoleName:$RoleName,InstanceProfileName:$InstanceProfileName}'
```
**Example output**
```json
{"RoleName":"NW-EC2-EIP-Test-EC2SSMRole","InstanceProfileName":"NW-EC2-EIP-Test-InstanceProfile"}
```
**Conclusion**
The deterministic `IAM` names are now set for this section:
- `EC2_ROLE_NAME` will be `NW-EC2-EIP-Test-EC2SSMRole`.
- `EC2_PROFILE_NAME` will be `NW-EC2-EIP-Test-InstanceProfile`.

##### 2.8.1.2 Create `EC2 SSM role` (idempotent-ish; JSONL)
```bash
ROLE_ARN=$(aws iam get-role --role-name "$EC2_ROLE_NAME" --no-cli-pager 2>/dev/null | jq -r '.Role.Arn' || true); 
[ -n "$ROLE_ARN" ] || ROLE_ARN=$(aws iam create-role --role-name "$EC2_ROLE_NAME" --assume-role-policy-document "$(jq -c -n '{Version:"2012-10-17",Statement:[{Effect:"Allow",Principal:{Service:"ec2.amazonaws.com"},Action:"sts:AssumeRole"}]}')" --no-cli-pager | jq -r '.Role.Arn'); 
jq -c -n --arg RoleName "$EC2_ROLE_NAME" --arg NameTag "IAMRole_EC2SSM_NW-EC2-EIP-Test" --arg RoleArn "$ROLE_ARN" '{RoleName:$RoleName,NameTag:$NameTag,RoleArn:$RoleArn}'
```
**Example output**
```json
{"RoleName":"NW-EC2-EIP-Test-EC2SSMRole","NameTag":"IAMRole_EC2SSM_NW-EC2-EIP-Test","RoleArn":"arn:aws:iam::180294215772:role/NW-EC2-EIP-Test-EC2SSMRole"}
```
**Conclusion**
The `RoleName` was created. We can also check it in the AWS console (IAM > Roles).

##### 2.8.1.3 Tag `EC2 SSM role` (no output; verify later)
```bash
aws iam tag-role --role-name "$EC2_ROLE_NAME" --tags Key=Name,Value="IAMRole_EC2SSM_NW-EC2-EIP-Test" Key=Project,Value="$TAG_PROJECT" Key=Environment,Value="$TAG_ENV" Key=Owner,Value="$TAG_OWNER" Key=ManagedBy,Value="$TAG_MANAGEDBY" Key=CostCenter,Value="$TAG_COSTCENTER" Key=Component,Value="$TAG_COMPONENT" Key=GroupName,Value="NW-EC2-EIP-Test" Key=RuleName,Value="EC2SSMRole" --no-cli-pager
```
**Conclusion**
There is no output but we can check in the AWS console that the 8 tags have been created.

##### 2.8.1.4 Attach `policy` `AmazonSSMManagedInstanceCore` (idempotent-ish; JSONL)
```bash
aws iam attach-role-policy --role-name "$EC2_ROLE_NAME" --policy-arn "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg RoleName "$EC2_ROLE_NAME" --arg PolicyName "AmazonSSMManagedInstanceCore" '{Action:"AttachRolePolicy",RoleName:$RoleName,PolicyName:$PolicyName,Result:"ATTACHED_OR_ALREADY"}' || jq -c -n --arg RoleName "$EC2_ROLE_NAME" --arg PolicyName "AmazonSSMManagedInstanceCore" '{Action:"AttachRolePolicy",RoleName:$RoleName,PolicyName:$PolicyName,Result:"FAILED"}'
```
**Example output**
```json
{"Action":"AttachRolePolicy","RoleName":"NW-EC2-EIP-Test-EC2SSMRole","PolicyName":"AmazonSSMManagedInstanceCore","Result":"ATTACHED_OR_ALREADY"}
```
**Conclusion**
We can also check in the AWS console that the AWS-managed policy `AmazonSSMManagedInstanceCore` is now attached to the `IAM role` `NW-EC2-EIP-Test-EC2SSMRole` (IAM > Roles > `NW-EC2-EIP-Test-EC2SSMRole` > Permissions tab > Permissions policies).

##### 2.8.1.5 Create `instance profile` + add `role` (idempotent-ish; JSONL)
```bash
aws iam get-instance-profile --instance-profile-name "$EC2_PROFILE_NAME" --no-cli-pager >/dev/null 2>&1 || aws iam create-instance-profile --instance-profile-name "$EC2_PROFILE_NAME" --no-cli-pager >/dev/null 2>&1; aws iam add-role-to-instance-profile --instance-profile-name "$EC2_PROFILE_NAME" --role-name "$EC2_ROLE_NAME" --no-cli-pager >/dev/null 2>&1 || true; PROFILE_ARN=$(aws iam get-instance-profile --instance-profile-name "$EC2_PROFILE_NAME" --no-cli-pager | jq -r '.InstanceProfile.Arn'); 
jq -c -n --arg InstanceProfileName "$EC2_PROFILE_NAME" --arg NameTag "InstanceProfile_EC2SSM_NW-EC2-EIP-Test" --arg InstanceProfileArn "$PROFILE_ARN" --arg RoleName "$EC2_ROLE_NAME" '{InstanceProfileName:$InstanceProfileName,NameTag:$NameTag,InstanceProfileArn:$InstanceProfileArn,RoleName:$RoleName}'
```
**Example output**
```json
{"InstanceProfileName":"NW-EC2-EIP-Test-InstanceProfile","NameTag":"InstanceProfile_EC2SSM_NW-EC2-EIP-Test","InstanceProfileArn":"arn:aws:iam::180294215772:instance-profile/NW-EC2-EIP-Test-InstanceProfile","RoleName":"NW-EC2-EIP-Test-EC2SSMRole"}
```
**Conclusion**
- The `instance profile NW-EC2-EIP-Test-InstanceProfile` now exists.
- The `role NW-EC2-EIP-Test-EC2SSMRole` is associated to it.

##### 2.8.1.6 Tag `instance profile` (no output; verify later)
```bash
aws iam tag-instance-profile --instance-profile-name "$EC2_PROFILE_NAME" --tags Key=Name,Value="InstanceProfile_EC2SSM_NW-EC2-EIP-Test" Key=Project,Value="$TAG_PROJECT" Key=Environment,Value="$TAG_ENV" Key=Owner,Value="$TAG_OWNER" Key=ManagedBy,Value="$TAG_MANAGEDBY" Key=CostCenter,Value="$TAG_COSTCENTER" Key=Component,Value="$TAG_COMPONENT" Key=GroupName,Value="NW-EC2-EIP-Test" Key=RuleName,Value="InstanceProfile" --no-cli-pager
```

##### 2.8.1.7 Verify `role` + `instance profile` `tags/policy` (JSONL; no arrays)
```bash
ROLE_TAGS=$(aws iam list-role-tags --role-name "$EC2_ROLE_NAME" --no-cli-pager | jq -c '.Tags'); ROLE_POLICIES=$(aws iam list-attached-role-policies --role-name "$EC2_ROLE_NAME" --no-cli-pager | jq -r '[.AttachedPolicies[]?.PolicyName]|join(",")');
PROFILE_TAGS=$(aws iam list-instance-profile-tags --instance-profile-name "$EC2_PROFILE_NAME" --no-cli-pager | jq -c '.Tags'); 
jq -c -n --arg RoleName "$EC2_ROLE_NAME" --arg InstanceProfileName "$EC2_PROFILE_NAME" --arg AttachedPolicies "$ROLE_POLICIES" --argjson RoleTags "$ROLE_TAGS" --argjson InstanceProfileTags "$PROFILE_TAGS" '{RoleName:$RoleName,InstanceProfileName:$InstanceProfileName,AttachedPolicies:$AttachedPolicies,RoleTagsJson:($RoleTags|tostring),InstanceProfileTagsJson:($InstanceProfileTags|tostring)}'
```
**Example output**
```json
{"RoleName":"NW-EC2-EIP-Test-EC2SSMRole","InstanceProfileName":"NW-EC2-EIP-Test-InstanceProfile","AttachedPolicies":"AmazonSSMManagedInstanceCore","RoleTagsJson":"[{\"Key\":\"Name\",\"Value\":\"IAMRole_EC2SSM_NW-EC2-EIP-Test\"},{\"Key\":\"Project\",\"Value\":\"ManageEIPs\"},{\"Key\":\"Environment\",\"Value\":\"dev\"},{\"Key\":\"Owner\",\"Value\":\"Malik\"},{\"Key\":\"ManagedBy\",\"Value\":\"CLI\"},{\"Key\":\"CostCenter\",\"Value\":\"Portfolio\"},{\"Key\":\"Component\",\"Value\":\"NW-EC2-EIP-Test\"},{\"Key\":\"GroupName\",\"Value\":\"NW-EC2-EIP-Test\"},{\"Key\":\"RuleName\",\"Value\":\"EC2SSMRole\"}]","InstanceProfileTagsJson":"[{\"Key\":\"GroupName\",\"Value\":\"NW-EC2-EIP-Test\"},{\"Key\":\"Project\",\"Value\":\"ManageEIPs\"},{\"Key\":\"Owner\",\"Value\":\"Malik\"},{\"Key\":\"ManagedBy\",\"Value\":\"CLI\"},{\"Key\":\"CostCenter\",\"Value\":\"Portfolio\"},{\"Key\":\"Environment\",\"Value\":\"dev\"},{\"Key\":\"Component\",\"Value\":\"NW-EC2-EIP-Test\"},{\"Key\":\"RuleName\",\"Value\":\"InstanceProfile\"},{\"Key\":\"Name\",\"Value\":\"InstanceProfile_EC2SSM_NW-EC2-EIP-Test\"}]"}
```
**Conclusion**
Both `role` and `instance profile` are correctly configured for `SSM`:
- `AmazonSSMManagedInstanceCore` is attached to `NW-EC2-EIP-Test-EC2SSMRole`.
- Both the `role` and the `instance profile` have the expected tags (shown in `RoleTagsJson` and `InstanceProfileTagsJson`).



#### 2.8.2 Launch test `instance` (`subnet` + `SG` + tags)
##### 2.8.2.1 Resolve latest `Amazon Linux AMI` (JSONL)
```bash
AMI_ID=$(aws ssm get-parameter --name "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-6.1-x86_64" --region "$AWS_REGION" --no-cli-pager | jq -r '.Parameter.Value'); 
jq -c -n --arg AmiId "$AMI_ID" --arg AmiParam "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-6.1-x86_64" '{AmiId:$AmiId,AmiParam:$AmiParam}'
```
**Example output**
```json
{"AmiId":"ami-068c0051b15cdb816","AmiParam":"/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-6.1-x86_64"}
```

##### 2.8.2.2 Tag primary `ENI` (JSONL; required to avoid naked `NetworkInterfaceId` later)
```bash
ENI_ID=$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" --region "$AWS_REGION" --no-cli-pager | jq -r '.Reservations[0].Instances[0].NetworkInterfaces[0].NetworkInterfaceId'); aws ec2 create-tags --resources "$ENI_ID" --tags Key=Name,Value="ENI_Primary_EC2_EIP_TestTarget_NW-EC2-EIP-Test" Key=Project,Value="$TAG_PROJECT" Key=Environment,Value="$TAG_ENV" Key=Owner,Value="$TAG_OWNER" Key=ManagedBy,Value="$TAG_MANAGEDBY" Key=CostCenter,Value="$TAG_COSTCENTER" Key=Component,Value="$TAG_COMPONENT" Key=GroupName,Value="NW-EC2-EIP-Test" Key=RuleName,Value="PrimaryENI" --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg NetworkInterfaceId "$ENI_ID" --arg NameTag "ENI_Primary_EC2_EIP_TestTarget_NW-EC2-EIP-Test" '{Action:"TagENI",NetworkInterfaceId:$NetworkInterfaceId,NameTag:$NameTag,Result:"TAGGED"}' || jq -c -n --arg NetworkInterfaceId "$ENI_ID" --arg NameTag "ENI_Primary_EC2_EIP_TestTarget_NW-EC2-EIP-Test" '{Action:"TagENI",NetworkInterfaceId:$NetworkInterfaceId,NameTag:$NameTag,Result:"FAILED"}'
```
**Example output**
```json
{"Action":"TagENI","NetworkInterfaceId":"eni-0ae73da1a6fcbf2bd","NameTag":"ENI_Primary_EC2_EIP_TestTarget_NW-EC2-EIP-Test","Result":"TAGGED"}
```

##### 2.8.2.3 Launch `instance` in `public subnet` (`EIP target`; no `key pair`; `SSM only`) (JSONL)
```bash
EC2_NAME="EC2_EIP_TestTarget_NW-EC2-EIP-Test"; INSTANCE_ID=$(aws ec2 run-instances --image-id "$AMI_ID" --instance-type "t2.micro" --iam-instance-profile "Name=$EC2_PROFILE_NAME" --network-interfaces "DeviceIndex=0,SubnetId=$PUB_SUBNET_ID,Groups=$SG_EC2_ID,AssociatePublicIpAddress=false" --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$EC2_NAME},{Key=Project,Value=$TAG_PROJECT},{Key=Environment,Value=$TAG_ENV},{Key=Owner,Value=$TAG_OWNER},{Key=ManagedBy,Value=$TAG_MANAGEDBY},{Key=CostCenter,Value=$TAG_COSTCENTER},{Key=Component,Value=$TAG_COMPONENT},{Key=GroupName,Value=NW-EC2-EIP-Test},{Key=RuleName,Value=EC2Instance}]" "ResourceType=volume,Tags=[{Key=Name,Value=$EC2_NAME},{Key=Project,Value=$TAG_PROJECT},{Key=Environment,Value=$TAG_ENV},{Key=Owner,Value=$TAG_OWNER},{Key=ManagedBy,Value=$TAG_MANAGEDBY},{Key=CostCenter,Value=$TAG_COSTCENTER},{Key=Component,Value=$TAG_COMPONENT},{Key=GroupName,Value=NW-EC2-EIP-Test},{Key=RuleName,Value=EBSRoot}]" --region "$AWS_REGION" --no-cli-pager | jq -r '.Instances[0].InstanceId'); 
jq -c -n --arg InstanceId "$INSTANCE_ID" --arg NameTag "$EC2_NAME" --arg SubnetId "$PUB_SUBNET_ID" --arg SubnetName "Subnet_Public_AZ1_NW-EC2-EIP-Test" '{InstanceId:$InstanceId,NameTag:$NameTag,SubnetId:$SubnetId,SubnetName:$SubnetName}'
```
**Example output**
```json
{"InstanceId":"i-08a6f769b30f80543","NameTag":"EC2_EIP_TestTarget_NW-EC2-EIP-Test","SubnetId":"subnet-074c3e8e1490cbb60","SubnetName":"Subnet_Public_AZ1_NW-EC2-EIP-Test"}
```

##### 2.8.2.4 Wait until `instance` is running (no output from `waiter`; emit JSONL)
```bash
aws ec2 wait instance-running --instance-ids "$INSTANCE_ID" --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg InstanceId "$INSTANCE_ID" --arg NameTag "$EC2_NAME" '{Action:"WaitInstanceRunning",InstanceId:$InstanceId,NameTag:$NameTag,Result:"RUNNING"}' || jq -c -n --arg InstanceId "$INSTANCE_ID" --arg NameTag "$EC2_NAME" '{Action:"WaitInstanceRunning",InstanceId:$InstanceId,NameTag:$NameTag,Result:"FAILED_OR_TIMEOUT"}'
```
**Example output**
```json
{"Action":"WaitInstanceRunning","InstanceId":"i-08a6f769b30f80543","NameTag":"EC2_EIP_TestTarget_NW-EC2-EIP-Test","Result":"RUNNING"}
```

#### 2.8.3 Verify `instance` (JSONL)
**Verify `instance` details (JSONL; no arrays; no naked `IDs`)**
```bash
(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" --region "$AWS_REGION" --no-cli-pager; aws ec2 describe-subnets --subnet-ids "$PUB_SUBNET_ID" --region "$AWS_REGION" --no-cli-pager; aws ec2 describe-vpcs --vpc-ids "$VPC_ID" --region "$AWS_REGION" --no-cli-pager) | jq -c -s '.[0].Reservations[0].Instances[0] as $i | .[1].Subnets[0] as $s | .[2].Vpcs[0] as $v | (($i.Tags//[])|map(select(.Key=="Name")|.Value)|.[0]//"MISSING-EC2-NAME") as $NameTag | (($s.Tags//[])|map(select(.Key=="Name")|.Value)|.[0]//"MISSING-SUBNET-NAME") as $SubnetName | (($v.Tags//[])|map(select(.Key=="Name")|.Value)|.[0]//"MISSING-VPC-NAME") as $VpcName | {InstanceId:$i.InstanceId,NameTag:$NameTag,State:($i.State.Name//""),InstanceType:($i.InstanceType//""),PrivateIp:($i.PrivateIpAddress//""),SubnetId:$s.SubnetId,SubnetName:$SubnetName,VpcId:$v.VpcId,VpcName:$VpcName,SecurityGroupIds:(($i.SecurityGroups//[])|map(.GroupId)|join(",")),SecurityGroupAwsNames:(($i.SecurityGroups//[])|map(.GroupName)|join(",")),IamInstanceProfileArn:($i.IamInstanceProfile.Arn//"")} | with_entries(select(.value!=""))'
```
**Example output**
```json
{"InstanceId":"i-08a6f769b30f80543","NameTag":"EC2_EIP_TestTarget_NW-EC2-EIP-Test","State":"running","InstanceType":"t2.micro","PrivateIp":"10.20.1.79","SubnetId":"subnet-074c3e8e1490cbb60","SubnetName":"Subnet_Public_AZ1_NW-EC2-EIP-Test","VpcId":"vpc-0757dfccfbd169bd8","VpcName":"VPC_NW-EC2-EIP-Test","SecurityGroupIds":"sg-097e258d5cdbdc0e0","SecurityGroupAwsNames":"SG_EC2_NW_EIP_Test","IamInstanceProfileArn":"arn:aws:iam::180294215772:instance-profile/NW-EC2-EIP-Test-InstanceProfile"}
```
**Conclusion**
We can also check in the AWS console: click on EC2 instance > Actions > Security > Modify IAM Role.

#### 2.8.4 Verify `SSM` registration (JSONL)
**Verify `SSM-managed instance status` for this `EC2` (JSONL; no arrays; no naked `IDs`)**
```bash
EC2_NAME=$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" --region "$AWS_REGION" --no-cli-pager | jq -r '.Reservations[0].Instances[0].Tags//[] | map(select(.Key=="Name")|.Value) | .[0] // "MISSING-EC2-NAME"'); 
aws ssm describe-instance-information --region "$AWS_REGION" --no-cli-pager --filters "Key=InstanceIds,Values=$INSTANCE_ID" | jq -c --arg NameTag "$EC2_NAME" '.InstanceInformationList[0] as $x | {InstanceId:($x.InstanceId//"'"$INSTANCE_ID"'"),NameTag:$NameTag,PingStatus:($x.PingStatus//""),PlatformType:($x.PlatformType//""),AgentVersion:($x.AgentVersion//""),IsLatestVersion:($x.IsLatestVersion//null),LastPingDateTime:($x.LastPingDateTime//"")} | with_entries(select(.value!="" and .value!=null))'
```
**Example output**
```json
{"InstanceId":"i-08a6f769b30f80543","NameTag":"EC2_EIP_TestTarget_NW-EC2-EIP-Test","PingStatus":"Online","PlatformType":"Linux","AgentVersion":"3.3.3050.0","LastPingDateTime":"2025-12-30T05:01:17.985000+01:00"}
```
**Conclusion**
The `EC2 instance i-08a6f769b30f80543` (`NameTag=EC2_EIP_TestTarget_NW-EC2-EIP-Test`) is successfully registered with `Systems Manager` and reachable via `Session Manager` (`PingStatus`:"Online"), running Linux with `SSM Agent version 3.3.3050.0`, last seen at 2025-12-30T05:01:17.985000+01:00.


### 2.9 `Elastic IP` (test `EIP` lifecycle)
#### 2.9.1 Allocate and Tag test `EIPs` (3x) (JSONL)
##### 2.9.1.1 Allocate and tag `VPC Elastic IP` #1 (sets `EIP_ALLOC_ID_1`; intended ATTACHED JSONL)
**Allocate**
```bash
EIP_ALLOC_ID_1=$(aws ec2 allocate-address --domain vpc --region "$AWS_REGION" --no-cli-pager | jq -r '.AllocationId'); 
jq -c -n --arg AllocationId "$EIP_ALLOC_ID_1" --arg NameTag "EIP_Attached_NW-EC2-EIP-Test" '{AllocationId:$AllocationId,NameTag:$NameTag}'
```
**Example output**
```json
{"AllocationId":"eipalloc-0bff9015be09e6951","NameTag":"EIP_Attached_NW-EC2-EIP-Test"}
```

 **Tag `EIP` #1 (no output; verify later)**
```bash
aws ec2 create-tags --resources "$EIP_ALLOC_ID_1" --tags Key=Name,Value="EIP_Attached_NW-EC2-EIP-Test" Key=Project,Value="$TAG_PROJECT" Key=Environment,Value="$TAG_ENV" Key=Owner,Value="$TAG_OWNER" Key=ManagedBy,Value="$TAG_MANAGEDBY" Key=CostCenter,Value="$TAG_COSTCENTER" Key=Component,Value="$TAG_COMPONENT" Key=GroupName,Value="NW-EC2-EIP-Test" Key=RuleName,Value="EIP_Attached" --region "$AWS_REGION" --no-cli-pager
```

##### 2.9.1.2 Allocate and tag `VPC Elastic IP` #2 (sets `EIP_ALLOC_ID_2`; intended UNATTACHED JSONL)
**Allocate**
```bash
EIP_ALLOC_ID_2=$(aws ec2 allocate-address --domain vpc --region "$AWS_REGION" --no-cli-pager | jq -r '.AllocationId'); 
jq -c -n --arg AllocationId "$EIP_ALLOC_ID_2" --arg NameTag "EIP_Unattached_1_NW-EC2-EIP-Test" '{AllocationId:$AllocationId,NameTag:$NameTag}'
```
**Example output**
```json
{"AllocationId":"eipalloc-0e281a4134e91af72","NameTag":"EIP_Unattached_1_NW-EC2-EIP-Test"}
```

 **Tag `EIP` #2 (no output; verify later)**
```bash
aws ec2 create-tags --resources "$EIP_ALLOC_ID_2" --tags Key=Name,Value="EIP_Unattached_1_NW-EC2-EIP-Test" Key=Project,Value="$TAG_PROJECT" Key=Environment,Value="$TAG_ENV" Key=Owner,Value="$TAG_OWNER" Key=ManagedBy,Value="$TAG_MANAGEDBY" Key=CostCenter,Value="$TAG_COSTCENTER" Key=Component,Value="$TAG_COMPONENT" Key=GroupName,Value="NW-EC2-EIP-Test" Key=RuleName,Value="EIP_Unattached" --region "$AWS_REGION" --no-cli-pager
```

##### 2.9.1.3 Allocate and tag `VPC Elastic IP` #3 (sets `EIP_ALLOC_ID_3`; intended UNATTACHED JSONL)
**Allocate**
```bash
EIP_ALLOC_ID_3=$(aws ec2 allocate-address --domain vpc --region "$AWS_REGION" --no-cli-pager | jq -r '.AllocationId'); 
jq -c -n --arg AllocationId "$EIP_ALLOC_ID_3" --arg NameTag "EIP_Unattached_2_NW-EC2-EIP-Test" '{AllocationId:$AllocationId,NameTag:$NameTag}'
```
**Example output**
```json
{"AllocationId":"eipalloc-0b8425b8a82aee46d","NameTag":"EIP_Unattached_2_NW-EC2-EIP-Test"}
```

 **Tag `EIP` #3 (no output; verify later)**
```bash
aws ec2 create-tags --resources "$EIP_ALLOC_ID_3" --tags Key=Name,Value="EIP_Unattached_2_NW-EC2-EIP-Test" Key=Project,Value="$TAG_PROJECT" Key=Environment,Value="$TAG_ENV" Key=Owner,Value="$TAG_OWNER" Key=ManagedBy,Value="$TAG_MANAGEDBY" Key=CostCenter,Value="$TAG_COSTCENTER" Key=Component,Value="$TAG_COMPONENT" Key=GroupName,Value="NW-EC2-EIP-Test" Key=RuleName,Value="EIP_Unattached" --region "$AWS_REGION" --no-cli-pager
```

#### 2.9.2 Associate `EIP` to test `instance` (JSONL)
**Associate `EIP` #1 to test `instance` (sets `EIP_ASSOC_ID_1`; JSONL)**
```bash
EIP_ASSOC_ID_1=$(aws ec2 associate-address --instance-id "$INSTANCE_ID" --allocation-id "$EIP_ALLOC_ID_1" --region "$AWS_REGION" --no-cli-pager | jq -r '.AssociationId'); 
jq -c -n --arg InstanceId "$INSTANCE_ID" --arg InstanceNameTag "EC2_EIP_TestTarget_NW-EC2-EIP-Test" --arg AllocationId "$EIP_ALLOC_ID_1" --arg EipNameTag "EIP_Attached_NW-EC2-EIP-Test" --arg AssociationId "$EIP_ASSOC_ID_1" '{InstanceId:$InstanceId,InstanceNameTag:$InstanceNameTag,AllocationId:$AllocationId,EipNameTag:$EipNameTag,AssociationId:$AssociationId}'
```
**Example output**
```json
{"InstanceId":"i-08a6f769b30f80543","InstanceNameTag":"EC2_EIP_TestTarget_NW-EC2-EIP-Test","AllocationId":"eipalloc-0bff9015be09e6951","EipNameTag":"EIP_Attached_NW-EC2-EIP-Test","AssociationId":"eipassoc-0bf30df6985210344"}
```

#### 2.9.3 Verify `association state` (JSONL)
**Verify all 3 EIPs (expect 1 attached, 2 unattached) (JSONL; no arrays; no naked IDs)**
```bash
aws ec2 describe-addresses --allocation-ids "$EIP_ALLOC_ID_1" "$EIP_ALLOC_ID_2" "$EIP_ALLOC_ID_3" --region "$AWS_REGION" --no-cli-pager | jq -c '.Addresses[] | ((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0]//"MISSING-EIP-NAME") as $NameTag | {AllocationId:(.AllocationId//""),NameTag:$NameTag,PublicIp:(.PublicIp//""),AssociationId:(.AssociationId//""),InstanceId:(.InstanceId//""),NetworkInterfaceId:(.NetworkInterfaceId//""),PrivateIpAddress:(.PrivateIpAddress//"")} | with_entries(select(.value!=""))'
```
**Example output**
```json
{"AllocationId":"eipalloc-0b8425b8a82aee46d","NameTag":"EIP_Unattached_2_NW-EC2-EIP-Test","RuleName":"EIP_Unattached","GroupName":"NW-EC2-EIP-Test","Project":"ManageEIPs-1region-SAM","Environment":"Dev","Owner":"mfh","ManagedBy":"ManageEIPs","CostCenter":"Lab","Component":"NW-EC2-EIP-Test"}
{"AllocationId":"eipalloc-0bff9015be09e6951","NameTag":"EIP_Attached_NW-EC2-EIP-Test","RuleName":"EIP_Attached","GroupName":"NW-EC2-EIP-Test","Project":"ManageEIPs-1region-SAM","Environment":"Dev","Owner":"mfh","ManagedBy":"ManageEIPs","CostCenter":"Lab","Component":"NW-EC2-EIP-Test"}
{"AllocationId":"eipalloc-0e281a4134e91af72","NameTag":"EIP_Unattached_1_NW-EC2-EIP-Test","RuleName":"EIP_Unattached","GroupName":"NW-EC2-EIP-Test","Project":"ManageEIPs-1region-SAM","Environment":"Dev","Owner":"mfh","ManagedBy":"ManageEIPs","CostCenter":"Lab","Component":"NW-EC2-EIP-Test"}
```
**Conclusion**
The EIPs were not tagged with the standard tags initially (only `Name`/`GroupName`/`RuleName` were present). 
After applying standard tags (including `ManagedBy=ManageEIPs`), the Lambda will no longer classify them as `skipped_not_managed` and can release the 2 `unattached EIPs` on the next scheduled run.

#### 2.9.4 Associate `EIP` to test `instance` (JSONL)
**Associate `EIP` to `instance` (sets `EIP_ASSOC_ID`; JSONL result)**
```bash
EIP_ASSOC_ID=$(aws ec2 associate-address --instance-id "$INSTANCE_ID" --allocation-id "$EIP_ALLOC_ID" --region "$AWS_REGION" --no-cli-pager | jq -r '.AssociationId'); 
jq -c -n --arg InstanceId "$INSTANCE_ID" --arg NameTag "EC2_EIP_TestTarget_NW-EC2-EIP-Test" --arg AllocationId "$EIP_ALLOC_ID" --arg AssociationId "$EIP_ASSOC_ID" '{InstanceId:$InstanceId,NameTag:$NameTag,AllocationId:$AllocationId,AssociationId:$AssociationId}'
```
**Example output**
```json
{"InstanceId":"i-08a6f769b30f80543","NameTag":"EC2_EIP_TestTarget_NW-EC2-EIP-Test","AllocationId":"eipalloc-06136040892770ba9","AssociationId":"eipassoc-0835136e494254716"}
```
**Conclusion**
- The `Elastic IP eipalloc-06136040892770ba9` was successfully associated to the `EC2 instance i-08a6f769b30f80543` (`NameTag`=`EC2_EIP_TestTarget_NW-EC2-EIP-Test`).
- AWS returned the `association identifier eipassoc-0835136e494254716` for `rollback/verification`.

#### 2.9.5 Verify `association state` (JSONL)
**Verify `EIP` is now associated (JSONL; no arrays; no naked IDs)**
```bash
ADDR_JSON=$(aws ec2 describe-addresses --allocation-ids "$EIP_ALLOC_ID" --region "$AWS_REGION" --no-cli-pager); 
ENI_ID=$(echo "$ADDR_JSON" | jq -r '.Addresses[0].NetworkInterfaceId // ""'); 
INST_ID=$(echo "$ADDR_JSON" | jq -r '.Addresses[0].InstanceId // ""'); ENI_JSON=$([ -n "$ENI_ID" ] && aws ec2 describe-network-interfaces --network-interface-ids "$ENI_ID" --region "$AWS_REGION" --no-cli-pager || echo '{"NetworkInterfaces":[{}]}'); 
INST_JSON=$([ -n "$INST_ID" ] && aws ec2 describe-instances --instance-ids "$INST_ID" --region "$AWS_REGION" --no-cli-pager || echo '{"Reservations":[{"Instances":[{}]}]}'); 
jq -c -s '.[0].Addresses[0] as $a | .[1].NetworkInterfaces[0] as $n | .[2].Reservations[0].Instances[0] as $i | ((($a.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"MISSING-EIP-NAME") as $EipNameTag | ((($n.TagSet//[])|map(select(.Key=="Name")|.Value)|.[0])//"MISSING-ENI-NAME") as $EniNameTag | ((($i.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"MISSING-EC2-NAME") as $Ec2NameTag | {AllocationId:($a.AllocationId//""),NameTag:$EipNameTag,PublicIp:($a.PublicIp//""),AssociationId:($a.AssociationId//""),InstanceId:($a.InstanceId//""),InstanceNameTag:$Ec2NameTag,NetworkInterfaceId:($a.NetworkInterfaceId//""),NetworkInterfaceNameTag:$EniNameTag,PrivateIpAddress:($a.PrivateIpAddress//"")} | with_entries(select(.value!=""))' <(echo "$ADDR_JSON") <(echo "$ENI_JSON") <(echo "$INST_JSON")
```
**Example output**
```json
{"AllocationId":"eipalloc-06136040892770ba9","NameTag":"EIP_Test_NW-EC2-EIP-Test","PublicIp":"100.48.157.234","AssociationId":"eipassoc-0835136e494254716","InstanceId":"i-08a6f769b30f80543","InstanceNameTag":"EC2_EIP_TestTarget_NW-EC2-EIP-Test","NetworkInterfaceId":"eni-0ae73da1a6fcbf2bd","NetworkInterfaceNameTag":"ENI_Primary_EC2_EIP_TestTarget_NW-EC2-EIP-Test","PrivateIpAddress":"10.20.1.79"}
```
**Conclusion**
The `EIP eipalloc-06136040892770ba9` (`NameTag=EIP_Test_NW-EC2-EIP-Test`, PublicIp=100.48.157.234) is currently associated (`AssociationId`=`eipassoc-0835136e494254716`) to `instance i-08a6f769b30f80543` via `network interface eni-0ae73da1a6fcbf2bd`, mapped to private IP 10.20.1.79.


### 2.10 Cleanup (optional)
#### 2.10.1 Release `test EIP` (JSONL)
##### 2.10.1.1 Capture current `attached EIP state` (JSONL)
```bash
aws ec2 describe-addresses --allocation-ids "$EIP_ALLOC_ID_1" --region "$AWS_REGION" --no-cli-pager | jq -c '.Addresses[0] as $a | ((($a.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"MISSING-EIP-NAME") as $NameTag | {AllocationId:($a.AllocationId//""),NameTag:$NameTag,PublicIp:($a.PublicIp//""),AssociationId:($a.AssociationId//""),InstanceId:($a.InstanceId//""),NetworkInterfaceId:($a.NetworkInterfaceId//"")} | with_entries(select(.value!=""))'
```
**Example output**
```json
{"AllocationId":"eipalloc-0bff9015be09e6951","NameTag":"EIP_Attached_NW-EC2-EIP-Test","PublicIp":"98.91.43.181","AssociationId":"eipassoc-0bf30df6985210344","InstanceId":"i-08a6f769b30f80543","NetworkInterfaceId":"eni-0ae73da1a6fcbf2bd"}
```
**Conclusion**
Confirm `AssociationId` and `InstanceId` are present (`EIP` is still associated and must be disassociated before release).


##### 2.10.1.2 Disassociate the remaining `EIP` (JSONL result)
```bash
EIP_ASSOC_ID=$(aws ec2 describe-addresses --allocation-ids "$EIP_ALLOC_ID_1" --region "$AWS_REGION" --no-cli-pager | jq -r '.Addresses[0].AssociationId // ""'); 
aws ec2 disassociate-address --association-id "$EIP_ASSOC_ID" --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg AllocationId "$EIP_ALLOC_ID_1" --arg AssociationId "$EIP_ASSOC_ID" '{Action:"DisassociateAddress",AllocationId:$AllocationId,AssociationId:$AssociationId,Result:"DISASSOCIATED"}' || jq -c -n --arg AllocationId "$EIP_ALLOC_ID_1" --arg AssociationId "$EIP_ASSOC_ID" '{Action:"DisassociateAddress",AllocationId:$AllocationId,AssociationId:$AssociationId,Result:"FAILED_OR_ALREADY"}'
```
**Example output**
```json
{"Action":"DisassociateAddress","AllocationId":"eipalloc-0bff9015be09e6951","AssociationId":"eipassoc-0bf30df6985210344","Result":"DISASSOCIATED"}
```
**Conclusion**
The Result is `DISASSOCIATED`, meaning the `EIP` is now eligible for release.

##### 2.10.1.3 Release the `EIP` (JSONL result)
```bash
aws ec2 release-address --allocation-id "$EIP_ALLOC_ID_1" --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg AllocationId "$EIP_ALLOC_ID_1" '{Action:"ReleaseAddress",AllocationId:$AllocationId,Result:"RELEASED"}' || jq -c -n --arg AllocationId "$EIP_ALLOC_ID_1" '{Action:"ReleaseAddress",AllocationId:$AllocationId,Result:"FAILED"}'
```
**Example output**
```json
{"Action":"ReleaseAddress","AllocationId":"eipalloc-0bff9015be09e6951","Result":"RELEASED"}
```
**Conclusion**
The Result is `RELEASED`, meaning the final `test EIP` is removed from your account.

#### 2.10.2 Terminate test `instance` (JSONL)
##### 2.10.2.1 Resolve `instance ID` by `NameTag` (JSONL)
```bash
INSTANCE_ID=$(aws ec2 describe-instances --region "$AWS_REGION" --no-cli-pager --filters "Name=tag:Name,Values=EC2_EIP_TestTarget_NW-EC2-EIP-Test" "Name=instance-state-name,Values=pending,running,stopping,stopped" | jq -r '.Reservations[].Instances[]? | .InstanceId' | head -n 1); 
jq -c -n --arg InstanceId "${INSTANCE_ID:-}" --arg NameTag "EC2_EIP_TestTarget_NW-EC2-EIP-Test" '{InstanceId:$InstanceId,NameTag:$NameTag}'
```
**Example output**
```json
{"InstanceId":"i-08a6f769b30f80543","NameTag":"EC2_EIP_TestTarget_NW-EC2-EIP-Test"}
```
**Conclusion**
If `InstanceId` were empty, the instance would be already gone (skip termination). It is not the case.

##### 2.10.2.2 Terminate `instance` (JSONL result)
```bash
[ -n "$INSTANCE_ID" ] && aws ec2 terminate-instances --instance-ids "$INSTANCE_ID" --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg InstanceId "$INSTANCE_ID" --arg NameTag "EC2_EIP_TestTarget_NW-EC2-EIP-Test" '{Action:"TerminateInstance",InstanceId:$InstanceId,NameTag:$NameTag,Result:"TERMINATION_REQUESTED"}' || jq -c -n --arg InstanceId "${INSTANCE_ID:-}" --arg NameTag "EC2_EIP_TestTarget_NW-EC2-EIP-Test" '{Action:"TerminateInstance",InstanceId:$InstanceId,NameTag:$NameTag,Result:"SKIPPED_NOT_FOUND_OR_FAILED"}'
```
**Example output**
```json
{"Action":"TerminateInstance","InstanceId":"i-08a6f769b30f80543","NameTag":"EC2_EIP_TestTarget_NW-EC2-EIP-Test","Result":"TERMINATION_REQUESTED"}
```
**Conclusion**
`TERMINATION_REQUESTED` means AWS accepted the terminate request; termination completes asynchronously.

#### 2.10.3 Verify no leftovers (JSONL)
##### 2.10.3.1 Verify no `EIPs` remain for this test group (JSONL)
```bash
aws ec2 describe-addresses --region "$AWS_REGION" --no-cli-pager --filters "Name=tag:GroupName,Values=NW-EC2-EIP-Test" | jq -c '.Addresses[]? | {AllocationId:(.AllocationId//""),PublicIp:(.PublicIp//"")} | with_entries(select(.value!=""))' || true
```
**Conclusion**
As expected: no output lines (no `EIPs` left with `GroupName`=`NW-EC2-EIP-Test`).

##### 2.10.3.2 Verify `instance` is terminated (JSONL)
```bash
[ -n "$INSTANCE_ID" ] && aws ec2 describe-instances --instance-ids "$INSTANCE_ID" --region "$AWS_REGION" --no-cli-pager 2>/dev/null | jq -c '.Reservations[0].Instances[0] as $i | (($i.Tags//[])|map(select(.Key=="Name")|.Value)|.[0]//"MISSING-EC2-NAME") as $NameTag | {InstanceId:$i.InstanceId,NameTag:$NameTag,State:($i.State.Name//"")}' || jq -c -n --arg InstanceId "${INSTANCE_ID:-}" '{InstanceId:$InstanceId,Exists:false}'
```
**Example output**
```json
{"InstanceId":"i-08a6f769b30f80543","NameTag":"EC2_EIP_TestTarget_NW-EC2-EIP-Test","State":"terminated"}
```
**Conclusion**
Expected: `State` becomes `terminated`, or `Exists:false` once the instance is fully gone. 
Result was as expected.


### 2.11 Full teardown cleanup (stop all ongoing costs; removes everything listed)
#### 2.11.1 Disable the schedule (prevents further `invocations`) (JSONL)
##### 2.11.1.1 Resolve `RULE_NAME` from the `stack` (JSONL)
```bash
RULE_NAME=$(aws cloudformation describe-stacks --stack-name "$SAM_STACK_NAME" --region "$AWS_REGION" --no-cli-pager 2>/dev/null | jq -r '.Stacks[0].Outputs[]? | select(.OutputKey=="ScheduleRuleName") | .OutputValue' | head -n 1); 
jq -c -n --arg RuleName "${RULE_NAME:-}" '{RuleName:$RuleName}'
```
**Example output**
```json
{"RuleName":"ManageEIPs-1region-SAM-ScheduleMonthly"}
```
##### 2.11.1.2 Disable `rule` (JSONL result)
```bash
aws events disable-rule --name "$RULE_NAME" --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg RuleName "$RULE_NAME" '{Action:"DisableRule",RuleName:$RuleName,Result:"DISABLED"}' || jq -c -n --arg RuleName "${RULE_NAME:-}" '{Action:"DisableRule",RuleName:$RuleName,Result:"FAILED_OR_NOT_FOUND"}'
```
**Example output**
```json
{"Action":"DisableRule","RuleName":"ManageEIPs-1region-SAM-ScheduleMonthly","Result":"DISABLED"}
```

##### 2.11.1.3 Verify `rule state` (JSONL)
```bash
aws events describe-rule --name "$RULE_NAME" --region "$AWS_REGION" --no-cli-pager 2>/dev/null | jq -c '{RuleName:(.Name//""),State:(.State//""),ScheduleExpression:(.ScheduleExpression//"")}' || jq -c -n --arg RuleName "${RULE_NAME:-}" '{RuleName:$RuleName,Exists:false}'
```
**Example output**
```json
{"RuleName":"ManageEIPs-1region-SAM-ScheduleMonthly","State":"DISABLED","ScheduleExpression":"cron(0 21 30 * ? *)"}
```

#### 2.11.2 Delete the `SAM stack` (removes `Lambda`, `EventBridge`, `SNS` created by the stack) (JSONL)
##### 2.11.2.1 Delete `stack` (JSONL result)
```bash
aws cloudformation delete-stack --stack-name "$SAM_STACK_NAME" --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg StackName "$SAM_STACK_NAME" '{Action:"DeleteStack",StackName:$StackName,Result:"DELETE_REQUESTED"}' || jq -c -n --arg StackName "${SAM_STACK_NAME:-}" '{Action:"DeleteStack",StackName:$StackName,Result:"FAILED_OR_NOT_FOUND"}'
```
**Example output**
```json
{"Action":"DeleteStack","StackName":"ManageEIPs-1region-SAM","Result":"DELETE_REQUESTED"}
```

##### 2.11.1.2 Wait for deletion (JSONL result)
```bash
aws cloudformation wait stack-delete-complete --stack-name "$SAM_STACK_NAME" --region "$AWS_REGION" >/dev/null 2>&1 && jq -c -n --arg StackName "$SAM_STACK_NAME" '{Action:"WaitStackDeleteComplete",StackName:$StackName,Result:"DELETED"}' || jq -c -n --arg StackName "$SAM_STACK_NAME" '{Action:"WaitStackDeleteComplete",StackName:$StackName,Result:"FAILED_OR_TIMEOUT"}'
```
**Example output**
```json
{"Action":"WaitStackDeleteComplete","StackName":"ManageEIPs-1region-SAM","Result":"DELETED"}
```

##### 2.11.1.3 Verify `stack` is gone (JSONL)
```bash
OUT=$(aws cloudformation describe-stacks --stack-name "$SAM_STACK_NAME" --region "$AWS_REGION" --no-cli-pager 2>/dev/null || true); [ -n "$OUT" ] && echo "$OUT" | jq -c --arg StackName "${SAM_STACK_NAME:-}" '{StackName:(.Stacks[0].StackName//$StackName),StackStatus:(.Stacks[0].StackStatus//""),Exists:true}' || jq -c -n --arg StackName "${SAM_STACK_NAME:-}" '{StackName:$StackName,Exists:false}'
```
**Example output**
```json
{"StackName":"ManageEIPs-1region-SAM","Exists":false}
```

#### 2.11.3 Delete the `SAM artifacts` `S3 bucket` (empty then delete) (JSONL)
##### 2.11.3.1 Empty `bucket` (JSONL result)
```bash
aws s3 rm "s3://$SAM_ARTIFACTS_BUCKET" --recursive --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg Bucket "$SAM_ARTIFACTS_BUCKET" '{Action:"EmptyBucket",Bucket:$Bucket,Result:"EMPTIED"}' || jq -c -n --arg Bucket "${SAM_ARTIFACTS_BUCKET:-}" '{Action:"EmptyBucket",Bucket:$Bucket,Result:"FAILED_OR_NOT_FOUND"}'
```
**Example output**
```json
{"Action":"EmptyBucket","Bucket":"","Result":"FAILED_OR_NOT_FOUND"}
```

**Re-export the `bucket` as it was not found (derives `AWS_ACCOUNT_ID` if missing; JSONL confirm)**
```bash
[ -n "${AWS_ACCOUNT_ID:-}" ] || AWS_ACCOUNT_ID=$(aws sts get-caller-identity --region "$AWS_REGION" --no-cli-pager | jq -r '.Account'); export AWS_ACCOUNT_ID; export SAM_ARTIFACTS_BUCKET="manageeips-sam-artifacts-${AWS_ACCOUNT_ID}-${AWS_REGION}"; 
jq -c -n --arg Bucket "$SAM_ARTIFACTS_BUCKET" --arg Region "$AWS_REGION" --arg AccountId "$AWS_ACCOUNT_ID" '{Action:"SetArtifactsBucket",Bucket:$Bucket,Region:$Region,AWS_ACCOUNT_ID:$AccountId}'
```
**Example output**
```json
{"Action":"SetArtifactsBucket","Bucket":"manageeips-sam-artifacts-180294215772-us-east-1","Region":"us-east-1","AWS_ACCOUNT_ID":"180294215772"}
```

**Verify the bucket exists (JSONL)**
```bash
aws s3api head-bucket --bucket "$SAM_ARTIFACTS_BUCKET" --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg Bucket "$SAM_ARTIFACTS_BUCKET" '{Bucket:$Bucket,Exists:true}' || jq -c -n --arg Bucket "$SAM_ARTIFACTS_BUCKET" '{Bucket:$Bucket,Exists:false}'
```
**Example output**
```json
{"Bucket":"manageeips-sam-artifacts-180294215772-us-east-1","Exists":true}
```

**Empty the `bucket` (JSONL)**
```bash
aws s3 rm "s3://$SAM_ARTIFACTS_BUCKET" --recursive --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg Bucket "$SAM_ARTIFACTS_BUCKET" '{Action:"EmptyBucket",Bucket:$Bucket,Result:"EMPTIED"}' || jq -c -n --arg Bucket "$SAM_ARTIFACTS_BUCKET" '{Action:"EmptyBucket",Bucket:$Bucket,Result:"FAILED"}'
```
**Example output**
```json
{"Action":"EmptyBucket","Bucket":"manageeips-sam-artifacts-180294215772-us-east-1","Result":"EMPTIED"}
```

##### 2.11.3.2 Delete `bucket` (JSONL result)
```bash
aws s3api delete-bucket --bucket "$SAM_ARTIFACTS_BUCKET" --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg Bucket "$SAM_ARTIFACTS_BUCKET" '{Action:"DeleteBucket",Bucket:$Bucket,Result:"DELETED"}' || jq -c -n --arg Bucket "${SAM_ARTIFACTS_BUCKET:-}" '{Action:"DeleteBucket",Bucket:$Bucket,Result:"FAILED_OR_NOT_EMPTY_OR_NOT_FOUND"}'
```
**Example output**
```json
{"Action":"DeleteBucket","Bucket":"manageeips-sam-artifacts-180294215772-us-east-1","Result":"DELETED"}
```

##### 2.11.3.3 Verify `bucket` is gone (JSONL)
```bash
aws s3api head-bucket --bucket "$SAM_ARTIFACTS_BUCKET" --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg Bucket "$SAM_ARTIFACTS_BUCKET" '{Bucket:$Bucket,Exists:true}' || jq -c -n --arg Bucket "$SAM_ARTIFACTS_BUCKET" '{Bucket:$Bucket,Exists:false}'
```
**Example output**
```json
{"Bucket":"manageeips-sam-artifacts-180294215772-us-east-1","Exists":false}
```
**Conclusion**
The output shows that the `bucket` doesn't exist anymore. This is also confirmed by checking in the AWS console.

#### 2.11.4 Delete leftover `CloudWatch artifacts` (`alarms`, `dashboards`, `log groups`) (JSONL)
##### 2.11.4.1 Delete `alarms` matching the `project name` (JSONL)
```bash
ALARM_NAMES=$(aws cloudwatch describe-alarms --region "$AWS_REGION" --no-cli-pager | jq -r '.MetricAlarms[]? | .AlarmName | select(test("ManageEIPs|NW-EC2-EIP-Test|Custom/FinOps";"i"))' | paste -sd' ' -); [ -n "$ALARM_NAMES" ] && aws cloudwatch delete-alarms --region "$AWS_REGION" --no-cli-pager --alarm-names $ALARM_NAMES >/dev/null 2>&1 && jq -c -n --arg Deleted "$ALARM_NAMES" '{Action:"DeleteAlarms",DeletedAlarmNames:$Deleted,Result:"DELETED"}' || jq -c -n --arg Deleted "${ALARM_NAMES:-}" '{Action:"DeleteAlarms",DeletedAlarmNames:$Deleted,Result:"NONE_OR_FAILED"}'
```
**Example output**
```json
{"Action":"DeleteAlarms","DeletedAlarmNames":"ManageEIPs-DurationHigh ManageEIPs-Errors ManageEIPs-Throttles","Result":"DELETED"}
```

##### 2.11.4.2 Delete `dashboards` matching the `project name` (JSONL)
```bash
DASH_NAMES=$(aws cloudwatch list-dashboards --region "$AWS_REGION" --no-cli-pager | jq -r '.DashboardEntries[]? | .DashboardName | select(test("ManageEIPs|NW-EC2-EIP-Test";"i"))' | paste -sd' ' -); [ -n "$DASH_NAMES" ] && printf '%s\n' "$DASH_NAMES" | tr ' ' '\n' | while read -r d; do aws cloudwatch delete-dashboards --region "$AWS_REGION" --no-cli-pager --dashboard-names "$d" >/dev/null 2>&1 && jq -c -n --arg DashboardName "$d" '{Action:"DeleteDashboard",DashboardName:$DashboardName,Result:"DELETED"}' || jq -c -n --arg DashboardName "$d" '{Action:"DeleteDashboard",DashboardName:$DashboardName,Result:"FAILED"}'; done || jq -c -n --arg Deleted "${DASH_NAMES:-}" '{Action:"DeleteDashboards",DeletedDashboardNames:$Deleted,Result:"NONE_OR_FAILED"}'
```
**Example output**
```json
{"Action":"DeleteDashboard","DashboardName":"ManageEIPs-Dashboard","Result":"DELETED"}
```

##### 2.11.4.3 Delete `Lambda log groups` (JSONL per `log group`)
```bash
aws logs describe-log-groups --region "$AWS_REGION" --no-cli-pager --log-group-name-prefix "/aws/lambda/" | jq -r '.logGroups[]? | .logGroupName | select(test("ManageEIPs";"i"))' | while read -r lg; do aws logs delete-log-group --region "$AWS_REGION" --no-cli-pager --log-group-name "$lg" >/dev/null 2>&1 && jq -c -n --arg LogGroupName "$lg" '{Action:"DeleteLogGroup",LogGroupName:$LogGroupName,Result:"DELETED"}' || jq -c -n --arg LogGroupName "$lg" '{Action:"DeleteLogGroup",LogGroupName:$LogGroupName,Result:"FAILED"}'; 
done
```
**Example output**
```json
{"Action":"DeleteLogGroup","LogGroupName":"/aws/lambda/ManageEIPs","Result":"DELETED"}
{"Action":"DeleteLogGroup","LogGroupName":"/aws/lambda/ManageEIPs-1region-SAM-Lambda","Result":"DELETED"}
```

#### 2.11.5 Delete leftover `IAM` (`customer-managed policies` + `roles`) (JSONL)
##### 2.11.5.1 Delete `customer-managed IAM policies` matching `project` (detach first; then delete)(JSONL)
```bash
aws iam list-policies --scope Local --no-cli-pager | jq -r '.Policies[]? | select(.PolicyName|test("ManageEIPs|NW-EC2-EIP-Test";"i")) | [.PolicyName,.Arn] | @tsv' | while IFS=$'\t' read -r pn pa; 
do for rn in $(aws iam list-entities-for-policy --policy-arn "$pa" --entity-filter Role --no-cli-pager | jq -r '.PolicyRoles[]?.RoleName'); 
do aws iam detach-role-policy --role-name "$rn" --policy-arn "$pa" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg PolicyName "$pn" --arg PolicyArn "$pa" --arg RoleName "$rn" '{Action:"DetachRolePolicy",PolicyName:$PolicyName,PolicyArn:$PolicyArn,RoleName:$RoleName,Result:"DETACHED"}' || jq -c -n --arg PolicyName "$pn" --arg PolicyArn "$pa" --arg RoleName "$rn" '{Action:"DetachRolePolicy",PolicyName:$PolicyName,PolicyArn:$PolicyArn,RoleName:$RoleName,Result:"FAILED"}'; 
done; 
for gn in $(aws iam list-entities-for-policy --policy-arn "$pa" --entity-filter Group --no-cli-pager | jq -r '.PolicyGroups[]?.GroupName'); 
do aws iam detach-group-policy --group-name "$gn" --policy-arn "$pa" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg PolicyName "$pn" --arg PolicyArn "$pa" --arg GroupName "$gn" '{Action:"DetachGroupPolicy",PolicyName:$PolicyName,PolicyArn:$PolicyArn,GroupName:$GroupName,Result:"DETACHED"}' || jq -c -n --arg PolicyName "$pn" --arg PolicyArn "$pa" --arg GroupName "$gn" '{Action:"DetachGroupPolicy",PolicyName:$PolicyName,PolicyArn:$PolicyArn,GroupName:$GroupName,Result:"FAILED"}'; 
done; 
for un in $(aws iam list-entities-for-policy --policy-arn "$pa" --entity-filter User --no-cli-pager | jq -r '.PolicyUsers[]?.UserName'); 
do aws iam detach-user-policy --user-name "$un" --policy-arn "$pa" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg PolicyName "$pn" --arg PolicyArn "$pa" --arg UserName "$un" '{Action:"DetachUserPolicy",PolicyName:$PolicyName,PolicyArn:$PolicyArn,UserName:$UserName,Result:"DETACHED"}' || jq -c -n --arg PolicyName "$pn" --arg PolicyArn "$pa" --arg UserName "$un" '{Action:"DetachUserPolicy",PolicyName:$PolicyName,PolicyArn:$PolicyArn,UserName:$UserName,Result:"FAILED"}'; 
done; 
aws iam list-policy-versions --policy-arn "$pa" --no-cli-pager | jq -r '.Versions[]? | select(.IsDefaultVersion==false) | .VersionId' | while read -r vid; 
do aws iam delete-policy-version --policy-arn "$pa" --version-id "$vid" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg PolicyName "$pn" --arg PolicyArn "$pa" --arg VersionId "$vid" '{Action:"DeletePolicyVersion",PolicyName:$PolicyName,PolicyArn:$PolicyArn,VersionId:$VersionId,Result:"DELETED"}' || jq -c -n --arg PolicyName "$pn" --arg PolicyArn "$pa" --arg VersionId "$vid" '{Action:"DeletePolicyVersion",PolicyName:$PolicyName,PolicyArn:$PolicyArn,VersionId:$VersionId,Result:"FAILED"}'; 
done; 
aws iam delete-policy --policy-arn "$pa" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg PolicyName "$pn" --arg PolicyArn "$pa" '{Action:"DeletePolicy",PolicyName:$PolicyName,PolicyArn:$PolicyArn,Result:"DELETED"}' || jq -c -n --arg PolicyName "$pn" --arg PolicyArn "$pa" '{Action:"DeletePolicy",PolicyName:$PolicyName,PolicyArn:$PolicyArn,Result:"FAILED_OR_IN_USE"}'; 
done
```
**Example output**
```json
{"Action":"DetachRolePolicy","PolicyName":"ManageEIPs-EC2ReleasePolicy","PolicyArn":"arn:aws:iam::180294215772:policy/ManageEIPs-EC2ReleasePolicy","RoleName":"ManageEIPsLambdaRole","Result":"DETACHED"}
{"Action":"DeletePolicy","PolicyName":"ManageEIPs-EC2ReleasePolicy","PolicyArn":"arn:aws:iam::180294215772:policy/ManageEIPs-EC2ReleasePolicy","Result":"DELETED"}
{"Action":"DetachRolePolicy","PolicyName":"ManageEIPs-EC2DescribePolicy","PolicyArn":"arn:aws:iam::180294215772:policy/ManageEIPs-EC2DescribePolicy","RoleName":"ManageEIPsLambdaRole","Result":"DETACHED"}
{"Action":"DeletePolicy","PolicyName":"ManageEIPs-EC2DescribePolicy","PolicyArn":"arn:aws:iam::180294215772:policy/ManageEIPs-EC2DescribePolicy","Result":"DELETED"}
```

##### 2.11.5.2 Delete `IAM roles` matching this `project` (detach `policies`, delete `inline policies`, remove from `instance profiles`, delete `role`) (JSONL per action)
```bash
aws iam list-roles --no-cli-pager | jq -r '.Roles[]? | select(.RoleName|test("ManageEIPs|NW-EC2-EIP-Test";"i")) | [.RoleName,.Arn] | @tsv' | while IFS=$'\t' read -r rn ra; 
do aws iam list-attached-role-policies --role-name "$rn" --no-cli-pager | jq -r '.AttachedPolicies[]? | .PolicyArn' | while read -r pa; 
do aws iam detach-role-policy --role-name "$rn" --policy-arn "$pa" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg RoleName "$rn" --arg RoleArn "$ra" --arg PolicyArn "$pa" '{Action:"DetachRolePolicy",RoleName:$RoleName,RoleArn:$RoleArn,PolicyArn:$PolicyArn,Result:"DETACHED"}' || jq -c -n --arg RoleName "$rn" --arg RoleArn "$ra" --arg PolicyArn "$pa" '{Action:"DetachRolePolicy",RoleName:$RoleName,RoleArn:$RoleArn,PolicyArn:$PolicyArn,Result:"FAILED"}'; 
done; 
aws iam list-role-policies --role-name "$rn" --no-cli-pager | jq -r '.PolicyNames[]?' | while read -r ipn; 
do aws iam delete-role-policy --role-name "$rn" --policy-name "$ipn" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg RoleName "$rn" --arg RoleArn "$ra" --arg InlinePolicyName "$ipn" '{Action:"DeleteInlineRolePolicy",RoleName:$RoleName,RoleArn:$RoleArn,InlinePolicyName:$InlinePolicyName,Result:"DELETED"}' || jq -c -n --arg RoleName "$rn" --arg RoleArn "$ra" --arg InlinePolicyName "$ipn" '{Action:"DeleteInlineRolePolicy",RoleName:$RoleName,RoleArn:$RoleArn,InlinePolicyName:$InlinePolicyName,Result:"FAILED"}'; 
done; 
aws iam list-instance-profiles-for-role --role-name "$rn" --no-cli-pager | jq -r '.InstanceProfiles[]?.InstanceProfileName' | while read -r ip; 
do aws iam remove-role-from-instance-profile --instance-profile-name "$ip" --role-name "$rn" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg RoleName "$rn" --arg RoleArn "$ra" --arg InstanceProfileName "$ip" '{Action:"RemoveRoleFromInstanceProfile",RoleName:$RoleName,RoleArn:$RoleArn,InstanceProfileName:$InstanceProfileName,Result:"REMOVED"}' || jq -c -n --arg RoleName "$rn" --arg RoleArn "$ra" --arg InstanceProfileName "$ip" '{Action:"RemoveRoleFromInstanceProfile",RoleName:$RoleName,RoleArn:$RoleArn,InstanceProfileName:$InstanceProfileName,Result:"FAILED"}'; 
aws iam delete-instance-profile --instance-profile-name "$ip" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg InstanceProfileName "$ip" '{Action:"DeleteInstanceProfile",InstanceProfileName:$InstanceProfileName,Result:"DELETED"}' || jq -c -n --arg InstanceProfileName "$ip" '{Action:"DeleteInstanceProfile",InstanceProfileName:$InstanceProfileName,Result:"SKIPPED_OR_FAILED"}'; 
done; 
aws iam delete-role --role-name "$rn" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg RoleName "$rn" --arg RoleArn "$ra" '{Action:"DeleteRole",RoleName:$RoleName,RoleArn:$RoleArn,Result:"DELETED"}' || jq -c -n --arg RoleName "$rn" --arg RoleArn "$ra" '{Action:"DeleteRole",RoleName:$RoleName,RoleArn:$RoleArn,Result:"FAILED_OR_IN_USE"}'; 
done
```
**Example output**
```json
{"Action":"DetachRolePolicy","RoleName":"ManageEIPs-EC2SSMRole","RoleArn":"arn:aws:iam::180294215772:role/ManageEIPs-EC2SSMRole","PolicyArn":"arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy","Result":"DETACHED"}
{"Action":"DetachRolePolicy","RoleName":"ManageEIPs-EC2SSMRole","RoleArn":"arn:aws:iam::180294215772:role/ManageEIPs-EC2SSMRole","PolicyArn":"arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore","Result":"DETACHED"}
{"Action":"RemoveRoleFromInstanceProfile","RoleName":"ManageEIPs-EC2SSMRole","RoleArn":"arn:aws:iam::180294215772:role/ManageEIPs-EC2SSMRole","InstanceProfileName":"ManageEIPs-EC2SSMInstanceProfile","Result":"REMOVED"}
{"Action":"DeleteInstanceProfile","InstanceProfileName":"ManageEIPs-EC2SSMInstanceProfile","Result":"DELETED"}
{"Action":"DeleteRole","RoleName":"ManageEIPs-EC2SSMRole","RoleArn":"arn:aws:iam::180294215772:role/ManageEIPs-EC2SSMRole","Result":"DELETED"}
{"Action":"DetachRolePolicy","RoleName":"ManageEIPsLambdaRole","RoleArn":"arn:aws:iam::180294215772:role/ManageEIPsLambdaRole","PolicyArn":"arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole","Result":"DETACHED"}
{"Action":"DeleteRole","RoleName":"ManageEIPsLambdaRole","RoleArn":"arn:aws:iam::180294215772:role/ManageEIPsLambdaRole","Result":"DELETED"}
{"Action":"DetachRolePolicy","RoleName":"NW-EC2-EIP-Test-EC2SSMRole","RoleArn":"arn:aws:iam::180294215772:role/NW-EC2-EIP-Test-EC2SSMRole","PolicyArn":"arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore","Result":"DETACHED"}
{"Action":"RemoveRoleFromInstanceProfile","RoleName":"NW-EC2-EIP-Test-EC2SSMRole","RoleArn":"arn:aws:iam::180294215772:role/NW-EC2-EIP-Test-EC2SSMRole","InstanceProfileName":"NW-EC2-EIP-Test-InstanceProfile","Result":"REMOVED"}
{"Action":"DeleteInstanceProfile","InstanceProfileName":"NW-EC2-EIP-Test-InstanceProfile","Result":"DELETED"}
{"Action":"DeleteRole","RoleName":"NW-EC2-EIP-Test-EC2SSMRole","RoleArn":"arn:aws:iam::180294215772:role/NW-EC2-EIP-Test-EC2SSMRole","Result":"DELETED"}
```

##### 2.11.5.3 Verify `IAM` cleanup (no matching `policies/roles` remain) (JSONL)
```bash
aws iam list-policies --scope Local --no-cli-pager | jq -c '{Check:"CustomerManagedPoliciesRemaining",MatchCount:([.Policies[]? | select(.PolicyName|test("ManageEIPs|NW-EC2-EIP-Test";"i"))] | length)}'; aws iam list-roles --no-cli-pager | jq -c '{Check:"RolesRemaining",MatchCount:([.Roles[]? | select(.RoleName|test("ManageEIPs|NW-EC2-EIP-Test";"i"))] | length)}'
```
**Example output**
```json
{"Check":"CustomerManagedPoliciesRemaining","MatchCount":0}
{"Check":"RolesRemaining","MatchCount":0}
```

#### 2.11.6 Delete the `network baseline` (`endpoints/ENIs`, `SGs`, `RT`, `IGW`, `subnets`, `VPC`) (JSONL)
##### 2.11.6.1 Resolve `network resource IDs` (sets variables; JSONL)
###### 2.11.6.1.1 Resolve `VPC_ID` by `Name tag` (JSONL; uses `include` `flatten_tags`)
```bash
VPC_ID=$(aws ec2 describe-vpcs --region "$AWS_REGION" --no-cli-pager | jq -r -L . 'include "flatten_tags"; .Vpcs[]? | (flatten_tags(.VpcId) + {VpcId:.VpcId,NameTag:(((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"")}) | select(.NameTag=="VPC_NW-EC2-EIP-Test") | .VpcId' | head -n 1); 
jq -c -n --arg VpcId "${VPC_ID:-}" --arg NameTag "VPC_NW-EC2-EIP-Test" '{VpcId:$VpcId,NameTag:$NameTag}'
```
**Example output**
```json
{"VpcId":"vpc-0757dfccfbd169bd8","NameTag":"VPC_NW-EC2-EIP-Test"}
```

###### 2.11.6.1.2 Resolve `IGW_ID` attached to `VPC_ID` (sets `IGW_ID`; JSONL)
```bash
IGW_ID=$(aws ec2 describe-internet-gateways --region "$AWS_REGION" --no-cli-pager --filters "Name=attachment.vpc-id,Values=$VPC_ID" | jq -r '.InternetGateways[]? | .InternetGatewayId' | head -n 1); 
IGW_NAME=$(aws ec2 describe-internet-gateways --region "$AWS_REGION" --no-cli-pager --internet-gateway-ids "$IGW_ID" 2>/dev/null | jq -r '.InternetGateways[0].Tags//[] | map(select(.Key=="Name")|.Value) | .[0] // ""'); 
jq -c -n --arg InternetGatewayId "${IGW_ID:-}" --arg NameTag "${IGW_NAME:-}" '{InternetGatewayId:$InternetGatewayId,NameTag:$NameTag}'
```
**Example output**
```json
{"InternetGatewayId":"igw-04a4c28ec13304061","NameTag":"IGW_NW-EC2-EIP-Test"}
```

###### 2.11.6.1.3 Resolve `RT_PUBLIC_ID` by `VpcId` + `NameTag` match (sets `RT_PUBLIC_ID`; JSONL)
```bash
RT_PUBLIC_ID=$(aws ec2 describe-route-tables --region "$AWS_REGION" --no-cli-pager --filters "Name=vpc-id,Values=$VPC_ID" | jq -r '.RouteTables[]? | ((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0]//"") as $NameTag | select(($NameTag|test("Public|RT_Public|NW-EC2-EIP-Test";"i")) and ([.Associations[]? | select(.Main==true)]|length)==0) | .RouteTableId' | head -n 1);
RT_PUBLIC_NAME=$(aws ec2 describe-route-tables --region "$AWS_REGION" --no-cli-pager --route-table-ids "$RT_PUBLIC_ID" 2>/dev/null | jq -r '.RouteTables[0].Tags//[] | map(select(.Key=="Name")|.Value) | .[0] // ""'); 
jq -c -n --arg RouteTableId "${RT_PUBLIC_ID:-}" --arg NameTag "${RT_PUBLIC_NAME:-}" '{RouteTableId:$RouteTableId,NameTag:$NameTag}'
```
**Example output**
```json
{"RouteTableId":"rtb-01b855a7a6a13b4a7","NameTag":"RT_Public_NW-EC2-EIP-Test"}
```

###### 2.11.6.1.4 Resolve `SG_EC2_ID` and `SG_EP_ID` by `NameTag` (sets `SG_EC2_ID,SG_EP_ID`; JSONL)
```bash
SG_EC2_ID=$(aws ec2 describe-security-groups --region "$AWS_REGION" --no-cli-pager --filters "Name=vpc-id,Values=$VPC_ID" | jq -r '.SecurityGroups[]? | ((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0]//"") as $NameTag | select($NameTag=="SG_EC2_NW-EC2-EIP-Test") | .GroupId' | head -n 1); 
SG_EP_ID=$(aws ec2 describe-security-groups --region "$AWS_REGION" --no-cli-pager --filters "Name=vpc-id,Values=$VPC_ID" | jq -r '.SecurityGroups[]? | ((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0]//"") as $NameTag | select($NameTag=="SG_Endpoints_SSM_NW-EC2-EIP-Test") | .GroupId' | head -n 1); 
jq -c -n --arg SG_EC2_ID "${SG_EC2_ID:-}" --arg SG_EC2_NameTag "SG_EC2_NW-EC2-EIP-Test" --arg SG_EP_ID "${SG_EP_ID:-}" --arg SG_EP_NameTag "SG_Endpoints_SSM_NW-EC2-EIP-Test" '{SG_EC2_ID:$SG_EC2_ID,SG_EC2_NameTag:$SG_EC2_NameTag,SG_EP_ID:$SG_EP_ID,SG_EP_NameTag:$SG_EP_NameTag}'
```
**Example output**
```json
{"SG_EC2_ID":"sg-097e258d5cdbdc0e0","SG_EC2_NameTag":"SG_EC2_NW-EC2-EIP-Test","SG_EP_ID":"sg-05e8e4c3756c6044a","SG_EP_NameTag":"SG_Endpoints_SSM_NW-EC2-EIP-Test"}
```

##### 2.11.6.2 Delete `VPC interface endpoints` (this also removes `endpoint ENIs`) (JSONL)
###### 2.11.6.2.1 Resolve all `endpoint IDs` for this test VPC (sets `VPCE_IDS`; JSONL)
```bash
VPCE_IDS=$(aws ec2 describe-vpc-endpoints --region "$AWS_REGION" --no-cli-pager --filters "Name=vpc-id,Values=$VPC_ID" | jq -r '.VpcEndpoints[]? | ((.Tags//[])|map(select(.Key=="GroupName")|.Value)|.[0]//"") as $GroupName | select($GroupName=="NW-EC2-EIP-Test") | .VpcEndpointId' | paste -sd' ' -); 
aws ec2 describe-vpc-endpoints --region "$AWS_REGION" --no-cli-pager --vpc-endpoint-ids ${VPCE_IDS:-} 2>/dev/null | jq -c '.VpcEndpoints[]? | ((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0]//"MISSING-VPCE-NAME") as $NameTag | {VpcEndpointId:(.VpcEndpointId//""),NameTag:$NameTag,ServiceName:(.ServiceName//""),State:(.State//"")} | with_entries(select(.value!=""))' || jq -c -n --arg VpcId "$VPC_ID" '{VpcId:$VpcId,Result:"NO_VPCE_FOUND"}'
```
**Example output**
```json
{"VpcEndpointId":"vpce-032a7f26278e58657","NameTag":"VPCE_SSM_NW-EC2-EIP-Test","ServiceName":"com.amazonaws.us-east-1.ssm","State":"available"}
{"VpcEndpointId":"vpce-07a790f816f2e056f","NameTag":"VPCE_EC2Messages_NW-EC2-EIP-Test","ServiceName":"com.amazonaws.us-east-1.ec2messages","State":"available"} 
{"VpcEndpointId":"vpce-00bcd0544364db667","NameTag":"VPCE_SSMMessages_NW-EC2-EIP-Test","ServiceName":"com.amazonaws.us-east-1.ssmmessages","State":"available"}
```

###### 2.11.6.2.2 Delete `endpoints (JSONL result)`
```bash
[ -n "${VPCE_IDS:-}" ] && aws ec2 delete-vpc-endpoints --vpc-endpoint-ids $VPCE_IDS --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg VPCE_IDS "$VPCE_IDS" '{Action:"DeleteVpcEndpoints",VpcEndpointIds:$VPCE_IDS,Result:"DELETE_REQUESTED"}' || jq -c -n --arg VPCE_IDS "${VPCE_IDS:-}" '{Action:"DeleteVpcEndpoints",VpcEndpointIds:$VPCE_IDS,Result:"NONE_OR_FAILED"}'
```
**Example output**
```json
{"Action":"DeleteVpcEndpoints","VpcEndpointIds":"vpce-032a7f26278e58657 vpce-07a790f816f2e056f vpce-00bcd0544364db667","Result":"DELETE_REQUESTED"}
```

###### 2.11.6.2.3 Wait until no `endpoints` remain (JSONL)
```bash
COUNT=$(aws ec2 describe-vpc-endpoints --region "$AWS_REGION" --no-cli-pager --filters "Name=vpc-id,Values=$VPC_ID" | jq -r '[.VpcEndpoints[]? | ((.Tags//[])|map(select(.Key=="GroupName")|.Value)|.[0]//"") as $GroupName | select($GroupName=="NW-EC2-EIP-Test")] | length'); 
jq -c -n --arg VpcId "$VPC_ID" --argjson Remaining "$COUNT" '{VpcId:$VpcId,RemainingVpcEndpoints:$Remaining}'
```
**Example output**
```json
{"VpcId":"vpc-0757dfccfbd169bd8","RemainingVpcEndpoints":3}
```

##### 2.11.6.3 Delete `routing` and `IGW` (`route-table association` → `route table` → `IGW`) (JSONL)
###### 2.11.6.3.1 Disassociate non-main `route table associations` (JSONL per disassociation)
```bash
aws ec2 describe-route-tables --region "$AWS_REGION" --no-cli-pager --route-table-ids "$RT_PUBLIC_ID" 2>/dev/null | jq -r '.RouteTables[0].Associations[]? | select(.Main!=true) | [.RouteTableAssociationId,(.SubnetId//"")] | @tsv' | while IFS=$'\t' read -r assoc sid; 
do sname=$(aws ec2 describe-subnets --region "$AWS_REGION" --no-cli-pager --subnet-ids "$sid" 2>/dev/null | jq -r '.Subnets[0].Tags//[] | map(select(.Key=="Name")|.Value) | .[0] // ""'); aws ec2 disassociate-route-table --association-id "$assoc" --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg RouteTableId "$RT_PUBLIC_ID" --arg AssociationId "$assoc" --arg SubnetId "$sid" --arg SubnetNameTag "$sname" '{Action:"DisassociateRouteTable",RouteTableId:$RouteTableId,AssociationId:$AssociationId,SubnetId:$SubnetId,SubnetNameTag:$SubnetNameTag,Result:"DISASSOCIATED"}' || jq -c -n --arg RouteTableId "$RT_PUBLIC_ID" --arg AssociationId "$assoc" --arg SubnetId "$sid" --arg SubnetNameTag "$sname" '{Action:"DisassociateRouteTable",RouteTableId:$RouteTableId,AssociationId:$AssociationId,SubnetId:$SubnetId,SubnetNameTag:$SubnetNameTag,Result:"FAILED"}'; 
done
```
**Example output**
```json
{"Action":"DisassociateRouteTable","RouteTableId":"rtb-01b855a7a6a13b4a7","AssociationId":"rtbassoc-0f8ad59ee8b80fcca","SubnetId":"subnet-074c3e8e1490cbb60","SubnetNameTag":"Subnet_Public_AZ1_NW-EC2-EIP-Test","Result":"DISASSOCIATED"}
```

###### 2.11.6.3.2 Delete `public route table` (JSONL result)
```bash
aws ec2 delete-route-table --route-table-id "$RT_PUBLIC_ID" --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg RouteTableId "$RT_PUBLIC_ID" --arg NameTag "${RT_PUBLIC_NAME:-}" '{Action:"DeleteRouteTable",RouteTableId:$RouteTableId,NameTag:$NameTag,Result:"DELETED"}' || jq -c -n --arg RouteTableId "${RT_PUBLIC_ID:-}" --arg NameTag "${RT_PUBLIC_NAME:-}" '{Action:"DeleteRouteTable",RouteTableId:$RouteTableId,NameTag:$NameTag,Result:"FAILED_OR_IN_USE_OR_NOT_FOUND"}'
```
**Example output**
```json
{"Action":"DeleteRouteTable","RouteTableId":"rtb-01b855a7a6a13b4a7","NameTag":"RT_Public_NW-EC2-EIP-Test","Result":"DELETED"}
```

###### 2.11.6.3.3 Detach `IGW` from `VPC` (JSONL result)
```bash
aws ec2 detach-internet-gateway --internet-gateway-id "$IGW_ID" --vpc-id "$VPC_ID" --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg InternetGatewayId "$IGW_ID" --arg NameTag "${IGW_NAME:-}" --arg VpcId "$VPC_ID" '{Action:"DetachIgw",InternetGatewayId:$InternetGatewayId,NameTag:$NameTag,VpcId:$VpcId,Result:"DETACHED"}' || jq -c -n --arg InternetGatewayId "${IGW_ID:-}" --arg NameTag "${IGW_NAME:-}" --arg VpcId "${VPC_ID:-}" '{Action:"DetachIgw",InternetGatewayId:$InternetGatewayId,NameTag:$NameTag,VpcId:$VpcId,Result:"FAILED_OR_NOT_FOUND"}'
```
**Example output**
```json
{"Action":"DetachIgw","InternetGatewayId":"igw-04a4c28ec13304061","NameTag":"IGW_NW-EC2-EIP-Test","VpcId":"vpc-0757dfccfbd169bd8","Result":"DETACHED"}
```

###### 2.11.6.3.4 Delete `IGW` (JSONL result)
```bash
aws ec2 delete-internet-gateway --internet-gateway-id "$IGW_ID" --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg InternetGatewayId "$IGW_ID" --arg NameTag "${IGW_NAME:-}" '{Action:"DeleteIgw",InternetGatewayId:$InternetGatewayId,NameTag:$NameTag,Result:"DELETED"}' || jq -c -n --arg InternetGatewayId "${IGW_ID:-}" --arg NameTag "${IGW_NAME:-}" '{Action:"DeleteIgw",InternetGatewayId:$InternetGatewayId,NameTag:$NameTag,Result:"FAILED_OR_IN_USE_OR_NOT_FOUND"}'
```
**Example output**
```json
{"Action":"DeleteIgw","InternetGatewayId":"igw-04a4c28ec13304061","NameTag":"IGW_NW-EC2-EIP-Test","Result":"DELETED"}
```

##### 2.11.6.4 Delete `security groups` (`endpoint SG` first, then `EC2 SG`) (JSONL)
###### 2.11.6.4.1 Delete `endpoint SG SG_EP_ID` (JSONL result)
```bash
aws ec2 delete-security-group --group-id "$SG_EP_ID" --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg GroupId "$SG_EP_ID" --arg NameTag "SG_Endpoints_SSM_NW-EC2-EIP-Test" '{Action:"DeleteSecurityGroup",GroupId:$GroupId,NameTag:$NameTag,Result:"DELETED"}' || jq -c -n --arg GroupId "${SG_EP_ID:-}" --arg NameTag "SG_Endpoints_SSM_NW-EC2-EIP-Test" '{Action:"DeleteSecurityGroup",GroupId:$GroupId,NameTag:$NameTag,Result:"FAILED_OR_IN_USE_OR_NOT_FOUND"}'
```
**Example output**
```json
{"Action":"DeleteSecurityGroup","GroupId":"sg-05e8e4c3756c6044a","NameTag":"SG_Endpoints_SSM_NW-EC2-EIP-Test","Result":"FAILED_OR_IN_USE_OR_NOT_FOUND"}
```
**Conclusion**
The `SG` couldn't be deleted as it is in use (still in the console). It means it has dependencies.

###### 2.11.6.4.2 Find out why it failed
```bash
ERR=$(aws ec2 delete-security-group --group-id "$SG_EP_ID" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null || true); 
jq -c -n --arg Action "DeleteSecurityGroup" --arg GroupId "$SG_EP_ID" --arg NameTag "SG_Endpoints_SSM_NW-EC2-EIP-Test" --arg Error "$ERR" '{Action:$Action,GroupId:$GroupId,NameTag:$NameTag,Error:$Error}'
```
**Example output**
```json
{"Action":"DeleteSecurityGroup","GroupId":"sg-05e8e4c3756c6044a","NameTag":"SG_Endpoints_SSM_NW-EC2-EIP-Test","Error":"\nAn error occurred (DependencyViolation) when calling the DeleteSecurityGroup operation: resource sg-05e8e4c3756c6044a has a dependent object"}
```
**Conclusion**
The output confirms the existence of a dependent object.

###### 2.11.6.4.2.1 List `VPC interface endpoints` that reference `SG_EP_ID` (JSONL, no arrays)
```bash
aws ec2 describe-vpc-endpoints --region "$AWS_REGION" --no-cli-pager --filters "Name=vpc-id,Values=$VPC_ID" | jq -c --arg SG "$SG_EP_ID" '.VpcEndpoints[]? | select(((.Groups//[])|map(.GroupId)|index($SG))!=null) | ((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0]//"MISSING-VPCE-NAME") as $NameTag | {VpcEndpointId:(.VpcEndpointId//""),NameTag:$NameTag,ServiceName:(.ServiceName//""),State:(.State//""),SecurityGroupIds:((.Groups//[])|map(.GroupId)|join(","))} | with_entries(select(.value!=""))'
```
**Conclusion**
No output: the dependent object is not an `interface endpoint`.

###### 2.11.6.4.2.2 List ENIs that reference `SG_EP_ID` (JSONL, no arrays)
```bash
aws ec2 describe-network-interfaces --region "$AWS_REGION" --no-cli-pager --filters "Name=group-id,Values=$SG_EP_ID" | jq -c '.NetworkInterfaces[]? | ((.TagSet//[])|map(select(.Key=="Name")|.Value)|.[0]//"MISSING-ENI-NAME") as $NameTag | {NetworkInterfaceId:(.NetworkInterfaceId//""),NameTag:$NameTag,Status:(.Status//""),InterfaceType:(.InterfaceType//""),Description:(.Description//""),AttachmentInstanceId:(.Attachment.InstanceId//""),VpcEndpointId:(try (.Description|capture("(?<vpce>vpce-[0-9a-f]+)").vpce) catch "")} | with_entries(select(.value!=""))'
```
**Conclusion**
No output: the dependent object is not an `ENI`.

###### 2.11.6.4.2.3 Show `inbound SG rules` anywhere in the `VPC` that reference `SG_EP_ID` (JSONL)
```bash
aws ec2 describe-security-groups --region "$AWS_REGION" --no-cli-pager --filters "Name=vpc-id,Values=$VPC_ID" | jq -c --arg EP "$SG_EP_ID" '.SecurityGroups[]? as $g | (($g.Tags//[])|map(select(.Key=="Name")|.Value)|.[0]//"MISSING-SG-NAME") as $NameTag | ($g.IpPermissions//[])[]? | select(((.UserIdGroupPairs//[])|map(.GroupId)|index($EP))!=null) | {RefType:"IngressReferences_SG_EP_ID",GroupId:($g.GroupId//""),NameTag:$NameTag,IpProtocol:(.IpProtocol//""),FromPort:(.FromPort//null),ToPort:(.ToPort//null),ReferencedGroupId:$EP,ReferencedGroupNameTag:"SG_Endpoints_SSM_NW-EC2-EIP-Test"} | with_entries(select(.value!="" and .value!=null))'
```
**Conclusion**
No output: the dependent object is not an `inbound SG rule`.

###### 2.11.6.4.2.4 Show `outbound SG rules` anywhere in the `VPC` that reference `SG_EP_ID` (JSONL)
```bash
aws ec2 describe-security-groups --region "$AWS_REGION" --no-cli-pager --filters "Name=vpc-id,Values=$VPC_ID" | jq -c --arg EP "$SG_EP_ID" '.SecurityGroups[]? as $g | (($g.Tags//[])|map(select(.Key=="Name")|.Value)|.[0]//"MISSING-SG-NAME") as $NameTag | ($g.IpPermissionsEgress//[])[]? | select(((.UserIdGroupPairs//[])|map(.GroupId)|index($EP))!=null) | {RefType:"EgressReferences_SG_EP_ID",GroupId:($g.GroupId//""),NameTag:$NameTag,IpProtocol:(.IpProtocol//""),FromPort:(.FromPort//null),ToPort:(.ToPort//null),ReferencedGroupId:$EP,ReferencedGroupNameTag:"SG_Endpoints_SSM_NW-EC2-EIP-Test"} | with_entries(select(.value!="" and .value!=null))'
```
**Example output**
```json
{"RefType":"EgressReferences_SG_EP_ID","GroupId":"sg-097e258d5cdbdc0e0","NameTag":"SG_EC2_NW-EC2-EIP-Test","IpProtocol":"tcp","FromPort":443,"ToPort":443,"ReferencedGroupId":"sg-05e8e4c3756c6044a","ReferencedGroupNameTag":"SG_Endpoints_SSM_NW-EC2-EIP-Test"}
```
**Conclusion**
The dependent object is an `outbound SG rule`.

###### 2.11.6.4.3 Remove the `egress rule` and delete the `Security Group` `endpoint SG SG_EP_ID`
###### 2.11.6.4.3.1 Revoke the dependent `egress rule` (`SG_EC2` → `SG_EP on TCP 443`) (JSONL)
```bash
aws ec2 revoke-security-group-egress --group-id "$SG_EC2_ID" --ip-permissions "$(jq -c -n --arg SG "$SG_EP_ID" '{IpProtocol:"tcp",FromPort:443,ToPort:443,UserIdGroupPairs:[{GroupId:$SG}] }')" --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg GroupId "$SG_EC2_ID" --arg NameTag "SG_EC2_NW-EC2-EIP-Test" --arg RevokedGroupId "$SG_EP_ID" --arg RevokedGroupNameTag "SG_Endpoints_SSM_NW-EC2-EIP-Test" '{Action:"RevokeEgress",GroupId:$GroupId,NameTag:$NameTag,FromPort:443,ToPort:443,Protocol:"tcp",RevokedGroupId:$RevokedGroupId,RevokedGroupNameTag:$RevokedGroupNameTag,Result:"REVOKED_OR_ALREADY"}' || jq -c -n --arg GroupId "$SG_EC2_ID" --arg NameTag "SG_EC2_NW-EC2-EIP-Test" --arg RevokedGroupId "$SG_EP_ID" --arg RevokedGroupNameTag "SG_Endpoints_SSM_NW-EC2-EIP-Test" '{Action:"RevokeEgress",GroupId:$GroupId,NameTag:$NameTag,FromPort:443,ToPort:443,Protocol:"tcp",RevokedGroupId:$RevokedGroupId,RevokedGroupNameTag:$RevokedGroupNameTag,Result:"FAILED"}'
```
**Example output**
```json
{"Action":"RevokeEgress","GroupId":"sg-097e258d5cdbdc0e0","NameTag":"SG_EC2_NW-EC2-EIP-Test","FromPort":443,"ToPort":443,"Protocol":"tcp","RevokedGroupId":"sg-05e8e4c3756c6044a","RevokedGroupNameTag":"SG_Endpoints_SSM_NW-EC2-EIP-Test","Result":"REVOKED_OR_ALREADY"}
```

###### 2.11.6.4.3.2 Delete the `Security Group` `endpoint SG SG_EP_ID`
```bash
aws ec2 delete-security-group --group-id "$SG_EP_ID" --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg GroupId "$SG_EP_ID" --arg NameTag "SG_Endpoints_SSM_NW-EC2-EIP-Test" '{Action:"DeleteSecurityGroup",GroupId:$GroupId,NameTag:$NameTag,Result:"DELETED"}' || (ERR=$(aws ec2 delete-security-group --group-id "$SG_EP_ID" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null || true); 
jq -c -n --arg GroupId "$SG_EP_ID" --arg NameTag "SG_Endpoints_SSM_NW-EC2-EIP-Test" --arg Error "$ERR" '{Action:"DeleteSecurityGroup",GroupId:$GroupId,NameTag:$NameTag,Result:"FAILED",Error:$Error}')
```
**Example output**
```json
{"Action":"DeleteSecurityGroup","GroupId":"sg-05e8e4c3756c6044a","NameTag":"SG_Endpoints_SSM_NW-EC2-EIP-Test","Result":"DELETED"}
```
**Conclusion**
The `SG` has been deleted.

###### 2.11.6.4.2 Delete `EC2 SG SG_EC2_ID` (JSONL result)
```bash
aws ec2 delete-security-group --group-id "$SG_EC2_ID" --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg GroupId "$SG_EC2_ID" --arg NameTag "SG_EC2_NW-EC2-EIP-Test" '{Action:"DeleteSecurityGroup",GroupId:$GroupId,NameTag:$NameTag,Result:"DELETED"}' || jq -c -n --arg GroupId "${SG_EC2_ID:-}" --arg NameTag "SG_EC2_NW-EC2-EIP-Test" '{Action:"DeleteSecurityGroup",GroupId:$GroupId,NameTag:$NameTag,Result:"FAILED_OR_IN_USE_OR_NOT_FOUND"}'
```
**Example output**
```json
{"Action":"DeleteSecurityGroup","GroupId":"sg-097e258d5cdbdc0e0","NameTag":"SG_EC2_NW-EC2-EIP-Test","Result":"DELETED"}
```

##### 2.11.6.5 Delete `subnets`, then `VPC` (JSONL)
###### 2.11.6.5.1 Delete all `subnets` in the `VPC` (JSONL per `subnet`)
```bash
aws ec2 describe-subnets --region "$AWS_REGION" --no-cli-pager --filters "Name=vpc-id,Values=$VPC_ID" | jq -r '.Subnets[]? | [.SubnetId,(((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"")] | @tsv' | while IFS=$'\t' read -r sid sname; 
do aws ec2 delete-subnet --subnet-id "$sid" --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg SubnetId "$sid" --arg NameTag "$sname" '{Action:"DeleteSubnet",SubnetId:$SubnetId,NameTag:$NameTag,Result:"DELETED"}' || jq -c -n --arg SubnetId "$sid" --arg NameTag "$sname" '{Action:"DeleteSubnet",SubnetId:$SubnetId,NameTag:$NameTag,Result:"FAILED_OR_IN_USE"}';
done
```
**Example output**
```json
{"Action":"DeleteSubnet","SubnetId":"subnet-074c3e8e1490cbb60","NameTag":"Subnet_Public_AZ1_NW-EC2-EIP-Test","Result":"DELETED"}
{"Action":"DeleteSubnet","SubnetId":"subnet-013030e88f7ac56be","NameTag":"Subnet_Private_AZ1_NW-EC2-EIP-Test","Result":"DELETED"}
```
**Conclusion**

###### 2.11.6.5.2 Delete `VPC` `VPC_ID` (JSONL result)
```bash
aws ec2 delete-vpc --vpc-id "$VPC_ID" --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg VpcId "$VPC_ID" --arg NameTag "VPC_NW-EC2-EIP-Test" '{Action:"DeleteVpc",VpcId:$VpcId,NameTag:$NameTag,Result:"DELETED"}' || jq -c -n --arg VpcId "${VPC_ID:-}" --arg NameTag "VPC_NW-EC2-EIP-Test" '{Action:"DeleteVpc",VpcId:$VpcId,NameTag:$NameTag,Result:"FAILED_OR_DEPENDENCIES_REMAIN"}'
```
**Example output**
```json
{"Action":"DeleteVpc","VpcId":"vpc-0757dfccfbd169bd8","NameTag":"VPC_NW-EC2-EIP-Test","Result":"DELETED"}
```

##### 2.11.6.6 Verify `network baseline` is gone (`VPC` not found) (JSONL)
```bash
aws ec2 describe-vpcs --region "$AWS_REGION" --no-cli-pager --filters "Name=vpc-id,Values=${VPC_ID:-__MISSING__}" | jq -c --arg VpcId "${VPC_ID:-}" --arg VpcNameExpected "VPC_NW-EC2-EIP-Test" 'if ($VpcId|length)==0 then {Check:"VPC lookup",VpcId:"",VpcNameExpected:$VpcNameExpected,Exists:false,Message:"VPC_ID is empty: export VPC_ID then retry"} elif (.Vpcs|length)==0 then {Check:"VPC lookup",VpcId:$VpcId,VpcNameExpected:$VpcNameExpected,Exists:false,Message:"No VPC returned for this VPC_ID in this region: verify AWS_REGION and VPC_ID"} else (.Vpcs[0] as $v | (($v.Tags//[])|map(select(.Key=="Name")|.Value)|.[0]//"MISSING-VPC-NAME") as $Name | {Check:"VPC lookup",VpcId:($v.VpcId//""),VpcName:$Name,VpcNameExpected:$VpcNameExpected,Exists:true,NameMatchesExpected:($Name==$VpcNameExpected),Message:(if ($Name==$VpcNameExpected) then "OK: VPC exists and Name matches expected" else "WARN: VPC exists but Name does not match expected" end)}) end'
```
**Example output**
```json
{"Check":"VPC lookup","VpcId":"vpc-0757dfccfbd169bd8","VpcNameExpected":"VPC_NW-EC2-EIP-Test","Exists":false,"Message":"No VPC returned for this VPC_ID in this region: verify AWS_REGION and VPC_ID"}
```
**Conclusion**
This was the end of the full teardown, which can also be checked in the AWS console.

#### 2.11.7 `EventBridge rule` teardown (default event bus)
##### 2.11.7.1 Locate `rule` by exact name (JSONL)
**Purpose**
Confirm the rule exists on the `default event bus` and capture its `name` (and `ARN`) before deleting.

```bash
RULE_NAME="CheckAndReleaseUnassociatedEIPs-Monthly"; aws events list-rules --event-bus-name "default" --region "$AWS_REGION" --no-cli-pager | jq -c --arg RuleName "$RULE_NAME" '.Rules[]? | select(.Name==$RuleName) | {RuleName:(.Name//""),Arn:(.Arn//""),EventBusName:(.EventBusName//""),State:(.State//""),ScheduleExpression:(.ScheduleExpression//""),Description:(.Description//"")} | with_entries(select(.value!=""))' || true
```
**Example output**
```json
{"RuleName":"CheckAndReleaseUnassociatedEIPs-Monthly","Arn":"arn:aws:events:us-east-1:180294215772:rule/CheckAndReleaseUnassociatedEIPs-Monthly","EventBusName":"default","State":"ENABLED","ScheduleExpression":"cron(40 2 25 * ? *)"}
```
**Conclusion**
A JSONL line is returned, so the rule exists and is in scope to delete.

##### 2.11.7.2 List `targets` for `rule` (JSONL)
**Purpose**
List all `targets` attached to the `rule` (must remove `targets` before deleting the `rule`).

```bash
RULE_NAME="CheckAndReleaseUnassociatedEIPs-Monthly"; aws events list-targets-by-rule --rule "$RULE_NAME" --event-bus-name "default" --region "$AWS_REGION" --no-cli-pager | jq -c --arg RuleName "$RULE_NAME" '.Targets[]? | {RuleName:$RuleName,TargetId:(.Id//""),TargetArn:(.Arn//""),LambdaFunctionName:(try (.Arn|capture("function:(?<fn>[^:]+)").fn) catch "")} | with_entries(select(.value!=""))' || jq -c -n --arg RuleName "$RULE_NAME" '{RuleName:$RuleName,TargetsFound:0}'
```
**Example output**
```json
{"RuleName":"CheckAndReleaseUnassociatedEIPs-Monthly","TargetId":"ManageEIPs","TargetArn":"arn:aws:lambda:us-east-1:180294215772:function:ManageEIPs","LambdaFunctionName":"ManageEIPs"}
```

##### 2.11.7.3 Remove `targets` (JSONL)
**Purpose**
Remove all `targets` from the rule (idempotent).

```bash
RULE_NAME="CheckAndReleaseUnassociatedEIPs-Monthly"; TARGET_IDS_CSV=$(aws events list-targets-by-rule --rule "$RULE_NAME" --event-bus-name "default" --region "$AWS_REGION" --no-cli-pager | jq -r '.Targets[]?.Id' | paste -sd',' -); 
[ -n "${TARGET_IDS_CSV:-}" ] && aws events remove-targets --rule "$RULE_NAME" --event-bus-name "default" --ids $(echo "$TARGET_IDS_CSV" | tr ',' ' ') --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg RuleName "$RULE_NAME" --arg TargetIdsCsv "$TARGET_IDS_CSV" '{Action:"RemoveTargets",RuleName:$RuleName,TargetIdsCsv:$TargetIdsCsv,Result:"REMOVED_OR_ALREADY"}' || jq -c -n --arg RuleName "$RULE_NAME" --arg TargetIdsCsv "${TARGET_IDS_CSV:-}" '{Action:"RemoveTargets",RuleName:$RuleName,TargetIdsCsv:$TargetIdsCsv,Result:"NONE_FOUND_OR_FAILED"}'
```
**Example output**
```json
{"Action":"RemoveTargets","RuleName":"CheckAndReleaseUnassociatedEIPs-Monthly","TargetIdsCsv":"ManageEIPs","Result":"REMOVED_OR_ALREADY"}
```
**Conclusion**
`REMOVED_OR_ALREADY` means `targets` are no longer blocking deletion.


##### 2.11.7.4 Delete `rule` (JSONL)
**Purpose**
Delete the rule after `targets` are removed.

```bash
RULE_NAME="CheckAndReleaseUnassociatedEIPs-Monthly"; aws events delete-rule --name "$RULE_NAME" --event-bus-name "default" --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg RuleName "$RULE_NAME" '{Action:"DeleteRule",RuleName:$RuleName,Result:"DELETED"}' || (ERR=$(aws events delete-rule --name "$RULE_NAME" --event-bus-name "default" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null || true); 
jq -c -n --arg RuleName "$RULE_NAME" --arg Error "$ERR" '{Action:"DeleteRule",RuleName:$RuleName,Result:"FAILED",Error:$Error}')
```
**Example output**
```json
{"Action":"DeleteRule","RuleName":"CheckAndReleaseUnassociatedEIPs-Monthly","Result":"DELETED"}
```
**Conclusion**
`DELETED` means the rule is removed.

##### 2.11.7.5 Verify `rule` is gone (JSONL)
**Purpose**
Confirm the `rule` no longer exists on the `default bus`.

```bash
RULE_NAME="CheckAndReleaseUnassociatedEIPs-Monthly"; OUT=$(aws events describe-rule --name "$RULE_NAME" --event-bus-name "default" --region "$AWS_REGION" --no-cli-pager 2>/dev/null || true); [ -n "$OUT" ] && jq -c --arg RuleName "$RULE_NAME" --arg Msg "WARN: rule still exists (not deleted)" '. as $r | {Check:"EventBridge rule deletion",RuleName:$RuleName,Exists:true,State:($r.State//""),ScheduleExpression:($r.ScheduleExpression//""),Message:$Msg} | with_entries(select(.value!=""))' <<<"$OUT" || jq -c -n --arg RuleName "$RULE_NAME" --arg Msg "OK: rule not found (deleted)" '{Check:"EventBridge rule deletion",RuleName:$RuleName,Exists:false,Message:$Msg}'
```
**Example output**
```json
{"Check":"EventBridge rule deletion","RuleName":"CheckAndReleaseUnassociatedEIPs-Monthly","Exists":false,"Message":"OK: rule not found (deleted)"}
```
**Conclusion**
`Exists:false` confirms the `rule` has been deleted.


#### 2.11.8 `SNS topics` teardown
##### 2.11.8.1 List `SNS topics` in scope (JSONL)
**Purpose**
List `SNS topics` that match your lab scope (by name pattern), and always output at least one JSONL line.

```bash
TOPIC_NAME_PATTERN="NW-EC2-EIP-Test|ManageEIPs|CheckAndReleaseUnassociatedEIPs"; OUT=$(aws sns list-topics --region "$AWS_REGION" --no-cli-pager 2>/dev/null || true); [ -n "$OUT" ] && (echo "$OUT" | jq -c --arg pat "$TOPIC_NAME_PATTERN" '.Topics[]? | .TopicArn as $a | ($a|capture(":(?<name>[^:]+)$").name) as $n | select($n|test($pat;"i")) | {TopicArn:$a,TopicName:$n,InScope:true}'; 
COUNT=$(echo "$OUT" | jq -r --arg pat "$TOPIC_NAME_PATTERN" '[.Topics[]? | .TopicArn as $a | ($a|capture(":(?<name>[^:]+)$").name) as $n | select($n|test($pat;"i"))] | length'); 
[ "${COUNT:-0}" -gt 0 ] || jq -c -n --arg pat "$TOPIC_NAME_PATTERN" '{Check:"SNS topics in scope",Pattern:$pat,TopicsFound:0,Message:"OK: no matching SNS topics found"}') || jq -c -n --arg pat "$TOPIC_NAME_PATTERN" '{Check:"SNS topics in scope",Pattern:$pat,TopicsFound:0,Message:"WARN: list-topics failed or returned empty"}'
```
**Example output**
```json
{"TopicArn":"arn:aws:sns:us-east-1:180294215772:ManageEIPs-Alarms","TopicName":"ManageEIPs-Alarms","InScope":true}
```
**Conclusion** 
`InScope:true` means there is at least 1 topic, for which we have the `TopicArn`.

##### 2.11.8.2 Delete `SNS topic(s)` (JSONL)
**Purpose**
Delete all in-scope `SNS topics` (idempotent), and always output at least one JSONL line.

```bash
TOPIC_NAME_PATTERN="NW-EC2-EIP-Test|ManageEIPs|CheckAndReleaseUnassociatedEIPs"; 
TOPIC_ARNS=$(aws sns list-topics --region "$AWS_REGION" --no-cli-pager 2>/dev/null | jq -r --arg pat "$TOPIC_NAME_PATTERN" '.Topics[]? | .TopicArn as $a | ($a|capture(":(?<name>[^:]+)$").name) as $n | select($n|test($pat;"i")) | $a' | paste -sd' ' -); 
[ -n "${TOPIC_ARNS:-}" ] && (for ARN in $TOPIC_ARNS; do NAME=$(echo "$ARN" | awk -F: '{print $NF}'); 
aws sns delete-topic --topic-arn "$ARN" --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1 && jq -c -n --arg TopicArn "$ARN" --arg TopicName "$NAME" '{Action:"DeleteSnsTopic",TopicArn:$TopicArn,TopicName:$TopicName,Result:"DELETED"}' || (ERR=$(aws sns delete-topic --topic-arn "$ARN" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null || true); 
jq -c -n --arg TopicArn "$ARN" --arg TopicName "$NAME" --arg Error "$ERR" '{Action:"DeleteSnsTopic",TopicArn:$TopicArn,TopicName:$TopicName,Result:"FAILED",Error:$Error}'); 
done) || jq -c -n --arg Pattern "$TOPIC_NAME_PATTERN" '{Action:"DeleteSnsTopic",Pattern:$Pattern,Result:"NONE_FOUND"}'
```
**Example output**
```json
{"Action":"DeleteSnsTopic","TopicArn":"arn:aws:sns:us-east-1:180294215772:ManageEIPs-Alarms","TopicName":"ManageEIPs-Alarms","Result":"DELETED"}
```
**Conclusion**
`DELETED` means the `topic deletion API` call succeeded.

##### 2.11.8.3 Verify `SNS topics` are gone (JSONL)
**Purpose**
Confirm no in-scope `SNS topics` remain, and always output at least one JSONL line.

```bash
TOPIC_NAME_PATTERN="NW-EC2-EIP-Test|ManageEIPs|CheckAndReleaseUnassociatedEIPs"; OUT=$(aws sns list-topics --region "$AWS_REGION" --no-cli-pager 2>/dev/null || true); 
[ -n "$OUT" ] && (echo "$OUT" | jq -c --arg pat "$TOPIC_NAME_PATTERN" '.Topics[]? | .TopicArn as $a | ($a|capture(":(?<name>[^:]+)$").name) as $n | select($n|test($pat;"i")) | {TopicArn:$a,TopicName:$n,InScope:true}'; 
TOTAL=$(echo "$OUT" | jq -r '.Topics|length'); MATCH=$(echo "$OUT" | jq -r --arg pat "$TOPIC_NAME_PATTERN" '[.Topics[]? | .TopicArn as $a | ($a|capture(":(?<name>[^:]+)$").name) as $n | select($n|test($pat;"i"))] | length'); [ "${MATCH:-0}" -gt 0 ] || jq -c -n --arg pat "$TOPIC_NAME_PATTERN" --argjson TotalTopics "${TOTAL:-0}" '{Check:"SNS list-topics",Pattern:$pat,TotalTopics:$TotalTopics,TopicsMatched:0,Message:"OK: no topics matched pattern (nothing to delete)"}') || jq -c -n --arg pat "$TOPIC_NAME_PATTERN" '{Check:"SNS list-topics",Pattern:$pat,Message:"WARN: list-topics failed or returned empty"}'
```
**Example output**
```json
{"Check":"SNS list-topics","Pattern":"NW-EC2-EIP-Test|ManageEIPs|CheckAndReleaseUnassociatedEIPs","TotalTopics":0,"TopicsMatched":0,"Message":"OK: no topics matched pattern (nothing to delete)"}
```
**Conclusion**
As the topic was already deleted, there was nothing more to delete.



## 3. Advanced Capabilities (Safety, Observability, Multi-Region)
This section describes architectural and operational capabilities that extend beyond the core `SAM` deployment and `Lambda` logic.
These features are not required for the function to operate correctly, but demonstrate how the solution can scale across Regions, remain safe to operate, and stay observable and cost-controlled in more advanced or production-like scenarios.
They are implemented primarily through deployment patterns, configuration, and monitoring resources, rather than by changing the Lambda decision logic.

### 3.1 Multi-Region Capability (Design & Rationale)
The solution is designed to be Region-agnostic and can be deployed independently in multiple AWS Regions without changes to the Lambda code.
Each deployment operates only on `Elastic IP` resources in its own Region and is triggered by its own `EventBridge schedule`.
This approach limits operational blast radius, avoids cross-Region dependencies, and aligns with AWS regional isolation principles while keeping observability and cost control local to each Region.


### 3.2 Multi-Region Deployment Model
The solution supports a multi-Region deployment by deploying the same `SAM` application independently into each target AWS Region (for example, Region A and Region B), using the same source code, the same template, and the same tagging/naming standards.
Each Region deployment is fully self-contained and includes:
- a regional `Lambda` function
- a regional `EventBridge schedule rule` (trigger)
- a regional `CloudWatch Logs` log group (created automatically on first invocation or pre-created)
- regional `alarms` / `dashboards`

**`IAM` note (global scope, `SAM` implication)**
`IAM` is global, so a multi-Region `SAM` strategy must avoid name collisions. 2 acceptable patterns are:
- Per-Region `stack-managed roles` (simplest): let `SAM/CloudFormation` create `roles` with unique names per `stack` (recommended for portfolio labs).
- `Shared role` (advanced): create 1 `IAM role` once (outside the regional `stacks`), then pass its `ARN` as a parameter and attach the function to that `role` in each Region.

**Key characteristics of this model**
- **No cross-Region dependencies**
  Each Region runs independently and operates only on Elastic IPs in its own Region. 
  There is no cross-Region replication, shared state, or cross-Region API calls.

- **Same code, different Region context**
  The `Lambda` logic does not change. When deployed to a different Region, the runtime uses that Region’s `endpoints`, and the function discovers and manages `EIPs` locally.

- **Independent schedules per Region**
  Each Region has its own `EventBridge rule`. 
  `Schedules` may differ (e.g., daily in Region A, weekly in Region B). 
  A secondary Region can be deployed with its `schedule` disabled by default, enabling it only when needed.

- **Isolation and blast-radius control**
  Failures, misconfigurations, or unintended behavior are isolated to the Region where they occur. 
  This prevents a single bad change from impacting resources in multiple Regions.

- **Consistent naming and tags across Regions (`resource name` vs `NameTag`)**
  Keep AWS resource names consistent while making the Region explicit, and ensure AWS-created names (for example, `FunctionName` / `RuleName`) are not identical to the `Name` tag value (NameTag). Example pattern:
  - Function name: `ManageEIPs-RegionA`, `ManageEIPs-RegionB`
  - Rule name: `ManageEIPs-Schedule-RegionA`, `ManageEIPs-Schedule-RegionB`
  - Name tag: `Lambda_ManageEIPs_RegionA`, `EventBridge_ManageEIPs_RegionB`

- **Repeatable deployment approach**
  Deploy by running the same `SAM deploy` command while changing only the `target` Region (and optionally the `stack name` / `config environment`). 
  All verification commands must be run per Region to confirm correct deployment and tagging.

This deployment model provides multi-Region capability as a deployment pattern rather than a code change, enabling the project to scale across Regions while keeping operational complexity and cost tightly controlled.


### 3.3 Cost Impact of Multi-Region Deployment
Deploying the solution in multiple AWS Regions introduces incremental costs, but these remain predictable, low, and controllable due to the independent and lightweight nature of each deployment.

**The primary cost drivers per Region are:**
- **AWS `Lambda` executions**
  Costs are driven by `invocation count` and execution duration. 
  With a low-frequency schedule (daily/weekly/monthly), `Lambda` costs remain negligible. 
  No additional cost is incurred when the function is idle.

- **Amazon `EventBridge schedules`**
  `EventBridge rules` incur minimal cost. 
  A secondary Region can be deployed with its `rule` disabled by default, resulting in near-zero ongoing cost until explicitly enabled.

- **Amazon `CloudWatch Logs`**
  Log ingestion and storage costs depend on log volume. 
  Keeping logs structured and concise helps ensure costs remain minimal. 
  No logs are generated if the function is not invoked.

- **Monitoring resources**
  `CloudWatch alarms`, `dashboards`, and `SNS notifications` can add small recurring costs per Region. 
  To minimize cost, deploy them only in the primary Region (or keep secondary Region monitoring minimal).

**Important cost-control characteristics of the multi-Region model**
- **No `always-on` infrastructure:**
  No `EC2 instances`, `load balancers`, or persistent services are required.

- **No cross-Region data transfer**
  Each Region operates entirely locally, avoiding inter-Region traffic charges.

- **Independent cost visibility**
  Regional resources can be tagged consistently to attribute and control costs per Region using `standard cost allocation tooling`.

- **On-demand activation**
  Secondary Regions can remain deployed but inactive (`disabled schedules`), allowing rapid enablement without ongoing execution costs.

Overall, the multi-Region deployment model provides high architectural flexibility with minimal financial impact, making it suitable for small-scale or experimental environments while remaining scalable to production patterns when required.


### 3.4 Testing Strategy & Validation
The solution is validated using a progressive testing strategy that prioritizes safety, observability, and cost control.
Testing confirms correct behavior without risking accidental release or modification of `Elastic IP` resources.

#### 3.4.1 Test data preparation (unused `Elastic IPs`)
Preparation of controlled test conditions required to validate the `Lambda` function, including the recreation of unused `Elastic IPs` after prior cleanup operations.

##### 3.4.1.0 `Reusable jq filter` (JSONL, no arrays inside objects)
```bash
JQ_TAGS_FLAT='def tag($k): ((.Tags//[])|map(select(.Key==$k)|.Value)|.[0])//"MISSING"; def tagsflat: {NameTag:tag("Name"),Tag_Project:tag("Project"),Tag_Environment:tag("Environment"),Tag_Owner:tag("Owner"),Tag_ManagedBy:tag("ManagedBy"),Tag_CostCenter:tag("CostCenter")};'
```

##### 3.4.1.1 Recreate first unused `Elastic IP`
```bash
EIP_UNUSED1_ALLOC_ID=$(aws ec2 allocate-address --region "$AWS_REGION" --domain vpc --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=EIP_Unused1_ManageEIPs},{Key=Project,Value=$TAG_PROJECT},{Key=Environment,Value=$TAG_ENV},{Key=Owner,Value=$TAG_OWNER},{Key=ManagedBy,Value=$TAG_MANAGEDBY},{Key=CostCenter,Value=$TAG_COSTCENTER}]" --no-cli-pager | jq -r '.AllocationId'); 
aws ec2 describe-addresses --region "$AWS_REGION" --no-cli-pager --allocation-ids "$EIP_UNUSED1_ALLOC_ID" | jq -c '[.Addresses[] | {AllocationId,Name:(((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"MISSING-EIP-NAME"),PublicIp}]'
```
**Example output:**
```json
[{"AllocationId":"eipalloc-06b9574f9e8fed2d7","Name":"EIP_Unused1_ManageEIPs","PublicIp":"54.204.14.253"}]
```

##### 3.4.1.2 Recreate second unused `Elastic IP`
```bash
EIP_UNUSED2_ALLOC_ID=$(aws ec2 allocate-address --region "$AWS_REGION" --domain vpc --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=EIP_Unused2_ManageEIPs},{Key=Project,Value=$TAG_PROJECT},{Key=Environment,Value=$TAG_ENV},{Key=Owner,Value=$TAG_OWNER},{Key=ManagedBy,Value=$TAG_MANAGEDBY},{Key=CostCenter,Value=$TAG_COSTCENTER}]" --no-cli-pager | jq -r '.AllocationId'); 
aws ec2 describe-addresses --region "$AWS_REGION" --no-cli-pager --allocation-ids "$EIP_UNUSED2_ALLOC_ID" | jq -c '[.Addresses[] | {AllocationId,Name:(((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"MISSING-EIP-NAME"),PublicIp}]'
```
**Example output:**
```json
[{"AllocationId":"eipalloc-036a044d806b80d83","Name":"EIP_Unused2_ManageEIPs","PublicIp":"18.234.11.69"}]
```

##### 3.4.1.3 Tag the 2 AllocationIds as `ManagedBy`=`ManageEIPs` (run once per ``EIP`, or combine)
**Create the tags:**
```bash
aws ec2 create-tags --resources "eipalloc-06b9574f9e8fed2d7" "eipalloc-036a044d806b80d83" --tags Key=ManagedBy,Value="ManageEIPs" --region "$AWS_REGION" --no-cli-pager
```

**Verify the tags:**
```bash
aws ec2 describe-addresses --allocation-ids "eipalloc-06b9574f9e8fed2d7" "eipalloc-036a044d806b80d83" --region "$AWS_REGION" --no-cli-pager | jq -c '.Addresses[] | {AllocationId,PublicIp,Tags}'
```
**Example output:**
```json
{"AllocationId":"eipalloc-036a044d806b80d83","PublicIp":"18.234.11.69","Tags":[{"Key":"CostCenter","Value":"Portfolio"},{"Key":"Environment","Value":"dev"},{"Key":"ManagedBy","Value":"ManageEIPs"},{"Key":"Name","Value":"EIP_Unused2_ManageEIPs"},{"Key":"Project","Value":"ManageEIPs"},{"Key":"Owner","Value":"Malik"}]}   
{"AllocationId":"eipalloc-06b9574f9e8fed2d7","PublicIp":"54.204.14.253","Tags":[{"Key":"Project","Value":"ManageEIPs"},{"Key":"Owner","Value":"Malik"},{"Key":"Environment","Value":"dev"},{"Key":"Name","Value":"EIP_Unused1_ManageEIPs"},{"Key":"ManagedBy","Value":"ManageEIPs"},{"Key":"CostCenter","Value":"Portfolio"}]} 
```

#### 3.4.2 `Dry-run` / safety mode validation
The `Lambda` function supports a `dry-run` mode in which all actions are evaluated and logged but no changes are applied to AWS resources.
This mode is used to confirm correct discovery, filtering, and decision logic without operational risk, and it can be enabled per Region to validate secondary Regions safely before enabling schedules.

#### 3.4.3 Manual `invocation` testing
The function can be invoked manually (CLI or Console) to validate behavior on demand.
Manual `invocations` confirm `environment variable configuration`, `IAM permissions`, and correct execution in newly deployed Regions before enabling `scheduled execution`.

#### 3.4.4 `Scheduled execution` testing
`EventBridge rules` should be deployed in a `disabled state` and enabled only after successful `dry-run` and manual validation.
This ensures `automated execution` is introduced gradually and only after confidence in correct behavior has been established.

#### 3.4.5 Observability and log verification
**CloudWatch Logs are reviewed after each test execution to confirm:**
`CloudWatch Logs` are reviewed after each test execution to confirm successful `invocation`, correct outcomes (action vs no-action), and absence of unexpected errors.
`Structured logging` enables consistent review and simplifies troubleshooting across Regions.

#### 3.4.6 Failure isolation validation
By deploying and testing each Region independently, failures can be confirmed to remain isolated to a single Region.
This validates `blast-radius containment` and prevents cross-Region impact during testing or operation.



## 4. `GitHub` (repository Publishing & Portfolio Standards)
### 4.1 repository purpose and scope
**Purpose:**
- Publish the `ManageEIPs SAM` project as a clean, reproducible portfolio repositorysitory: `SAM template(s)`, `Lambda code`, and `helper jq filters`, with instructions to deploy and run.

- Contains: source code, `SAM/IaC templates`, docs, sample configs (sanitized), and `reusable jq filters`.

- Explicitly does not contain: credentials, `account IDs` that don’t need to be public, state files, deployment outputs,
local `artifacts`, or any exported CLI JSON dumps unless sanitized.


### 4.2 Local repository initialization (if not already a repository)
#### 4.2.1 Folder selection
Project root folder: `ManageEIPs-1region_SAM`

#### 4.2.2 Initilise `git`
```bash
git init
```

**Expected output: Git created the repository and - by default - started on branch `master`, instead of `main`**
```text
Using 'master' as the name for the initial branch. This default branch name is subject to change.
```

#### 4.2.3 Set `default` branch to `main`
**Make all future `git init` default to `main`**
```bash
git config --global init.defaultBranch main
```

**Rename `current branch` to `main` + list repository status**
```bash
git branch -M main
git status
```
**Expected output:**
```text
  .vs/
  .aws-sam/
  ManageEIPs_Automation-SAM.md
  function/
  sam_build_debug.log
  samconfig.toml
  src/
  template.yaml
```

#### 4.2.4 Configure `Git author identity` (mandatory for first `commit`)
```bash
git config --global user.name "<user name>"
git config --global user.email "username@example.com"
```

#### 4.2.5 Create `gitignore`to avoid committing certain files
```bash
cat > .gitignore <<'EOF'
# Secrets / keys
*.pem
*.key
*.p12
*.pfx
.env
.env.*

# IDE
.vs/
.vscode/
.idea/

# Build / artifacts
*.zip

# Local outputs / scratch
response*.json
event.json

# OS junk
.DS_Store
Thumbs.db
EOF
```

#### 4.2.6 Stage what should be published and create the first `commit`
```bash
git add .gitignore README.md ManageEIPs_Automation-SAM.md lambda_function.py manage-eips-policy.json manage-eips-trust.json *.jq
git commit -m "chore: initialize repository with docs, lambda, and jq helpers"
git status
```

#### 4.2.7 Verify `status/log` (`git status` / `git log`...)
```bash
git status
git log --oneline -n 5
```
**Example output:**
```text
On branch main
Your branch is up to date with 'origin/main'.

Changes not staged for commit:
  (use "git add/rm <file>..." to update what will be committed)
  (use "git restore <file>..." to discard changes in working directory)
        modified:   ManageEIPs_Automation-SAM.md
        deleted:    ManageEIPs_Automation.md

Untracked files:
  (use "git add <file>..." to include in what will be committed)
        .aws-sam/
        function/
        sam_build_debug.log
        samconfig.toml
        src/
        template.yaml
```
I want to add `template.yaml` and `src/`. Let's also check if it is safe to add `samconfig.toml`

**Check contents of `samconfig.toml`**
```bash
sed -n '1,200p' samconfig.toml
```
As there is nothing secret in it, the file will be added.

**Add SAM templates and 2 other files**
```bash
git add template.yaml src/ samconfig.toml
```


### 4.3 File set to publish
- Required files: `README.md`, `ManageEIPs_Automation-SAM.md`, `Lambda` code, templates, `helper jq filters`
- Excluded files: credentials, any private notes, large binaries


### 4.4 Commit standards (“portfolio discipline”)
- Small commits, 1 concern per commit
- Commit messages format we prefer
- “No hardcoded IDs / CLI + `jq -c` outputs / consistent tags” as review checklist


### 4.5 Remote creation and first `push`
#### 4.5.1 Create `GitHub` repository (UI)
- Go to `GitHub` and sign in.
- click on + > New repository > add Description > Visibility = public (portfolio)
- Leave `Add README`, `Add .gitignore` and `Add License` unchecked > click on `CREATE repository`
- Click on `Code` tab > click on `HTTPS` (grey when active)
- Copy the repository URL ending with `.git`: https://github.com/fred1717/ManageEIPs-1region_SAM.git

```bash
git remote add origin https://github.com/fred1717/ManageEIPs-1region_SAM.git
git remote -v
```

**Example output:**
```text
error: remote origin already exists.
origin  https://github.com/fred1717/ManageEIPs-1region.git (fetch)
origin  https://github.com/fred1717/ManageEIPs-1region.git (push)
```
Our local repository already has a `remote named origin` saved in `.git/config` (pointing to `ManageEIPs-1region.git` instead of `ManageEIPs-1region_SAM.git`), so Git refuses to add another origin and keeps the existing one.

**Find the exact location of the Git directory**
```bash
git rev-parse --show-toplevel
git rev-parse --git-dir
```
**Example output**
```text
/mnt/c/Users/mfham/Documents/CloudDev/Python/Lambda/ManageEIPs-1region_SAM
.git
```

**Check whether `.git` exists but is hidden**
```bash
ls -la
```
**Example output**
```text
drwxrwxrwx 1 mfh mfh 512 Dec 31 21:09 .git
```

**Fix the wrong remote**
```bash
git remote set-url origin https://github.com/fred1717/ManageEIPs-1region_SAM.git
git remote -v
```
**Example output**
```text
origin  https://github.com/fred1717/ManageEIPs-1region_SAM.git (fetch)
origin  https://github.com/fred1717/ManageEIPs-1region_SAM.git (push)
```


#### 4.5.2 First `push`
**Create a `GitHub Personal Access Token` (`PAT`) for `git push` (HTTPS):**
- Open token settings (UI)
  - Profile picture > Settings > Scroll down and click `Developer settings` > Click `Personal access tokens` > 
  - Click `Token (classic)` > Click `Generate new token (classic)`

- Configure the `token`
  - Note: `ManageEIPs-1region_SAM`
  - Expiration: choose 1 (recommended: 30 or 90 days; or your preference)
  - Select scopes: tick repository (this covers `push` to repository)

- Generate and copy
  - Scroll down and click `Generate token`.
  - Copy the `token` immediately (will be invisible afterwards).

- Use it for the `push`

```bash
git push -u origin main
```

- When prompted:
  Username for 'https://github.com': fred1717
  Password for 'https://fred1717@github.com': paste the `PAT` I just copied


### 4.6 Release/Versioning (optional)
- repository renders `README` properly
- Markdown links work
- No secrets committed (final sanity check)

#### 4.6.1 Verify `push` succeeded (CLI)
```bash
git status
git log --oneline -n 5
```
**Expected output: branch is up to date with 'origin/main', except for the markdown document, still being edited**
**Check what changed (updated markdown)**
```bash
git diff
```

**Stage**
```bash
git add ManageEIPs_Automation-SAM.md
```

**Commit**
```bash
git commit -m "sam: add template, source directory, and samconfig.toml"
```

**Push**
```bash
git push
```

**Verify**
```bash
git status
git log --oneline -n 5
```
**Expected output: `local branch` and `GitHub` are now synchronised**
Changes not staged for commit:
  (use "git add/rm <file>..." to update what will be committed)
  (use "git restore <file>..." to discard changes in working directory)
        modified:   `.gitignore`
        modified:   `ManageEIPs_Automation-SAM.md`
        deleted:    ManageEIPs_Automation.md
no changes added to commit (use "git add" and/or "git commit -a")

**Commit `.gitignore` and `ManageEIPs_Automation-SAM.md`**
```bash
git diff
git add .gitignore ManageEIPs_Automation-SAM.md
git rm ManageEIPs_Automation.md
git commit -m "docs: update runbook and clean legacy filename; ignore SAM build artifacts"
git push
```

#### 4.6.2  Verify files on `GitHub` (UI)
- `GitHub` > Your repository > Click `ManageEIPs-1region_SAM` > Refresh page > check latest `commit` + list of present files 
- Click `README.md` to confirm it renders properly
- Go back > click on `ManageEIPs_Automation-SAM.md`: check code blocks render properly.

#### 4.6.3 Final sanity check: no secrets committed (CLI)
**Checking filenames**
```bash
git ls-files | grep -Ei "(\.pem|\.key|\.p12|\.pfx|\.env|tfstate|credentials|secret|token|response.*\.json|event\.json)" || true
```
**Expected output:**
No output. That’s the desired outcome: it means none of the tracked files match those risky patterns.

**Checking file contents on the repository**
```bash
git grep -nE "(AKIA|ASIA|aws_secret_access_key|aws_access_key_id|BEGIN RSA PRIVATE KEY)" || true
```
**Expected output:**
No output. That’s the desired outcome, once again: it means none of those strings appear in tracked files.

#### 4.6.4 Check repository has no ignored `artifacts` staged
```bash
git status --ignored
```
The only uncommitted edit is, as before, `ManageEIPs_Automation-SAM.md`. This was to be expected as it is still being edited.


### 4.7 Release/Versioning
#### 4.7.0 Important note (timing)
This repository’s documentation (`ManageEIPs_Automation-SAM.md`) is still being updated, and the `README.md` will be revised again after the documentation is final.
Therefore do not create the final release tag (`v1.0.0`) until:
- `ManageEIPs_Automation-SAM.md` is finished.
- `README.md` has been updated to its final version.

We can still `commit` and `push` changes normally while documentation is in progress.


#### 4.7.1 Normal workflow while docs are still changing (`commit` + `push`)
```bash
git status
git add ManageEIPs_Automation-SAM.md README.md
git commit -m "docs: add monitoring (SNS + CW alarms/dashboard) and align multi-region wording"
git push
```

#### 4.7.2 Pre-release tag while docs are WIP (Work-In-Progress)
It provides a visible milestone before the final documentation is complete.
**Create pre-release tag `v0.0.1`**
```bash
git tag -a v0.0.1 -m "WIP: docs milestone (monitoring + README observability)"
git push origin v0.0.1
```

**In `GitHub`**
- repository `ManageEIPs-1region_SAM` > Releases > Create a new release > Select tag: `v0.0.1` > 
  Title: v0.0.1 — Initial WIP pre-release (SAM deployment + baseline runbook) > tick "Set as a pre-release"
- Publish release

**Create pre-release tag `v0.0.2` (later WIP milestone)**
```bash
git tag -a v0.0.2 -m "Cleaned up formatting in the 2 markdown documents"
git push origin v0.0.2
```

**In `GitHub`**
- repository `ManageEIPs-1region_SAM` > Releases > Create a new release > Select tag: `v0.0.2` > tick "Set as a pre-release"
- Release title: `v0.0.2` (WIP - formatting cleaned up) > Publish release


#### 4.7.3 Final release when documentation is complete (`v1.0.0`)
**Prerequisite: working tree clean**
```bash
git status
```

**Create final tag**
```bash
git tag -a v1.0.0 -m "v1.0.0 first portfolio release"
git push origin v1.0.0
```
fatal: tag 'v1.0.0' already exists

**Check existing version tags**
```bash
git tag --list | grep -E '^v'
```
**Example output**
```text
v0.0.1
v0.0.2
v0.1.0
v1.0.0
```

**Remove v1.0.0 and v0.1.0**
```bash
git tag -d v1.0.0 v0.1.0
```
**Check existing version tags**
```bash
git tag --list | grep -E '^v'
```
**Example output**
```text
v0.0.1
v0.0.2
```

**New attempt at creating final tag `v1.0.0`**
```bash
git tag -a v1.0.0 -m "v1.0.0 first stable portfolio release (SAM template + Lambda + strict JSON runbook)"
git push origin v1.0.0
```

**Create `GitHub` Release (UI)**
- repository `ManageEIPs-1region_SAM` > Releases > Create a new release > Select tag: `v1.0.0` > don't tick "Set as a pre-release"
- Release title: `v1.0.0` > Publish release
This final release is actually the same as v0.0.2 with exception of this line.

**Verify**
- repository > Releases
- Confirm `v1.0.0` points to the intended commit.

**Additional attempt at creating final tag `v1.0.1`**
```bash
git status
git add ManageEIPs_Automation-SAM.md
git commit -m "docs: Amend spelling errors and add new git version"
git push
```

```bash
git tag -a v1.0.1 -m "v1.0.1 second stable portfolio release (SAM template + Lambda + strict JSON runbook)"
git push origin v1.0.1
```

**Create `GitHub` Release (UI)**
- repository `ManageEIPs-1region_SAM` > Releases > Create a new release > Select tag: `v1.0.1` > don't tick "Set as a pre-release"
- Release title: `v1.0.1` > Publish release
This final release is actually the same as v0.0.2 with exception of this line.

**Verify**
- repository > Releases
- Confirm `v1.0.1` points to the intended commit.

**Final attempt at creating final tag `v1.0.2`**
```bash
git status
git add ManageEIPs_Automation-SAM.md
git commit -m "docs: Amend errors in runbook (last git versioning) and add new git version"
git push
```

```bash
git tag -a v1.0.2 -m "v1.0.2 third stable portfolio release (SAM template + Lambda + strict JSON runbook)"
git push origin v1.0.2
```

**Create `GitHub` Release (UI)**
- repository `ManageEIPs-1region_SAM` > Releases > Create a new release > Select tag: `v1.0.2` > don't tick "Set as a pre-release"
- Release title: `v1.0.2 - third stable portfolio release (SAM template + Lambda + strict JSON runbook)` > Publish release
This final release is actually the same as v0.0.2 with exception of this line.

**Verify**
- repository > Releases
- Confirm `v1.0.2` points to the intended commit.
