from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app import models, schemas
from app.security import verify_password, create_access_token

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=schemas.Token)
def login(payload: schemas.LoginRequest, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == payload.email).first()
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    token = create_access_token({"sub": user.id, "role": user.role,
                                 "tv": int(user.token_version or 0)})
    return schemas.Token(access_token=token)


@router.post("/logout", response_model=schemas.AccountMessage)
def logout(current_user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    """End this staff member's sessions — every token issued before now.

    Tokens are stateless, so forgetting one in the browser leaves it usable by
    anyone who copied it until it expires (24h by default). Bumping the version
    makes the server refuse all of them immediately. It signs the person out
    everywhere, which is the safe reading of "sign out" for an admin account.
    """
    current_user.token_version = int(current_user.token_version or 0) + 1
    db.commit()
    return schemas.AccountMessage(message="Signed out.")


@router.get("/me", response_model=schemas.UserOut)
def get_me(current_user: models.User = Depends(get_current_user)):
    return current_user
