"""An order's history, one row per step.

Written in the SAME transaction as the change it describes, so the timeline a
customer sees can never claim a status the order did not actually reach.
"""
from sqlalchemy.orm import Session

from app import models


def record(db: Session, order: models.Order, status: str, *,
           note: str = None, actor: str = None) -> models.OrderEvent:
    event = models.OrderEvent(order=order, status=status, note=note, actor=actor)
    db.add(event)
    return event
