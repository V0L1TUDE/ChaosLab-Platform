"""
Learning Management System - Course Service
===========================================
Manages course catalog, instructional syllabi, and modular lessons.
Exposes /health for ChaosLab automated recovery verification.
"""

import logging
from typing import Dict, List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from telemetry_helper import init_tracer
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("course-service")

# Initialize OpenTelemetry
tracer = init_tracer("course-service")

app = FastAPI(title="LMS - Course Service", version="1.0.0")
FastAPIInstrumentor.instrument_app(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class Lesson(BaseModel):
    id: str
    title: str
    duration_mins: int
    completed: bool = False

class Course(BaseModel):
    id: str
    title: str
    instructor: str
    category: str
    level: str
    lessons_count: int
    duration_hours: int
    rating: float
    description: str

# In-memory course store
COURSES_DB: Dict[str, Course] = {
    "crs-101": Course(
        id="crs-101",
        title="Production Kubernetes & Chaos Engineering",
        instructor="Dr. Aris Thorne",
        category="Cloud Infrastructure",
        level="Advanced",
        lessons_count=12,
        duration_hours=18,
        rating=4.9,
        description="Master failure injection, PodChaos, NetworkChaos, and automated recovery oracles on Kubernetes."
    ),
    "crs-102": Course(
        id="crs-102",
        title="Distributed Systems Observability with OpenTelemetry",
        instructor="Maya Lin",
        category="DevOps & Reliability",
        level="Intermediate",
        lessons_count=8,
        duration_hours=10,
        rating=4.8,
        description="Instrument multi-language microservices with W3C trace context, Jaeger, and SLA metrics."
    ),
    "crs-103": Course(
        id="crs-103",
        title="Modern Microservice Architectures with FastAPI",
        instructor="Kenji Sato",
        category="Software Architecture",
        level="Intermediate",
        lessons_count=10,
        duration_hours=14,
        rating=4.7,
        description="Design event-driven, decoupled REST and gRPC microservices with async Python."
    )
}

COURSE_LESSONS_DB: Dict[str, List[Lesson]] = {
    "crs-101": [
        Lesson(id="les-101-1", title="Kubernetes Control Plane Failure Modes", duration_mins=45, completed=True),
        Lesson(id="les-101-2", title="Declarative Chaos Mesh Manifest Generation", duration_mins=55, completed=True),
        Lesson(id="les-101-3", title="Multi-Tier Verification Oracles", duration_mins=60, completed=False)
    ],
    "crs-102": [
        Lesson(id="les-102-1", title="Tracing Principles & Span Context Propagation", duration_mins=40, completed=True),
        Lesson(id="les-102-2", title="Deploying Jaeger All-In-One Collector", duration_mins=50, completed=True)
    ],
    "crs-103": [
        Lesson(id="les-103-1", title="FastAPI Async Coroutines & Routing", duration_mins=35, completed=True),
        Lesson(id="les-103-2", title="Cross-Service HTTP Dependency Injection", duration_mins=45, completed=False)
    ]
}

@app.get("/health")
def health_check():
    """ChaosLab health verification endpoint."""
    return {
        "status": "SERVING",
        "service": "course-service",
        "total_courses": len(COURSES_DB),
        "version": "1.0.0"
    }

@app.get("/courses", response_model=List[Course])
def get_courses():
    """Retrieve full course catalog."""
    logger.info("Fetching complete course catalog")
    return list(COURSES_DB.values())

@app.get("/courses/{course_id}", response_model=Course)
def get_course(course_id: str):
    """Retrieve course details by ID."""
    logger.info(f"Looking up course {course_id}")
    if course_id not in COURSES_DB:
        raise HTTPException(status_code=404, detail=f"Course '{course_id}' not found")
    return COURSES_DB[course_id]

@app.get("/courses/{course_id}/lessons", response_model=List[Lesson])
def get_course_lessons(course_id: str):
    """Retrieve syllabus lessons for a course."""
    if course_id not in COURSES_DB:
        raise HTTPException(status_code=404, detail=f"Course '{course_id}' not found")
    return COURSE_LESSONS_DB.get(course_id, [])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8012, reload=False)
