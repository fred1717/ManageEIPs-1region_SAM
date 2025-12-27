# ManageEIPs Automation – Dynamic AWS CLI + Lambda (Strict JSONL Discipline)
End-to-end infrastructure automation exercise designed to be:
- fully reproducible
- teardown-first / rebuild-from-scratch
- auditable via structured CLI output
- free of hardcoded AWS resource IDs

## Scope
This project demonstrates how to:
- Create a complete VPC-based environment:
  - VPC, subnets, route tables, Internet Gateway
  - security groups
  - EC2 instance
- Allocate and manage Elastic IPs:
  - 1 attached EIP
  - 2 intentionally unused EIPs
- Create IAM roles and policies
- Deploy a Lambda function that:
  - detects unused Elastic IPs
  - safely releases them
- Implement operational safeguards:
  - Dry-Run safety mode
  - structured CloudWatch logging (JSON)
  - CloudWatch metrics for released EIPs

## Engineering principles enforced throughout
All steps in this document follow **strict operational rules**:

- **AWS CLI + jq only** (no Console, no SDKs)
- **Dynamic resource discovery**  
  (no hardcoded VPC IDs, subnet IDs, SG IDs, etc.)
- **Strict JSON Lines (JSONL) output**
  - exactly **1 JSON object per line**
  - no human-formatted tables
- **No nested arrays in output**
  - tag data is fully flattened
  - **1 JSON object per tag**
- **Reusable jq filters**
  - shared once, reused everywhere
  - guarantee consistent output contracts
- **Separation of concerns**
  - imperative commands (create / delete)
  - declarative commands (describe / verify)

## Workflow
1. **Clean existing resources** to start from a known empty state
2. **Rebuild the entire environment from scratch**
3. **Deploy and validate the ManageEIPs Lambda**
4. **Verify behavior via structured logs and metrics**

The cleanup phase is intentionally documented first to allow:
- repeatable testing
- safe re-runs
- forensic debugging when things go wrong



## 0. Global environment and conventions
This section defines the **global execution context** for the entire project.
All subsequent commands assume these settings are in place.

### 0.1 AWS CLI environment and credentials
**Check which AWS credentials - Access Key ID / Secret Access Key - are used by the default profile:**
This ensures the correct AWS account is in use before proceeding.
```bash
cat ~/.aws/credentials
```

### 0.2 Tooling and CLI behavior
**Install jq (JSON formatter) in WSL, if not already done (which it was):**
```bash
sudo apt install -y jq
```

**Disable the AWS CLI pager globally to prevent commands from opening ***less*** and to ensure output is printed directly to ***stdout***:**
```bash
aws configure set cli_pager ""
```

### 0.3 Global environment variables and tagging conventions
Initialize AWS CLI environment variables for the current shell session.
These variables are used consistently throughout the project to:
- avoid hardcoded values
- enforce a uniform tagging strategy
- keep commands portable and reproducible

```bash
export AWS_PROFILE=default;   # Makes the profile explicit (no implicit default confusion)
export AWS_REGION=us-east-1;  # single-region now, portable later, so the code doesn´t break

ACCOUNT_ID=$(aws sts get-caller-identity --no-cli-pager | jq -r '.Account'); # get $ACCOUNT_ID dynamically

AWS_AZ="${AWS_REGION}a";      # Defines $AWS_AZ consistently

SNS_TOPIC_ARN="arn:aws:sns:${AWS_REGION}:${ACCOUNT_ID}:ManageEIPs-Alarms"   # Prepares $SNS_TOPIC_ARN for later commands

export TAG_PROJECT="ManageEIPs"; 
export TAG_ENV="dev"; 
export TAG_OWNER="Malik"; 
export TAG_MANAGEDBY="CLI"; 
export TAG_COSTCENTER="Portfolio"
```

**The following tag keys are used consistently across all resources:**
- Name
- Project
- Component
- Environment
- Owner
- ManagedBy
- CostCenter

### 0.4 Reusable jq filters (one-time setup)
#### 0.4.1 Creating the reusable filter file `flatten_tags.jq`:
**To enforce strict, atomic JSON output throughout the document, a reusable `jq` filter is created to flatten resource tags.**
**This guarantees:**
- exactly 1 JSON object per line
- no nested arrays
- 1 object per tag

```bash
cat > flatten_tags.jq <<'JQ'
def flatten_tags($tags): ($tags // [] | if length>0 then . else [] end);
JQ
```

#### 0.4.2 creating the reusable filter file `tag_helpers.jq`
**It safely extracts tag values (especially the `NameTag`) from AWS JSON objects, regardless of whether tags are under `.Tags` or `.TagSet`, and never breaks if the tag is missing.**
**Precisely what each function does:**
- `_tags($obj)` → returns the tag array from `$obj (.Tags or .TagSet, or empty)`
- `tag_value($obj; "Key")` → returns the first value of that tag key, or null
- `tag_name($obj)` → returns the Name tag value, or null
- `must_tag_name($obj)` → returns the Name tag value, or the literal string MISSING-NAME-TAG

```bash
cat > tag_helpers.jq <<'JQ'
def _tags($o): ($o.Tags // $o.TagSet // []);
def tag_value($o; $k): (_tags($o) | first(.[]? | select(.Key==$k) | .Value) // null);
def tag_name($o): tag_value($o; "Name");
def must_tag_name($o): (tag_name($o) // "MISSING-NAME-TAG");
JQ
```

These filters will be reused in all subsequent inspection commands involving tags.



## 1. AWS CLI profile and region

### 1.1 Resetting existing resources but IAM before implementing multi-region capabilities
Before re-running the deployment commands, I reset all regional AWS resources created by this project (VPC, subnets, Elastic IPs, EventBridge rules, Lambda function, CloudWatch dashboards and alarms).

This reset is intentional and serves 2 purposes:
- to validate that all commands in this document are reproducible from a clean state;
- to prepare the project for a future multi-Region deployment, where the same steps must be executed independently in each Region.

IAM roles and policies are not deleted, as IAM is global and already validated.
The following commands remove only regional resources and can be skipped when starting from a clean AWS account or Region.
--------------------------------------------------------------------------------------------
#### 1.1.1 Safety check
```bash
aws configure list
```
**Expected output:**
```text
NAME       : VALUE                    : TYPE             : LOCATION
profile    : <not set>                : None             : None
access_key : ****************7FBQ     : shared-credentials-file : 
secret_key : ****************Hvxp     : shared-credentials-file : 
region     : us-east-1                : config-file      : ~/.aws/config
```

#### 1.1.2 EventBridge — rules & targets (MUST go first)
##### 1.1.2.1 List rules (confirm names)
```bash
aws events list-rules --region "$AWS_REGION" --no-cli-pager | jq '.Rules[].Name'
```
or
```bash
aws events list-rules --region "$AWS_REGION" --no-cli-pager | jq -L . -r 'include "tag_helpers"; 
.Rules[]? | .Name'
```
**Explanation:**
-L = jq library search path

**Expected output:**
```text
"CheckAndReleaseUnassociatedEIPs-Monthly"
"LambdaEC2DailySnapshot-LambdaEC2DailySnapshotDailyS-043x91XkQkno"
"start-nat-2300"
"stop-nat-2359"
"tempstop-nat-0235"
"tempstop-nat-0335"
```

##### 1.1.2.2 Remove targets and rules
##### 1.1.2.2.1 Remove daily targets and rules
**Remove daily target:**
```bash
aws events remove-targets --rule CheckAndReleaseUnassociatedEIPs-Daily --ids 1 --region "$AWS_REGION" --no-cli-pager  # error as the daily rule did not exist anymore
```

**Delete daily rule:** (unnecessary but that would be the command below)
```bash
aws events delete-rule --name CheckAndReleaseUnassociatedEIPs-Daily --region "$AWS_REGION" --no-cli-pager
```

##### 1.1.2.2.2 Remove monthly targets and rules
**Remove monthly target:**
```bash
aws events remove-targets --rule CheckAndReleaseUnassociatedEIPs-Monthly --ids 1 --region "$AWS_REGION" --no-cli-pager  
```
```json
{
    "FailedEntryCount": 0,
    "FailedEntries": []
}
```

**Delete monthly rule:**
```bash
aws events delete-rule --name CheckAndReleaseUnassociatedEIPs-Monthly --region "$AWS_REGION" --no-cli-pager
```

#### 1.1.3 Lambda — permissions and functions
##### 1.1.3.1 List permission (sanity)
```bash
aws lambda get-policy --function-name ManageEIPs --region "$AWS_REGION" --no-cli-pager | jq '.Policy'
```
**Expected output:**
```json
"{\"Version\":\"2012-10-17\",\"Id\":\"default\",\"Statement\":[{\"Sid\":\"AllowEventBridgeMonthlyTrigger\",\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"events.amazonaws.com\"},\"Action\":\"lambda:InvokeFunction\",\"Resource\":\"arn:aws:lambda:us-east-1:180294215772:function:ManageEIPs\",\"Condition\":{\"ArnLike\":{\"AWS:SourceArn\":\"arn:aws:events:us-east-1:180294215772:rule/CheckAndReleaseUnassociatedEIPs-Monthly\"}}}]}"
```

##### 1.1.3.2 Remove permission (Monthly)
```bash
aws lambda remove-permission --function-name ManageEIPs --statement-id AllowEventBridgeMonthlyTrigger --region "$AWS_REGION" --no-cli-pager && jq -c -n '{FunctionName:"ManageEIPs",StatementId:"AllowEventBridgeMonthlyTrigger",Removed:true}'
```

##### 1.1.3.3 Delete Lambda function
```bash
aws lambda delete-function --function-name ManageEIPs --region "$AWS_REGION" --no-cli-pager && jq -c -n '{FunctionName:"ManageEIPs",Deleted:true}'
```

#### 1.1.4 CloudWatch — alarms & dashboards
##### 1.1.4.1 List alarms
```bash
aws cloudwatch describe-alarms --region "$AWS_REGION" --no-cli-pager | jq '.MetricAlarms[].AlarmName'
```
**Expected output:**
```text
"ManageEIPs-DurationHigh"
"ManageEIPs-Errors"
"ManageEIPs-Throttles"
```

##### 1.1.4.2 Delete alarms
```bash
aws cloudwatch delete-alarms --alarm-names ManageEIPs-Errors ManageEIPs-DurationHigh ManageEIPs-Throttles --region "$AWS_REGION" --no-cli-pager # if run previous command for listing alarms, it now returns nothing
```

##### 1.1.4.3 Delete dashboard
```bash
aws cloudwatch delete-dashboards --dashboard-names ManageEIPs-Dashboard --region "$AWS_REGION" --no-cli-pager
```

#### 1.1.5 SNS — topic & subscription
##### 1.1.5.1 List topics
**variable assignment (shell plumbing):**
Good practice if there is only 1 value, to be reused later:
```bash
SNS_TOPIC_ARN=$(aws sns list-topics --region "$AWS_REGION" --no-cli-pager | jq -r '.Topics[] | select(.TopicArn | contains("ManageEIPs")) | .TopicArn')
```

**JSONL inspection output (strict contract):**
```bash
aws sns list-topics --region "$AWS_REGION" --no-cli-pager | jq -c '.Topics[] | {TopicArn:.TopicArn}'
```
**Expected output:**
```json
{"TopicArn":"arn:aws:sns:us-east-1:180294215772:MyAppTopic"}
{"TopicArn":"arn:aws:sns:us-east-1:180294215772:maliksemail"}
```

##### 1.1.5.2 Delete topics
**Extract the Topic ARNs (shell plumbing):**
```bash
aws sns list-topics --region "$AWS_REGION" --no-cli-pager | jq -r '.Topics[] | .TopicArn'
```
**Example output:**
```text
arn:aws:sns:us-east-1:180294215772:MyAppTopic
arn:aws:sns:us-east-1:180294215772:maliksemail
```

**Delete topics + JSONL result (strict):**
```bash
aws sns list-topics --region "$AWS_REGION" --no-cli-pager | jq -r '.Topics[] | .TopicArn' | while read -r ARN; 
do OUT=$(aws sns delete-topic --topic-arn "$ARN" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null); 
RC=$?; 
if [ $RC -eq 0 ]; then jq -c -n --arg arn "$ARN" '{TopicArn:$arn, Deleted:true}'; 
else jq -c -n --arg arn "$ARN" --arg err "$OUT" '{TopicArn:$arn, Deleted:false, Error:$err}'; 
fi; 
done
```
**Expected output:**
```json
{"TopicArn":"arn:aws:sns:us-east-1:180294215772:MyAppTopic","Deleted":true}
{"TopicArn":"arn:aws:sns:us-east-1:180294215772:maliksemail","Deleted":true}
```


#### 1.1.6 EC2 — Elastic IPs
##### 1.1.6.1 List EIPs
**JSONL, 1 object per line, no arrays:**
```bash
aws ec2 describe-addresses --region "$AWS_REGION" --no-cli-pager | jq -c '.Addresses[] | {AllocationId:.AllocationId,Name:(((.Tags // []) | map(select(.Key=="Name") | .Value) | .[0]) // "NO-NAME-TAG"),PublicIp:(.PublicIp // "no-ip")}'
```
or with filter (tag_helpers)
```bash
aws ec2 describe-addresses --region "$AWS_REGION" --no-cli-pager | jq -L . -c 'include "tag_helpers"; .Addresses[]? | {AllocationId:.AllocationId,EipName:(must_tag_name(.)//"MISSING-NAME-TAG"),PublicIp:(.PublicIp//"no-ip")}'
``` 

##### 1.1.6.2 Release each unassociated EIP
**Get AllocationIds - only the unassociated ones, using '*select(.AssociationId == null)*' (shell plumbing):**
```bash
aws ec2 describe-addresses --region "$AWS_REGION" --no-cli-pager | jq -r '.Addresses[] | select(.AssociationId == null) | .AllocationId'
```
**Release each unassociated EIP (JSONL result per EIP):**
```bash
aws ec2 describe-addresses --region "$AWS_REGION" --no-cli-pager | jq -r '.Addresses[] | select(.AssociationId == null) | .AllocationId' | while read -r ALLOC; 
do OUT=$(aws ec2 release-address --allocation-id "$ALLOC" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null); 
RC=$?; 
if [ $RC -eq 0 ]; 
then jq -c -n --arg a "$ALLOC" '{AllocationId:$a, Released:true}'; 
else jq -c -n --arg a "$ALLOC" --arg err "$OUT" '{AllocationId:$a, Released:false, Error:$err}'; 
fi; 
done
```

##### 1.1.6.3 Release associated EIPs
**Find what these EIPs are attached to (`AssociationId`, `InstanceId`, ENIs, returning Names and Tags):**
**Here using `select(.AssociationId != null)`
```bash
aws ec2 describe-addresses --region "$AWS_REGION" --no-cli-pager | jq -c '.Addresses[] | select(.AssociationId != null) | ((.Tags // []) as $T | ($T | map(select(.Key=="Name") | .Value) | .[0]) as $Name | (.AllocationId as $A | .PublicIp as $P | (.AssociationId // "no-association") as $Assoc | (.InstanceId // "no-instance") as $I | (.NetworkInterfaceId // "no-eni") as $ENI | (.PrivateIpAddress // "no-private-ip") as $Priv | (if ($T|length)>0 then $T[] else {"Key":"NO-TAGS","Value":"NO-TAGS"} end) | {AllocationId:$A, PublicIp:($P // "no-ip"), AssociationId:$Assoc, InstanceId:$I, NetworkInterfaceId:$ENI, PrivateIpAddress:$Priv, Name:($Name // "NO-NAME-TAG"), TagKey:.Key, TagValue:.Value}))'
```
**Possible output:**
```json
{"AllocationId":"eipalloc-...","PublicIp":"54.x.x.x","AssociationId":"eipassoc-...","InstanceId":"i-...","NetworkInterfaceId":"eni-...","PrivateIpAddress":"10.0.1.10","Name":"ManageEIPs-Attached","TagKey":"Name","TagValue":"ManageEIPs-Attached"}
{"AllocationId":"eipalloc-...","PublicIp":"54.x.x.x","AssociationId":"eipassoc-...","InstanceId":"i-...","NetworkInterfaceId":"eni-...","PrivateIpAddress":"10.0.1.10","Name":"ManageEIPs-Attached","TagKey":"Project","TagValue":"ManageEIPs"}
```
or with filter (`flatten_tags`)
```bash
aws ec2 describe-addresses --region "$AWS_REGION" --no-cli-pager | jq -L . -c 'include "flatten_tags"; .Addresses[]? | select(.AssociationId!=null) | ((.Tags//[]) as $T | (flatten_tags($T)[]? // {"Key":"NO-TAGS","Value":"NO-TAGS"}) as $t | {AllocationId:.AllocationId,AssociationId:(.AssociationId//"no-association"),InstanceId:(.InstanceId//"no-instance"),NetworkInterfaceId:(.NetworkInterfaceId//"no-eni"),PrivateIpAddress:(.PrivateIpAddress//"no-private-ip"),Name:((($T|map(select(.Key=="Name")|.Value))|.[0])//"NO-NAME-TAG"),PublicIp:(.PublicIp//"no-ip"),TagKey:$t.Key,TagValue:$t.Value})'
```
**Example output:**
```json
{"AllocationId":"eipalloc-0da06ada08055dcb5","AssociationId":"eipassoc-02a07f8bc7b6d1adf","InstanceId":"i-0c69cfce5294ffdd4","NetworkInterfaceId":"eni-04feb50245d8d6a14","PrivateIpAddress":"10.0.1.121","Name":"EIP_Attached_ManageEIPs","PublicIp":"100.50.117.194","TagKey":"CostCenter","TagValue":"Portfolio"}
{"AllocationId":"eipalloc-0da06ada08055dcb5","AssociationId":"eipassoc-02a07f8bc7b6d1adf","InstanceId":"i-0c69cfce5294ffdd4","NetworkInterfaceId":"eni-04feb50245d8d6a14","PrivateIpAddress":"10.0.1.121","Name":"EIP_Attached_ManageEIPs","PublicIp":"100.50.117.194","TagKey":"Name","TagValue":"EIP_Attached_ManageEIPs"}    
{"AllocationId":"eipalloc-0da06ada08055dcb5","AssociationId":"eipassoc-02a07f8bc7b6d1adf","InstanceId":"i-0c69cfce5294ffdd4","NetworkInterfaceId":"eni-04feb50245d8d6a14","PrivateIpAddress":"10.0.1.121","Name":"EIP_Attached_ManageEIPs","PublicIp":"100.50.117.194","TagKey":"Environment","TagValue":"dev"}
{"AllocationId":"eipalloc-0da06ada08055dcb5","AssociationId":"eipassoc-02a07f8bc7b6d1adf","InstanceId":"i-0c69cfce5294ffdd4","NetworkInterfaceId":"eni-04feb50245d8d6a14","PrivateIpAddress":"10.0.1.121","Name":"EIP_Attached_ManageEIPs","PublicIp":"100.50.117.194","TagKey":"ManagedBy","TagValue":"CLI"}
{"AllocationId":"eipalloc-0da06ada08055dcb5","AssociationId":"eipassoc-02a07f8bc7b6d1adf","InstanceId":"i-0c69cfce5294ffdd4","NetworkInterfaceId":"eni-04feb50245d8d6a14","PrivateIpAddress":"10.0.1.121","Name":"EIP_Attached_ManageEIPs","PublicIp":"100.50.117.194","TagKey":"Project","TagValue":"ManageEIPs"}
{"AllocationId":"eipalloc-0da06ada08055dcb5","AssociationId":"eipassoc-02a07f8bc7b6d1adf","InstanceId":"i-0c69cfce5294ffdd4","NetworkInterfaceId":"eni-04feb50245d8d6a14","PrivateIpAddress":"10.0.1.121","Name":"EIP_Attached_ManageEIPs","PublicIp":"100.50.117.194","TagKey":"Owner","TagValue":"Malik"}
```

**Dissociate all attached EIPs (iterate on '*AssociationId*'):**
```bash
aws ec2 describe-addresses --region "$AWS_REGION" --no-cli-pager | jq -r '.Addresses[] | select(.AssociationId != null) | .AssociationId' | while read -r ASSOC; 
do OUT=$(aws ec2 disassociate-address --association-id "$ASSOC" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null); 
RC=$?; 
if [ $RC -eq 0 ]; 
then jq -c -n --arg a "$ASSOC" '{AssociationId:$a, Dissociated:true}'; 
else jq -c -n --arg a "$ASSOC" --arg err "$OUT" '{AssociationId:$a, Dissociated:false, Error:$err}'; 
fi; 
done
```

**Retry releasing the now unattached EIPs - using `select(.AssociationId == null)`:**
```bash
aws ec2 describe-addresses --region "$AWS_REGION" --no-cli-pager | jq -r '.Addresses[] | select(.AssociationId == null) | .AllocationId' | while read -r ALLOC; 
do OUT=$(aws ec2 release-address --allocation-id "$ALLOC" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null); 
RC=$?; 
if [ $RC -eq 0 ]; 
then jq -c -n --arg a "$ALLOC" '{AllocationId:$a, Released:true}'; 
else jq -c -n --arg a "$ALLOC" --arg err "$OUT" '{AllocationId:$a, Released:false, Error:$err}'; 
fi; 
done
```

#### 1.1.7 EC2 — networking (reverse dependency order)
##### 1.1.7.1 Process of deleting subnets
###### 1.1.7.1.2 Identifying subnets
**Subnets inventory (no tag arrays, clean JSONL):**
```bash
(aws ec2 describe-subnets --region "$AWS_REGION" --no-cli-pager; aws ec2 describe-vpcs --region "$AWS_REGION" --no-cli-pager) | jq -c -s '.[0].Subnets as $S | .[1].Vpcs as $V | $S[] | (.VpcId) as $Vid | ($V[] | select(.VpcId==$Vid)) as $Vpc | {SubnetId:.SubnetId,SubnetName:(((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"NO-SUBNET-NAME"),VpcId:$Vid,VpcName:((($Vpc.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"NO-VPC-NAME"),AvailabilityZone:.AvailabilityZone}'
```
**Expected output:**
```json
{"SubnetId":"subnet-05eba23bef4be5f45","VpcId":"vpc-08f94ba54ced6f05d","AvailabilityZone":"us-east-1a"}
{"SubnetId":"subnet-0f1aa99796f3d4016","VpcId":"vpc-08f94ba54ced6f05d","AvailabilityZone":"us-east-1e"}
{"SubnetId":"subnet-0105e9145116d6036","VpcId":"vpc-08f94ba54ced6f05d","AvailabilityZone":"us-east-1d"}
{"SubnetId":"subnet-0628c05fd04b50423","VpcId":"vpc-08f94ba54ced6f05d","AvailabilityZone":"us-east-1c"}
{"SubnetId":"subnet-0253b19d2922590bc","VpcId":"vpc-08f94ba54ced6f05d","AvailabilityZone":"us-east-1f"}
{"SubnetId":"subnet-0e2c62cc284851a28","VpcId":"vpc-08f94ba54ced6f05d","AvailabilityZone":"us-east-1b"}
{"SubnetId":"subnet-008faa4552159667a","VpcId":"vpc-08f94ba54ced6f05d","AvailabilityZone":"us-east-1b"}
```
or with filter `tag_helpers`
```bash
(aws ec2 describe-subnets --region "$AWS_REGION" --no-cli-pager; aws ec2 describe-vpcs --region "$AWS_REGION" --no-cli-pager) | jq -L . -c -s 'include "tag_helpers"; (.[0].Subnets//[]) as $S | (.[1].Vpcs//[]) as $V | $S[]? | (.VpcId) as $Vid | (($V | map(select(.VpcId==$Vid)) | .[0]) // {}) as $Vpc | {SubnetId:.SubnetId,SubnetName:(must_tag_name(.)),VpcId:$Vid,VpcName:(must_tag_name($Vpc)),AvailabilityZone:.AvailabilityZone}'
```
**Expected output:**
```json
{"SubnetId":"subnet-03949955e141deebb","SubnetName":"Subnet_ManageEIPs_AZ1","VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs","AvailabilityZone":"us-east-1a"}
```

