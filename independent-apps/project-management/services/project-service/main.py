"""
Project Management Platform - Project Service
=============================================
Manages project lifecycle, progress tracking, and ownership.
Integrates with User Service for owner validation and team resolution.
Exposes /health for ChaosLab liveness/readiness verification.
"""

import logging
import os
from typing import Dict, List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests
from telemetry_helper import init_tracer, inject_trace_headers
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("project-service")

# Initialize OpenTelemetry
tracer = init_tracer("project-service")
RequestsInstrumentor().instrument()

app = FastAPI(title="Project Management - Project Service", version="1.0.0")
FastAPIInstrumentor.instrument_app(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

USER_SERVICE_URL = os.getenv("USER_SERVICE_URL", "http://user-service:8001").rstrip("/")

class ProjectCreate(BaseModel):
    name: str
    description: str
    owner_id: str
    category: str = "Cloud Infrastructure"
    status: str = "ACTIVE"
    progress: int = 25

class Project(BaseModel):
    id: str
    name: str
    description: str
    owner_id: str
    owner_name: Optional[str] = None
    category: str
    status: str
    progress: int

# In-memory project store
PROJECTS_DB: Dict[str, Project] = {
    "proj-101": Project(
        id="proj-101",
        name="Kubernetes Resilience Engine",
        description="Automated chaos testing and recovery verification platform for production clusters.",
        owner_id="usr-1",
        owner_name="Alice Chen",
        category="Reliability Engineering",
        status="ACTIVE",
        progress=85
    ),
    "proj-102": Project(
        id="proj-102",
        name="Telemetry & Distributed Tracing Pipeline",
        description="OpenTelemetry collector integration with Jaeger and metric aggregators.",
        owner_id="usr-2",
        owner_name="Bob Martinez",
        category="Observability",
        status="ACTIVE",
        progress=70
    ),
    "proj-103": Project(
        id="proj-103",
        name="Zero-Trust Service Mesh Migration",
        description="Implementing mTLS, strict RBAC policies, and SPIFFE workload identities across namespaces.",
        owner_id="usr-3",
        owner_name="Carol Davis",
        category="Security",
        status="PLANNING",
        progress=30
    )
}

def resolve_owner_name(owner_id: str) -> str:
    """Fetch user name from User Service with distributed trace propagation."""
    headers = inject_trace_headers()
    try:
        resp = requests.get(f"{USER_SERVICE_URL}/users/{owner_id}", headers=headers, timeout=2.0)
        if resp.status_code == 200:
            return resp.json().get("name", owner_id)
    except Exception as e:
        logger.warning(f"Could not reach User Service to resolve {owner_id}: {e}")
    return "Team Member"

@app.get("/health")
def health_check():
    """ChaosLab health verification endpoint."""
    return {
        "status": "SERVING",
        "service": "project-service",
        "total_projects": len(PROJECTS_DB),
        "version": "1.0.0"
    }

@app.get("/projects", response_model=List[Project])
def get_projects():
    """Retrieve all projects with owner resolution."""
    logger.info("Fetching all projects")
    projects = []
    for p in PROJECTS_DB.values():
        p_dict = p.model_dump()
        if not p_dict.get("owner_name") or p_dict["owner_name"] == "Team Member":
            p_dict["owner_name"] = resolve_owner_name(p_dict["owner_id"])
        projects.append(Project(**p_dict))
    return projects

@app.get("/projects/{project_id}", response_model=Project)
def get_project(project_id: str):
    """Retrieve single project by ID."""
    logger.info(f"Looking up project {project_id}")
    if project_id not in PROJECTS_DB:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    proj = PROJECTS_DB[project_id]
    owner_name = resolve_owner_name(proj.owner_id)
    return Project(**{**proj.model_dump(), "owner_name": owner_name})

@app.post("/projects", response_model=Project, status_code=201)
def create_project(data: ProjectCreate):
    """Create new project and resolve owner with User Service."""
    new_id = f"proj-{len(PROJECTS_DB) + 101}"
    owner_name = resolve_owner_name(data.owner_id)
    new_proj = Project(
        id=new_id,
        name=data.name,
        description=data.description,
        owner_id=data.owner_id,
        owner_name=owner_name,
        category=data.category,
        status=data.status,
        progress=data.progress
    )
    PROJECTS_DB[new_id] = new_proj
    logger.info(f"Created project {new_id}: '{data.name}'")
    return new_proj

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8002, reload=False)
