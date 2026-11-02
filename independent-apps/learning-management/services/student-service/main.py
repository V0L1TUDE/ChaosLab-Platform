"""
Learning Management System - Student Service
============================================
Manages student profiles, cohorts, and learning identities.
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
logger = logging.getLogger("student-service")

# Initialize OpenTelemetry
tracer = init_tracer("student-service")

app = FastAPI(title="LMS - Student Service", version="1.0.0")
FastAPIInstrumentor.instrument_app(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class Student(BaseModel):
    id: str
    name: str
    email: str
    cohort: str
    enrolled_date: str
    avatar_color: str = "#10b981"

# In-memory student database
STUDENTS_DB: Dict[str, Student] = {
    "stu-01": Student(id="stu-01", name="Sarah Jenkins", email="sarah.jenkins@academy.edu", cohort="Cloud Architecture 2026", enrolled_date="2026-01-15", avatar_color="#10b981"),
    "stu-02": Student(id="stu-02", name="Liam Patel", email="liam.patel@academy.edu", cohort="Distributed Systems", enrolled_date="2026-02-01", avatar_color="#3b82f6"),
    "stu-03": Student(id="stu-03", name="Elena Rostova", email="elena.rostova@academy.edu", cohort="Site Reliability Engineering", enrolled_date="2026-02-15", avatar_color="#8b5cf6"),
    "stu-04": Student(id="stu-04", name="Marcus Vance", email="marcus.vance@academy.edu", cohort="Cloud Architecture 2026", enrolled_date="2026-03-01", avatar_color="#f59e0b")
}

@app.get("/health")
def health_check():
    """ChaosLab health verification endpoint."""
    return {
        "status": "SERVING",
        "service": "student-service",
        "total_students": len(STUDENTS_DB),
        "version": "1.0.0"
    }

@app.get("/students", response_model=List[Student])
def get_students():
    """Retrieve all enrolled students."""
    logger.info("Fetching all registered students")
    return list(STUDENTS_DB.values())

@app.get("/students/{student_id}", response_model=Student)
def get_student(student_id: str):
    """Retrieve student profile by ID."""
    logger.info(f"Looking up student {student_id}")
    if student_id not in STUDENTS_DB:
        raise HTTPException(status_code=404, detail=f"Student '{student_id}' not found")
    return STUDENTS_DB[student_id]

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8011, reload=False)
