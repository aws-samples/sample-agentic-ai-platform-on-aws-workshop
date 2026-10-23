import boto3
import pytest
from botocore.stub import ANY, Stubber

from platform_sdk import identity

NAME = "litellm-hr-assistant-dev-claude-sonnet-5"
ARN = f"arn:aws:bedrock-agentcore:us-east-1:123456789012:token-vault/default/apikeycredentialprovider/{NAME}"
SECRET = {
    "secretArn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:bedrock-agentcore-identity!default/apikey/"
    + NAME
}


@pytest.fixture
def control():
    client = boto3.client("bedrock-agentcore-control", region_name="us-east-1")
    with Stubber(client) as stub:
        yield client, stub
        stub.assert_no_pending_responses()


def test_store_api_key_creates_with_tags(control):
    client, stub = control
    stub.add_response(
        "create_api_key_credential_provider",
        {"apiKeySecretArn": SECRET, "name": NAME, "credentialProviderArn": ARN},
        {
            "name": NAME,
            "apiKey": "sk-virtual",
            "tags": {
                "Project": "workshop-agent-platform",
                "ManagedBy": "platform",
                "ProjectId": "hr-assistant",
                "auto-delete": "no",
            },
        },
    )
    assert identity.store_api_key(NAME, "sk-virtual", tags={"ProjectId": "hr-assistant"}, client=client) == ARN


def test_store_api_key_conflict_falls_back_to_update(control):
    client, stub = control
    stub.add_client_error(
        "create_api_key_credential_provider",
        service_error_code="ConflictException",
        service_message=f"Credential provider {NAME} already exists",
        http_status_code=409,
        expected_params={"name": NAME, "apiKey": "sk-new", "tags": ANY},
    )
    stub.add_response(
        "update_api_key_credential_provider",
        {"apiKeySecretArn": SECRET, "name": NAME, "credentialProviderArn": ARN, "createdTime": 1, "lastUpdatedTime": 2},
        {"name": NAME, "apiKey": "sk-new"},
    )
    assert identity.store_api_key(NAME, "sk-new", client=client) == ARN


def test_store_api_key_other_errors_propagate(control):
    client, stub = control
    stub.add_client_error(
        "create_api_key_credential_provider",
        service_error_code="ValidationException",
        service_message="bad name",
        http_status_code=400,
    )
    with pytest.raises(Exception, match="bad name"):
        identity.store_api_key("bad name", "sk", client=client)


def test_get_provider_arn_found_and_missing(control):
    client, stub = control
    stub.add_response(
        "get_api_key_credential_provider",
        {"apiKeySecretArn": SECRET, "name": NAME, "credentialProviderArn": ARN, "createdTime": 1, "lastUpdatedTime": 2},
        {"name": NAME},
    )
    stub.add_client_error(
        "get_api_key_credential_provider",
        service_error_code="ResourceNotFoundException",
        service_message="nope",
        http_status_code=404,
    )
    assert identity.get_provider_arn(NAME, client=client) == ARN
    assert identity.get_provider_arn("missing", client=client) is None


def test_delete_and_list(control):
    client, stub = control
    stub.add_response("delete_api_key_credential_provider", {}, {"name": NAME})
    stub.add_client_error(
        "delete_api_key_credential_provider",
        service_error_code="ResourceNotFoundException",
        service_message="nope",
        http_status_code=404,
    )
    stub.add_response(
        "list_api_key_credential_providers",
        {"credentialProviders": [{"name": NAME, "credentialProviderArn": ARN, "createdTime": 1, "lastUpdatedTime": 2}]},
        {},
    )
    assert identity.delete_provider(NAME, client=client) is True
    assert identity.delete_provider("missing", client=client) is False
    assert identity.list_providers(client=client) == [{"name": NAME, "arn": ARN}]


def test_provider_arn_format():
    assert identity.provider_arn("us-east-1", "123456789012", NAME) == ARN
