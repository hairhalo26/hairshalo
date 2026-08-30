from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_admin
from app import models, schemas, notifications as notify

router = APIRouter(prefix="/api/appointments", tags=["appointments"])


@router.post("", response_model=schemas.AppointmentOut, status_code=201)
def book_appointment(payload: schemas.AppointmentCreate, background: BackgroundTasks,
                     db: Session = Depends(get_db)):
    appt = models.Appointment(**payload.model_dump())
    db.add(appt)
    # The id is needed for the notification's event key, and the acknowledgement
    # is queued in this same transaction — see app/notifications.py.
    db.flush()
    notify.notify_appointment(db, appt, "appointment.booked")
    db.commit()
    db.refresh(appt)
    notify.schedule_dispatch(background)
    return appt


@router.get("", response_model=List[schemas.AppointmentOut])
def list_appointments(
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    return db.query(models.Appointment).order_by(models.Appointment.scheduled_at.asc()).all()


#: Appointment statuses that are worth an email. "Completed" is bookkeeping,
#: not news to the customer who was just in the chair.
APPOINTMENT_STATUS_EVENTS = {
    "Confirmed": "appointment.confirmed",
    "Cancelled": "appointment.cancelled",
}


@router.put("/{appointment_id}/status", response_model=schemas.AppointmentOut)
def update_appointment_status(
    appointment_id: str,
    payload: schemas.AppointmentStatusUpdate,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    _admin=Depends(get_current_admin),
):
    appt = db.query(models.Appointment).filter(models.Appointment.id == appointment_id).first()
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found")
    if payload.status not in [s.value for s in models.AppointmentStatus]:
        raise HTTPException(status_code=400, detail="Invalid status")
    appt.status = payload.status
    event_type = APPOINTMENT_STATUS_EVENTS.get(payload.status)
    if event_type:
        notify.notify_appointment(db, appt, event_type)
    db.commit()
    db.refresh(appt)
    notify.schedule_dispatch(background)
    return appt
