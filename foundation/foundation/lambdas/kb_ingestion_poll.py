"""Custom resource (Provider isComplete): the ingestion job started by kb_ingestion_start.py is done when COMPLETE.

FAILED or STOPPED jobs fail the stack with the service's failure reasons so a broken document is visible at deploy time.
"""

import boto3

agent = boto3.client("bedrock-agent")


def handler(event, context):
    if event["RequestType"] == "Delete":
        return {"IsComplete": True}
    props = event["ResourceProperties"]
    job_id = (event.get("Data") or {}).get("IngestionJobId")
    job = agent.get_ingestion_job(
        knowledgeBaseId=props["KnowledgeBaseId"], dataSourceId=props["DataSourceId"], ingestionJobId=job_id
    )["ingestionJob"]
    status = job["status"]
    print("ingestion job", job_id, status, job.get("statistics"))
    if status == "COMPLETE":
        stats = job.get("statistics") or {}
        return {
            "IsComplete": True,
            "Data": {
                "IngestionJobId": job_id,
                "DocumentsScanned": str(stats.get("numberOfDocumentsScanned", 0)),
                "DocumentsIndexed": str(stats.get("numberOfNewDocumentsIndexed", 0)),
                "DocumentsFailed": str(stats.get("numberOfDocumentsFailed", 0)),
            },
        }
    if status in ("FAILED", "STOPPED"):
        raise RuntimeError(f"ingestion job {job_id} ended {status}: {job.get('failureReasons')}")
    return {"IsComplete": False}
