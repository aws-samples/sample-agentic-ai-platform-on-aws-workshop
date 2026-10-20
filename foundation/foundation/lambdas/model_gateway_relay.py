"""Relay: the only door into the private model gateway from outside the VPC.

LiteLLM's load balancer is internal (the VPC has no internet gateway), so the backend Lambdas, the workshop CLI and the doctor,
which run outside the VPC, invoke this function (lambda:InvokeFunction) and it forwards exactly one HTTP request to
MODEL_GATEWAY_URL. A dumb pipe: it adds no credentials and stores nothing; the caller supplies the Authorization header
(master key or virtual key) and the Content-Type. Holding lambda:InvokeFunction is therefore the access control for the
proxy's admin API.

Event:    {"method": "POST", "path": "/key/info", "query": {"key": "..."} | null,
           "headers": {"Authorization": "Bearer sk-...", "Content-Type": "application/json"} | null,
           "body": "<string>" | null, "timeout": 30}
Response: {"status": 200, "headers": {"content-type": "application/json", ...}, "body": "<text>"}
HTTP 4xx/5xx come back the same way; transport failures (DNS, refused, timeout) raise so the invoker sees a FunctionError.
"""

import os
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = os.environ["MODEL_GATEWAY_URL"].rstrip("/")
MAX_TIMEOUT = 110  # below the function's 120 s, so a slow proxy yields an error payload instead of a Lambda timeout


def handler(event, context):
    # Only ever the configured host: the path is appended to it, leading slashes stripped so "//host/x" cannot escape.
    url = f"{BASE_URL}/{(event.get('path') or '').lstrip('/')}"
    if event.get("query"):
        url += "?" + urllib.parse.urlencode(event["query"], doseq=True)
    body = event.get("body")
    request = urllib.request.Request(
        url,
        data=body.encode("utf-8") if isinstance(body, str) else None,
        method=(event.get("method") or "GET").upper(),
        headers=event.get("headers") or {},
    )
    timeout = min(float(event.get("timeout") or 30), MAX_TIMEOUT)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return _reply(response.status, response.headers, response.read())
    except urllib.error.HTTPError as error:  # an answer from the proxy, not a transport failure
        return _reply(error.code, error.headers, error.read())


def _reply(status, headers, payload):
    return {
        "status": status,
        "headers": {name.lower(): value for name, value in headers.items()},
        "body": payload.decode("utf-8", "replace"),
    }
