"""Custom resource: write a LiteLLM master key ("sk-" + 40 url-safe random characters) into an empty secret, once."""

import secrets

import boto3

sm = boto3.client("secretsmanager")


def handler(event, context):
    arn = event["ResourceProperties"]["SecretArn"]
    if event["RequestType"] in ("Create", "Update"):
        try:
            sm.get_secret_value(SecretId=arn)  # raises when the secret has no value yet
        except sm.exceptions.ResourceNotFoundException:
            sm.put_secret_value(SecretId=arn, SecretString="sk-" + secrets.token_urlsafe(30)[:40])
    return {"PhysicalResourceId": "master-key:" + arn.split(":")[-1]}
