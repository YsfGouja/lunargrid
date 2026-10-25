"""
Shared FastAPI dependencies.

get_current_user  -> any logged-in user (401 if no/invalid token)
require_roles(..) -> logged-in AND role is one of the given names (403 otherwise)

Assumes your token/password helper file is named security.py. If it has another
name, change the import below and in auth_routes.py.
"""
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models
from database import SessionLocal
from security import verify_access_token

# Role names are hard-coded in permission checks, so the set of roles is fixed.
ROLE_ADMIN = "admin"      # everything, including users and deleting sites
ROLE_PLANNER = "planner"  # create / edit / delete planning data
ROLE_VIEWER = "viewer"    # read-only
ALL_ROLES = (ROLE_ADMIN, ROLE_PLANNER, ROLE_VIEWER)

# tokenUrl is what Swagger's "Authorize" button posts the username/password to.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_or_404(db: Session, model, item_id: int):
    obj = db.get(model, item_id)
    if obj is None:
        raise HTTPException(404, f"{model.__name__} {item_id} not found")
    return obj


def commit(db: Session):
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "Constraint violated (duplicate or invalid reference)") from exc


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> models.User:
    credentials_error = HTTPException(
        status_code=401,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    subject = verify_access_token(token)
    if subject is None:
        raise credentials_error
    try:
        user_id = int(subject)
    except (TypeError, ValueError):
        raise credentials_error

    # Loaded fresh from the DB on every request, so a role change or a deleted
    # user takes effect immediately, even though the token is still valid.
    user = db.get(models.User, user_id)
    if user is None:
        raise credentials_error
    return user


def require_roles(*allowed: str):
    def checker(user: models.User = Depends(get_current_user)) -> models.User:
        if user.role.name not in allowed:
            raise HTTPException(403, f"Requires one of these roles: {', '.join(allowed)}")
        return user

    return checker


require_admin = require_roles(ROLE_ADMIN)
require_planner = require_roles(ROLE_ADMIN, ROLE_PLANNER)  # planner or admin
