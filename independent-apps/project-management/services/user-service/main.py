"""
Project Management Platform - User Service
==========================================
Manages team member profiles, roles, and identity.
Exposes /health for ChaosLab liveness/readiness verification.
"""

import logging
from typing import Dict, List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from telemetry_helper import init_tracer
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("user-service")

# Initialize OpenTelemetry
tracer = init_tracer("user-service")

app = FastAPI(title="Project Management - User Service", version="1.0.0")
FastAPIInstrumentor.instrument_app(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class User(BaseModel):
    id: str
    name: str
    email: str
    role: str
    department: str
    avatar_color: str = "#2563eb"

# In-memory user database
USERS_DB: Dict[str, User] = {
    "usr-1": User(id="usr-1", name="Alice Chen", email="alice.chen@saas.internal", role="Engineering Lead", department="Core Engineering", avatar_color="#3b82f6"),
    "usr-2": User(id="usr-2", name="Bob Martinez", email="bob.martinez@saas.internal", role="Backend Architect", department="Infrastructure", avatar_color="#10b981"),
    "usr-3": User(id="usr-3", name="Carol Davis", email="carol.davis@saas.internal", role="DevOps / SRE", department="Reliability", avatar_color="#8b5cf6"),
    "usr-4": User(id="usr-4", name="Danielle Kim", email="danielle.kim@saas.internal", role="Product Manager", department="Product Delivery", avatar_color="#f59e0b")
}

@app.get("/health")
def health_check():
    """ChaosLab health verification endpoint."""
    return {
        "status": "SERVING",
        "service": "user-service",
        "total_users": len(USERS_DB),
        "version": "1.0.0"
    }

@app.get("/users", response_model=List[User])
def get_users():
    """Retrieve all team members."""
    logger.info("Fetching all registered team members")
    return list(USERS_DB.values())

@app.get("/users/{user_id}", response_model=User)
def get_user(user_id: str):
    """Retrieve specific team member by ID."""
    logger.info(f"Looking up user profile for {user_id}")
    if user_id not in USERS_DB:
        raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")
    return USERS_DB[user_id]

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=False)
