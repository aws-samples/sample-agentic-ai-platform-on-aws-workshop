"""Custom resource (Provider onEvent): start an ingestion job of the Managed Knowledge Base data source.

Runs on Create and Update (the DocsHash property changes whenever the documents change); nothing on Delete. If a job is
already running (ConflictException) the newest job is adopted. The isComplete handler (kb_ingestion_poll.py) waits for it.
"""

import boto3

agent = boto3.client("bedrock-agent")


def newest_job_id(kb_id, ds_id):
    jobs = agent.list_ingestion_jobs(
        knowledgeBaseId=kb_id,
        dataSourceId=ds_id,
        sortBy={"attribute": "STARTED_AT", "order": "DESCENDING"},
        maxResults=1,
    )["ingestionJobSummaries"]
    return jobs[0]["ingestionJobId"] if jobs else None


def handler(event, context):
    props = event["ResourceProperties"]
    kb_id, ds_id = props["KnowledgeBaseId"], props["DataSourceId"]
    physical_id = f"kb-ingestion:{kb_id}:{ds_id}"
    if event["RequestType"] == "Delete":
        return {"PhysicalResourceId": event.get("PhysicalResourceId", physical_id)}
    try:
        job_id = agent.start_ingestion_job(
            knowledgeBaseId=kb_id, dataSourceId=ds_id, description="agent-platform scenario documents"
        )["ingestionJob"]["ingestionJobId"]
    except agent.exceptions.ConflictException:
        job_id = newest_job_id(kb_id, ds_id)
        if not job_id:
            raise
    print("ingestion job", job_id)
    return {"PhysicalResourceId": physical_id, "Data": {"IngestionJobId": job_id}}
