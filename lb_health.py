"""
RunPod load balancer probes PORT_HEALTH for GET /ping only.
Main API stays on PORT (see https://docs.runpod.io/serverless/load-balancing/overview).
"""
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(title="RunPod LB health")


@app.get("/ping")
async def ping():
    return JSONResponse({"status": "healthy"})