**subnet tags fully flattened (strictest: one object per tag):**
```bash
aws ec2 describe-subnets --region "$AWS_REGION" --no-cli-pager | jq -c '.Subnets[]? | ((.Tags//[]) as $T | (($T|map(select(.Key=="Name")|.Value)|.[0])//"NO-SUBNET-NAME") as $Name | .SubnetId as $s | (($T[]?) | {SubnetId:$s,SubnetName:$Name,TagKey:.Key,TagValue:.Value}))'
```
**Expected output:**
```json
{"SubnetId":"subnet-05eba23bef4be5f45","TagKey":"Name","TagValue":"ITF_PublicSub_AZ1"}
{"SubnetId":"subnet-0f1aa99796f3d4016","TagKey":"Name","TagValue":"ITF_PublicSub_AZ5"}
{"SubnetId":"subnet-0105e9145116d6036","TagKey":"Name","TagValue":"ITF_PublicSub_AZ4"}
{"SubnetId":"subnet-0628c05fd04b50423","TagKey":"Name","TagValue":"ITF_PublicSub_AZ3"}
{"SubnetId":"subnet-0253b19d2922590bc","TagKey":"Name","TagValue":"ITF_PublicSub_AZ6"}
{"SubnetId":"subnet-0e2c62cc284851a28","TagKey":"Name","TagValue":"ITF_PrivateSub_AZ2"}
{"SubnetId":"subnet-008faa4552159667a","TagKey":"Name","TagValue":"ITF_PublicSub_AZ2"}
```
or with filter `flatten_tags`
```bash
aws ec2 describe-subnets --region "$AWS_REGION" --no-cli-pager | jq -L . -c 'include "flatten_tags"; .Subnets[]? | (.SubnetId as $s | ((.Tags//[]) as $T | (($T|map(select(.Key=="Name")|.Value)|.[0])//"NO-SUBNET-NAME") as $Name | flatten_tags($T)[]? | {SubnetId:$s,SubnetName:$Name,TagKey:.Key,TagValue:.Value}))'
```

**Try deleting all subnets if no dependencies:**
```bash
aws ec2 describe-subnets --region "$AWS_REGION" --no-cli-pager | jq -r '.Subnets[] | .SubnetId' | while read -r SUBNET; 
do OUT=$(aws ec2 delete-subnet --subnet-id "$SUBNET" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null); 
RC=$?; 
if [ $RC -eq 0 ]; then jq -c -n --arg s "$SUBNET" '{SubnetId:$s, Deleted:true}'; 
else jq -c -n --arg s "$SUBNET" --arg err "$OUT" '{SubnetId:$s, Deleted:false, Error:$err}'; 
fi; 
done
```

###### 1.1.7.1.2 Checking subnet dependencies, so we can delete the subnet
**Check possible dependencies one by one (ENIs in subnet, with Name + flattened tags = 1 JSON object per line):**
**Check dependencies for ENIs (in subnet, with Name + flattened tags = 1 JSON object per line):**
```bash
aws ec2 describe-network-interfaces --filters "Name=subnet-id,Values=subnet-026728bcde20943b5" --region "$AWS_REGION" --no-cli-pager | jq -c '.NetworkInterfaces[] | ((.TagSet // .Tags // []) as $T | ($T | map(select(.Key=="Name") | .Value) | .[0]) as $Name | (.NetworkInterfaceId as $ENI | .Status as $St | (.InterfaceType // "n/a") as $Ty | (.Description // "no-desc") as $Desc | (.Attachment.InstanceId // "no-instance") as $Inst | (.RequesterId // "n/a") as $Req | (if ($T|length)>0 then $T[] else {"Key":"NO-TAGS","Value":"NO-TAGS"} end) | {NetworkInterfaceId:$ENI, Name:($Name // "NO-NAME-TAG"), Status:$St, InterfaceType:$Ty, Description:$Desc, InstanceId:$Inst, RequesterId:$Req, TagKey:.Key, TagValue:.Value}))'
```
or with filter "flatten_tags"
```bash
aws ec2 describe-network-interfaces --filters "Name=subnet-id,Values=subnet-026728bcde20943b5" --region "$AWS_REGION" --no-cli-pager | jq -L . -c 'include "flatten_tags"; .NetworkInterfaces[]? | ((.TagSet // .Tags // []) as $T | (($T|map(select(.Key=="Name")|.Value)|.[0])//"NO-ENI-NAME") as $Name | (.NetworkInterfaceId as $ENI | (.Status//"unknown") as $St | (.InterfaceType//"n/a") as $Ty | (.Description//"no-desc") as $Desc | (.Attachment.InstanceId//"no-instance") as $Inst | (.RequesterId//"n/a") as $Req | (flatten_tags($T)[]? // {"Key":"NO-TAGS","Value":"NO-TAGS"}) as $t | {NetworkInterfaceId:$ENI,EniName:$Name,Status:$St,InterfaceType:$Ty,Description:$Desc,InstanceId:$Inst,RequesterId:$Req,TagKey:$t.Key,TagValue:$t.Value}))'
```

**Check dependencies for Security Groups - iterate ENIs → explode .Groups[] → emit 1 JSON object per (SG × ENI):**
```bash
aws ec2 describe-network-interfaces --region "$AWS_REGION" --no-cli-pager | jq -c '.NetworkInterfaces[]? | (.NetworkInterfaceId as $ENI | (.Attachment.InstanceId // "no-instance") as $Inst | (.InterfaceType // "n/a") as $Ty | (.Status // "unknown") as $St | (.Description // "no-desc") as $Desc | (.SubnetId // "NO-SUBNET") as $Sub | (.VpcId // "NO-VPC") as $V | (.Groups[]? | {GroupId:.GroupId, GroupName:(.GroupName // "NO-SG-NAME"), NetworkInterfaceId:$ENI, InstanceId:$Inst, InterfaceType:$Ty, Status:$St, Description:$Desc, SubnetId:$Sub, VpcId:$V}))'
```

**Check dependencies for NAT Gateways (in subnet, with Name + flattened tags = 1 JSON object per line):**
```bash
aws ec2 describe-nat-gateways --region "$AWS_REGION" --no-cli-pager | jq -c '.NatGateways[]? | ((.Tags // []) as $T | ($T | map(select(.Key=="Name") | .Value) | .[0]) as $Name | (.NatGatewayId as $NG | .State as $St | .SubnetId as $Sub | (if ($T|length)>0 then $T[] else {"Key":"NO-TAGS","Value":"NO-TAGS"} end) | {NatGatewayId:$NG, Name:($Name // "NO-NAME-TAG"), State:$St, SubnetId:$Sub, TagKey:.Key, TagValue:.Value}))'
```

**Check dependencies for Internet Gateways (in subnet, with Name + flattened tags = 1 JSON object per line):**
```bash
aws ec2 describe-internet-gateways --region "$AWS_REGION" --no-cli-pager | jq -c '.InternetGateways[]? | ((.Tags // []) as $T | ($T | map(select(.Key=="Name") | .Value) | .[0]) as $Name | (.InternetGatewayId as $IGW | ((.Attachments // [])[]? // {} ) as $Att | (if ($T|length)>0 then $T[] else {"Key":"NO-TAGS","Value":"NO-TAGS"} end) | {InternetGatewayId:$IGW, Name:($Name // "NO-NAME-TAG"), VpcId:($Att.VpcId // "NO-VPC"), AttachmentState:($Att.State // "NO-ATTACHMENT"), TagKey:.Key, TagValue:.Value}))'
```

**Check dependencies for VPC Endpoints (in subnet, with Name + flattened tags = 1 JSON object per line):**
```bash
aws ec2 describe-vpc-endpoints --region "$AWS_REGION" --no-cli-pager | jq -c '.VpcEndpoints[] | ((.Tags // []) as $T | ($T | map(select(.Key=="Name") | .Value) | .[0]) as $Name | (.VpcEndpointId as $V | .VpcEndpointType as $Ty | .ServiceName as $Svc | .State as $St | ((.SubnetIds // [])[]? // "NO-SUBNET") as $Sub | (if ($T|length)>0 then $T[] else {"Key":"NO-TAGS","Value":"NO-TAGS"} end) | {VpcEndpointId:$V, Name:($Name // "NO-NAME-TAG"), VpcEndpointType:$Ty, ServiceName:$Svc, State:$St, SubnetId:$Sub, TagKey:.Key, TagValue:.Value}))'
```

**Check dependencies for Load Balancers (in subnet, with Name + flattened tags = 1 JSON object per line):**
```bash
aws elbv2 describe-load-balancers --region "$AWS_REGION" --no-cli-pager | jq -c '.LoadBalancers[] | (.LoadBalancerArn as $A | .LoadBalancerName as $N | .Type as $T | (.State.Code // "unknown") as $S | (.AvailabilityZones[]? | {LoadBalancerArn:$A, LoadBalancerName:$N, Type:$T, State:$S, SubnetId:.SubnetId, ZoneName:(.ZoneName // "n/a")}))'
```

**Check dependencies for EC2 instances (in subnet, with Name + flattened tags = 1 JSON object per line):**
```bash
aws ec2 describe-instances --region "$AWS_REGION" --no-cli-pager | jq -c '.Reservations[].Instances[]? | ((.Tags // []) as $T | ($T | map(select(.Key=="Name") | .Value) | .[0]) as $Name | (.InstanceId as $I | .SubnetId as $Sub | .VpcId as $V | .State.Name as $St | (.PrivateIpAddress // "no-ip") as $IP | (if ($T|length)>0 then $T[] else {"Key":"NO-TAGS","Value":"NO-TAGS"} end) | {InstanceId:$I, Name:($Name // "NO-NAME-TAG"), State:$St, PrivateIp:$IP, SubnetId:($Sub // "NO-SUBNET"), VpcId:($V // "NO-VPC"), TagKey:.Key, TagValue:.Value}))'
```
**Example output with terminated instances:**
```json
{"InstanceId":"i-0f5eeb0fc21ef5092","Name":"TempSSMHelper-AZ1","State":"terminated","PrivateIp":"no-ip","SubnetId":"NO-SUBNET","VpcId":"NO-VPC","TagKey":"Name","TagValue":"TempSSMHelper-AZ1"}
{"InstanceId":"i-00294cfba71a6edf4","Name":"SSM-connect","State":"terminated","PrivateIp":"no-ip","SubnetId":"NO-SUBNET","VpcId":"NO-VPC","TagKey":"Name","TagValue":"SSM-connect"}
{"InstanceId":"i-060f6f8d4a61e7f3f","Name":"dct-instance-1","State":"terminated","PrivateIp":"no-ip","SubnetId":"NO-SUBNET","VpcId":"NO-VPC","TagKey":"Name","TagValue":"dct-instance-1"}
```
                                             
###### 1.1.7.1.3 Deleting whatever dependency needs to be deleted
**Deleting ENIs (in subnet, with Name + flattened tags = 1 JSON object per line):**
```bash
aws ec2 describe-network-interfaces --region "$AWS_REGION" --no-cli-pager | jq -r '.NetworkInterfaces[]? | select((.Status // "")=="available") | .NetworkInterfaceId' | while read -r ENI; 
do OUT=$(aws ec2 delete-network-interface --network-interface-id "$ENI" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null); 
RC=$?; 
if [ $RC -eq 0 ]; 
then jq -c -n --arg e "$ENI" '{NetworkInterfaceId:$e, Deleted:true}'; 
else jq -c -n --arg e "$ENI" --arg err "$OUT" '{NetworkInterfaceId:$e, Deleted:false, Error:$err}'; 
fi; 
done
```

**Deleting all deletable Security Groups (with Name + flattened tags = 1 JSON object per line):**
```bash
aws ec2 describe-security-groups --region "$AWS_REGION" --no-cli-pager | jq -r '.SecurityGroups[] | select(.GroupName!="default") | .GroupId' | while read -r SG; 
do USED=$(aws ec2 describe-network-interfaces --filters "Name=group-id,Values=$SG" --region "$AWS_REGION" --no-cli-pager | jq -r '.NetworkInterfaces | length'); 
if [ "$USED" -eq 0 ]; 
then OUT=$(aws ec2 delete-security-group --group-id "$SG" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null); RC=$?; 
if [ $RC -eq 0 ]; 
then jq -c -n --arg g "$SG" '{GroupId:$g, Deleted:true}'; 
else jq -c -n --arg g "$SG" --arg err "$OUT" '{GroupId:$g, Deleted:false, Error:$err}'; 
fi; 
fi; 
done
```

**Deleting NAT Gateways (with Name + flattened tags = 1 JSON object per line):**
```bash
aws ec2 describe-nat-gateways --region "$AWS_REGION" --no-cli-pager | jq -r '.NatGateways[]? | .NatGatewayId' | while read -r NGW; 
do OUT=$(aws ec2 delete-nat-gateway --nat-gateway-id "$NGW" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null); 
RC=$?; 
if [ $RC -eq 0 ]; 
then jq -c -n --arg n "$NGW" '{NatGatewayId:$n, Deleted:true}'; 
else jq -c -n --arg n "$NGW" --arg err "$OUT" '{NatGatewayId:$n, Deleted:false, Error:$err}'; 
fi; 
done
```

**Deleting Internet Gateways (with Name + flattened tags = 1 JSON object per line):**
```bash
aws ec2 describe-internet-gateways --region "$AWS_REGION" --no-cli-pager | jq -r '.InternetGateways[]? | .InternetGatewayId' | while read -r IGW; 
do VPC=$(aws ec2 describe-internet-gateways --internet-gateway-ids "$IGW" --region "$AWS_REGION" --no-cli-pager | jq -r '.InternetGateways[0].Attachments[0].VpcId // empty'); 
[ -n "$VPC" ] && aws ec2 detach-internet-gateway --internet-gateway-id "$IGW" --vpc-id "$VPC" --region "$AWS_REGION" --no-cli-pager >/dev/null 2>&1; 
OUT=$(aws ec2 delete-internet-gateway --internet-gateway-id "$IGW" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null); 
RC=$?; 
if [ $RC -eq 0 ]; 
then jq -c -n --arg g "$IGW" '{InternetGatewayId:$g, Deleted:true}'; 
else jq -c -n --arg g "$IGW" --arg err "$OUT" '{InternetGatewayId:$g, Deleted:false, Error:$err}'; 
fi; 
done
```

**Deleting VPC Endpoints (with Name + flattened tags = 1 JSON object per line):**
```bash
aws ec2 describe-vpc-endpoints --region "$AWS_REGION" --no-cli-pager | jq -r '.VpcEndpoints[]? | .VpcEndpointId' | while read -r VPCE; 
do OUT=$(aws ec2 delete-vpc-endpoints --vpc-endpoint-ids "$VPCE" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null); 
RC=$?; 
if [ $RC -eq 0 ]; then jq -c -n --arg v "$VPCE" '{VpcEndpointId:$v, Deleted:true}'; 
else jq -c -n --arg v "$VPCE" --arg err "$OUT" '{VpcEndpointId:$v, Deleted:false, Error:$err}'; 
fi; 
done
```

**Deleting Load Balancers (with Name + flattened tags = 1 JSON object per line):**
```bash
aws elbv2 describe-load-balancers --region "$AWS_REGION" --no-cli-pager | jq -r '.LoadBalancers[]? | .LoadBalancerArn' | while read -r LBA; 
do OUT=$(aws elbv2 delete-load-balancer --load-balancer-arn "$LBA" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null); 
RC=$?; 
if [ $RC -eq 0 ]; then jq -c -n --arg a "$LBA" '{LoadBalancerArn:$a, Deleted:true}'; 
else jq -c -n --arg a "$LBA" --arg err "$OUT" '{LoadBalancerArn:$a, Deleted:false, Error:$err}'; 
fi; 
done
```

**Terminating EC2 instances (in subnet, with Name + flattened tags = 1 JSON object per line):**
```bash
aws ec2 describe-instances --region "$AWS_REGION" --no-cli-pager | jq -r '.Reservations[].Instances[]? | .InstanceId' | while read -r IID; 
do OUT=$(aws ec2 terminate-instances --instance-ids "$IID" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null); 
RC=$?; 
if [ $RC -eq 0 ]; 
then jq -c -n --arg i "$IID" '{InstanceId:$i, Terminated:true}'; 
else jq -c -n --arg i "$IID" --arg err "$OUT" '{InstanceId:$i, Terminated:false, Error:$err}'; 
fi; 
done
```
**Example output with terminated instances:**
```json
{"InstanceId":"i-0f5eeb0fc21ef5092","Terminated":true}
{"InstanceId":"i-00294cfba71a6edf4","Terminated":true}
{"InstanceId":"i-060f6f8d4a61e7f3f","Terminated":true}
```

