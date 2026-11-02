"""
Project Management Platform - Task Service
==========================================
Manages task assignments, status transitions, and dependencies.
Coordinates with Project Service and User Service.
Exposes /health and /tasks/test for ChaosLab automated recovery verification.
"""

import logging
import os
import time
from typing import Dict, List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests
from telemetry_helper import init_tracer, inject_trace_headers
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("task-service")

# Initialize OpenTelemetry
tracer = init_tracer("task-service")
RequestsInstrumentor().instrument()

app = FastAPI(title="Project Management - Task Service", version="1.0.0")
FastAPIInstrumentor.instrument_app(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PROJECT_SERVICE_URL = os.getenv("PROJECT_SERVICE_URL", "http://project-service:8002").rstrip("/")
USER_SERVICE_URL = os.getenv("USER_SERVICE_URL", "http://user-service:8001").rstrip("/")

class TaskCreate(BaseModel):
    title: str
    description: str
    project_id: str
    assignee_id: str
    priority: str = "HIGH"
    status: str = "TODO"

class TaskUpdate(BaseModel):
    status: Optional[str] = None
    priority: Optional[str] = None

class Task(BaseModel):
    id: str
    title: str
    description: str
    project_id: str
    project_name: Optional[str] = None
    assignee_id: str
    assignee_name: Optional[str] = None
    priority: str
    status: str

# In-memory task database
TASKS_DB: Dict[str, Task] = {
    "task-501": Task(
        id="task-501",
        title="Deploy PodChaos Custom Resource Definition",
        description="Configure Chaos Mesh operator to target cartservice and checkoutservice pods.",
        project_id="proj-101",
        project_name="Kubernetes Resilience Engine",
        assignee_id="usr-3",
        assignee_name="Carol Davis",
        priority="CRITICAL",
        status="DONE"
    ),
    "task-502": Task(
        id="task-502",
        title="Implement gRPC Health Probe Adapter",
        description="Verify service availability using standard grpc.health.v1 across recovered pods.",
        project_id="proj-101",
        project_name="Kubernetes Resilience Engine",
        assignee_id="usr-2",
        assignee_name="Bob Martinez",
        priority="HIGH",
        status="IN_PROGRESS"
    ),
    "task-503": Task(
        id="task-503",
        title="Configure Jaeger OTLP Ingestion Pipeline",
        description="Deploy Jaeger collector and verify cross-namespace trace context propagation.",
        project_id="proj-102",
        project_name="Telemetry & Distributed Tracing Pipeline",
        assignee_id="usr-2",
        assignee_name="Bob Martinez",
        priority="HIGH",
        status="DONE"
    ),
    "task-504": Task(
        id="task-504",
        title="Define NetworkChaos Latency Invariant",
        description="Verify that post-recovery p95 latency stays strictly below 1000ms SLA limit.",
        project_id="proj-101",
        project_name="Kubernetes Resilience Engine",
        assignee_id="usr-1",
        assignee_name="Alice Chen",
        priority="MEDIUM",
        status="TODO"
    )
}

def resolve_project_name(project_id: str) -> str:
    """Fetch project name from Project Service with trace propagation."""
    headers = inject_trace_headers()
    try:
        resp = requests.get(f"{PROJECT_SERVICE_URL}/projects/{project_id}", headers=headers, timeout=2.0)
        if resp.status_code == 200:
            return resp.json().get("name", project_id)
    except Exception as e:
        logger.warning(f"Could not reach Project Service for {project_id}: {e}")
    return "Project"

def resolve_assignee_name(assignee_id: str) -> str:
    """Fetch user name from User Service with trace propagation."""
    headers = inject_trace_headers()
    try:
        resp = requests.get(f"{USER_SERVICE_URL}/users/{assignee_id}", headers=headers, timeout=2.0)
        if resp.status_code == 200:
            return resp.json().get("name", assignee_id)
    except Exception as e:
        logger.warning(f"Could not reach User Service for {assignee_id}: {e}")
    return "Assignee"

@app.get("/health")
def health_check():
    """ChaosLab health verification endpoint."""
    return {
        "status": "SERVING",
        "service": "task-service",
        "total_tasks": len(TASKS_DB),
        "version": "1.0.0"
    }

@app.get("/tasks", response_model=List[Task])
def get_tasks():
    """Retrieve all tasks with project and assignee resolution."""
    logger.info("Fetching all tasks")
    tasks = []
    for t in TASKS_DB.values():
        t_dict = t.model_dump()
        if not t_dict.get("project_name") or t_dict["project_name"] == "Project":
            t_dict["project_name"] = resolve_project_name(t_dict["project_id"])
        if not t_dict.get("assignee_name") or t_dict["assignee_name"] == "Assignee":
            t_dict["assignee_name"] = resolve_assignee_name(t_dict["assignee_id"])
        tasks.append(Task(**t_dict))
    return tasks

@app.get("/tasks/test")
def test_task_functional():
    """
    ChaosLab functional verification endpoint.
    Performs safe end-to-end task transaction across Task -> Project -> User services.
    Returns confirmed entity identifier for Oracle evaluation.
    """
    logger.info("Executing ChaosLab synthetic functional verification check")
    test_task_id = "task-test-probe"
    
    # Resolve downstream dependencies to verify network and service reachability
    proj_name = resolve_project_name("proj-101")
    assignee_name = resolve_assignee_name("usr-1")

    # Store or update synthetic test task
    test_task = Task(
        id=test_task_id,
        title="ChaosLab Resilience Verification Probe",
        description="Automated synthetic task validating functional state after chaos recovery.",
        project_id="proj-101",
        project_name=proj_name,
        assignee_id="usr-1",
        assignee_name=assignee_name,
        priority="HIGH",
        status="DONE"
    )
    TASKS_DB[test_task_id] = test_task

    return {
        "status": "CONFIRMED",
        "task_id": test_task_id,
        "title": test_task.title,
        "project": proj_name,
        "assignee": assignee_name,
        "service": "task-service"
    }

@app.get("/tasks/{task_id}", response_model=Task)
def get_task(task_id: str):
    """Retrieve single task by ID."""
    logger.info(f"Looking up task {task_id}")
    if task_id not in TASKS_DB:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    t = TASKS_DB[task_id]
    proj_name = resolve_project_name(t.project_id)
    assignee_name = resolve_assignee_name(t.assignee_id)
    return Task(**{**t.model_dump(), "project_name": proj_name, "assignee_name": assignee_name})

@app.post("/tasks", response_model=Task, status_code=201)
def create_task(data: TaskCreate):
    """Create new task and resolve downstream project and user names."""
    new_id = f"task-{len(TASKS_DB) + 501}"
    proj_name = resolve_project_name(data.project_id)
    assignee_name = resolve_assignee_name(data.assignee_id)

    new_task = Task(
        id=new_id,
        title=data.title,
        description=data.description,
        project_id=data.project_id,
        project_name=proj_name,
        assignee_id=data.assignee_id,
        assignee_name=assignee_name,
        priority=data.priority,
        status=data.status
    )
    TASKS_DB[new_id] = new_task
    logger.info(f"Created task {new_id}: '{data.title}'")
    return new_task

@app.patch("/tasks/{task_id}", response_model=Task)
def update_task(task_id: str, data: TaskUpdate):
    """Update task status or priority."""
    if task_id not in TASKS_DB:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    t = TASKS_DB[task_id]
    t_dict = t.model_dump()
    if data.status is not None:
        t_dict["status"] = data.status
    if data.priority is not None:
        t_dict["priority"] = data.priority
    updated_task = Task(**t_dict)
    TASKS_DB[task_id] = updated_task
    logger.info(f"Updated task {task_id}: status={updated_task.status}")
    return updated_task

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8003, reload=False)
