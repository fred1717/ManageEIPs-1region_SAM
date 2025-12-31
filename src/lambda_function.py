import os       # Python standard library module
import json     # Used for structured logging
import time     # Used for EMF metric timestamp

import boto3    # third-party, AWS SDK for Python, to interact with AWS services from Lambda
from botocore.exceptions import ClientError

def log(level, message, **fields):
    """
    Emit a single structured JSON log line to stdout.
    CloudWatch Logs captures stdout automatically.
    """
    log_event = {
        "level": level,
        "message": message,
        **fields
    }

    # Make sure the **fields I pass includes a serializable object, i.e. a string.
    # Otherwise, json.dumps will raise TypeError and the Lambda will crash.
    print(json.dumps(log_event, default=str))




def _get_tag_value(tags, key):
    """
    Safely read a tag value from an AWS 'Tags' list:
    tags = [{"Key": "...", "Value": "..."}, ...]
    """
    if not tags:
        return None

    for t in tags:
        if t.get("Key") == key:
            return t.get("Value")

    return None



def emit_emf_metrics(namespace, dimensions, metrics):
    """
    Emit CloudWatch custom metrics via Embedded Metric Format (EMF).
    This is written to logs; CloudWatch can extract metrics automatically.

    AWS official position: EMF is the preferred way to publish custom metrics from Lambda.

    Notes:
    - This does NOT replace AWS/Lambda built-in metrics (Invocations, Errors, Duration, Throttles).
    - It supplements them with project metrics like EIPsReleased / EIPsWouldRelease.
    """
    metric_definitions = []
    for metric_name in metrics.keys():
        metric_definitions.append({"Name": metric_name, "Unit": "Count"})

    emf_event = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": namespace,
                    "Dimensions": [list(dimensions.keys())],
                    "Metrics": metric_definitions,
                }
            ],
        },
        **dimensions,
        **metrics,
    }
    print(json.dumps(emf_event))