###### 1.1.7.1.4 Now actually deleting the subnet
**Retry deleting the subnet now (there was no ENI):**
```bash
**Try deleting all subnets if no dependencies:**
```bash
aws ec2 describe-subnets --region "$AWS_REGION" --no-cli-pager | jq -r '.Subnets[] | .SubnetId' | while read -r SUBNET; 
do OUT=$(aws ec2 delete-subnet --subnet-id "$SUBNET" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null); 
RC=$?; 
if [ $RC -eq 0 ]; then jq -c -n --arg s "$SUBNET" '{SubnetId:$s, Deleted:true}'; 
else jq -c -n --arg s "$SUBNET" --arg err "$OUT" '{SubnetId:$s, Deleted:false, Error:$err}'; 
fi; 
done
```

##### 1.1.7.2 Process of deleting VPC

###### 1.1.7.2.1 Identifying VPCs:
**First, identifying VPC - VPC tags fully flattened (strictest: 1 object per tag, no arrays):**
```bash
aws ec2 describe-vpcs --region "$AWS_REGION" --no-cli-pager | jq -c '.Vpcs[] | ((.Tags // []) as $T | ($T | map(select(.Key=="Name") | .Value) | .[0]) as $Name | (.VpcId as $V | (if ($T|length)>0 then $T[] else {"Key":"NO-TAGS","Value":"NO-TAGS"} end) | {VpcId:$V, VpcName:($Name // "MISSING-NAME-TAG"), TagKey:.Key, TagValue:.Value}))'
```
or with filter "flatten_tags"
```bash
aws ec2 describe-vpcs --region "$AWS_REGION" --no-cli-pager | jq -L . -c 'include "flatten_tags"; .Vpcs[] | (.Tags // []) as $T | ($T | map(select(.Key=="Name") | .Value) | .[0]) as $Name | .VpcId as $V | (flatten_tags($T) | (if length>0 then .[] else {"Key":"NO-TAGS","Value":"NO-TAGS"} end) | {VpcId:$V, VpcName:($Name // "MISSING-NAME-TAG"), TagKey:.Key, TagValue:.Value})'
```
**Example output:**
```json
{"VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs","TagKey":"CostCenter","TagValue":"Portfolio"}
{"VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs","TagKey":"Component","TagValue":"VPC"}
{"VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs","TagKey":"Project","TagValue":"ManageEIPs"}
{"VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs","TagKey":"Owner","TagValue":"Malik"}
{"VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs","TagKey":"ManagedBy","TagValue":"CLI"}
{"VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs","TagKey":"Environment","TagValue":"dev"}
{"VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs","TagKey":"Name","TagValue":"VPC_ManageEIPs"}
```


###### 1.1.7.2.2 Deleting Route Table
**First, identifying route tables (in VPC, with Name + flattened tags = 1 JSON object per line):**
```bash
(aws ec2 describe-route-tables --region "$AWS_REGION" --no-cli-pager; aws ec2 describe-vpcs --region "$AWS_REGION" --no-cli-pager) | jq -c -s '.[0].RouteTables as $RT | .[1].Vpcs as $V | $RT[] | ((.Tags//[]) as $T | ((($T|map(select(.Key=="Name")|.Value))|.[0]) // "NO-RT-NAME")) as $RTName | (.VpcId) as $Vid | ($V[] | select(.VpcId==$Vid)) as $Vpc | {RouteTableId:.RouteTableId,RouteTableName:$RTName,VpcId:$Vid,VpcName:((($Vpc.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"NO-VPC-NAME"),IsMain:(((.Associations//[])|any(.Main==true)))}'
```

**Example output:**
```json
{"RouteTableId":"rtb-0435ffce50cebdffb","RouteTableName":"RTB_ManageEIPs","VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs","IsMain":false}
{"RouteTableId":"rtb-0f77d6166bf48ce99","RouteTableName":"NO-RT-NAME","VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs","IsMain":true}
```

**Second, Detach all non-main route table associations (disassociate) (JSONL result):**
```bash
aws ec2 describe-route-tables --region "$AWS_REGION" --no-cli-pager | jq -r '.RouteTables[]? | (.Associations // [])[]? | select((.Main // false)==false) | .RouteTableAssociationId' | while read -r ASSOC; 
do OUT=$(aws ec2 disassociate-route-table --association-id "$ASSOC" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null); 
RC=$?; 
if [ $RC -eq 0 ]; 
then jq -c -n --arg a "$ASSOC" '{RouteTableAssociationId:$a, Disassociated:true}'; 
else jq -c -n --arg a "$ASSOC" --arg err "$OUT" '{RouteTableAssociationId:$a, Disassociated:false, Error:$err}'; 
fi; 
done
```

**Third, Delete all non-main route tables (JSONL result):**
```bash
aws ec2 describe-route-tables --region "$AWS_REGION" --no-cli-pager | jq -r '.RouteTables[]? | select(((.Associations // []) | any(.Main==true))|not) | .RouteTableId' | while read -r RTB; 
do OUT=$(aws ec2 delete-route-table --route-table-id "$RTB" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null); 
RC=$?; 
if [ $RC -eq 0 ]; 
then jq -c -n --arg r "$RTB" '{RouteTableId:$r, Deleted:true}'; 
else jq -c -n --arg r "$RTB" --arg err "$OUT" '{RouteTableId:$r, Deleted:false, Error:$err}'; 
fi; 
done
```

###### 1.1.7.2.3 Deleting VPC
```bash
aws ec2 describe-vpcs --region "$AWS_REGION" --no-cli-pager | jq -r '.Vpcs[]? | select(.IsDefault|not) | .VpcId' | while read -r VPC; 
do OUT=$(aws ec2 delete-vpc --vpc-id "$VPC" --region "$AWS_REGION" --no-cli-pager 2>&1 >/dev/null); 
RC=$?; 
if [ $RC -eq 0 ]; 
then jq -c -n --arg v "$VPC" '{VpcId:$v, Deleted:true}'; 
else jq -c -n --arg v "$VPC" --arg err "$OUT" '{VpcId:$v, Deleted:false, Error:$err}'; 
fi; 
done
```


### 1.2 Setting AWS CLI profile and region dynamically
**Check which Access Key ID / Secret Access Key are used in the default profile:**
```bash
cat ~/.aws/credentials
```

**Install jq (JSON formatter) in WSL, if not already done (which it was):**
```bash
sudo apt install -y jq
```

**Initialize AWS CLI environment variables (profile, region, derived identifiers) for the current session:**
**Using consistent tag set (Project, Component, Environment, Owner, ManagedBy, CostCenter, Name), some of them keeping the same value throughout the project:**
```bash
export AWS_PROFILE=default;   # Makes the profile explicit (no implicit default confusion)
export AWS_REGION=us-east-1;  # single-region now, portable later, so the code doesn´t break
ACCOUNT_ID=$(aws sts get-caller-identity --no-cli-pager | jq -r '.Account'); # get $ACCOUNT_ID dynamically
AWS_AZ="${AWS_REGION}a";      # Defines $AWS_AZ consistently
SNS_TOPIC_ARN="arn:aws:sns:${AWS_REGION}:${ACCOUNT_ID}:ManageEIPs-Alarms"   # Prepares $SNS_TOPIC_ARN for later commands
export TAG_PROJECT="ManageEIPs"; 
export TAG_ENV="dev"; 
export TAG_OWNER="Malik"; 
export TAG_MANAGEDBY="CLI"; 
export TAG_COSTCENTER="Portfolio"
```

**Disable AWS CLI pager globally, prevent commands from opening LESS, ensure output is printed directly:**
```bash
aws configure set cli_pager ""
```



## 2. VPC Section

### 2.1 List existing VPCs (with `NameTag`)
```bash
aws ec2 describe-vpcs --region "$AWS_REGION" --no-cli-pager | jq -c '.Vpcs[] | ((.Tags // []) as $T | ($T | map(select(.Key=="Name") | .Value) | .[0]) as $Name | (.VpcId as $V | (if ($T|length)>0 then $T[] else {"Key":"NO-TAGS","Value":"NO-TAGS"} end) | {VpcId:$V, VpcName:($Name // "MISSING-NAME-TAG"), TagKey:.Key, TagValue:.Value}))'
```


### 2.2 Create VPC VPC_ManageEIPs
#### 2.2.1 Create the VPC, tag it, Store the VPC ID for later steps:
**Create + capture ID (shell concern):**
```bash
VPC_ID=$(aws ec2 create-vpc --cidr-block 10.0.0.0/16 --region "$AWS_REGION" --no-cli-pager | jq -r '.Vpc.VpcId')
```
**CIDR explanation (10.0.0.0/16):**
Defines the IP range of the VPC
10.0.0.0 → starting IP
/16 → subnet mask → 65,536 IPs (10.0.0.0–10.0.255.255)

**Apply tags using environment variables:**
```bash
aws ec2 create-tags --resources "$VPC_ID" --tags Key=Name,Value="VPC_ManageEIPs" Key=Project,Value="$TAG_PROJECT" Key=Component,Value="VPC" Key=Environment,Value="$TAG_ENV" Key=Owner,Value="$TAG_OWNER" Key=ManagedBy,Value="$TAG_MANAGEDBY" Key=CostCenter,Value="$TAG_COSTCENTER" --region "$AWS_REGION" --no-cli-pager
```

**Describe the VPC - VPC tags fully flattened (strictest: 1 object per tag, no arrays):**
```bash
aws ec2 describe-vpcs --region "$AWS_REGION" --no-cli-pager | jq -c '.Vpcs[] | ((.Tags // []) as $T | ($T | map(select(.Key=="Name") | .Value) | .[0]) as $Name | (.VpcId as $V | (if ($T|length)>0 then $T[] else {"Key":"NO-TAGS","Value":"NO-TAGS"} end) | {VpcId:$V, VpcName:($Name // "MISSING-NAME-TAG"), TagKey:.Key, TagValue:.Value}))'
```
or with filter "flatten_tags"
```bash
aws ec2 describe-vpcs --region "$AWS_REGION" --no-cli-pager | jq -L . -c 'include "flatten_tags"; .Vpcs[] | (.Tags // []) as $T | ($T | map(select(.Key=="Name") | .Value) | .[0]) as $Name | .VpcId as $V | (flatten_tags($T) | (if length>0 then .[] else {"Key":"NO-TAGS","Value":"NO-TAGS"} end) | {VpcId:$V, VpcName:($Name // "MISSING-NAME-TAG"), TagKey:.Key, TagValue:.Value})'
```
**Expected output:**
```json
{"VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs","TagKey":"CostCenter","TagValue":"Portfolio"}
{"VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs","TagKey":"Component","TagValue":"VPC"}
{"VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs","TagKey":"Project","TagValue":"ManageEIPs"}
{"VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs","TagKey":"Owner","TagValue":"Malik"}
{"VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs","TagKey":"ManagedBy","TagValue":"CLI"}
{"VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs","TagKey":"Environment","TagValue":"dev"}
{"VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs","TagKey":"Name","TagValue":"VPC_ManageEIPs"}
```

#### 2.2.2 Enable DNS support (required for almost everything AWS-managed)
**Ensuring EC2 instances receive public and private DNS names in a non-default VPC**
```bash
aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-support '{"Value":true}' --region "$AWS_REGION" --no-cli-pager
```

#### 2.2.3 Enable DNS hostnames (required for ALB, EC2 public DNS, etc...):
**Ensuring EC2 instances launched in this VPC are assigned DNS names by AWS (not only IP addresses)**
```bash
aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-hostnames '{"Value":true}' --region "$AWS_REGION" --no-cli-pager
```

#### 2.2.4 Verify VPC state
**Show VPC ID + Name + CIDR + IsDefault**
```bash
aws ec2 describe-vpcs --region "$AWS_REGION" --no-cli-pager | jq -c '.Vpcs[] | ((.Tags // []) as $T | ($T | map(select(.Key=="Name") | .Value) | .[0]) as $Name | (.VpcId as $V | (.CidrBlock // "NO-CIDR") as $C | (if ($T|length)>0 then $T[] else {"Key":"NO-TAGS","Value":"NO-TAGS"} end) | {VpcId:$V, VpcName:($Name // "MISSING-NAME-TAG"), CidrBlock:$C, TagKey:.Key, TagValue:.Value}))'
```
```json
{"VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs","CidrBlock":"10.0.0.0/16","TagKey":"CostCenter","TagValue":"Portfolio"}
{"VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs","CidrBlock":"10.0.0.0/16","TagKey":"Component","TagValue":"VPC"}
{"VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs","CidrBlock":"10.0.0.0/16","TagKey":"Project","TagValue":"ManageEIPs"}
{"VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs","CidrBlock":"10.0.0.0/16","TagKey":"Owner","TagValue":"Malik"}
{"VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs","CidrBlock":"10.0.0.0/16","TagKey":"ManagedBy","TagValue":"CLI"}
{"VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs","CidrBlock":"10.0.0.0/16","TagKey":"Environment","TagValue":"dev"}
{"VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs","CidrBlock":"10.0.0.0/16","TagKey":"Name","TagValue":"VPC_ManageEIPs"}
```

**To show just “VPC_ID + VPC Name” (fast confirm)**
```bash
aws ec2 describe-vpcs --vpc-ids "$VPC_ID" --region "$AWS_REGION" --no-cli-pager | jq -c '.Vpcs[] | ((.Tags // []) as $T | ($T | map(select(.Key=="Name") | .Value) | .[0]) as $Name | {VpcId:.VpcId, VpcName:($Name // "MISSING-NAME-TAG"), CidrBlock:(.CidrBlock // "NO-CIDR"), IsDefault:(.IsDefault // false)})'
```
**Expected output:**
```json
{"VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs","CidrBlock":"10.0.0.0/16","IsDefault":false}
```

**Confirm the VPC I just created is the one tagged `VPC_ManageEIPs` (ID lookup by name)**
```bash
aws ec2 describe-vpcs --filters "Name=tag:Name,Values=VPC_ManageEIPs" --region "$AWS_REGION" --no-cli-pager | jq -c '.Vpcs[] | ((.Tags // []) as $T | ($T | map(select(.Key=="Name") | .Value) | .[0]) as $Name | {VpcId:.VpcId, VpcName:($Name // "MISSING-NAME-TAG"), CidrBlock:(.CidrBlock // "NO-CIDR"), IsDefault:(.IsDefault // false)})'
```
```json
{"VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs","CidrBlock":"10.0.0.0/16","IsDefault":false}
```



## 3. Internet Gateway (IGW) 
### 3.1 Create Internet Gateway (`IGW_ManageEIPs`) and verify
#### 3.1.1 Create Internet Gateway (variable assignment)
```bash
IGW_ID=$(aws ec2 create-internet-gateway --region "$AWS_REGION" --no-cli-pager | jq -r '.InternetGateway.InternetGatewayId')
```

#### 3.1.2 Apply tags to Internet Gateway (policy-based tagging)
```bash
aws ec2 create-tags --resources "$IGW_ID" --tags Key=Name,Value="IGW_ManageEIPs" Key=Project,Value="$TAG_PROJECT" Key=Component,Value="IGW" Key=Environment,Value="$TAG_ENV" Key=Owner,Value="$TAG_OWNER" Key=ManagedBy,Value="$TAG_MANAGEDBY" Key=CostCenter,Value="$TAG_COSTCENTER" --region "$AWS_REGION" --no-cli-pager
```

#### 3.1.3 Verify Internet Gateway creation (JSONL, ID + Name adjacent)
```bash
aws ec2 describe-internet-gateways --internet-gateway-ids "$IGW_ID" --region "$AWS_REGION" --no-cli-pager | jq -c '.InternetGateways[] | ((.Tags // []) as $T | ($T | map(select(.Key=="Name") | .Value) | .[0]) as $Name | {InternetGatewayId:.InternetGatewayId, Name:($Name // "NO-NAME"), AttachedVpcId:((.Attachments // [])[0].VpcId // "NOT-ATTACHED")})'
```
**Example output:**
```json
{"InternetGatewayId":"igw-0b3e1619bb8b88318","Name":"IGW_ManageEIPs","AttachedVpcId":"NOT-ATTACHED"}
```


### 3.2 Attach IGW to VPC and verify:
#### 3.2.1 Attach Internet Gateway to the VPC (imperative action)
```bash
aws ec2 attach-internet-gateway --internet-gateway-id "$IGW_ID" --vpc-id "$VPC_ID" --region "$AWS_REGION" --no-cli-pager
```
#### 3.2.2 Build `VPCId` → `VpcName` lookup map (required for verification)
```bash
VPCNAMES=$(aws ec2 describe-vpcs --region "$AWS_REGION" --no-cli-pager | jq -c '.Vpcs[] | {VpcId:.VpcId, VpcName:(((.Tags // []) | map(select(.Key=="Name") | .Value) | .[0]) // "NO-VPC-NAME")}' | jq -s 'reduce .[] as $v ({}; .[$v.VpcId]=$v.VpcName)')
```

#### 3.2.3 Verify Internet Gateway attachment (JSONL, ID + Name adjacent, with VPC Name)
```bash
aws ec2 describe-internet-gateways --internet-gateway-ids "$IGW_ID" --region "$AWS_REGION" --no-cli-pager | jq -c --argjson vpcnames "$VPCNAMES" '.InternetGateways[] | ((.Tags // []) as $T | ($T | map(select(.Key=="Name") | .Value) | .[0]) as $IgwName | (((.Attachments // [])[0].VpcId) // "NOT-ATTACHED") as $V | {InternetGatewayId:.InternetGatewayId, Name:($IgwName // "NO-NAME"), VpcId:$V, VpcName:(if $V=="NOT-ATTACHED" then "NOT-ATTACHED" else ($vpcnames[$V] // "NO-VPC-NAME") end)})'
```
**Expected output:**
```json
{"InternetGatewayId":"igw-0b3e1619bb8b88318","Name":"IGW_ManageEIPs","VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs"}
```



## 4. Subnet Design and Creation
### 4.1 Verify and derive VPC_ID from VPC Name (to know where the subnet is created)
#### 4.1.1 Variable assignment:
```bash
VPC_ID=$(aws ec2 describe-vpcs --filters "Name=tag:Name,Values=VPC_ManageEIPs" --region "$AWS_REGION" --no-cli-pager | jq -r '.Vpcs[0].VpcId')
```

#### 4.1.2 Verify VPC (ID, Name, CIDR):
```bash
aws ec2 describe-vpcs --vpc-ids "$VPC_ID" --region "$AWS_REGION" --no-cli-pager | jq -c '.Vpcs[0] | {VpcId:.VpcId, VpcName:([.Tags[]? | select(.Key=="Name") | .Value] | first // "NO-VPC-NAME"), CidrBlock:.CidrBlock}'
```
**Expected output:**
```json
{"VpcId":"vpc-0df77650a8551f9cf","VpcName":"VPC_ManageEIPs","CidrBlock":"10.0.0.0/16"}
```

#### 4.1.3 4.1.3 Derive and store VPC CIDR block
```bash
VPC_CIDR=$(aws ec2 describe-vpcs --vpc-ids "$VPC_ID" --region "$AWS_REGION" --no-cli-pager | jq -r '.Vpcs[0].CidrBlock')
```


### 4.2 Subnet CIDR assignment (AZ1 – App / Private)
```bash
SUBNET_CIDR="10.0.1.0/24"
```
**CIDR explanation (10.0.1.0/24):**
Subnet range: 10.0.1.0–10.0.1.255  
256 addresses, 251 usable (AWS reserves 5)  


### 4.3 Create the subnet `Subnet_ManageEIPs_AZ1` in `VPC_ManageEIPs` with `NameTag`, and capture `SUBNET_ID`
```bash
SUBNET_ID=$(aws ec2 create-subnet --vpc-id "$VPC_ID" --cidr-block "$SUBNET_CIDR" --availability-zone "$AWS_AZ" --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=Subnet_ManageEIPs_AZ1},{Key=Project,Value=$TAG_PROJECT},{Key=Component,Value=Subnet},{Key=Environment,Value=$TAG_ENV},{Key=Owner,Value=$TAG_OWNER},{Key=ManagedBy,Value=$TAG_MANAGEDBY},{Key=CostCenter,Value=$TAG_COSTCENTER}]" --region "$AWS_REGION" --no-cli-pager | jq -r '.Subnet.SubnetId')
```


### 4.4 Make it private-by-default immediately (no auto public IPv4), which can be modified by routing later
```bash
aws ec2 modify-subnet-attribute --subnet-id "$SUBNET_ID" --no-map-public-ip-on-launch --region "$AWS_REGION" --no-cli-pager
```


### 4.5 Verify subnet includes Subnet + VPC name/id (and private-by-default setting)
```bash
aws ec2 describe-subnets --subnet-ids "$SUBNET_ID" --region "$AWS_REGION" --no-cli-pager | jq -c '.Subnets[0] | {SubnetId:.SubnetId, SubnetName:([.Tags[]? | select(.Key=="Name") | .Value] | first // "NO-SUBNET-NAME"), VpcId:.VpcId, AvailabilityZone:.AvailabilityZone, CidrBlock:.CidrBlock, MapPublicIpOnLaunch:.MapPublicIpOnLaunch}' | while read -r S; 
do VPC_ID=$(echo "$S" | jq -r '.VpcId'); 
VPC_NAME=$(aws ec2 describe-vpcs --vpc-ids "$VPC_ID" --region "$AWS_REGION" --no-cli-pager | jq -r '.Vpcs[0] | ([.Tags[]? | select(.Key=="Name") | .Value] | first // "NO-VPC-NAME")'); 
echo "$S" | jq -c --arg VpcName "$VPC_NAME" '{SubnetId, SubnetName, VpcId, VpcName:$VpcName, AvailabilityZone, CidrBlock, MapPublicIpOnLaunch}'; 
done
```
**Example output, pretty-printed: subnet private by default (no automatic public IPv4 - `MapPublicIpOnLaunch`: false)**
```json
{
  "SubnetId": "subnet-03949955e141deebb",
  "SubnetName": "Subnet_ManageEIPs_AZ1",
  "VpcId": "vpc-0df77650a8551f9cf",
  "VpcName": "VPC_ManageEIPs",
  "AvailabilityZone": "us-east-1a",
  "CidrBlock": "10.0.1.0/24",
  "MapPublicIpOnLaunch": false
}
```
 


## 5. Routing and Route Tables

### 5.1 Create route table `RTB_ManageEIPs`
```bash
RTB_ID=$(aws ec2 create-route-table --vpc-id "$VPC_ID" --tag-specifications "ResourceType=route-table,Tags=[{Key=Name,Value=RTB_ManageEIPs},{Key=Project,Value=$TAG_PROJECT},{Key=Component,Value=RouteTable},{Key=Environment,Value=$TAG_ENV},{Key=Owner,Value=$TAG_OWNER},{Key=ManagedBy,Value=$TAG_MANAGEDBY},{Key=CostCenter,Value=$TAG_COSTCENTER}]" --region "$AWS_REGION" --no-cli-pager | jq -r '.RouteTable.RouteTableId')
```


### 5.2 Verify route table attributes (ID, Name, VPC, Main)
```bash
aws ec2 describe-route-tables --route-table-ids "$RTB_ID" --region "$AWS_REGION" --no-cli-pager | jq -c '.RouteTables[0] | {RouteTableId:.RouteTableId, RouteTableName:([.Tags[]? | select(.Key=="Name") | .Value] | first // "NO-RT-NAME"), VpcId:.VpcId, Main:([.Associations[]? | select(.Main==true)] | length>0)}' | while read -r R; 
do VPC_ID=$(echo "$R" | jq -r '.VpcId'); VPC_NAME=$(aws ec2 describe-vpcs --vpc-ids "$VPC_ID" --region "$AWS_REGION" --no-cli-pager | jq -r '.Vpcs[0] | ([.Tags[]? | select(.Key=="Name") | .Value] | first // "NO-VPC-NAME")'); 
echo "$R" | jq --arg VpcName "$VPC_NAME" '{RouteTableId, RouteTableName, VpcId, VpcName:$VpcName, Main}'; 
done
```
**Example output, pretty-printed:**
```json
{
  "RouteTableId": "rtb-0435ffce50cebdffb",
  "RouteTableName": "RTB_ManageEIPs",
  "VpcId": "vpc-0df77650a8551f9cf",
  "VpcName": "VPC_ManageEIPs",
  "Main": false
}
```
**Explanation of '"Main": false':**
This means that this route table is NOT the VPC's default route table.
The route table does nothing until you explicitly associate it to a subnet.


### 5.3 Associate route table `RTB_ManageEIPs` with `Subnet_ManageEIPs_AZ1` (route table becomes effective)
```bash
RTB_ASSOC_ID=$(aws ec2 associate-route-table --route-table-id "$RTB_ID" --subnet-id "$SUBNET_ID" --region "$AWS_REGION" --no-cli-pager | jq -r '.AssociationId')
```
#### 5.3.1 Verify association by subnet (subnet → route table)
Answers the question: “Given this subnet, which route table is actually associated with it?”

```bash
aws ec2 describe-route-tables --filters "Name=association.subnet-id,Values=$SUBNET_ID" --region "$AWS_REGION" --no-cli-pager | jq -c '.RouteTables[0] | {RouteTableId:.RouteTableId, RouteTableName:([.Tags[]? | select(.Key=="Name") | .Value] | first // "NO-RT-NAME"), VpcId:.VpcId, Main:([.Associations[]? | select(.Main==true)] | length>0), AssociationId:([.Associations[]? | select(.SubnetId=="'"$SUBNET_ID"'") | .RouteTableAssociationId] | first // "NO-ASSOCIATION")}' | while read -r R; 
do SUBNET_NAME=$(aws ec2 describe-subnets --subnet-ids "$SUBNET_ID" --region "$AWS_REGION" --no-cli-pager | jq -r '.Subnets[0] | ([.Tags[]? | select(.Key=="Name") | .Value] | first // "NO-SUBNET-NAME")'); 
VPC_ID=$(echo "$R" | jq -r '.VpcId'); VPC_NAME=$(aws ec2 describe-vpcs --vpc-ids "$VPC_ID" --region "$AWS_REGION" --no-cli-pager | jq -r '.Vpcs[0] | ([.Tags[]? | select(.Key=="Name") | .Value] | first // "NO-VPC-NAME")'); 
echo "$R" | jq --arg SubnetId "$SUBNET_ID" --arg SubnetName "$SUBNET_NAME" --arg VpcName "$VPC_NAME" '{SubnetId:$SubnetId, SubnetName:$SubnetName, RouteTableId, RouteTableName, AssociationId, VpcId, VpcName:$VpcName, Main}'; 
done
```
**Example output, pretty-printed:**
```json
{
  "SubnetId": "subnet-03949955e141deebb",
  "SubnetName": "Subnet_ManageEIPs_AZ1",
  "RouteTableId": "rtb-0435ffce50cebdffb",
  "RouteTableName": "RTB_ManageEIPs",
  "AssociationId": "rtbassoc-0dd77938d3acde36b",
  "VpcId": "vpc-0df77650a8551f9cf",
  "VpcName": "VPC_ManageEIPs",
  "Main": false
}
```

#### 5.3.2 Verify association by route table (route table → subnets)
Answers the question: “Given this route table, which subnets are explicitly associated with it?”

```bash
aws ec2 describe-route-tables --route-table-ids "$RTB_ID" --region "$AWS_REGION" --no-cli-pager | jq -c '.RouteTables[0] | {RouteTableId:.RouteTableId, RouteTableName:([.Tags[]? | select(.Key=="Name") | .Value] | first // "NO-RT-NAME"), VpcId:.VpcId, Main:([.Associations[]? | select(.Main==true)] | length>0), SubnetId:([.Associations[]? | select(.SubnetId!=null) | .SubnetId] | first // "NO-SUBNET"), AssociationId:([.Associations[]? | select(.SubnetId!=null) | .RouteTableAssociationId] | first // "NO-ASSOCIATION")}' | while read -r R; 
do SID=$(echo "$R" | jq -r '.SubnetId'); 
SNAME=$(aws ec2 describe-subnets --subnet-ids "$SID" --region "$AWS_REGION" --no-cli-pager | jq -r '.Subnets[0] | ([.Tags[]? | select(.Key=="Name") | .Value] | first // "NO-SUBNET-NAME")'); 
VPC_ID=$(echo "$R" | jq -r '.VpcId'); 
VPC_NAME=$(aws ec2 describe-vpcs --vpc-ids "$VPC_ID" --region "$AWS_REGION" --no-cli-pager | jq -r '.Vpcs[0] | ([.Tags[]? | select(.Key=="Name") | .Value] | first // "NO-VPC-NAME")'); 
echo "$R" | jq --arg SubnetName "$SNAME" --arg VpcName "$VPC_NAME" '{RouteTableId, RouteTableName, SubnetId, SubnetName:$SubnetName, AssociationId, VpcId, VpcName:$VpcName, Main}'; 
done
```
**Example output, pretty-printed:**
```json
{
  "RouteTableId": "rtb-0435ffce50cebdffb",
  "RouteTableName": "RTB_ManageEIPs",
  "SubnetId": "subnet-03949955e141deebb",
  "SubnetName": "Subnet_ManageEIPs_AZ1",
  "AssociationId": "rtbassoc-0dd77938d3acde36b",
  "VpcId": "vpc-0df77650a8551f9cf",
  "VpcName": "VPC_ManageEIPs",
  "Main": false
}
```
In future sections, verification will primarily be subnet-based.


### 5.4 Confirm subnet is private (no public IPv4, no default route)
**Confirm subnet is private-by-default (no public IPv4 on launch):**
```bash
aws ec2 describe-subnets --subnet-ids "$SUBNET_ID" --region "$AWS_REGION" --no-cli-pager | jq -c '.Subnets[0] | {SubnetId:.SubnetId, SubnetName:([.Tags[]? | select(.Key=="Name") | .Value] | first // "NO-SUBNET-NAME"), MapPublicIpOnLaunch:.MapPublicIpOnLaunch}'
```
**Expected output ('"MapPublicIpOnLaunch": false' means EC2 instances won't get public IPv4):**
```json
{"SubnetId":"subnet-03949955e141deebb","SubnetName":"Subnet_ManageEIPs_AZ1","MapPublicIpOnLaunch":false}
```

**Confirm route table has no 0.0.0.0/0 route (and show RTB name + routes):**
```bash
aws ec2 describe-route-tables --route-table-ids "$RTB_ID" --region "$AWS_REGION" --no-cli-pager | jq '.RouteTables[0] | {RouteTableId:.RouteTableId, RouteTableName:([.Tags[]? | select(.Key=="Name") | .Value] | first // "NO-RT-NAME"), DefaultRouteTarget:([.Routes[]? | select(.DestinationCidrBlock=="0.0.0.0/0") | (.GatewayId // .NatGatewayId // .InstanceId // .TransitGatewayId // .VpcPeeringConnectionId // "UNKNOWN")] | first // "NO-DEFAULT-ROUTE"), Routes:[.Routes[]? | {Dest:(.DestinationCidrBlock // "n/a"), Target:(.GatewayId // .NatGatewayId // .InstanceId // .TransitGatewayId // .VpcPeeringConnectionId // "n/a")}]}' 
```
**Expected output: "DefaultRouteTarget": "NO-DEFAULT-ROUTE"**
That means the Route table has no internet default route.
The only route is 10.0.0.0/16 → local (VPC-internal traffic only)
```json
{
  "RouteTableId": "rtb-0435ffce50cebdffb",
  "RouteTableName": "RTB_ManageEIPs",
  "DefaultRouteTarget": "NO-DEFAULT-ROUTE",
  "Routes": [
    {
      "Dest": "10.0.0.0/16",
      "Target": "local"
    }
  ]
}
```



## 6. Systems Manager (SSM) Access

### 6.1 IAM prerequisites for Systems Manager (IAM + Agent):
Goal: ensure the instance can talk to SSM.
It covers:
- IAM role/policy for EC2
- SSM Agent presence/running

#### 6.1.1 Identify the EC2 IAM role intended for SSM access
**First, list candidate roles looking like SSM (show RoleName + RoleArn):**
```bash
aws iam list-roles --no-cli-pager | jq -c '.Roles[] | select(.RoleName | test("SSM|ManageEIPs";"i")) | {RoleName:.RoleName, RoleArn:.Arn}'
```
**Expected output:**
```json
{"RoleName":"AWSServiceRoleForAmazonSSM","RoleArn":"arn:aws:iam::180294215772:role/aws-service-role/ssm.amazonaws.com/AWSServiceRoleForAmazonSSM"}
{"RoleName":"AWSServiceRoleForAmazonSSM_AccountDiscovery","RoleArn":"arn:aws:iam::180294215772:role/aws-service-role/accountdiscovery.ssm.amazonaws.com/AWSServiceRoleForAmazonSSM_AccountDiscovery"}
{"RoleName":"AWSServiceRoleForSSMQuickSetup","RoleArn":"arn:aws:iam::180294215772:role/aws-service-role/ssm-quicksetup.amazonaws.com/AWSServiceRoleForSSMQuickSetup"}
{"RoleName":"EC2SSMRole","RoleArn":"arn:aws:iam::180294215772:role/EC2SSMRole"}
{"RoleName":"ManageEIPs-EC2SSMRole","RoleArn":"arn:aws:iam::180294215772:role/ManageEIPs-EC2SSMRole"}
{"RoleName":"ManageEIPsLambdaRole","RoleArn":"arn:aws:iam::180294215772:role/ManageEIPsLambdaRole"}
```
It looks as if it would be `ManageEIPs-EC2SSMRole`.

#### 6.1.2 List attached managed policies to the `ManageEIPs-EC2SSMRole` role (Name + ARN):
```bash
aws iam list-attached-role-policies --role-name "ManageEIPs-EC2SSMRole" --no-cli-pager | jq -c '.AttachedPolicies[] | {PolicyName:.PolicyName, PolicyArn:.PolicyArn}'
```
**Example output (we only need `AmazonSSMManagedInstanceCore` and `CloudWatchAgentServerPolicy`):**
```json
{"PolicyName":"CloudWatchAgentServerPolicy","PolicyArn":"arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"}
{"PolicyName":"AmazonSSMManagedInstanceCore","PolicyArn":"arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"}
```

#### 6.1.3 Check that no inline policies are attached to the `ManageEIPs-EC2SSMRole` role (least privilege)
```bash
aws iam list-role-policies --role-name "ManageEIPs-EC2SSMRole" --no-cli-pager | jq -r '.PolicyNames[]'  # no output
```
The role was fine, with the proper policies attached but without the proper tagging.
I decided to delete it and recreate it.


### 6.2 Delete and clean up the existing `ManageEIPs-EC2SSMRole`
#### 6.2.1 Detach all managed policies from the role:
```bash
aws iam detach-role-policy --role-name "ManageEIPs-EC2SSMRole" --policy-arn "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore" --no-cli-pager;
aws iam detach-role-policy --role-name "ManageEIPs-EC2SSMRole" --policy-arn "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy" --no-cli-pager
```

#### 6.2.2 Confirm no inline policies are attached (documentation only)
```bash
aws iam list-role-policies --role-name "ManageEIPs-EC2SSMRole" --no-cli-pager | jq -r '.PolicyNames[]'  # no output
```

#### 6.2.3 Ensure the role is not attached to an instance profile
**List instance profiles using the role:**
```bash
aws iam list-instance-profiles-for-role --role-name "ManageEIPs-EC2SSMRole" --no-cli-pager | jq -r '.InstanceProfiles[].InstanceProfileName'    # ManageEIPs-EC2SSMInstanceProfile
```

#### 6.2.4 Detach the role from the instance profile `ManageEIPs-EC2SSMInstanceProfile`
```bash
aws iam remove-role-from-instance-profile --instance-profile-name "ManageEIPs-EC2SSMInstanceProfile" --role-name "ManageEIPs-EC2SSMRole" --no-cli-pager
```

#### 6.2.5 Delete the `ManageEIPs-EC2SSMInstanceProfile` instance profile
```bash
aws iam delete-instance-profile --instance-profile-name "ManageEIPs-EC2SSMInstanceProfile" --no-cli-pager
```

#### 6.2.6 Check the `ManageEIPs-EC2SSMInstanceProfile` instance profile is gone ("No SuchEntity")
```bash
aws iam get-instance-profile --instance-profile-name "ManageEIPs-EC2SSMInstanceProfile" --no-cli-pager
```
**Expected output:**
```text
An error occurred (NoSuchEntity) when calling the `GetInstanceProfile` operation: Instance Profile `ManageEIPs-EC2SSMInstanceProfile` cannot be found.
```

#### 6.2.7 Delete the `ManageEIPs-EC2SSMRole` role
```bash
aws iam delete-role --role-name "ManageEIPs-EC2SSMRole" --no-cli-pager
```

#### 6.2.8 Verify deletion of the `ManageEIPs-EC2SSMRole` role (error expected)
```bash
aws iam get-role --role-name "ManageEIPs-EC2SSMRole" --no-cli-pager
```
**Expected output:**
```text
An error occurred (NoSuchEntity) when calling the GetRole operation: The role with name ManageEIPs-EC2SSMRole cannot be found.
```


### 6.3 Create the EC2 trust role for Systems Manager (named `ManageEIPs-EC2SSMRole`)
**Allows EC2 instances to assume the `ManageEIPs-EC2SSMRole`**
```bash
aws iam create-role --role-name "ManageEIPs-EC2SSMRole" --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' --tags Key=Name,Value="ManageEIPs-EC2SSMRole" Key=Project,Value="$TAG_PROJECT" Key=Component,Value="IAMRole" Key=Environment,Value="$TAG_ENV" Key=Owner,Value="$TAG_OWNER" Key=ManagedBy,Value="$TAG_MANAGEDBY" Key=CostCenter,Value="$TAG_COSTCENTER" --no-cli-pager | jq -r '.Role.Arn'
```
**Expected output:**
```text
arn:aws:iam::180294215772:role/ManageEIPs-EC2SSMRole
```

#### 6.3.1 Attach the `AmazonSSMManagedInstanceCore` policy
It grants the minimum permissions required for Systems Manager (Session Manager, Run Command, inventory, patching).
```bash
aws iam attach-role-policy --role-name "ManageEIPs-EC2SSMRole" --policy-arn "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore" --no-cli-pager
```

#### 6.3.2 Attach CloudWatch Agent policy
The CloudWatch Agent is installed and configured to collect custom OS metrics (disk, memory, swap) and to forward application logs to CloudWatch Logs. 
Systems Manager alone does not provide these capabilities. 
The ManageEIPs-EC2SSMRole role therefore includes the CloudWatchAgentServerPolicy in addition to the SSM core permissions.

```bash
aws iam attach-role-policy --role-name "ManageEIPs-EC2SSMRole" --policy-arn "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy" --no-cli-pager
```

#### 6.3.3 Verify both policies have been attached to the role
```bash
aws iam list-attached-role-policies --role-name "ManageEIPs-EC2SSMRole" --no-cli-pager | jq -c '.AttachedPolicies[] | {PolicyName:.PolicyName, PolicyArn:.PolicyArn}'
```
**Example output:**
```json
{"PolicyName":"CloudWatchAgentServerPolicy","PolicyArn":"arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"}
{"PolicyName":"AmazonSSMManagedInstanceCore","PolicyArn":"arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"}
```

#### 6.3.4 Verify no inline policy has been attached to the role (least privilege):
```bash
aws iam list-role-policies --role-name "ManageEIPs-EC2SSMRole" --no-cli-pager | jq -r '.PolicyNames[]'  # no output
```

#### 6.3.5 Create an instance profile for the EC2 role
```bash
aws iam create-instance-profile --instance-profile-name "ManageEIPs-EC2SSMInstanceProfile" --tags Key=Name,Value="ManageEIPs-EC2SSMInstanceProfile" Key=Project,Value="$TAG_PROJECT" Key=Component,Value="InstanceProfile" Key=Environment,Value="$TAG_ENV" Key=Owner,Value="$TAG_OWNER" Key=ManagedBy,Value="$TAG_MANAGEDBY" Key=CostCenter,Value="$TAG_COSTCENTER" --no-cli-pager | jq -r '.InstanceProfile.Arn'
```
**Example output (we need `AmazonSSMManagedInstanceCore`):**
```text
arn:aws:iam::180294215772:instance-profile/ManageEIPs-EC2SSMInstanceProfile
```

#### 6.3.6 Add the role to the instance profile
```bash
aws iam add-role-to-instance-profile --instance-profile-name "ManageEIPs-EC2SSMInstanceProfile" --role-name "ManageEIPs-EC2SSMRole" --no-cli-pager
```

#### 6.3.7 Verify instance profile wiring
```bash
aws iam get-instance-profile --instance-profile-name "ManageEIPs-EC2SSMInstanceProfile" --no-cli-pager | jq '.InstanceProfile | {InstanceProfileName:.InstanceProfileName, InstanceProfileArn:.Arn, Roles:[.Roles[] | {RoleName:.RoleName, RoleArn:.Arn}]}'
```
**Expected output:**
```json
{
  "InstanceProfileName": "ManageEIPs-EC2SSMInstanceProfile",
  "InstanceProfileArn": "arn:aws:iam::180294215772:instance-profile/ManageEIPs-EC2SSMInstanceProfile",
  "Roles": [
    {
      "RoleName": "ManageEIPs-EC2SSMRole",
      "RoleArn": "arn:aws:iam::180294215772:role/ManageEIPs-EC2SSMRole"
    }
  ]
}
```
Now a dedicated, least-privilege EC2 role (`ManageEIPs-EC2SSMRole`) has been created for Systems Manager access and attached via an instance profile. 
The role includes `AmazonSSMManagedInstanceCore` for Systems Manager access and `CloudWatchAgentServerPolicy` to enable custom OS metrics and application log forwarding to CloudWatch.
This is required by the project design and follows least privilege principles.
 


## 7. Private Operations Access via VPC Interface Endpoints
### 7.1 Overview: SSM interface endpoints required
Goal: Allow the EC2 instance in a private subnet (no IGW, no NAT) to reach SSM services over the AWS network.

SSM requires three interface endpoints (ssm, ec2messages, ssmmessages). 
Together they provide instance registration, command delivery, and interactive session channels. 
All are created with "Private DNS enabled" to allow SSM connectivity from a private subnet without internet access.

SSM requires exactly 3 interface endpoints, in the same VPC for 3 VPC Endpoint service names: 'com.amazonaws.<region>.<service>'
- SSM (endpoint for AWS Systems Manager) > com.amazonaws.${AWS_REGION}.ssm.
    Instance registers with Systems Manager.
    Inventory, patching, automation metadata.
    Required for SSM to “know” the instance exists.

- ec2messages (endpoint for Command Delivery Channel) > com.amazonaws.${AWS_REGION}.ec2messages
    Delivers Run Command instructions.
    Used by SSM Agent to receive tasks.
      Without it:
        Instance appears offline.
        Commands never reach the instance.

- ssmmessages (endpoint for Session Manager Data Channel) > com.amazonaws.${AWS_REGION}.ssmmessages
    Interactive shell (Session Manager).
    Bi-directional streaming of input/output.
      Without it:
        aws ssm start-session fails.
        No interactive access.


### 7.2 Endpoint security group for SSM interface endpoints
This SG will allow HTTPS from the VPC only to the endpoints.

#### 7.2.1 Create endpoint security group (capture `SSM_EP_SG_ID`)
```bash
SSM_EP_SG_ID=$(aws ec2 create-security-group --group-name "SG_SSM_Endpoints_ManageEIPs" --description "SSM interface endpoints SG" --vpc-id "$VPC_ID" --region "$AWS_REGION" --no-cli-pager | jq -r '.GroupId')
```

#### 7.2.2 Apply standard tags to endpoint SG
```bash
aws ec2 create-tags --resources "$SSM_EP_SG_ID" --tags Key=Name,Value="SG_SSM_Endpoints_ManageEIPs" Key=Project,Value="$TAG_PROJECT" Key=Component,Value="SecurityGroup" Key=Environment,Value="$TAG_ENV" Key=Owner,Value="$TAG_OWNER" Key=ManagedBy,Value="$TAG_MANAGEDBY" Key=CostCenter,Value="$TAG_COSTCENTER" --region "$AWS_REGION" --no-cli-pager
```

#### 7.2.3 Verify endpoint SG identity and VPC context
```bash
aws ec2 describe-security-groups --group-ids "$SSM_EP_SG_ID" --region "$AWS_REGION" --no-cli-pager | jq -c '.SecurityGroups[0] | {GroupId:.GroupId, GroupName:.GroupName, NameTag:([.Tags[]? | select(.Key=="Name") | .Value] | first // "MISSING-NAME-TAG"), VpcId:.VpcId}' | while read -r S; 
do VPC_ID=$(echo "$S" | jq -r '.VpcId'); 
VPC_NAME=$(aws ec2 describe-vpcs --vpc-ids "$VPC_ID" --region "$AWS_REGION" --no-cli-pager | jq -r '.Vpcs[0] | ([.Tags[]? | select(.Key=="Name") | .Value] | first // "NO-VPC-NAME")'); 
echo "$S" | jq --arg VpcName "$VPC_NAME" '. + {VpcName:$VpcName}'; 
done
```
**Example output:**
```json
{
  "GroupId": "sg-0370469d180f27a70",
  "GroupName": "SG_SSM_Endpoints_ManageEIPs",
  "NameTag": "SG_SSM_Endpoints_ManageEIPs",
  "VpcId": "vpc-0df77650a8551f9cf",
  "VpcName": "VPC_ManageEIPs"
}
```

#### 7.2.4 Ensure `NameTag` differs from `GroupName`
```bash
aws ec2 create-tags --resources "$SSM_EP_SG_ID" --tags Key=Name,Value="SG_SSM_Endpoints_ManageEIPs_tag" --region "$AWS_REGION" --no-cli-pager
```
**Example output:**
```json
{
  "GroupId": "sg-0370469d180f27a70",
  "GroupName": "SG_SSM_Endpoints_ManageEIPs",
  "NameTag": "SG_SSM_Endpoints_ManageEIPs_tag",
  "VpcId": "vpc-0df77650a8551f9cf",
  "VpcName": "VPC_ManageEIPs"
}
```

#### 7.2.5 Allow HTTPS ingress from VPC CIDR (443)
```bash
aws ec2 authorize-security-group-ingress --group-id "$SSM_EP_SG_ID" --protocol tcp --port 443 --cidr "$VPC_CIDR" --region "$AWS_REGION" --no-cli-pager || true
```

#### 7.2.6 Tag HTTPS ingress rule
```bash
SSM_HTTPS_RULE_ID=$(aws ec2 describe-security-group-rules --filters "Name=group-id,Values=$SSM_EP_SG_ID" --region "$AWS_REGION" --no-cli-pager | jq -r --arg CIDR "$VPC_CIDR" '.SecurityGroupRules[] | select(.IsEgress==false and .IpProtocol=="tcp" and .FromPort==443 and .ToPort==443 and .CidrIpv4==$CIDR) | .SecurityGroupRuleId'); 
aws ec2 create-tags --resources "$SSM_HTTPS_RULE_ID" --tags Key=Name,Value="SSM-HTTPS-From-VPC" Key=Purpose,Value="Allow SSM interface endpoint access from VPC CIDR" Key=Project,Value="$TAG_PROJECT" Key=Component,Value="SecurityGroupRule" Key=Environment,Value="$TAG_ENV" Key=Owner,Value="$TAG_OWNER" Key=ManagedBy,Value="$TAG_MANAGEDBY" Key=CostCenter,Value="$TAG_COSTCENTER" --region "$AWS_REGION" --no-cli-pager
```

#### 7.2.7 Verify HTTPS ingress rule (IDs + Names + VPC context)
```bash
RULE_ID=$(aws ec2 describe-security-group-rules --filters "Name=group-id,Values=$SSM_EP_SG_ID" --region "$AWS_REGION" --no-cli-pager | jq -r --arg CIDR "$VPC_CIDR" '.SecurityGroupRules[] | select(.IsEgress==false and .IpProtocol=="tcp" and .FromPort==443 and .ToPort==443 and .CidrIpv4==$CIDR) | .SecurityGroupRuleId' | head -n 1); 
aws ec2 describe-security-group-rules --security-group-rule-ids "$RULE_ID" --region "$AWS_REGION" --no-cli-pager | jq -c '.SecurityGroupRules[0] | {SecurityGroupRuleId:.SecurityGroupRuleId, RuleName:([.Tags[]? | select(.Key=="Name") | .Value] | first // "NO-RULE-NAME"), GroupId:.GroupId, IpProtocol:.IpProtocol, FromPort:.FromPort, ToPort:.ToPort, CidrIpv4:.CidrIpv4}' | while read -r R; 
do SG_ID=$(echo "$R" | jq -r '.GroupId'); 
aws ec2 describe-security-groups --group-ids "$SG_ID" --region "$AWS_REGION" --no-cli-pager | jq -c '.SecurityGroups[0] | {GroupId:.GroupId, GroupName:.GroupName, NameTag:([.Tags[]? | select(.Key=="Name") | .Value] | first // "NO-SG-NAME"), VpcId:.VpcId}' | while read -r S; 
do VPC_ID=$(echo "$S" | jq -r '.VpcId'); 
VPC_NAME=$(aws ec2 describe-vpcs --vpc-ids "$VPC_ID" --region "$AWS_REGION" --no-cli-pager | jq -r '.Vpcs[0] | ([.Tags[]? | select(.Key=="Name") | .Value] | first // "NO-VPC-NAME")'); 
echo "$R" | jq --argjson SG "$(echo "$S")" --arg VpcName "$VPC_NAME" '. + {GroupName:$SG.GroupName, SecurityGroupNameTag:$SG.NameTag, VpcId:$SG.VpcId, VpcName:$VpcName}'; 
done; 
done
```
**Example output:**
```json
{
  "SecurityGroupRuleId": "sgr-07381919f8be03c46",
  "RuleName": "SSM-HTTPS-From-VPC",
  "GroupId": "sg-0370469d180f27a70",
  "IpProtocol": "tcp",
  "FromPort": 443,
  "ToPort": 443,
  "CidrIpv4": "10.0.0.0/16",
  "GroupName": "SG_SSM_Endpoints_ManageEIPs",
  "SecurityGroupNameTag": "SG_SSM_Endpoints_ManageEIPs",
  "VpcId": "vpc-0df77650a8551f9cf",
  "VpcName": "VPC_ManageEIPs"
}
```

#### 7.2.8 Create the 3 SSM interface endpoints
**Create VPC interface endpoint `ssm`:**
```bash
SSM_VPCE_ID=$(aws ec2 create-vpc-endpoint --vpc-id "$VPC_ID" --service-name "com.amazonaws.${AWS_REGION}.ssm" --vpc-endpoint-type Interface --subnet-ids "$SUBNET_ID" --security-group-ids "$SSM_EP_SG_ID" --private-dns-enabled --tag-specifications "ResourceType=vpc-endpoint,Tags=[{Key=Name,Value=VPCE_SSM_ManageEIPs},{Key=Project,Value=$TAG_PROJECT},{Key=Component,Value=VPCE},{Key=Environment,Value=$TAG_ENV},{Key=Owner,Value=$TAG_OWNER},{Key=ManagedBy,Value=$TAG_MANAGEDBY},{Key=CostCenter,Value=$TAG_COSTCENTER}]" --region "$AWS_REGION" --no-cli-pager | jq -r '.VpcEndpoint.VpcEndpointId')
```

**Create VPC interface endpoint `ec2messages`:**
```bash
EC2MSG_VPCE_ID=$(aws ec2 create-vpc-endpoint --vpc-id "$VPC_ID" --service-name "com.amazonaws.${AWS_REGION}.ec2messages" --vpc-endpoint-type Interface --subnet-ids "$SUBNET_ID" --security-group-ids "$SSM_EP_SG_ID" --private-dns-enabled --tag-specifications "ResourceType=vpc-endpoint,Tags=[{Key=Name,Value=VPCE_EC2Messages_ManageEIPs},{Key=Project,Value=$TAG_PROJECT},{Key=Component,Value=VPCE},{Key=Environment,Value=$TAG_ENV},{Key=Owner,Value=$TAG_OWNER},{Key=ManagedBy,Value=$TAG_MANAGEDBY},{Key=CostCenter,Value=$TAG_COSTCENTER}]" --region "$AWS_REGION" --no-cli-pager | jq -r '.VpcEndpoint.VpcEndpointId')
```

**Create VPC interface endpoint `ssmmessages`:**
```bash
SSMM_VPCE_ID=$(aws ec2 create-vpc-endpoint --vpc-id "$VPC_ID" --service-name "com.amazonaws.${AWS_REGION}.ssmmessages" --vpc-endpoint-type Interface --subnet-ids "$SUBNET_ID" --security-group-ids "$SSM_EP_SG_ID" --private-dns-enabled --tag-specifications "ResourceType=vpc-endpoint,Tags=[{Key=Name,Value=VPCE_SSMMessages_ManageEIPs},{Key=Project,Value=$TAG_PROJECT},{Key=Component,Value=VPCE},{Key=Environment,Value=$TAG_ENV},{Key=Owner,Value=$TAG_OWNER},{Key=ManagedBy,Value=$TAG_MANAGEDBY},{Key=CostCenter,Value=$TAG_COSTCENTER}]" --region "$AWS_REGION" --no-cli-pager | jq -r '.VpcEndpoint.VpcEndpointId')
```

#### 7.2.9 Verify interface endpoints (ID + Name + service + state + subnet + VPC)
**Ensure it ouputs only endpoints with Name not evaluating to `NO-NAME`:**
```bash
aws ec2 describe-vpc-endpoints --vpc-endpoint-ids "$SSM_VPCE_ID" "$EC2MSG_VPCE_ID" "$SSMM_VPCE_ID" --region "$AWS_REGION" --no-cli-pager | jq -c '.VpcEndpoints[] | {VpcEndpointId:.VpcEndpointId, Name:([.Tags[]? | select(.Key=="Name") | .Value] | first // "NO-NAME"), ServiceName:.ServiceName, VpcId:.VpcId, SubnetIds:.SubnetIds, PrivateDnsEnabled:.PrivateDnsEnabled, State:.State}' | while read -r E; 
do VPC_ID=$(echo "$E" | jq -r '.VpcId'); 
VPC_NAME=$(aws ec2 describe-vpcs --vpc-ids "$VPC_ID" --region "$AWS_REGION" --no-cli-pager | jq -r '.Vpcs[0] | ([.Tags[]? | select(.Key=="Name") | .Value] | first // "NO-VPC-NAME")'); 
echo "$E" | jq -c --arg VpcName "$VPC_NAME" '. + {VpcName:$VpcName}'; 
done
```
**Expected output:**
```json
{"VpcEndpointId":"vpce-04a9cae3727e1e9b9","Name":"VPCE_SSM_ManageEIPs","ServiceName":"com.amazonaws.us-east-1.ssm","VpcId":"vpc-0df77650a8551f9cf","SubnetIds":["subnet-03949955e141deebb"],"PrivateDnsEnabled":true,"State":"available","VpcName":"VPC_ManageEIPs"}
{"VpcEndpointId":"vpce-049d68eff853c959f","Name":"VPCE_EC2Messages_ManageEIPs","ServiceName":"com.amazonaws.us-east-1.ec2messages","VpcId":"vpc-0df77650a8551f9cf","SubnetIds":["subnet-03949955e141deebb"],"PrivateDnsEnabled":true,"State":"available","VpcName":"VPC_ManageEIPs"}
{"VpcEndpointId":"vpce-08eac81e86e6b75de","Name":"VPCE_SSMMessages_ManageEIPs","ServiceName":"com.amazonaws.us-east-1.ssmmessages","VpcId":"vpc-0df77650a8551f9cf","SubnetIds":["subnet-03949955e141deebb"],"PrivateDnsEnabled":true,"State":"available","VpcName":"VPC_ManageEIPs"}
```


### 7.3 Private EC2 instance for SSM access
#### 7.3.1 List security groups in the VPC (selection step)
```bash
aws ec2 describe-security-groups --filters "Name=vpc-id,Values=$VPC_ID" --region "$AWS_REGION" --no-cli-pager | jq '.SecurityGroups[] | {GroupId:.GroupId, GroupName:.GroupName, NameTag:([.Tags[]? | select(.Key=="Name") | .Value] | first // "NO-NAME-TAG"), VpcId:.VpcId}'
```
**Expected output:**
```json
{
  "GroupId": "sg-026d671c587113fb7",
  "GroupName": "default",
  "NameTag": "NO-NAME-TAG",
  "VpcId": "vpc-0df77650a8551f9cf"
}
{
  "GroupId": "sg-0370469d180f27a70",
  "GroupName": "SG_SSM_Endpoints_ManageEIPs",
  "NameTag": "SG_SSM_Endpoints_ManageEIPs_tag",
  "VpcId": "vpc-0df77650a8551f9cf"
}
```

#### 7.3.2 EC2 security group:**
We can't use "SG_SSM_Endpoints_ManageEIPs". 
This SG was created for the VPC endpoints ENIs, not for the instance. 
It currently allows inbound 443 from 10.0.0.0/16. 
That does not necessarily match what our EC2 needs.
Therefore, we are creating a new one:
- inbound: keep it minimal (often none, unless your app needs something).
- outbound: must allow 443 to reach the endpoint ENIs (default SG egress allows all, which would be fine).

##### 7.3.2.1 Create EC2 security group (capture `EC2_SG_ID`)
```bash
EC2_SG_ID=$(aws ec2 create-security-group --group-name "SG_EC2_ManageEIPs" --description "Least-privilege EC2 SG (SSM via VPC endpoints, no public access)" --vpc-id "$VPC_ID" --region "$AWS_REGION" --no-cli-pager | jq -r '.GroupId'); 
aws ec2 create-tags --resources "$EC2_SG_ID" --tags Key=Name,Value="SG_EC2_ManageEIPs" Key=Project,Value=ManageEIPs --region "$AWS_REGION" --no-cli-pager
```

**Apply standard tags to EC2 security group:**
```bash
aws ec2 create-tags --resources "$EC2_SG_ID" --tags Key=Name,Value="SG_EC2_ManageEIPs" Key=Project,Value="$TAG_PROJECT" Key=Component,Value="SecurityGroup" Key=Environment,Value="$TAG_ENV" Key=Owner,Value="$TAG_OWNER" Key=ManagedBy,Value="$TAG_MANAGEDBY" Key=CostCenter,Value="$TAG_COSTCENTER" --region "$AWS_REGION" --no-cli-pager
```

##### 7.3.2.2 Verify EC2 SG identity and VPC context
```bash
aws ec2 describe-security-groups --group-ids "$EC2_SG_ID" --region "$AWS_REGION" --no-cli-pager | jq -c '.SecurityGroups[0] | {GroupId:.GroupId, GroupName:.GroupName, NameTag:([.Tags[]? | select(.Key=="Name") | .Value] | first // "NO-NAME-TAG"), VpcId:.VpcId}' | while read -r S; 
do VPC_NAME=$(aws ec2 describe-vpcs --vpc-ids "$(echo "$S" | jq -r '.VpcId')" --region "$AWS_REGION" --no-cli-pager | jq -r '.Vpcs[0] | ([.Tags[]? | select(.Key=="Name") | .Value] | first // "NO-VPC-NAME")'); 
echo "$S" | jq --arg VpcName "$VPC_NAME" '. + {VpcName:$VpcName}'; 
done
```
**Expected output:**
```json
{
  "GroupId": "sg-02864ee1dea47d873",
  "GroupName": "SG_EC2_ManageEIPs",
  "NameTag": "SG_EC2_ManageEIPs",
  "VpcId": "vpc-0df77650a8551f9cf",
  "VpcName": "VPC_ManageEIPs"
}
```

##### 7.3.2.3 Ensure `NameTag` differs from `GroupName`
```bash
aws ec2 create-tags --resources "$EC2_SG_ID" --tags Key=Name,Value="SG_EC2_ManageEIPs_tag" --region "$AWS_REGION" --no-cli-pager
```
**Expected output:**
```json
{
  "GroupId": "sg-02864ee1dea47d873",
  "GroupName": "SG_EC2_ManageEIPs",
  "NameTag": "SG_EC2_ManageEIPs_tag",
  "VpcId": "vpc-0df77650a8551f9cf",
  "VpcName": "VPC_ManageEIPs"
}
```

##### 7.3.2.4 Least-privilege egress: remove default allow-all
This is because when we create a new security group, AWS automatically adds a default egress rule:
**Outbound: ALL protocols (-1) to 0.0.0.0/0**
So:
```bash
aws ec2 revoke-security-group-egress --group-id "$EC2_SG_ID" --ip-permissions '[{"IpProtocol":"-1","IpRanges":[{"CidrIp":"0.0.0.0/0"}]}]' --region "$AWS_REGION" --no-cli-pager
```

##### 7.3.2.5 Verify egress is empty
```bash
aws ec2 describe-security-groups --group-ids "$EC2_SG_ID" --region "$AWS_REGION" --no-cli-pager | jq -c '.SecurityGroups[0] | {GroupId:.GroupId, GroupName:.GroupName, NameTag:([.Tags[]? | select(.Key=="Name") | .Value] | first // "MISSING-NAME-TAG"), VpcId:.VpcId, Egress:[.IpPermissionsEgress[]? | {Protocol:.IpProtocol, FromPort:(.FromPort // "n/a"), ToPort:(.ToPort // "n/a"), CIDRs:[.IpRanges[]?.CidrIp]}]}' | while read -r S; 
do VPC_ID=$(echo "$S" | jq -r '.VpcId'); VPC_NAME=$(aws ec2 describe-vpcs --vpc-ids "$VPC_ID" --region "$AWS_REGION" --no-cli-pager | jq -r '.Vpcs[0] | ([.Tags[]? | select(.Key=="Name") | .Value] | first // "NO-VPC-NAME")'); 
echo "$S" | jq --arg VpcName "$VPC_NAME" '{GroupId:.GroupId, GroupName:.GroupName, NameTag:.NameTag, VpcId:.VpcId, VpcName:$VpcName, Egress:.Egress}'; 
done
```
**Example output, confirming that default allow-all egress is gone ("Egress": []):**
```json
{
  "GroupId": "sg-02864ee1dea47d873",
  "GroupName": "SG_EC2_ManageEIPs",
  "NameTag": "SG_EC2_ManageEIPs_tag",
  "VpcId": "vpc-0df77650a8551f9cf",
  "VpcName": "VPC_ManageEIPs",
  "Egress": []
}
```

##### 7.3.2.6 SG-to-SG HTTPS tunnel (EC2 → VPCE SG)
**Goal:**
Allow EC2 to:
- initiate HTTPS (443)
- only toward the VPC interface endpoints and nothing else
**That means:**
- EC2 egress → endpoint SG
- Endpoint SG ingress → EC2 SG
This creates a closed HTTPS tunnel between the instance and SSM.

###### 7.3.2.6.1 Add HTTPS egress to endpoint SG only (443)
In the command below:
- EC2 can open HTTPS connections only if the destination ENI belongs to SG_SSM_Endpoints_ManageEIPs.
- No CIDR, no wildcard, no internet.
This is maximum least privilege.

```bash
OUT=$(aws ec2 authorize-security-group-egress --group-id "$EC2_SG_ID" --ip-permissions "$(jq -nc --arg SG "$SSM_EP_SG_ID" '[{IpProtocol:"tcp",FromPort:443,ToPort:443,UserIdGroupPairs:[{GroupId:$SG,Description:"HTTPS only to SSM VPCE SG"}]}]')" --region "$AWS_REGION" --no-cli-pager); 
EC2_EGRESS_HTTPS_RULE_ID=$(echo "$OUT" | jq -r '.SecurityGroupRules[0].SecurityGroupRuleId'); aws ec2 create-tags --resources "$EC2_EGRESS_HTTPS_RULE_ID" --tags Key=Name,Value="RULE_EC2_to_SSMVPCE_443" Key=Purpose,Value="EC2 egress HTTPS to SSM VPCE SG only" Key=Project,Value="$TAG_PROJECT" Key=Component,Value="SecurityGroupRule" Key=Environment,Value="$TAG_ENV" Key=Owner,Value="$TAG_OWNER" Key=ManagedBy,Value="$TAG_MANAGEDBY" Key=CostCenter,Value="$TAG_COSTCENTER" --region "$AWS_REGION" --no-cli-pager
```

###### 7.3.2.6.2 Verify both SGs (IDs + Names + VPC)
```bash
aws ec2 describe-security-groups --group-ids "$EC2_SG_ID" "$SSM_EP_SG_ID" --region "$AWS_REGION" --no-cli-pager | jq -c '.SecurityGroups[] | {GroupId:.GroupId, GroupName:.GroupName, NameTag:([.Tags[]? | select(.Key=="Name") | .Value] | first // "MISSING-NAME-TAG"), VpcId:.VpcId}' | while read -r S; 
do VPC_ID=$(echo "$S" | jq -r '.VpcId'); 
VPC_NAME=$(aws ec2 describe-vpcs --vpc-ids "$VPC_ID" --region "$AWS_REGION" --no-cli-pager | jq -r '.Vpcs[0] | ([.Tags[]? | select(.Key=="Name") | .Value] | first // "NO-VPC-NAME")'); 
echo "$S" | jq --arg VpcName "$VPC_NAME" '{GroupId:.GroupId, GroupName:.GroupName, NameTag:.NameTag, VpcId:.VpcId, VpcName:$VpcName}'; 
done
```
**Example output:**
```json
{
  "GroupId": "sg-02864ee1dea47d873",
  "GroupName": "SG_EC2_ManageEIPs",
  "NameTag": "SG_EC2_ManageEIPs_tag",
  "VpcId": "vpc-0df77650a8551f9cf",
  "VpcName": "VPC_ManageEIPs"
}
{
  "GroupId": "sg-0370469d180f27a70",
  "GroupName": "SG_SSM_Endpoints_ManageEIPs",
  "NameTag": "SG_SSM_Endpoints_ManageEIPs_tag",
  "VpcId": "vpc-0df77650a8551f9cf",
  "VpcName": "VPC_ManageEIPs"
}
```

###### 7.3.2.6.3 Verify egress destination SG (EC2 → endpoint SG)
```bash
aws ec2 describe-security-groups --group-ids "$EC2_SG_ID" --region "$AWS_REGION" --no-cli-pager | jq -c '.SecurityGroups[0] | {EC2GroupId:.GroupId, EC2GroupName:.GroupName, EC2NameTag:([.Tags[]? | select(.Key=="Name") | .Value] | first // "MISSING-NAME-TAG"), VpcId:.VpcId, EgressToSG:([.IpPermissionsEgress[]? | select(.IpProtocol=="tcp" and .FromPort==443 and .ToPort==443) | .UserIdGroupPairs[]?.GroupId] | unique)}' | while read -r O; 
do VPC_ID=$(echo "$O" | jq -r '.VpcId'); 
VPC_NAME=$(aws ec2 describe-vpcs --vpc-ids "$VPC_ID" --region "$AWS_REGION" --no-cli-pager | jq -r '.Vpcs[0] | ([.Tags[]? | select(.Key=="Name") | .Value] | first // "NO-VPC-NAME")'); 
DEST_IDS=$(echo "$O" | jq -r '.EgressToSG[]?'); 
for G in $DEST_IDS; 
do D=$(aws ec2 describe-security-groups --group-ids "$G" --region "$AWS_REGION" --no-cli-pager | jq -c '.SecurityGroups[0] | {DestGroupId:.GroupId, DestGroupName:.GroupName, DestNameTag:([.Tags[]? | select(.Key=="Name") | .Value] | first // "MISSING-NAME-TAG"), DestVpcId:.VpcId}'); 
DVPC_ID=$(echo "$D" | jq -r '.DestVpcId'); 
DVPC_NAME=$(aws ec2 describe-vpcs --vpc-ids "$DVPC_ID" --region "$AWS_REGION" --no-cli-pager | jq -r '.Vpcs[0] | ([.Tags[]? | select(.Key=="Name") | .Value] | first // "NO-VPC-NAME")'); 
echo "$O" | jq --arg VpcName "$VPC_NAME" --argjson Dest "$D" --arg DestVpcName "$DVPC_NAME" '{EC2GroupId:.EC2GroupId, EC2GroupName:.EC2GroupName, EC2NameTag:.EC2NameTag, VpcId:.VpcId, VpcName:$VpcName, DestGroupId:$Dest.DestGroupId, DestGroupName:$Dest.DestGroupName, DestNameTag:$Dest.DestNameTag, DestVpcId:$Dest.DestVpcId, DestVpcName:$DestVpcName}'; 
done; 
done
```
**Example output:**
```json
{
  "EC2GroupId": "sg-02864ee1dea47d873",
  "EC2GroupName": "SG_EC2_ManageEIPs",
  "EC2NameTag": "SG_EC2_ManageEIPs_tag",
  "VpcId": "vpc-0df77650a8551f9cf",
  "VpcName": "VPC_ManageEIPs",
  "DestGroupId": "sg-0370469d180f27a70",
  "DestGroupName": "SG_SSM_Endpoints_ManageEIPs",
  "DestNameTag": "SG_SSM_Endpoints_ManageEIPs_tag",
  "DestVpcId": "vpc-0df77650a8551f9cf",
  "DestVpcName": "VPC_ManageEIPs"
}
```

#### 7.3.3 Launch EC2 instance in private subnet (capture `INSTANCE_ID`)
**Run instance (capture `INSTANCE_ID`):**
```bash
INSTANCE_ID=$(aws ec2 run-instances --image-id "$AMI_ID" --instance-type "t3.micro" --subnet-id "$SUBNET_ID" --security-group-ids "$EC2_SG_ID" --iam-instance-profile Name="ManageEIPs-EC2SSMInstanceProfile" --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=ManageEIPs-ec2},{Key=Project,Value=$TAG_PROJECT},{Key=Component,Value=EC2},{Key=Environment,Value=$TAG_ENV},{Key=Owner,Value=$TAG_OWNER},{Key=ManagedBy,Value=$TAG_MANAGEDBY},{Key=CostCenter,Value=$TAG_COSTCENTER}]" --region "$AWS_REGION" --no-cli-pager | jq -r '.Instances[0].InstanceId')
```

#### 7.3.4 Verify instance identity and placement (Subnet + VPC names):
```bash
aws ec2 describe-instances --instance-ids "$INSTANCE_ID" --region "$AWS_REGION" --no-cli-pager | jq -c '.Reservations[0].Instances[0] | {InstanceId:.InstanceId, NameTag:([.Tags[]? | select(.Key=="Name") | .Value] | first // "MISSING-NAME-TAG"), InstanceType:.InstanceType, SubnetId:.SubnetId, VpcId:.VpcId, State:.State.Name}' | while read -r I; 
do SUBNET_ID=$(echo "$I" | jq -r '.SubnetId'); 
SUBNET_NAME=$(aws ec2 describe-subnets --subnet-ids "$SUBNET_ID" --region "$AWS_REGION" --no-cli-pager | jq -r '.Subnets[0] | ([.Tags[]? | select(.Key=="Name") | .Value] | first // "NO-SUBNET-NAME")'); 
VPC_ID=$(echo "$I" | jq -r '.VpcId'); 
VPC_NAME=$(aws ec2 describe-vpcs --vpc-ids "$VPC_ID" --region "$AWS_REGION" --no-cli-pager | jq -r '.Vpcs[0] | ([.Tags[]? | select(.Key=="Name") | .Value] | first // "NO-VPC-NAME")'); 
echo "$I" | jq --arg subnet_name "$SUBNET_NAME" --arg vpc_name "$VPC_NAME" '{InstanceId:.InstanceId, NameTag:.NameTag, InstanceType:.InstanceType, SubnetId:.SubnetId, SubnetName:$subnet_name, VpcId:.VpcId, VpcName:$vpc_name, State:.State}'; 
done
```
**Example output:**
```json
{
  "InstanceId": "i-0c69cfce5294ffdd4",
  "NameTag": "ManageEIPs-ec2",
  "InstanceType": "t3.micro",
  "SubnetId": "subnet-03949955e141deebb",
  "SubnetName": "Subnet_ManageEIPs_AZ1",
  "VpcId": "vpc-0df77650a8551f9cf",
  "VpcName": "VPC_ManageEIPs",
  "State": "running"
}
```

 

## 8. Systems Manager Connectivity Verification
This project does not use EC2 key pairs.
All access is performed via AWS Systems Manager Session Manager, as the EC2 instance runs in a private subnet with no direct network access.

### 8.1 EC2 instance registered in Systems Manager
#### 8.1.1 Retrieve EC2 Instance IDs with enforced Name tags
```bash
aws ec2 describe-instances --region "$AWS_REGION" --no-cli-pager | jq -c '[.Reservations[].Instances[] | {InstanceId,InstanceName:(((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"MISSING-INSTANCE-NAME")}]'
```
**Expected output:**
```json
[{"InstanceId":"i-0c69cfce5294ffdd4","InstanceName":"ManageEIPs-ec2"}]
```

#### 8.1.2 Verify the instance is visible to SSM
**Retrieve Systems Manager–registered instances:**
```bash
aws ssm describe-instance-information --region "$AWS_REGION" --no-cli-pager | jq '.InstanceInformationList'
```
**Example output:**
```json
[
  {
    "InstanceId": "i-0c69cfce5294ffdd4",
    "PingStatus": "Online",
    "LastPingDateTime": "2025-12-22T07:19:50.428000+01:00",
    "AgentVersion": "3.3.3050.0",
    "IsLatestVersion": false,
    "PlatformType": "Linux",
    "PlatformName": "Amazon Linux",
    "PlatformVersion": "2023",
    "ResourceType": "EC2Instance",
    "IPAddress": "10.0.1.121",
    "ComputerName": "ip-10-0-1-121.ec2.internal",
    "AssociationStatus": "Success",
    "LastAssociationExecutionDate": "2025-12-22T05:46:10.343000+01:00",
    "LastSuccessfulAssociationExecutionDate": "2025-12-22T05:46:10.343000+01:00",
    "AssociationOverview": {
      "InstanceAssociationStatusAggregatedCount": {
        "Success": 1
      }
    },
    "SourceId": "i-0c69cfce5294ffdd4",
    "SourceType": "AWS::EC2::Instance"
  }
]
```

#### 8.1.3 Join SSM status with EC2 Name tags (IDs + Names)
```bash
jq -c -s '.[0] as $SSM | .[1] as $EC2 | [$SSM[] | .InstanceId as $id | {InstanceId:$id,InstanceName:(([$EC2[]|select(.InstanceId==$id)|.InstanceName][0])//"MISSING-INSTANCE-NAME"),PingStatus:.PingStatus,PlatformName:.PlatformName,IPAddress:.IPAddress}]' <(aws ssm describe-instance-information --region "$AWS_REGION" --no-cli-pager | jq '.InstanceInformationList') <(aws ec2 describe-instances --region "$AWS_REGION" --no-cli-pager | jq '[.Reservations[].Instances[] | {InstanceId,InstanceName:(((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"MISSING-INSTANCE-NAME")}]')
```
**Example output:**
```json
[{"InstanceId":"i-0c69cfce5294ffdd4","InstanceName":"ManageEIPs-ec2","PingStatus":"Online","PlatformName":"Amazon Linux","IPAddress":"10.0.1.121"}]
```

<u>What this confirms (fact-by-fact)</u>:
- InstanceId present: i-0c69cfce5294ffdd4 → ✔ registered in SSM
- PingStatus = Online → ✔ SSM agent running + reachable
- Platform: Amazon Linux 2023 → ✔ supported
- IPAddress: 10.0.1.121 (private) → ✔ no public access required
- AssociationStatus = Success → ✔ IAM role + permissions working
- Last ping timestamps recent → ✔ live connectivity
- SourceType = AWS::EC2::Instance → ✔ native EC2 registration (not hybrid)


### 8.2 SSM Agent status on EC2 (later)
#### 8.2.1 Verify SSM Agent status indirectly (SSM Online + agent version) with `InstanceName` attached
<u>Purpose</u>: 
Prove the agent is running/reachable by checking **PingStatus** + **AgentVersion** from SSM, joined to EC2 Name.

```bash
jq -s '.[0] as $SSM | .[1] as $EC2 | [$SSM[] | .InstanceId as $id | {InstanceId:$id,InstanceName:(([$EC2[]|select(.InstanceId==$id)|.InstanceName][0])//"MISSING-INSTANCE-NAME"),PingStatus:.PingStatus,AgentVersion:.AgentVersion,IsLatestVersion:.IsLatestVersion,PlatformName:.PlatformName,PlatformVersion:.PlatformVersion}]' <(aws ssm describe-instance-information --region "$AWS_REGION" --no-cli-pager | jq '.InstanceInformationList') <(aws ec2 describe-instances --region "$AWS_REGION" --no-cli-pager | jq '[.Reservations[].Instances[] | {InstanceId,InstanceName:(((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"MISSING-INSTANCE-NAME")}]')
```
**Example output:**
<u>Short code explanation</u>:
-s (slurp): reads all input JSON documents and combines them into one array before processing.
```json
[
  {
    "InstanceId": "i-0c69cfce5294ffdd4",
    "InstanceName": "ManageEIPs-ec2",
    "PingStatus": "Online",
    "AgentVersion": "3.3.3050.0",
    "IsLatestVersion": false,
    "PlatformName": "Amazon Linux",
    "PlatformVersion": "2023"
  }
]
```

#### 8.2.2 Verify SSM Agent service status on the instance (Session Manager, no SSH), with `InstanceName` attached
<u>Purpose</u>: 
Run OS-level checks (*systemctl*) via SSM, output includes the instance Name.

```bash
aws ssm send-command --region "$AWS_REGION" --no-cli-pager --document-name "AWS-RunShellScript" --targets "Key=tag:Name,Values=*" --parameters 'commands=["sudo systemctl is-active amazon-ssm-agent || true","sudo systemctl status amazon-ssm-agent --no-pager -l | sed -n \"1,20p\""]' | jq -c '{CommandId:.Command.CommandId}'
```
**Example output:**
```json
{"CommandId":"0e2020e9-2cd8-4174-8aeb-8fa327692855"}
```

#### 8.2.3 Retrieve command output (pair InstanceId with `InstanceName`, no naked IDs)
<u>Purpose</u>: 
fetch results per instance and print ID + Name + output (paste `CommandId` into $CMD_ID).

```bash
aws ssm send-command --region "$AWS_REGION" --no-cli-pager --document-name "AWS-RunShellScript" --targets "Key=tag:Name,Values=ManageEIPs-ec2" --parameters 'commands=["systemctl is-active amazon-ssm-agent"]' | jq -r '.Command.CommandId'
```
```text
e2563cfe-e8ec-4415-ba9e-941bc194c006
```
```bash
export CMD_ID="e2563cfe-e8ec-4415-ba9e-941bc194c006"
```

```bash
jq -c -s '.[0] as $OUT | .[1] as $EC2 | [$OUT[] | .InstanceId as $id | {InstanceId:$id,InstanceName:(([$EC2[]|select(.InstanceId==$id)|.InstanceName][0])//"MISSING-INSTANCE-NAME"),StdOut:.StdOut,StdErr:.StdErr}]' <(aws ssm list-command-invocations --region "$AWS_REGION" --no-cli-pager --command-id "$CMD_ID" --details | jq '[.CommandInvocations[] | {InstanceId:.InstanceId,StdOut:(.CommandPlugins[0].Output//""),StdErr:(.CommandPlugins[0].StandardErrorContent//"")} ]') <(aws ec2 describe-instances --region "$AWS_REGION" --no-cli-pager | jq '[.Reservations[].Instances[] | {InstanceId,InstanceName:(((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"MISSING-INSTANCE-NAME")}]')
```
**Example output:**
```json
[{"InstanceId":"i-0c69cfce5294ffdd4","InstanceName":"ManageEIPs-ec2","StdOut":"active\n","StdErr":""}]
```

**<u>Check in the AWS console</u>:** 
In Systems Manager > Fleet > Managed nodes.



## 9. Elastic IP
Goal: 3 EIPs total
1 attached to ManageEIPs-ec2
2 unused (to be deleted by Lambda)

Use the WSL dry-run wrapper before letting Lambda run for real

### 9.1 Allocate EIP (to attach): `EIP_Attached_ManageEIPs`
<u>Purpose</u>: 
Create the EIP with full tags, then immediately print AllocationId + Name.

```bash
EIP_ALLOC_ID=$(aws ec2 allocate-address --domain vpc --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=EIP_Attached_ManageEIPs},{Key=Project,Value=$TAG_PROJECT},{Key=Environment,Value=$TAG_ENV},{Key=Owner,Value=$TAG_OWNER},{Key=ManagedBy,Value=$TAG_MANAGEDBY},{Key=CostCenter,Value=$TAG_COSTCENTER}]" --region "$AWS_REGION" --no-cli-pager | jq -r '.AllocationId'); aws ec2 describe-addresses --allocation-ids "$EIP_ALLOC_ID" --region "$AWS_REGION" --no-cli-pager | jq -c '[.Addresses[] | {AllocationId,Name:(((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"MISSING-EIP-NAME"),PublicIp}]'
```
**Example output:**
```json
[{"AllocationId":"eipalloc-0da06ada08055dcb5","Name":"EIP_Attached_ManageEIPs","PublicIp":"100.50.117.194"}]
```


### 9.2 Attach EIP to EC2 dynamically
<u>Purpose</u>: 
Attach the tagged EIP to ManageEIPs-ec2 and print IDs + Names + PublicIp.

```bash
ASSOC_ID=$(aws ec2 associate-address --region "$AWS_REGION" --no-cli-pager --instance-id "$INSTANCE_ID" --allocation-id "$EIP_ALLOC_ID" | jq -r '.AssociationId'); jq -c -s '.[0] as $EC2 | .[1] as $EIP | {AssociationId:"'"$ASSOC_ID"'",InstanceId:"'"$INSTANCE_ID"'",InstanceName:(([$EC2[]|select(.InstanceId=="'"$INSTANCE_ID"'")|.InstanceName][0])//"MISSING-INSTANCE-NAME"),AllocationId:"'"$EIP_ALLOC_ID"'",EipName:(([$EIP[]|select(.AllocationId=="'"$EIP_ALLOC_ID"'")|.EipName][0])//"MISSING-EIP-NAME"),PublicIp:(([$EIP[]|select(.AllocationId=="'"$EIP_ALLOC_ID"'")|.PublicIp][0])//"MISSING-PUBLIC-IP")}' <(aws ec2 describe-instances --region "$AWS_REGION" --no-cli-pager | jq '[.Reservations[].Instances[] | {InstanceId,InstanceName:(((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"MISSING-INSTANCE-NAME")}]') <(aws ec2 describe-addresses --region "$AWS_REGION" --no-cli-pager --allocation-ids "$EIP_ALLOC_ID" | jq '[.Addresses[] | {AllocationId,PublicIp,EipName:(((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"MISSING-EIP-NAME")}]')
```
**Example output:**
```json
{"AssociationId":"eipassoc-02a07f8bc7b6d1adf","InstanceId":"i-0c69cfce5294ffdd4","InstanceName":"ManageEIPs-ec2","AllocationId":"eipalloc-0da06ada08055dcb5","EipName":"EIP_Attached_ManageEIPs","PublicIp":"100.50.117.194"}
```


### 9.3 Create unused Elastic IPs (cost-control / Lambda test fixtures)
#### 9.3.1 Create first unused EIP
<u>Purpose</u>: 
Allocate an unused EIP for testing / release logic, and immediately display AllocationId + Name.

```bash
EIP_UNUSED1_ALLOC_ID=$(aws ec2 allocate-address --region "$AWS_REGION" --domain vpc --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=EIP_Unused1_ManageEIPs},{Key=Project,Value=$TAG_PROJECT},{Key=Environment,Value=$TAG_ENV},{Key=Owner,Value=$TAG_OWNER},{Key=ManagedBy,Value=$TAG_MANAGEDBY},{Key=CostCenter,Value=$TAG_COSTCENTER}]" --no-cli-pager | jq -r '.AllocationId'); 
aws ec2 describe-addresses --region "$AWS_REGION" --no-cli-pager --allocation-ids "$EIP_UNUSED1_ALLOC_ID" | jq -c '[.Addresses[] | {AllocationId,Name:(((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"MISSING-EIP-NAME"),PublicIp}]'
```
**Example output:**
```json
[{"AllocationId":"eipalloc-0cb2c1dd93ad66184","Name":"EIP_Unused1_ManageEIPs","PublicIp":"100.52.71.230"}]
```

#### 9.3.2 Create second unused EIP
<u>Purpose</u>: 
Allocate a second unused EIP to validate multi-resource handling in Lambda logic.

```bash
EIP_UNUSED2_ALLOC_ID=$(aws ec2 allocate-address --region "$AWS_REGION" --domain vpc --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=EIP_Unused2_ManageEIPs},{Key=Project,Value=$TAG_PROJECT},{Key=Environment,Value=$TAG_ENV},{Key=Owner,Value=$TAG_OWNER},{Key=ManagedBy,Value=$TAG_MANAGEDBY},{Key=CostCenter,Value=$TAG_COSTCENTER}]" --no-cli-pager | jq -r '.AllocationId'); 
aws ec2 describe-addresses --region "$AWS_REGION" --no-cli-pager --allocation-ids "$EIP_UNUSED2_ALLOC_ID" | jq -c '[.Addresses[] | {AllocationId,Name:(((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"MISSING-EIP-NAME"),PublicIp}]'
```
**Example output:**
```json
[{"AllocationId":"eipalloc-03c791ad93b266fe6","Name":"EIP_Unused2_ManageEIPs","PublicIp":"3.225.31.165"}]
```

#### 9.3.3 Notes on necessity (design clarification)
- One unused EIP is technically sufficient to test release logic.
- Two unused EIPs are useful to:
  - validate iteration logic in Lambda
  - demonstrate cost-leak detection at scale
- We can safely keep one in a minimal setup.

 

## 10. Lambda
### 10.0 Cleanup
#### 10.0.1 Customer-managed IAM policies
##### 10.0.1.1 List customer-managed IAM policies (cleanup scope)
**Purpose:**
List only customer-managed IAM policies with Name + ARN together (no naked IDs).

```bash
aws iam list-policies --scope Local --no-cli-pager | jq -c '.Policies[] | {PolicyName,PolicyArn:.Arn}'
```
**Example output ('PolicyArn' shouldn´t be null. Fix = ".Arn", see above):**
```json
{"PolicyName":"AWSLambdaBasicExecutionRole-b44db2ea-d850-49f2-9a7f-cd9fd26b8822","PolicyArn":null}
{"PolicyName":"AWSLambdaBasicExecutionRole-bcace15d-eb17-45da-8adf-64a992ae33b1","PolicyArn":null}
{"PolicyName":"MyPolicyTest","PolicyArn":null}
{"PolicyName":"AWSLambdaBasicExecutionRole-0dd0bb70-3e27-44b2-b660-f3ca5ca15006","PolicyArn":null}
{"PolicyName":"Amazon_EventBridge_Invoke_Lambda_1595937576","PolicyArn":null}
{"PolicyName":"AWSLambdaBasicExecutionRole-feba8ba5-b84e-4c9b-977e-64fa39fcfb8b","PolicyArn":null}
{"PolicyName":"Amazon_EventBridge_Invoke_Lambda_918418397","PolicyArn":null}
{"PolicyName":"LambdaRoutePermissionsFinal","PolicyArn":null}
{"PolicyName":"AWSLambdaBasicExecutionRole-1c42ea15-373b-41ad-bb54-f3f651adf364","PolicyArn":null}
{"PolicyName":"AWSLambdaBasicExecutionRole-82fcbd29-8764-4fd5-abf6-b21744605b6a","PolicyArn":null}
{"PolicyName":"AWSLambdaBasicExecutionRole-a8b79d2a-06b4-4408-9a98-27d062da6c6a","PolicyArn":null}
{"PolicyName":"AWSLambdaBasicExecutionRole-e4f30bb5-0955-4bf5-be36-3d594d212906","PolicyArn":null}
{"PolicyName":"LambdaNATGatewaySchedulerPolicy","PolicyArn":null}
{"PolicyName":"Amazon-EventBridge-Scheduler-Execution-Policy-1c5eb6ac-8f0a-46c8-a7be-b840a8c440b1","PolicyArn":null}
{"PolicyName":"AWSLambdaBasicExecutionRole-a016dbd5-d99b-461a-95c2-ed19c8beb320","PolicyArn":null}
```

##### 10.0.1.2 Detach each policy from all roles (required)
**Purpose:**
Policies cannot be deleted while attached.

```bash
aws iam list-policies --scope Local --no-cli-pager | jq -r '.Policies[].Arn' | while read -r ARN; 
do aws iam list-entities-for-policy --policy-arn "$ARN" --no-cli-pager | jq -r '.PolicyRoles[]?.RoleName' | while read -r ROLE;
do aws iam detach-role-policy --role-name "$ROLE" --policy-arn "$ARN" --no-cli-pager; 
done; 
done
```

##### 10.0.1.3 Delete all customer-managed policies
**Purpose:**
Remove all non-default versions for the remaining customer-managed policy(ies), then delete them.

```bash
aws iam list-policies --scope Local --no-cli-pager | jq -r '.Policies[].Arn' | while read -r ARN; 
do aws iam list-policy-versions --policy-arn "$ARN" --no-cli-pager | jq -r '.Versions[] | select(.IsDefaultVersion==false) | .VersionId' | while read -r VID; 
do aws iam delete-policy-version --policy-arn "$ARN" --version-id "$VID" --no-cli-pager; 
done; 
aws iam delete-policy --policy-arn "$ARN" --no-cli-pager && echo "DELETED $ARN" || echo "FAILED $ARN"; 
done
```

##### 10.0.1.4 Verify: no customer-managed policies remain
**Purpose:**
Confirm clean slate.

```bash
aws iam list-policies --scope Local --no-cli-pager | jq -c '.Policies[] | {PolicyName,PolicyArn: .Arn}'  # no output
```

#### 10.0.2 IAM roles (customer-created only)
##### 10.0.2.1 List Lambda execution roles (by trust policy):
```bash
aws iam list-roles --no-cli-pager | jq -r '.Roles[] | select(.AssumeRolePolicyDocument.Statement[].Principal.Service=="lambda.amazonaws.com") | .RoleName' # no output
```

##### 10.0.2.2 Cleanup check for all roles (broader inventory)
**Purpose:**
List everything, so we can spot leftover lab roles (not only Lambda).
```bash
aws iam list-roles --no-cli-pager | jq -r '.Roles[].RoleName'
```
It outputs a long list of roles, one of which we must keep: "*ManageEIPs-EC2SSMRole*".

##### 10.0.2.3 delete listed roles except `ManageEIPs-EC2SSMRole`
**Purpose:**
Delete exactly the roles you named, while hard-skipping `ManageEIPs-EC2SSMRole`, and also skipping protected AWS roles (`AWSServiceRoleFor*`, `AWSReservedSSO_*`).
```bash
for r in Amazon_EventBridge_Invoke_Lambda_1595937576 Amazon_EventBridge_Invoke_Lambda_918418397 Amazon_EventBridge_Scheduler_LAMBDA_4c3b20c146 assume-role-exercise AWSDataLifecycleManagerDefaultRole AWSReservedSSO_AdministratorAccess_ae6789e688ff8920 AWSReservedSSO_ViewOnlyAccess_1b24f4624b5a0f83 AWSServiceRoleForAmazonSSM AWSServiceRoleForAmazonSSM_AccountDiscovery AWSServiceRoleForAPIGateway AWSServiceRoleForAutoScaling AWSServiceRoleForConfig AWSServiceRoleForEc2InstanceConnect AWSServiceRoleForECS AWSServiceRoleForElasticLoadBalancing AWSServiceRoleForOrganizations AWSServiceRoleForRDS AWSServiceRoleForResourceExplorer AWSServiceRoleForSSMQuickSetup AWSServiceRoleForSSO AWSServiceRoleForSupport AWSServiceRoleForTrustedAdvisor ec2-role EC2SSMRole ecsTaskExecutionRole rds-monitoring-role S3ReadOnly SRR-S3; 
do [ "$r" = "ManageEIPs-EC2SSMRole" ] && { echo "SKIP-KEEP $r"; 
continue; 
}; 
[[ "$r" == AWSServiceRoleFor* || "$r" == AWSReservedSSO_* ]] && { echo "SKIP-PROTECTED $r"; 
continue; 
}; 
aws iam list-attached-role-policies --role-name "$r" --no-cli-pager | jq -r '.AttachedPolicies[]?.PolicyArn' | while read -r p;
do aws iam detach-role-policy --role-name "$r" --policy-arn "$p" --no-cli-pager; 
done; 
aws iam list-role-policies --role-name "$r" --no-cli-pager | jq -r '.PolicyNames[]?' | while read -r ip; 
do aws iam delete-role-policy --role-name "$r" --policy-name "$ip" --no-cli-pager; 
done; 
aws iam list-instance-profiles-for-role --role-name "$r" --no-cli-pager | jq -r '.InstanceProfiles[]?.InstanceProfileName' | while read -r prof; 
do aws iam remove-role-from-instance-profile --instance-profile-name "$prof" --role-name "$r" --no-cli-pager; 
aws iam delete-instance-profile --instance-profile-name "$prof" --no-cli-pager; done; aws iam delete-role --role-name "$r" --no-cli-pager && echo "DELETED $r" || echo "FAILED $r"; 
done
```

##### 10.0.2.4 Verify all listed roles except `ManageEIPs-EC2SSMRole` have really been deleted
```bash
aws iam list-roles --no-cli-pager | jq -c '.Roles[].RoleName'
```
**Expected output (we can see only `ManageEIPs-EC2SSMRole` is the only custom role remaining):**
```json
"AWSReservedSSO_AdministratorAccess_ae6789e688ff8920"
"AWSReservedSSO_ViewOnlyAccess_1b24f4624b5a0f83"
"AWSServiceRoleForAmazonSSM"
"AWSServiceRoleForAmazonSSM_AccountDiscovery"
"AWSServiceRoleForAPIGateway"
"AWSServiceRoleForAutoScaling"
"AWSServiceRoleForConfig"
"AWSServiceRoleForEc2InstanceConnect"
"AWSServiceRoleForECS"
"AWSServiceRoleForElasticLoadBalancing"
"AWSServiceRoleForOrganizations"
"AWSServiceRoleForRDS"
"AWSServiceRoleForResourceExplorer"
"AWSServiceRoleForSSMQuickSetup"
"AWSServiceRoleForSSO"
"AWSServiceRoleForSupport"
"AWSServiceRoleForTrustedAdvisor"
"ManageEIPs-EC2SSMRole"
```

#### 10.0.3 Lambda functions
**Purpose:**
Functions implicitly recreate roles/policies later.
Delete all project Lambdas if doing a true reset.

```bash
aws lambda list-functions --region "$AWS_REGION" --no-cli-pager | jq -r '.Functions[].FunctionName' # no output
```

#### 10.0.4 Lambda resource policies (invoke permissions)
**Why:**
Stale EventBridge / scheduler permissions linger.

**Purpose:**
Remove all remaining permission statements.

```bash
aws lambda list-functions --region "$AWS_REGION" --no-cli-pager | jq -r '.Functions[].FunctionName' | while read -r FN; 
do aws lambda get-policy --function-name "$FN" --region "$AWS_REGION" --no-cli-pager 2>/dev/null | jq -r '.Policy'; 
done
```
No output.

#### 10.0.5 EventBridge rules
**Why:**
Rules re-attach permissions automatically.

**Purpose:**
- Delete all project rules.
- Ignore default AWS rules.

##### 10.0.5.1 List EventBridge rules:
```bash
aws events list-rules --region "$AWS_REGION" --no-cli-pager | jq -r '.Rules[].Name'
```

**Example output:**
```text
LambdaEC2DailySnapshot-LambdaEC2DailySnapshotDailyS-043x91XkQkno
start-nat-2300
stop-nat-2359
tempstop-nat-0235
tempstop-nat-0335
```

##### 10.0.5.2 Delete EventBridge listed rules
**Delete the listed rules safely:**
```bash
for r in LambdaEC2DailySnapshot-LambdaEC2DailySnapshotDailyS-043x91XkQkno start-nat-2300 stop-nat-2359 tempstop-nat-0235 tempstop-nat-0335; 
do aws events list-targets-by-rule --region "$AWS_REGION" --no-cli-pager --rule "$r" | jq -r '.Targets[]?.Id' | while read -r tid; 
do aws events remove-targets --region "$AWS_REGION" --no-cli-pager --rule "$r" --ids "$tid" >/dev/null; 
done; 
aws events delete-rule --region "$AWS_REGION" --no-cli-pager --name "$r" --force >/dev/null && echo "DELETED $r" || echo "FAILED $r"; 
done
```

**Verify they're gone:**
```bash
aws events list-rules --region "$AWS_REGION" --no-cli-pager | jq -r '.Rules[].Name' | egrep 'LambdaEC2DailySnapshot|start-nat-2300|stop-nat-2359|tempstop-nat-0235|tempstop-nat-0335' || true       # no output
```

#### 10.0.6 CloudWatch log groups
**Why:** 
*/aws/lambda/* persists after Lambda deletion.
- They are CloudWatch Logs log group automatically created by AWS Lambda.
- They store execution logs (print, logger output, START/END/REPORT lines).
- They persist even after the Lambda function is deleted.
- They cost money (log ingestion + retention) and are safe to delete once you no longer need the history.


**What the command does:**
Delete all project log groups.

##### 10.0.6.1 List all the /aws/Lambda/* log groups:
```bash
aws logs describe-log-groups --log-group-name-prefix "/aws/lambda/" --region "$AWS_REGION" --no-cli-pager | jq -r '.logGroups[].logGroupName'
```
**Example output:**
```text
/aws/lambda/BillingBucketParser
/aws/lambda/DailyEbsSnapshotFunction
/aws/lambda/ManageEIPs
/aws/lambda/SendSupportEmail
/aws/lambda/SubmitSupportTicket
/aws/lambda/WriteToCloudWatch
/aws/lambda/simple-event-driven-app
/aws/lambda/start_nat_gateway
/aws/lambda/stop_nat_gateway
/aws/lambda/transcription-function
/aws/lambda/translation-function
```

##### 10.0.6.2 Delete all Lambda log groups (clean slate)
**Purpose:**
Remove all leftover Lambda log groups for deleted/old functions.

```bash
aws logs describe-log-groups --log-group-name-prefix "/aws/lambda/" --region "$AWS_REGION" --no-cli-pager | jq -r '.logGroups[].logGroupName' | while read -r LG; 
do aws logs delete-log-group --log-group-name "$LG" --region "$AWS_REGION" --no-cli-pager && echo "DELETED $LG"; 
done
```

##### 10.0.6.3 Verify cleanup
```bash
aws logs describe-log-groups --log-group-name-prefix "/aws/lambda/" --region "$AWS_REGION" --no-cli-pager | jq -r '.logGroups[].logGroupName'       # no output
```

#### 10.0.7 CloudWatch alarms & dashboards
**Why:** 
Monitoring resources survive function deletion.

##### 10.0.7.1 List all alarms and dashboards
**List alarms:** 
```bash
aws cloudwatch describe-alarms --region "$AWS_REGION" --no-cli-pager | jq -r '.MetricAlarms[].AlarmName'  # no output
```

**List dashboards:** 
```bash
aws cloudwatch list-dashboards --region "$AWS_REGION" --no-cli-pager | jq -r '.DashboardEntries[].DashboardName'  # no output
```

##### 10.0.7.2 Delete all alarms and dashboards
No output means that there were alarms or dashboards to delete.

**Clean slate achieved as:**
- No customer IAM policies
- No project IAM roles
- No Lambda functions
- No EventBridge rules
- No /aws/lambda/* log groups
- No project alarms/dashboards


### 10.1 Local packaging (WSL)
#### 10.1.1 Install `zip` and verify

```bash
sudo apt-get update && sudo apt-get install -y zip && zip --version
```
**Example output:**
```text
Zip environment options:
             ZIP:  [none]
          ZIPOPT:  [none]
