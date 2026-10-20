#!/usr/bin/env python3
"""Signal the deployer's CloudFormation custom resource from the CodeBuild post_build phase (standard library only).

Runs with the image's `python3` so it works even when the build failed before `uv` was installed. Reads the
environment CodeBuild received from the StartBuild Lambda (CFN_RESPONSE_URL, CFN_STACK_ID, CFN_REQUEST_ID,
CFN_LOGICAL_ID, CFN_PHYSICAL_ID, ACTION) plus CodeBuild's own CODEBUILD_BUILD_SUCCEEDING / CODEBUILD_BUILD_ID, and
on a successful deploy adds the platform URLs (SSM parameters read with the AWS CLI) and the doctor verdict
(/tmp/doctor.status written by the build phase) to the response data. Without CFN_RESPONSE_URL (a build started by hand)
it only prints what it would have sent.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request

OUTPUTS = {
    "UiUrl": "/agent-platform/ui/url",
    "ApiUrl": "/agent-platform/api/url",
    "ModelGatewayUrl": "/agent-platform/model-gateway/url",
    "UsersSecretArn": "/agent-platform/auth/users-secret-arn",
}
DOCTOR_STATUS_FILE = "/tmp/doctor.status"


def ssm_parameter(name: str) -> str:
    cmd = ["aws", "ssm", "get-parameter", "--name", name, "--query", "Parameter.Value", "--output", "text"]
    try:
        return subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=60).stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        if "ParameterNotFound" in str(detail):  # the landing zone (module 1) and the personas (module 2) come later
            return "not deployed yet: the learners create it (workshop apply 1 or 2)"
        return f"unavailable: {str(detail).strip()[:200]}"


def build_response() -> dict:
    ok = os.environ.get("CODEBUILD_BUILD_SUCCEEDING") == "1"
    action = os.environ.get("ACTION", "deploy")
    build_id = os.environ.get("CODEBUILD_BUILD_ID", "")
    log_group = os.environ.get("DEPLOYER_LOG_GROUP", "/aws/codebuild/<project>")
    data = {"Action": action, "BuildId": build_id}
    if ok and action != "destroy":
        for key, name in OUTPUTS.items():
            data[key] = ssm_parameter(name)
        try:
            with open(DOCTOR_STATUS_FILE, encoding="utf-8") as fh:
                data["Doctor"] = fh.read().strip() or "unknown"
        except OSError:
            data["Doctor"] = "not run"
    reason = f"CodeBuild {build_id} ({action}) {'succeeded' if ok else 'FAILED'}; log group {log_group}"
    return {
        "Status": "SUCCESS" if ok else "FAILED",
        "Reason": reason[:1000],
        "PhysicalResourceId": os.environ.get("CFN_PHYSICAL_ID") or "agent-platform-foundation",
        "StackId": os.environ.get("CFN_STACK_ID", ""),
        "RequestId": os.environ.get("CFN_REQUEST_ID", ""),
        "LogicalResourceId": os.environ.get("CFN_LOGICAL_ID", ""),
        "Data": data,
    }


def main() -> int:
    response = build_response()
    url = os.environ.get("CFN_RESPONSE_URL", "")
    print("CloudFormation response:", json.dumps({k: v for k, v in response.items() if k != "StackId"}))
    if not url:
        print("CFN_RESPONSE_URL is empty (build started by hand): nothing to signal")
        return 0
    body = json.dumps(response).encode()
    req = urllib.request.Request(url, data=body, method="PUT", headers={"Content-Type": ""})
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                print(f"signalled CloudFormation: {response['Status']} (HTTP {resp.status})")
                return 0
        except Exception as exc:  # noqa: BLE001 - any failure here leaves the stack waiting, so retry and report
            print(f"signal attempt {attempt} failed: {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
