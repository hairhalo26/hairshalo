from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_admin
from app import models, schemas

router = APIRouter(prefix="/api/appointments", tags=["appointments"])


@router.post("", response_model=schemas.AppointmentOut, status_code=201)
def book_appointment(payload: schemas.AppointmentCreate, db: Session = Depends(get_db)):
    appt = models.Appointment(**payload.model_dump())
    db.add(appt)
    db.commit()
    db.refresh(appt)
    return appt


@router.get("", response_model=List[schemas.AppointmentOut])
def list_appointments(
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    return db.query(models.Appointment).order_by(models.Appointment.scheduled_at.asc()).all()


@router.put("/{appointment_id}/status", response_model=schemas.AppointmentOut)
def update_appointment_status(
    appointment_id: str,
    payload: schemas.AppointmentStatusUpdate,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    appt = db.query(models.Appointment).filter(models.Appointment.id == appointment_id).first()
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found")
    if payload.status not in [s.value for s in models.AppointmentStatus]:
        raise HTTPException(status_code=400, detail="Invalid status")
    appt.status = payload.status
    db.commit()
    db.refresh(appt)
    return appt
