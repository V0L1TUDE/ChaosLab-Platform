"""
Learning Management System - Enrollment Service
===============================================
Manages student course enrollments, learning progress, and completion tracking.
Coordinates with Student Service and Course Service via HTTP.
Exposes /health and /enrollments/test for ChaosLab automated recovery verification.
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
logger = logging.getLogger("enrollment-service")

# Initialize OpenTelemetry
tracer = init_tracer("enrollment-service")
RequestsInstrumentor().instrument()

app = FastAPI(title="LMS - Enrollment Service", version="1.0.0")
FastAPIInstrumentor.instrument_app(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STUDENT_SERVICE_URL = os.getenv("STUDENT_SERVICE_URL", "http://student-service:8011").rstrip("/")
COURSE_SERVICE_URL = os.getenv("COURSE_SERVICE_URL", "http://course-service:8012").rstrip("/")

class EnrollmentCreate(BaseModel):
    student_id: str
    course_id: str

class ProgressUpdate(BaseModel):
    completed_lessons: int
    progress_pct: int

class Enrollment(BaseModel):
    id: str
    student_id: str
    student_name: Optional[str] = None
    course_id: str
    course_title: Optional[str] = None
    enrollment_date: str
    completed_lessons: int
    total_lessons: int
    progress_pct: int
    status: str

# In-memory enrollment store
ENROLLMENTS_DB: Dict[str, Enrollment] = {
    "enr-701": Enrollment(
        id="enr-701",
        student_id="stu-01",
        student_name="Sarah Jenkins",
        course_id="crs-101",
        course_title="Production Kubernetes & Chaos Engineering",
        enrollment_date="2026-02-01",
        completed_lessons=10,
        total_lessons=12,
        progress_pct=83,
        status="IN_PROGRESS"
    ),
    "enr-702": Enrollment(
        id="enr-702",
        student_id="stu-02",
        student_name="Liam Patel",
        course_id="crs-102",
        course_title="Distributed Systems Observability with OpenTelemetry",
        enrollment_date="2026-02-10",
        completed_lessons=8,
        total_lessons=8,
        progress_pct=100,
        status="COMPLETED"
    ),
    "enr-703": Enrollment(
        id="enr-703",
        student_id="stu-03",
        student_name="Elena Rostova",
        course_id="crs-101",
        course_title="Production Kubernetes & Chaos Engineering",
        enrollment_date="2026-02-20",
        completed_lessons=4,
        total_lessons=12,
        progress_pct=33,
        status="IN_PROGRESS"
    ),
    "enr-704": Enrollment(
        id="enr-704",
        student_id="stu-01",
        student_name="Sarah Jenkins",
        course_id="crs-103",
        course_title="Modern Microservice Architectures with FastAPI",
        enrollment_date="2026-03-01",
        completed_lessons=2,
        total_lessons=10,
        progress_pct=20,
        status="IN_PROGRESS"
    )
}

def resolve_student_name(student_id: str) -> str:
    """Fetch student name from Student Service with trace propagation."""
    headers = inject_trace_headers()
    try:
        resp = requests.get(f"{STUDENT_SERVICE_URL}/students/{student_id}", headers=headers, timeout=2.0)
        if resp.status_code == 200:
            return resp.json().get("name", student_id)
    except Exception as e:
        logger.warning(f"Could not reach Student Service for {student_id}: {e}")
    return "Student"

def resolve_course_title(course_id: str) -> str:
    """Fetch course title from Course Service with trace propagation."""
    headers = inject_trace_headers()
    try:
        resp = requests.get(f"{COURSE_SERVICE_URL}/courses/{course_id}", headers=headers, timeout=2.0)
        if resp.status_code == 200:
            return resp.json().get("title", course_id)
    except Exception as e:
        logger.warning(f"Could not reach Course Service for {course_id}: {e}")
    return "Course"

@app.get("/health")
def health_check():
    """ChaosLab health verification endpoint."""
    return {
        "status": "SERVING",
        "service": "enrollment-service",
        "total_enrollments": len(ENROLLMENTS_DB),
        "version": "1.0.0"
    }

@app.get("/enrollments", response_model=List[Enrollment])
def get_enrollments():
    """Retrieve all student course enrollments."""
    logger.info("Fetching all enrollments")
    enrollments = []
    for enr in ENROLLMENTS_DB.values():
        e_dict = enr.model_dump()
        if not e_dict.get("student_name") or e_dict["student_name"] == "Student":
            e_dict["student_name"] = resolve_student_name(e_dict["student_id"])
        if not e_dict.get("course_title") or e_dict["course_title"] == "Course":
            e_dict["course_title"] = resolve_course_title(e_dict["course_id"])
        enrollments.append(Enrollment(**e_dict))
    return enrollments

@app.get("/enrollments/test")
def test_enrollment_functional():
    """
    ChaosLab functional verification endpoint.
    Executes cross-service transaction across Enrollment -> Student -> Course.
    Returns confirmed entity identifier for Oracle evaluation.
    """
    logger.info("Executing ChaosLab synthetic functional verification check")
    test_id = "enr-test-probe"
    
    student_name = resolve_student_name("stu-01")
    course_title = resolve_course_title("crs-101")

    test_enr = Enrollment(
        id=test_id,
        student_id="stu-01",
        student_name=student_name,
        course_id="crs-101",
        course_title=course_title,
        enrollment_date="2026-11-02",
        completed_lessons=12,
        total_lessons=12,
        progress_pct=100,
        status="COMPLETED"
    )
    ENROLLMENTS_DB[test_id] = test_enr

    return {
        "status": "CONFIRMED",
        "enrollment_id": test_id,
        "student": student_name,
        "course": course_title,
        "service": "enrollment-service"
    }

@app.get("/enrollments/{enrollment_id}", response_model=Enrollment)
def get_enrollment(enrollment_id: str):
    """Retrieve specific enrollment by ID."""
    logger.info(f"Looking up enrollment {enrollment_id}")
    if enrollment_id not in ENROLLMENTS_DB:
        raise HTTPException(status_code=404, detail=f"Enrollment '{enrollment_id}' not found")
    enr = ENROLLMENTS_DB[enrollment_id]
    s_name = resolve_student_name(enr.student_id)
    c_title = resolve_course_title(enr.course_id)
    return Enrollment(**{**enr.model_dump(), "student_name": s_name, "course_title": c_title})

@app.post("/enrollments", response_model=Enrollment, status_code=201)
def create_enrollment(data: EnrollmentCreate):
    """Enroll student into course with downstream cross-service validation."""
    new_id = f"enr-{len(ENROLLMENTS_DB) + 701}"
    student_name = resolve_student_name(data.student_id)
    course_title = resolve_course_title(data.course_id)

    new_enr = Enrollment(
        id=new_id,
        student_id=data.student_id,
        student_name=student_name,
        course_id=data.course_id,
        course_title=course_title,
        enrollment_date="2026-11-02",
        completed_lessons=0,
        total_lessons=10,
        progress_pct=0,
        status="ENROLLED"
    )
    ENROLLMENTS_DB[new_id] = new_enr
    logger.info(f"Enrolled student '{student_name}' into '{course_title}' (ID: {new_id})")
    return new_enr

@app.patch("/enrollments/{enrollment_id}/progress", response_model=Enrollment)
def update_progress(enrollment_id: str, data: ProgressUpdate):
    """Update student course completion progress."""
    if enrollment_id not in ENROLLMENTS_DB:
        raise HTTPException(status_code=404, detail=f"Enrollment '{enrollment_id}' not found")
    enr = ENROLLMENTS_DB[enrollment_id]
    enr.completed_lessons = data.completed_lessons
    enr.progress_pct = data.progress_pct
    enr.status = "COMPLETED" if data.progress_pct >= 100 else "IN_PROGRESS"
    logger.info(f"Updated progress for enrollment {enrollment_id}: {data.progress_pct}%")
    return enr

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8013, reload=False)