```

#### 10.1.2 Clean rebuild `zip`
From the directory containing lambda_function.py:
```bash
rm -f function.zip && zip -j function.zip lambda_function.py
```
**Explanation:**
-j = “junk paths”
What it does (precise):
- Removes directory paths when creating the ZIP
- Only the file names are stored at the root of the archive

Example:
zip -j function.zip src/lambda_function.py

Result inside the ZIP:
lambda_function.py

Why it matters for Lambda:
- AWS Lambda expects lambda_function.py at the root of the ZIP
- Avoids accidental folder nesting that would break the handler path

#### 10.1.3 Verify the `ZIP` contains the handler file
<u>Purpose</u>:
Verify the ZIP contains the Lambda handler at the root.

```bash
unzip -l function.zip
```
**Example output:**
```text
Archive:  function.zip
  Length      Date    Time    Name
---------  ---------- -----   ----
     8973  2025-12-14 21:21   lambda_function.py
---------                     -------
     8973                     1 file
```

**Code explanation:**
- unzip → tool to inspect or extract ZIP archives
- -l → list contents only (no extraction)
- function.zip → the Lambda deployment package

Result: shows every file inside the ZIP, letting you confirm lambda_function.py is present at the root level, which is required for the Lambda handler to work.


### 10.2 Verify no Lambda execution role exists (post-cleanup)
**List Lambda execution roles (by trust policy):**
```bash
aws iam list-roles --no-cli-pager | jq -r '.Roles[] | select(.AssumeRolePolicyDocument.Statement[].Principal.Service=="lambda.amazonaws.com") | .RoleName'          # no output
```


### 10.3 Create the Lambda execution role (from scratch)
#### 10.3.1 Create IAM role with trust policy and tags (variable assignment + full tagging + reusable filter + AWS name ≠ `NameTag`)
```bash
ROLE_NAME="ManageEIPsLambdaRole"; 
NAME_TAG="IAMRole_ManageEIPsLambdaRole"; 
LAMBDA_ROLE_ARN=$(aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' --tags Key=Name,Value="$NAME_TAG" Key=Project,Value="$TAG_PROJECT" Key=Component,Value="IAMRole" Key=Environment,Value="$TAG_ENV" Key=Owner,Value="$TAG_OWNER" Key=ManagedBy,Value="$TAG_MANAGEDBY" Key=CostCenter,Value="$TAG_COSTCENTER" --no-cli-pager | jq -r '.Role.Arn'); 
aws iam get-role --role-name "$ROLE_NAME" --no-cli-pager | jq -c -L . --arg arn "$LAMBDA_ROLE_ARN" 'include "tag_helpers"; {RoleName:.Role.RoleName,RoleArn:$arn,NameTag:(must_tag_name(.Role))}'
```
**Expected output:**
```json
{"RoleName":"ManageEIPsLambdaRole","RoleArn":"arn:aws:iam::180294215772:role/ManageEIPsLambdaRole","NameTag":"IAMRole_ManageEIPsLambdaRole"}
```

#### 10.3.2 Attach the basic execution policy (CloudWatch Logs) to the role
**Purpose:**
Give the Lambda role the minimum permissions to write logs to `/aws/lambda/....`

```bash
ROLE_NAME="ManageEIPsLambdaRole"; 
aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole" --no-cli-pager && aws iam list-attached-role-policies --role-name "$ROLE_NAME" --no-cli-pager | jq -c '{RoleName:"'"$ROLE_NAME"'",AttachedPolicies:[.AttachedPolicies[]|{PolicyName,PolicyArn}]}'
```
**Expected output:**
```json
{"RoleName":"ManageEIPsLambdaRole","AttachedPolicies":[{"PolicyName":"AWSLambdaBasicExecutionRole","PolicyArn":"arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"}]}
```


### 10.4 Create the Lambda function
#### 10.4.1 Provision Lambda resource (`ZIP`, role, tags, description, using variable assignment & reusable `jq` filter)
**Purpose:**
- Create a Lambda function called "ManageEIPs", using our packaged ZIP and the role ARN.
- Then print FunctionName + NameTag + FunctionArn (no naked IDs).

```bash
FUNCTION_NAME="ManageEIPs"; 
NAME_TAG="Lambda_ManageEIPs"; 
LAMBDA_DESC="ManageEIPs: scan Elastic IPs and release unassociated EIPs tagged for this project (supports dry-run safety mode)."; 
LAMBDA_ARN=$(aws lambda create-function --function-name "$FUNCTION_NAME" --description "$LAMBDA_DESC" --runtime python3.12 --role "$LAMBDA_ROLE_ARN" --handler lambda_function.lambda_handler --zip-file fileb://function.zip --tags "{\"Name\":\"$NAME_TAG\",\"Project\":\"$TAG_PROJECT\",\"Component\":\"Lambda\",\"Environment\":\"$TAG_ENV\",\"Owner\":\"$TAG_OWNER\",\"ManagedBy\":\"$TAG_MANAGEDBY\",\"CostCenter\":\"$TAG_COSTCENTER\"}" --region "$AWS_REGION" --no-cli-pager | jq -r '.FunctionArn'); aws lambda list-tags --resource "$LAMBDA_ARN" --region "$AWS_REGION" --no-cli-pager | jq -c -L . 'include "tag_helpers"; 
(.Tags|to_entries|map({Key:.key,Value:.value})) as $ts | {FunctionName:"'"$FUNCTION_NAME"'",FunctionArn:"'"$LAMBDA_ARN"'",NameTag:(must_tag_name({TagSet:$ts}))}'
```
**Expected output:**
```json
{"FunctionName":"ManageEIPs","FunctionArn":"arn:aws:lambda:us-east-1:180294215772:function:ManageEIPs","NameTag":"Lambda_ManageEIPs"}
```

#### 10.4.2 Publish Version 1 (best practice)
**Purpose:**
Create an immutable version and store it in $VERSION.

```bash
ALIAS_NAME="live"; 
VERSION=$(aws lambda publish-version --function-name "$FUNCTION_NAME" --region "$AWS_REGION" --no-cli-pager | jq -r '.Version');
echo "{\"FunctionName\":\"$FUNCTION_NAME\",\"PublishedVersion\":\"$VERSION\"}"
```
**Expected output (first version is Nr 2 as we had to delete the function and recreate it):**

#### 10.4.3 Create alias live pointing to that version (best practice)
**Purpose:**
Create a stable alias for invocations (later EventBridge should target the alias, not $LATEST).

```bash
ALIAS_NAME="live"; 
aws lambda create-alias --function-name "$FUNCTION_NAME" --name "$ALIAS_NAME" --function-version "$VERSION" --description "Stable alias for scheduled invocations (do not target \$LATEST)." --region "$AWS_REGION" --no-cli-pager >/dev/null; 
aws lambda get-alias --function-name "$FUNCTION_NAME" --name "$ALIAS_NAME" --region "$AWS_REGION" --no-cli-pager | jq -c '{FunctionName:"'"$FUNCTION_NAME"'",AliasName:.Name,FunctionVersion:.FunctionVersion,AliasArn:.AliasArn}'
```
**Expected output:**
```json
{"FunctionName":"ManageEIPs","AliasName":"live","FunctionVersion":"2","AliasArn":"arn:aws:lambda:us-east-1:180294215772:function:ManageEIPs:live"}
```


### 10.5 Invoke the Lambda via the alias (best practice, no $LATEST)
**Purpose:**
Run a first real test against the stable alias live, and capture the response.

```bash
ALIAS_NAME="live"; 
aws lambda invoke --function-name "ManageEIPs:live" --cli-binary-format raw-in-base64-out --payload '{}' --region "$AWS_REGION" --no-cli-pager response.json && jq -c '{FunctionName:"ManageEIPs",Alias:"live",StatusCode:.StatusCode,FunctionError:(.FunctionError//null)}' response.json 2>/dev/null || cat response.json
```
**Example output:**
```json
{
    "StatusCode": 200,
    "FunctionError": "Unhandled",
    "ExecutedVersion": "2"
}
```
Lambda ran, but the handler threw an exception (unhandled). 
The real error is in response.json and/or CloudWatch Logs.

#### 10.5.1 Inspect the Lambda invocation response (shows the exception payload)
```bash
jq -c '{statusCode:(.statusCode//null),errorType:(.errorType//null),errorMessage:(.errorMessage//null),stackTrace:(.stackTrace//null)}' response.json
```
**Example output:**
```json
{"statusCode":null,"errorType":"Sandbox.Timedout","errorMessage":"RequestId: 8d436847-0c54-4219-91da-b3a6488b7c91 Error: Task timed out after 3.00 seconds","stackTrace":null}
```
It confirms the timeout issue.

#### 10.5.2 Read the latest CloudWatch log stream for the alias execution (shows the full traceback):
```bash
STREAM=$(aws logs describe-log-streams --log-group-name "/aws/lambda/ManageEIPs" --order-by LastEventTime --descending --limit 1 --region "$AWS_REGION" --no-cli-pager | jq -r '.logStreams[0].logStreamName'); 
aws logs get-log-events --log-group-name "/aws/lambda/ManageEIPs" --log-stream-name "$STREAM" --limit 200 --region "$AWS_REGION" --no-cli-pager | jq -r '.events[] | "\(.timestamp/1000|strftime("%Y-%m-%d %H:%M:%S UTC"))  \(.message|rtrimstr("\n"))"'
```
**Example text:**
```text
2025-12-24 03:16:36 UTC  INIT_START Runtime Version: python:3.12.v101   Runtime Version ARN: arn:aws:lambda:us-east-1::runtime:994aac32248ecf4d69d9f5e9a3a57aba3ccea19d94170a61d5ecf978927e1b0f
2025-12-24 03:16:37 UTC  START RequestId: 8d436847-0c54-4219-91da-b3a6488b7c91 Version: 2
2025-12-24 03:16:37 UTC  {"level": "INFO", "message": "ManageEIPs start", "dry_run": false, "function_name": "ManageEIPs", "request_id": "8d436847-0c54-4219-91da-b3a6488b7c91", "managed_tag_key": "ManagedBy", "managed_tag_value": "ManageEIPs", "protect_tag_key": "Protection", "protect_tag_value": "DoNotRelease", "metrics_enabled": true, "metrics_namespace": "Custom/FinOps"}
2025-12-24 03:16:40 UTC  END RequestId: 8d436847-0c54-4219-91da-b3a6488b7c91
2025-12-24 03:16:40 UTC  REPORT RequestId: 8d436847-0c54-4219-91da-b3a6488b7c91 Duration: 3000.00 ms    Billed Duration: 3336 ms        Memory Size: 128 MB    Max Memory Used: 98 MB   Init Duration: 335.67 ms        Status: timeout
```
Again, confirmation of the timeout issue.

#### 10.5.3 Increase timeout from 3 to 10 seconds (keep best-practice output: name + ARN)
```bash
LAMBDA_TIMEOUT=10; 
V=$(aws lambda update-function-configuration --function-name "$FUNCTION_NAME" --timeout "$LAMBDA_TIMEOUT" --region "$AWS_REGION" --no-cli-pager >/dev/null && aws lambda wait function-updated --function-name "$FUNCTION_NAME" --region "$AWS_REGION" --no-cli-pager && aws lambda publish-version --function-name "$FUNCTION_NAME" --region "$AWS_REGION" --no-cli-pager | jq -r '.Version'); 
aws lambda update-alias --function-name "$FUNCTION_NAME" --name "$ALIAS_NAME" --function-version "$V" --region "$AWS_REGION" --no-cli-pager >/dev/null; aws lambda get-alias --function-name "$FUNCTION_NAME" --name "$ALIAS_NAME" --region "$AWS_REGION" --no-cli-pager | jq -c '{FunctionName:"'"$FUNCTION_NAME"'",AliasName:.Name,FunctionVersion:.FunctionVersion,AliasArn:.AliasArn}';
aws lambda get-function-configuration --function-name "$FUNCTION_NAME" --qualifier "$ALIAS_NAME" --region "$AWS_REGION" --no-cli-pager | jq -c '{FunctionName,Qualifier:"'"$ALIAS_NAME"'",Version,Timeout,FunctionArn}'

```
**Example output:**
```json
{"FunctionName":"ManageEIPs","Qualifier":"live","Version":"4","Timeout":10,"FunctionArn":"arn:aws:lambda:us-east-1:180294215772:function:ManageEIPs:live"}
```

#### 10.5.4 Wait until the update is applied (prevents racing)
```bash
aws lambda wait function-updated --function-name "$FUNCTION_NAME" --region "$AWS_REGION" --no-cli-pager && aws lambda get-function-configuration --function-name "$FUNCTION_NAME" --region "$AWS_REGION" --no-cli-pager | jq -c '{FunctionName,Timeout,FunctionArn,LastUpdateStatus}'
```
**Example output:**
```json
{"FunctionName":"ManageEIPs","Timeout":10,"FunctionArn":"arn:aws:lambda:us-east-1:180294215772:function:ManageEIPs","LastUpdateStatus":"Successful"}
```

#### 10.5.5 Re-invoke via alias (best practice):
```bash
aws lambda invoke --function-name "ManageEIPs:live" --cli-binary-format raw-in-base64-out --payload '{}' --region "$AWS_REGION" --no-cli-pager response.json
```
**Example output:**
```json
{
    "StatusCode": 200,
    "FunctionError": "Unhandled",
    "ExecutedVersion": "4"
}
```

#### 10.5.6 Show the actual exception from the invoke payload
```bash
jq -c '{errorType:(.errorType//null),errorMessage:(.errorMessage//null)}' response.json
```
**Example output:**
```json
{"statusCode":null,"errorType":"ClientError","errorMessage":"An error occurred (UnauthorizedOperation) when calling the DescribeAddresses operation: You are not authorized to perform this operation. User: arn:aws:sts::180294215772:assumed-role/ManageEIPsLambdaRole/ManageEIPs is not authorized to perform: ec2:DescribeAddresses because no identity-based policy allows the ec2:DescribeAddresses action"}
```
So, the Lambda role only has AWSLambdaBasicExecutionRole (logs). 
It has no EC2 permissions, so ec2:DescribeAddresses is denied.


### 10.6 Attach the EC2 read permissions to the Lambda role (best practice: least privilege for this project)
**Purpose:**
Allow the function to list/describe EIPs (and tags), so it can decide what to release.

```bash
POLICY_NAME="ManageEIPs-EC2DescribePolicy"; 
POLICY_ARN=$(aws iam create-policy --policy-name "$POLICY_NAME" --policy-document '{"Version":"2012-10-17","Statement":[{"Sid":"EC2DescribeForEIPScan","Effect":"Allow","Action":["ec2:DescribeAddresses","ec2:DescribeTags"],"Resource":"*"}]}' --tags Key=Name,Value="IAMPolicy_ManageEIPs_EC2Describe" Key=Project,Value="$TAG_PROJECT" Key=Component,Value="IAMPolicy" Key=Environment,Value="$TAG_ENV" Key=Owner,Value="$TAG_OWNER" Key=ManagedBy,Value="$TAG_MANAGEDBY" Key=CostCenter,Value="$TAG_COSTCENTER" --no-cli-pager | jq -r '.Policy.Arn'); aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn "$POLICY_ARN" --no-cli-pager && aws iam list-attached-role-policies --role-name "$ROLE_NAME" --no-cli-pager | jq -c '{RoleName:"'"$ROLE_NAME"'",AttachedPolicies:[.AttachedPolicies[]|{PolicyName,PolicyArn}]}'
```
**Example output:**
```json
{"RoleName":"ManageEIPsLambdaRole","AttachedPolicies":[{"PolicyName":"AWSLambdaBasicExecutionRole","PolicyArn":"arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"},{"PolicyName":"ManageEIPs-EC2DescribePolicy","PolicyArn":"arn:aws:iam::180294215772:policy/ManageEIPs-EC2DescribePolicy"}]}
```


### 10.7 Publish a new version and repoint alias live (because aliases pin versions)
```bash
V=$(aws lambda publish-version --function-name "$FUNCTION_NAME" --region "$AWS_REGION" --no-cli-pager | jq -r '.Version'); 
aws lambda update-alias --function-name "$FUNCTION_NAME" --name "$ALIAS_NAME" --function-version "$V" --region "$AWS_REGION" --no-cli-pager >/dev/null; 
aws lambda get-alias --function-name "$FUNCTION_NAME" --name "$ALIAS_NAME" --region "$AWS_REGION" --no-cli-pager | jq -c '{FunctionName:"'"$FUNCTION_NAME"'",AliasName:.Name,FunctionVersion:.FunctionVersion,AliasArn:.AliasArn}'
```
**Example output:**
```json
{"FunctionName":"ManageEIPs","AliasName":"live","FunctionVersion":"4","AliasArn":"arn:aws:lambda:us-east-1:180294215772:function:ManageEIPs:live"}
```


### 10.8 Re-invoke via alias (validation)
```bash
aws lambda invoke --function-name "$FUNCTION_NAME:$ALIAS_NAME" --cli-binary-format raw-in-base64-out --payload '{}' --region "$AWS_REGION" --no-cli-pager response.json
```
**Example output:**
```json
{
    "StatusCode": 200,
    "ExecutedVersion": "4"
}
```

**Verify what live points to (no ambiguity):**
```bash
aws lambda invoke --function-name "$FUNCTION_NAME:$ALIAS_NAME" --cli-binary-format raw-in-base64-out --payload '{}' --region "$AWS_REGION" --no-cli-pager response.json 2>/dev/null | jq -c '{StatusCode,ExecutedVersion,FunctionError:(.FunctionError//null)}';
jq -c '.' response.json
```
**Example output:**
```json
{"StatusCode":200,"ExecutedVersion":"4","FunctionError":null}
{"statusCode":200,"body":"Processed elastic IPs."}
```

Status: PASS — Lambda executed successfully via alias live.


### 10.9 — Verify Elastic IP state after Lambda execution
**Purpose:**
Confirm functional correctness:
- attached EIP still present and associated with ManageEIPs-ec2.
- unused EIPs that match the release criteria have been released.
- no protected or unmanaged EIPs were touched.

#### 10.9.1 List and inspect all Elastic IPs after Lambda execution

```bash
aws ec2 describe-addresses --region "$AWS_REGION" --no-cli-pager | jq -L . 'include "tag_helpers"; [.Addresses[] | {AllocationId,NameTag:(must_tag_name(.)),PublicIp,InstanceId:(.InstanceId//null),ManagedBy:(tag_value(.;"ManagedBy")),Protection:(tag_value(.;"Protection"))}]'
```
**Example output:**
```json
[
  {
    "AllocationId": "eipalloc-0da06ada08055dcb5",
    "NameTag": "EIP_Attached_ManageEIPs",
    "PublicIp": "100.50.117.194",
    "InstanceId": "i-0c69cfce5294ffdd4",
    "ManagedBy": "CLI",
    "Protection": null
  },
  {
    "AllocationId": "eipalloc-0cb2c1dd93ad66184",
    "NameTag": "EIP_Unused1_ManageEIPs",
    "PublicIp": "100.52.71.230",
    "InstanceId": null,
    "ManagedBy": "CLI",
    "Protection": null
  },
  {
    "AllocationId": "eipalloc-03c791ad93b266fe6",
    "NameTag": "EIP_Unused2_ManageEIPs",
    "PublicIp": "3.225.31.165",
    "InstanceId": null,
    "ManagedBy": "CLI",
    "Protection": null
  }
]
```
The Lambda function ran but the two unused EIPs are still there.

#### 10.9.2 Verify the release criteria (show expected vs actual tags, per EIP)
```bash
aws ec2 describe-addresses --region "$AWS_REGION" --no-cli-pager | jq -L . 'include "tag_helpers"; [.Addresses[] | {AllocationId,NameTag:(must_tag_name(.)),PublicIp,InstanceId:(.InstanceId//null),ManagedBy:(tag_value(.;"ManagedBy")),IsManaged:((tag_value(.;"ManagedBy"))=="ManageEIPs"),Protection:(tag_value(.;"Protection")),IsProtected:((tag_value(.;"Protection"))=="DoNotRelease")}]'
```
**Example output:**
```json
[
  {
    "AllocationId": "eipalloc-0da06ada08055dcb5",
    "NameTag": "EIP_Attached_ManageEIPs",
    "PublicIp": "100.50.117.194",
    "InstanceId": "i-0c69cfce5294ffdd4",
    "ManagedBy": "CLI",
    "IsManaged": false,
    "Protection": null,
    "IsProtected": false
  },
  {
    "AllocationId": "eipalloc-0cb2c1dd93ad66184",
    "NameTag": "EIP_Unused1_ManageEIPs",
    "PublicIp": "100.52.71.230",
    "InstanceId": null,
    "ManagedBy": "CLI",
    "IsManaged": false,
    "Protection": null,
    "IsProtected": false
  },
  {
    "AllocationId": "eipalloc-03c791ad93b266fe6",
    "NameTag": "EIP_Unused2_ManageEIPs",
    "PublicIp": "3.225.31.165",
    "InstanceId": null,
    "ManagedBy": "CLI",
    "IsManaged": false,
    "Protection": null,
    "IsProtected": false
  }
]
```
**Conclusion:**
- Observed: ManagedBy="CLI" on attached + unused EIPs → IsManaged=false for all.
- Rule: Lambda manages only resources tagged ManagedBy="ManageEIPs".
- Result: Lambda correctly skipped the two unused EIPs (and would also skip the attached one if relying only on tags).

#### 10.9.3 Bring the two unused EIPs into scope (set `ManagedBy=ManageEIPs`)
```bash
EIP_FIXTURE_NAMES="EIP_Unused1_ManageEIPs,EIP_Unused2_ManageEIPs"; 
EIP_FIXTURE_ALLOC_IDS=$(aws ec2 describe-addresses --region "$AWS_REGION" --no-cli-pager --filters "Name=tag:Name,Values=$EIP_FIXTURE_NAMES" | jq -r '.Addresses[].AllocationId' | paste -sd' ' -); 
aws ec2 create-tags --region "$AWS_REGION" --no-cli-pager --resources $EIP_FIXTURE_ALLOC_IDS --tags Key=ManagedBy,Value="ManageEIPs" && aws ec2 describe-addresses --region "$AWS_REGION" --no-cli-pager --allocation-ids $EIP_FIXTURE_ALLOC_IDS | jq -c -L . 'include "tag_helpers"; [.Addresses[] | {AllocationId,NameTag:(must_tag_name(.)),PublicIp,InstanceId:(.InstanceId//null),ManagedBy:(tag_value(.;"ManagedBy"))}]'
```
**Example output:**
```json
[{"AllocationId":"eipalloc-03c791ad93b266fe6","NameTag":"EIP_Unused2_ManageEIPs","PublicIp":"3.225.31.165","InstanceId":null,"ManagedBy":"ManageEIPs"},{"AllocationId":"eipalloc-0cb2c1dd93ad66184","NameTag":"EIP_Unused1_ManageEIPs","PublicIp":"100.52.71.230","InstanceId":null,"ManagedBy":"ManageEIPs"}]
```
**Conclusion:**
The two unused test EIPs are now tagged ManagedBy=ManageEIPs and are in scope for the automation.

#### 10.9.4 Invoke `ManageEIPs` (alias live)
```bash
aws lambda invoke --function-name "$FUNCTION_NAME:$ALIAS_NAME" --cli-binary-format raw-in-base64-out --payload '{}' --region "$AWS_REGION" --no-cli-pager response.json 2>/dev/null | jq -c '{StatusCode,ExecutedVersion,FunctionError:(.FunctionError//null)}'
```
**Example output:**
```json
{"StatusCode":200,"ExecutedVersion":"4","FunctionError":"Unhandled"}
```
**Conclusion:**
The 2 unused EIPs are still there. We have to examine the release permissions to the Lambda role (least privilege).

##### 10.9.4.1 Extract the actual exception (from payload file)
```bash
jq -c '{errorType:(.errorType//null),errorMessage:(.errorMessage//null)}' response.json
```
**Example output:**
```json
{"errorType":"ClientError","errorMessage":"An error occurred (UnauthorizedOperation) when calling the ReleaseAddress operation: You are not authorized to perform this operation. User: arn:aws:sts::180294215772:assumed-role/ManageEIPsLambdaRole/ManageEIPs is not authorized to perform: ec2:ReleaseAddress on resource: arn:aws:ec2:us-east-1:180294215772:elastic-ip/eipalloc-0cb2c1dd93ad66184 because no identity-based policy allows the ec2:ReleaseAddress action. Encoded authorization failure message"...}
```

**Conclusion:**
The Lambda execution role ManageEIPsLambdaRole is missing an identity-based IAM policy allowing ec2:ReleaseAddress (and typically ec2:DisassociateAddress).
Result: the function can detect unused EIPs but cannot release them → test fails at the release step.

##### 10.9.4.2 Attach EC2 release permissions to the Lambda role (least privilege for this project)
```bash
POLICY_NAME="ManageEIPs-EC2ReleasePolicy"; 
POLICY_ARN=$(aws iam create-policy --policy-name "$POLICY_NAME" --policy-document '{"Version":"2012-10-17","Statement":[{"Sid":"EC2ReleaseEIPs","Effect":"Allow","Action":["ec2:DisassociateAddress","ec2:ReleaseAddress"],"Resource":"*"}]}' --tags Key=Name,Value="IAMPolicy_ManageEIPs_EC2Release" Key=Project,Value="$TAG_PROJECT" Key=Component,Value="IAMPolicy" Key=Environment,Value="$TAG_ENV" Key=Owner,Value="$TAG_OWNER" Key=ManagedBy,Value="$TAG_MANAGEDBY" Key=CostCenter,Value="$TAG_COSTCENTER" --no-cli-pager | jq -r '.Policy.Arn'); 
aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn "$POLICY_ARN" --no-cli-pager && aws iam list-attached-role-policies --role-name "$ROLE_NAME" --no-cli-pager | jq -c '{RoleName:"'"$ROLE_NAME"'",AttachedPolicies:[.AttachedPolicies[]|{PolicyName,PolicyArn}]}'
```
**Example output:**
```json
{"RoleName":"ManageEIPsLambdaRole","AttachedPolicies":[{"PolicyName":"AWSLambdaBasicExecutionRole","PolicyArn":"arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"},{"PolicyName":"ManageEIPs-EC2DescribePolicy","PolicyArn":"arn:aws:iam::180294215772:policy/ManageEIPs-EC2DescribePolicy"},{"PolicyName":"ManageEIPs-EC2ReleasePolicy","PolicyArn":"arn:aws:iam::180294215772:policy/ManageEIPs-EC2ReleasePolicy"}]}
```

##### 10.9.4.3 Re-invoke
```bash
aws lambda invoke --function-name "$FUNCTION_NAME:$ALIAS_NAME" --cli-binary-format raw-in-base64-out --payload '{}' --region "$AWS_REGION" --no-cli-pager response.json 2>/dev/null | jq -c '{StatusCode,ExecutedVersion,FunctionError:(.FunctionError//null)}'
```
**Example output:**
```json
{"StatusCode":200,"ExecutedVersion":"4","FunctionError":null}
```

**Conclusion:**
The Lambda executed successfully with no errors.
Proceed to verify that the two unused Elastic IPs have been released.

##### 10.9.4.4 Re-check EIPs (proof they’re gone)
```bash
aws ec2 describe-addresses --region "$AWS_REGION" --no-cli-pager | jq -c -L . 'include "tag_helpers"; [.Addresses[] | {AllocationId,NameTag:(must_tag_name(.)),PublicIp,InstanceId:(.InstanceId//null),ManagedBy:(tag_value(.;"ManagedBy"))}]'
```
**Example output:**
```json
[{"AllocationId":"eipalloc-0da06ada08055dcb5","NameTag":"EIP_Attached_ManageEIPs","PublicIp":"100.50.117.194","InstanceId":"i-0c69cfce5294ffdd4","ManagedBy":"CLI"}]
```

**Conclusion:**
Only the attached Elastic IP remains.
The two unused Elastic IPs have been successfully released by the Lambda.

##### 10.9.5 Validation summary
The ManageEIPs Lambda correctly identified managed resources, released unused Elastic IPs, preserved the attached Elastic IP, and enforced tag-based ownership rules.
Status: PASS.



## 11. Advanced Capabilities (Safety, Observability, Multi-Region)
This section describes architectural and operational capabilities that extend beyond the core Lambda implementation.
These features are not required for the function to operate correctly, but demonstrate how the solution can scale, adapt, and remain cost-controlled in more advanced or production-like scenarios.
They are implemented without modifying the Lambda logic itself and focus on deployment, resilience, and operational considerations.

### 11.1 Multi-Region Capability (Design & Rationale)
The solution is designed to be Region-agnostic and can be deployed independently in multiple AWS Regions without any changes to the Lambda code.
Each deployment operates on the Elastic IP resources local to its Region and is triggered by its own EventBridge schedule.
This approach limits operational blast radius (how far the damage spreads in case of failure, bug, or bad changes), avoids cross-Region dependencies, and aligns with AWS regional isolation principles, while keeping observability and cost control fully local to each Region. 


### 11.2 Multi-Region Deployment Model
The solution supports a multi-Region deployment by deploying the same stack independently into each target AWS Region (e.g., Region A and Region B), using the same Lambda code package and the same tagging/naming standards.
Each Region deployment is fully self-contained and includes:
- a regional Lambda function
- a regional EventBridge schedule rule (trigger)
- a regional CloudWatch Logs log group (created automatically on first invocation)
- the same IAM execution role (IAM is global, reused across Regions)
- optional regional alarms / dashboards (if enabled)

**Key characteristics of this model**
- **No cross-Region dependencies**
  Each Region runs independently and operates only on Elastic IPs in its own Region. 
  There is no need for cross-Region API calls, replication, or shared state.

- **Same code, different Region context**
  The Lambda logic does not change. 
  When deployed in a different Region, the AWS SDK automatically targets that Region (based on the function’s runtime environment and regional endpoints). 
  Therefore the same logic discovers and manages EIPs locally.

- **Independent schedules per Region**
  Each Region has its own EventBridge rule. 
  Schedules may differ per Region (e.g., daily in Region A, weekly in Region B). 
  To keep costs minimal, a secondary Region can be deployed with its schedule disabled by default, enabling it only when needed.

- **Isolation and blast-radius control**
  Failures, misconfigurations, or unintended behavior are isolated to the Region where they occur. 
  This prevents a single bad change from impacting resources in multiple Regions.

- **Consistent naming and tags across Regions**
  Resource naming should remain consistent while making the Region explicit in either the AWS resource name or Name tag (or both), for example:
  - Function name: `ManageEIPs-RegionA`, `ManageEIPs-RegionB`
  - Rule name: `ManageEIPs-Schedule-RegionA`, `ManageEIPs-Schedule-RegionB`
  - Name tag: `Lambda_ManageEIPs_RegionA`, `EventBridge_ManageEIPs_RegionB`

- **Repeatable deployment approach**
  The deployment is performed by running the same creation commands (or applying the same IaC template) while changing only the target Region variable (e.g., AWS_REGION). 
  All verification commands must be run per Region to confirm correct deployment and tagging.

This deployment model provides multi-Region capability as a deployment pattern rather than a code change, enabling the project to scale across Regions while keeping operational complexity and cost tightly controlled.


### 11.3 Cost Impact of Multi-Region Deployment
Deploying the solution in multiple AWS Regions introduces incremental costs, but these remain predictable, low, and controllable due to the independent and lightweight nature of each deployment.

**The primary cost drivers per Region are:**
- **AWS Lambda executions**
  Costs are driven by invocation count and execution duration. 
  With a low-frequency schedule (e.g., daily or weekly), Lambda costs remain negligible. 
  No additional cost is incurred when the function is idle.

- **Amazon EventBridge schedules**
  EventBridge rules incur minimal cost. 
  A secondary Region can be deployed with its rule disabled by default, resulting in near-zero ongoing cost until explicitly enabled.

- **Amazon CloudWatch Logs**
  Log ingestion and storage costs depend on log volume. 
  Keeping structured, concise logs and avoiding excessive verbosity ensures costs stay minimal. 
  No logs are generated if the function is not invoked.

- **Optional monitoring resources**
  Additional CloudWatch metrics, alarms, or dashboards (if enabled) may introduce small recurring costs per Region. These are optional and can be limited to the primary Region only.

**Important cost-control characteristics of the multi-Region model**
- **No always-on infrastructure:**
  There are no EC2 instances, load balancers, or persistent services running continuously in each Region.

- **No cross-Region data transfer**
  Each Region operates entirely locally, avoiding inter-Region traffic charges.

- **Independent cost visibility**
  Because resources are deployed and tagged per Region, costs can be monitored, attributed, and controlled independently using tags and standard cost allocation tools.

- **On-demand activation**
  Secondary Regions can remain deployed but inactive (disabled schedules), allowing rapid enablement without incurring ongoing execution costs.

Overall, the multi-Region deployment model provides high architectural flexibility with minimal financial impact, making it suitable even for small-scale or experimental environments while remaining scalable to production use cases when required.


### 11.4 Testing Strategy & Validation
The solution is validated using a progressive testing strategy that prioritizes safety, observability, and cost control.
Testing is designed to confirm correct behavior without risking accidental modification or release of Elastic IP resources.

#### 11.4.1 Test data preparation (unused Elastic IPs)
Preparation of controlled test conditions required to validate the Lambda function, including the recreation of unused Elastic IPs after prior cleanup operations.

##### 11.4.1.1 Recreate first unused Elastic IP
```bash
EIP_UNUSED1_ALLOC_ID=$(aws ec2 allocate-address --region "$AWS_REGION" --domain vpc --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=EIP_Unused1_ManageEIPs},{Key=Project,Value=$TAG_PROJECT},{Key=Environment,Value=$TAG_ENV},{Key=Owner,Value=$TAG_OWNER},{Key=ManagedBy,Value=$TAG_MANAGEDBY},{Key=CostCenter,Value=$TAG_COSTCENTER}]" --no-cli-pager | jq -r '.AllocationId'); 
aws ec2 describe-addresses --region "$AWS_REGION" --no-cli-pager --allocation-ids "$EIP_UNUSED1_ALLOC_ID" | jq -c '[.Addresses[] | {AllocationId,Name:(((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"MISSING-EIP-NAME"),PublicIp}]'
```
**Example output:**
```json
[{"AllocationId":"eipalloc-06b9574f9e8fed2d7","Name":"EIP_Unused1_ManageEIPs","PublicIp":"54.204.14.253"}]
```

##### 11.4.1.2 Recreate second unused Elastic IP
```bash
EIP_UNUSED2_ALLOC_ID=$(aws ec2 allocate-address --region "$AWS_REGION" --domain vpc --tag-specifications "ResourceType=elastic-ip,Tags=[{Key=Name,Value=EIP_Unused2_ManageEIPs},{Key=Project,Value=$TAG_PROJECT},{Key=Environment,Value=$TAG_ENV},{Key=Owner,Value=$TAG_OWNER},{Key=ManagedBy,Value=$TAG_MANAGEDBY},{Key=CostCenter,Value=$TAG_COSTCENTER}]" --no-cli-pager | jq -r '.AllocationId'); 
aws ec2 describe-addresses --region "$AWS_REGION" --no-cli-pager --allocation-ids "$EIP_UNUSED2_ALLOC_ID" | jq -c '[.Addresses[] | {AllocationId,Name:(((.Tags//[])|map(select(.Key=="Name")|.Value)|.[0])//"MISSING-EIP-NAME"),PublicIp}]'
```
**Example output:**
```json
[{"AllocationId":"eipalloc-036a044d806b80d83","Name":"EIP_Unused2_ManageEIPs","PublicIp":"18.234.11.69"}]
```

##### 11.4.1.3 ag the two AllocationIds as `ManagedBy`=`ManageEIPs` (run once per EIP, or combine)
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

#### 11.4.2 Dry-run / safety mode validation
The Lambda function supports a dry-run mode in which all actions are evaluated and logged but no changes are applied to AWS resources.
This mode is used as the primary validation mechanism to confirm:
- correct discovery of Elastic IPs in the Region
- correct filtering and decision logic
- correct logging and tagging behavior
Dry-run mode can be enabled per Region, allowing safe validation of secondary Regions without operational risk.

#### 11.4.3 Manual invocation testing
The function can be invoked manually using the AWS CLI or the AWS Console to validate behavior on demand.
Manual invocations are used to:
- verify environment variable configuration
- validate IAM permissions
- confirm correct execution in newly deployed Regions before enabling scheduled execution

#### 11.4.4 Scheduled execution testing
EventBridge rules are initially deployed in a disabled state and enabled only after successful dry-run and manual validation.
This ensures that automated execution is introduced gradually and only after confidence in correct behavior has been established.

#### 11.4.5 Observability and log verification
**CloudWatch Logs are reviewed after each test execution to confirm:**
- successful function invocation
- correct decision outcomes (action vs. no-action)
- absence of unexpected errors or warnings
Structured logging enables consistent review and simplifies troubleshooting across Regions.

**Observed execution:**
During the scheduled monthly execution, the scheduled EventBridge invocation executed successfully at 02:10 UTC with dry_run=false. 
The function scanned 3 Elastic IPs and skipped all of them because they were not tagged as managed (ManagedBy=ManageEIPs), resulting in released=0.
After applying the required `ManagedBy=ManageEIPs` tag to the two unused Elastic IPs, the subsequent scheduled execution successfully released both addresses as expected.

#### 11.4.6 Failure isolation validation
By deploying and testing each Region independently, failures can be verified to remain isolated to a single Region.
This confirms the intended blast-radius containment and prevents cross-Region impact during testing or operation.

This testing and validation strategy ensures that the solution can be safely deployed, verified, and operated in both single-Region and multi-Region scenarios while maintaining strong safeguards against unintended changes and unnecessary costs.



## 12. EventBridge

### 12.1 Create the monthly EventBridge rule
The rule is configured to run at 22:00 UTC on the 25th of each month.
```bash
RULE_ARN=$(aws events put-rule --name "CheckAndReleaseUnassociatedEIPs-Monthly" --schedule-expression "cron(0 22 16 * ? *)" --state ENABLED --region "$AWS_REGION" --no-cli-pager | jq -r '.RuleArn')
```

Update the rule to run at 02:40 UTC on the 25th of each month.
```bash
aws events put-rule --name "CheckAndReleaseUnassociatedEIPs-Monthly" --schedule-expression "cron(40 2 25 * ? *)" --state ENABLED --region "$AWS_REGION" --no-cli-pager | jq -c
```
**Example output:**
```json
{"RuleArn":"arn:aws:events:us-east-1:180294215772:rule/CheckAndReleaseUnassociatedEIPs-Monthly"}
```

### 12.2 Verify the EventBridge rule
```bash
aws events describe-rule --name "CheckAndReleaseUnassociatedEIPs-Monthly" --region "$AWS_REGION" --no-cli-pager | jq -c '{Name,ScheduleExpression,State}'
```
**Example output:**
```json
{"Name":"CheckAndReleaseUnassociatedEIPs-Monthly","ScheduleExpression":"cron(40 2 25 * ? *)","State":"ENABLED"}
```

### 12.3 Allow EventBridge to invoke the Lambda function
```bash
ACCOUNT_ID=$(aws sts get-caller-identity --no-cli-pager | jq -r '.Account'); aws lambda add-permission --function-name "ManageEIPs" --statement-id "AllowEventBridgeMonthlyTrigger" --action "lambda:InvokeFunction" --principal "events.amazonaws.com" --source-arn "arn:aws:events:${AWS_REGION}:${ACCOUNT_ID}:rule/CheckAndReleaseUnassociatedEIPs-Monthly" --region "$AWS_REGION" --no-cli-pager | jq
```
**Example output:**
```json
{
  "Statement": "{\"Sid\":\"AllowEventBridgeMonthlyTrigger\",\"Effect\":\"Allow\",\"Principal\":{\"Service\":\"events.amazonaws.com\"},\"Action\":\"lambda:InvokeFunction\",\"Resource\":\"arn:aws:lambda:us-east-1:180294215772:function:ManageEIPs\",\"Condition\":{\"ArnLike\":{\"AWS:SourceArn\":\"arn:aws:events:us-east-1:180294215772:rule/CheckAndReleaseUnassociatedEIPs-Monthly\"}}}"
}
```


### 12.4 Add Lambda as the target of the monthly rule
```bash
aws events put-targets --rule "CheckAndReleaseUnassociatedEIPs-Monthly" --targets "[{\"Id\":\"ManageEIPs\",\"Arn\":\"$LAMBDA_ARN\"}]" --region "$AWS_REGION" --no-cli-pager | jq
```
**Example output, meaning that no target was rejected (and visible in AWS EventBridge):**
```json
{
  "FailedEntryCount": 0,
  "FailedEntries": []
}
```


### 12.5 Verify targets are attached
```bash
aws events list-targets-by-rule --rule "CheckAndReleaseUnassociatedEIPs-Monthly" --region "$AWS_REGION" --no-cli-pager | jq -c
```
```json
{"Targets":[{"Id":"ManageEIPs","Arn":"arn:aws:lambda:us-east-1:180294215772:function:ManageEIPs"}]}
```



## 13. GitHub (Repository Publishing & Portfolio Standards)
### 13.1 Repository purpose and scope
**Purpose:** 
- Publish the ManageEIPs automation as a clean, reproducible portfolio repository: infrastructure templates, Lambda code, 
  and helper jq filters, with instructions to deploy and run.

- Contains: source code, IaC/templates, docs, sample configs (sanitized), and reusable jq filters.

- Explicitly does not contain: credentials, account IDs that don’t need to be public, state files, deployment outputs, 
  local artifacts, or any exported CLI JSON dumps unless sanitized.


### 13.2 Local repo initialization (if not already a repo)
#### 13.2.1 Folder selection
Project root folder: `ManageEIPs-1region`

#### 13.2.2 Initilise `git`
```bash
git init
```

**Expected output: Git created the repo and - by default - started on branch `master`, instead of `main`** 
```text
Using 'master' as the name for the initial branch. This default branch name is subject to change.
```

#### 13.2.3 Set default branch to main
**Make all future git init default to main**
```bash
git config --global init.defaultBranch main
```

**Rename current branch to main + list repo status**
```bash
git branch -M main
git status
```
**Expected output:**
```text
.vs/
        ManageEIPsKeyPair.pem
        ManageEIPs_Automation.md
        README.md
        event.json
        flatten_tags.jq
        function.zip
        lambda_function.py
        manage-eips-policy.json
        manage-eips-trust.json
        response.1766551137.json
        response.json
        rules_names.jq
        tag_helpers.jq
```

#### 13.2.4 Configure `Git author identity` (mandatory for first commit)
```bash
git config --global user.name "Malik Hamdane"
git config --global user.email "mfhamdane@hotmail.com"
```

#### 13.2.5 Create `gitignore`to avoid committing certain files
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

#### 13.2.6 Stage what should be published and create the first commit
```bash
git add .gitignore README.md ManageEIPs_Automation.md lambda_function.py manage-eips-policy.json manage-eips-trust.json *.jq
git commit -m "chore: initialize repo with docs, lambda, and jq helpers"
git status
```

#### 13.2.7 Verify status/log (`git status` / `git log`...)
```bash
git status
git log --oneline -n 5
```


### 13.3 File set to publish
- Required files: `README.md`, `ManageEIPs_Automation.md`, Lambda code, templates, helper `jq` filters
- Excluded files: credentials, any private notes, large binaries


### 13.4 Commit standards (“portfolio discipline”)
- Small commits, one concern per commit
- Commit messages format you prefer
- “No hardcoded IDs / CLI + `jq -c` outputs / consistent tags” as review checklist


### 13.5 Remote creation and first push
#### 13.5.1 Create GitHub repo (UI)
- Go to GitHub and sign in.
- click on + > New Repository > add Description > Visibility = public (portfolio)
- Leave 'Add README', 'Add .gitignore' and 'Add License' unchecked > click on CREATE REPOSITORY
- Click on 'Code' tab > click on HTTPS (grey when active)
- Copy URL ending with git: https://github.com/fred1717/ManageEIPs-1region.git

```bash
git remote add origin https://github.com/fred1717/ManageEIPs-1region.git
git remote -v
```

**Expected output:**
```text
origin  https://github.com/fred1717/ManageEIPs-1region.git (fetch)
origin  https://github.com/fred1717/ManageEIPs-1region.git (push)
```

#### 13.5.2 First push
**Create a GitHub Personal Access Token (PAT) for git push (HTTPS):**
- Open token settings (UI)
  - Profile picture > Settings > Scroll down and click 'Developer settings' > Click Personal access tokens > 
  - Click Token (classic) > Click 'Generate new token (classic)

- Configure the token
  - Note: ManageEIPs-1region
  - Expiration: choose one (recommended: 30 or 90 days; or your preference)
  - Select scopes: tick repo (this covers push to repositories)

- Generate and copy
  - Scroll down and click Generate token.
  - Copy the token immediately (will be invisible afterwards).

- Use it for the push
```bash
git push -u origin main
```

- When prompted:
  Username for 'https://github.com': fred1717
  Password for 'https://fred1717@github.com': paste the PAT I just copied


### 13.6 Release/Versioning (optional)
- Repo renders README properly
- Markdown links work
- No secrets committed (final sanity check)

#### 13.6.1 Verify push succeeded (CLI)
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
git add ManageEIPs_Automation.md
```

**Commit**
```bash
git commit -m "docs: update ManageEIPs_Automation"        # 1 file changed, 55 insertions(+), 22 deletions(-)
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
**Expected output: local brand and GitHub are now synchronised**
eaf773f (HEAD -> main, origin/main) docs: update ManageEIPs_Automation
515a7a5 chore: initialize repo with docs, lambda, and jq helpers

#### 13.6.2  Verify files on GitHub (UI)
- GitHub > Your Repositories > Click ManageEIPs-1region > Refresh page > check latest commit + list of present files 
- Click README.md to confirm it renders properly
- Go back > click on ManageEIPs_Automation.md: check code blocks render properly.

#### 13.6.3 Final sanity check: no secrets committed (CLI)
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

#### 13.6.4 Check repo has no ignored artifacts staged
```bash
git status --ignored
```
The only uncommitted edit is, as before, `ManageEIPs_Automation.md`. This was to be expected as it is still being edited.


### 13.7 Release/Versioning
#### 13.7.0 Important note (timing)
This repository’s documentation (`ManageEIPs_Automation.md`) is still being updated, and the `README.md` will be revised again after the documentation is final.
Therefore do not create the final release tag (`v1.0.0`) until:
- `ManageEIPs_Automation.md` is finished.
- `README.md` has been updated to its final version.

We can still commit and push changes normally while documentation is in progress.


#### 13.7.1 Normal workflow while docs are still changing (commit + push)
```bash
git status
git add ManageEIPs_Automation.md README.md
git commit -m "docs: update documentation"
git push
```

#### 13.7.2 Pre-release tag while docs are WIP (Work-In-Progress)
It provides a visible milestone before the final documentation is complete.
**Create pre-release tag**
```bash
git tag -a v0.1.0 -m "v0.1.0 work in progress (docs not final)"
git push origin v0.1.0
```
**In GitHub**
- repo `ManageEIPs-1region` > Releases > Create a new release > Select tag: `v0.1.0` > tick "Set as a pre-release"
- Release title: `v0.1.0` (WIP) > Publish release

#### 13.7.3 Final release when documentation is complete (v1.0.0)
**Prerequisite: workijg tree clean**
```bash
git status
```

**Create final tag**
```bash
git tag -a v1.0.0 -m "v1.0.0 first portfolio release"
git push origin v1.0.0
```

**Create GitHub Release (UI)**
- repo `ManageEIPs-1region` > Releases > Create a new release > Select tag: `v1.0.0` > don't tick "Set as a pre-release"
- Release title: `v1.0.0` > Publish release

**Verify**
- Repo > Releases
- Confirm `v1.0.0` points to the intended commit.