def lambda_handler(event, context):
    # Dry-run flag: when true, only logs which EIPs would be released (no destructive action).
    # Priority: event.json ("dry_run") overrides Lambda env var ("DRY_RUN").
    dry_run = str(event.get("dry_run", os.getenv("DRY_RUN", "false"))).lower() == "true"
    
    # Safety controls (tag-based allow-list + protection)
    # Only EIPs explicitly tagged as "managed" will be considered for release.
    MANAGED_TAG_KEY = os.getenv("MANAGED_TAG_KEY", "ManagedBy")
    MANAGED_TAG_VALUE = os.getenv("MANAGED_TAG_VALUE", "ManageEIPs")

    # If an EIP has this protection tag/value, it will never be released.
    PROTECT_TAG_KEY = os.getenv("PROTECT_TAG_KEY", "Protection")
    PROTECT_TAG_VALUE = os.getenv("PROTECT_TAG_VALUE", "DoNotRelease")

    # Optional custom metrics via EMF (in addition to AWS/Lambda built-ins)
    METRICS_ENABLED = str(os.getenv("METRICS_ENABLED", "true")).lower() == "true"
    METRICS_NAMESPACE = os.getenv("METRICS_NAMESPACE", "Custom/FinOps")

    # Always log at least one line so CloudWatch proves what mode you ran in
    log(
        "INFO",
        "ManageEIPs start",
        dry_run=dry_run,
        function_name=context.function_name if context else None,
        request_id=context.aws_request_id if context else None,
        managed_tag_key=MANAGED_TAG_KEY,
        managed_tag_value=MANAGED_TAG_VALUE,
        protect_tag_key=PROTECT_TAG_KEY,
        protect_tag_value=PROTECT_TAG_VALUE,
        metrics_enabled=METRICS_ENABLED,
        metrics_namespace=METRICS_NAMESPACE
    )

    ec2_resource = boto3.resource('ec2')

    # Counters used for summary logging and optional custom metrics
    scanned = 0
    skipped_not_managed = 0
    skipped_protected = 0
    associated = 0
    would_release = 0
    released = 0
    per_eip_errors = 0
    
    for elastic_ip in ec2_resource.vpc_addresses.all():
        scanned += 1

        try:
            # Tags may be None if not set
            tags = getattr(elastic_ip, "tags", None) or []

            managed_val = _get_tag_value(tags, MANAGED_TAG_KEY)
            protect_val = _get_tag_value(tags, PROTECT_TAG_KEY)

            # Allow-list: only act on EIPs explicitly managed by this automation
            if managed_val != MANAGED_TAG_VALUE:
                skipped_not_managed += 1
                continue

            # Deny-list: never touch protected EIPs
            if protect_val == PROTECT_TAG_VALUE:
                skipped_protected += 1
                log(
                    "INFO",
                    "Skipping protected Elastic IP",
                    allocation_id=elastic_ip.allocation_id,
                    action="skip",
                    reason="protected",
                    dry_run=dry_run
                )
                continue

            # Associated EIPs are not released
            if elastic_ip.instance_id is not None:
                associated += 1
                continue

            # Unassociated and managed => release (or would release if dry-run)
            if dry_run:
                would_release += 1
                log(
                    "INFO",
                    "Elastic IP would be released (dry-run)",
                    allocation_id=elastic_ip.allocation_id,
                    action="release",
                    dry_run=True
                )
            else:
                log(
                    "INFO",
                    "Releasing unassociated Elastic IP",
                    allocation_id=elastic_ip.allocation_id,
                    action="release",
                    dry_run=False
                )
                elastic_ip.release()    
                released += 1
    
        except ClientError as e:
            per_eip_errors += 1

            # Access denied
            # Invalid state
            # Throttling
            # AWS-side validation errors
            code = e.response.get("Error", {}).get("Code", "Unknown")
            log(
                "ERROR",
                "AWS ClientError while processing Elastic IP",
                allocation_id=getattr(elastic_ip, "allocation_id", None),
                error_code=code,
                error_message=str(e)
            )

            # Fail fast on systemic auth/permission problems
            if code in {
                "AccessDenied",
                "AccessDeniedException",
                "UnauthorizedOperation",
                "UnrecognizedClientException",
                "InvalidClientTokenId",
                "SignatureDoesNotMatch",
                "ExpiredToken",
            }:
                raise

            # Otherwise treat as per-EIP failure and keep going
            continue

        except Exception as e:
            # For generic exceptions
            # Catches any unexpected Python/runtime errors:
            # - logic bugs
            # - NoneType errors
            # - formatting or attribute errors
            log(
                "ERROR",
                "Unexpected error while processing Elastic IP",
                allocation_id=getattr(elastic_ip, "allocation_id", None),
                error_type=type(e).__name__,
                error_message=str(e)
            )
            raise   # Re-raise to avoid hiding critical failures

    # Explicit end-of-execution marker:
    # This log confirms that the Lambda completed all processing
    # without timing out or raising an exception.
    # It is used later for log queries, metrics, and run validation.
    log(
        "INFO",
        "ManageEIPs completed",
        dry_run=dry_run,
        scanned=scanned,
        skipped_not_managed=skipped_not_managed,
        skipped_protected=skipped_protected,
        associated=associated,
        would_release=would_release,
        released=released,
        per_eip_errors=per_eip_errors
    )


    # Optional custom metrics (EMF)
    if METRICS_ENABLED:
        emit_emf_metrics(
            namespace=METRICS_NAMESPACE,
            dimensions={"FunctionName": (context.function_name if context else "ManageEIPs")},
            metrics={
                "EIPsScanned": scanned,
                "EIPsSkippedNotManaged": skipped_not_managed,
                "EIPsSkippedProtected": skipped_protected,
                "EIPsAssociated": associated,
                "EIPsWouldRelease": would_release,
                "EIPsReleased": released,
                "EIPsPerEipErrors": per_eip_errors,
            }
        )


    return {
        'statusCode': 200,
        'body': 'Processed elastic IPs.'
    }


# ------------------------------------------------------------
# Local test harness (runs only when executed locally, not in Lambda)
# ------------------------------------------------------------
if __name__ == "__main__":
    test_event = {"dry_run": True}
    print(lambda_handler(test_event, None))
