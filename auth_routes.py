from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import or_
from sqlalchemy.orm import Session

import models
import schemas
from database import SessionLocal
from deps import (
    ALL_ROLES,
    ROLE_ADMIN,
    ROLE_VIEWER,
    commit,
    get_current_user,
    get_db,
    get_or_404,
    require_admin,
)
from security import create_access_token, hash_password, verify_password

auth_router = APIRouter(prefix="/auth", tags=["Auth"])
users_router = APIRouter(
    prefix="/users", tags=["Users"], dependencies=[Depends(require_admin)]
)
roles_router = APIRouter(
    prefix="/roles", tags=["Roles"], dependencies=[Depends(get_current_user)]
)

# Verified against when the email doesn't exist, so a wrong email and a wrong
# password take about the same time (stops attackers probing which emails exist).
_DUMMY_HASH = hash_password("not-a-real-password")


def seed_roles():
    """Create admin / planner / viewer if missing. Called on startup."""
    with SessionLocal() as db:
        existing = {r.name for r in db.query(models.Role).all()}
        for name in ALL_ROLES:
            if name not in existing:
                db.add(models.Role(name=name))
        db.commit()


def authenticate(db: Session, email: str, password: str) -> models.User | None:
    user = db.query(models.User).filter(models.User.email == email.lower()).first()
    password_ok = verify_password(password, user.hashed_password if user else _DUMMY_HASH)
    return user if (user and password_ok) else None


def token_for(user: models.User) -> schemas.TokenResponse:
    return schemas.TokenResponse(access_token=create_access_token(user.id))


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@auth_router.post("/register", response_model=schemas.UserResponse, status_code=201)
def register(payload: schemas.UserCreate, db: Session = Depends(get_db)):
    """
    Public sign-up. New accounts are VIEWERS. The very first account in an
    empty database becomes ADMIN (bootstrap). An admin promotes everyone else.
    """
    email = str(payload.email).lower()
    taken = (
        db.query(models.User)
        .filter(or_(models.User.email == email, models.User.username == payload.username))
        .first()
    )
    if taken:
        raise HTTPException(409, "Email or username already taken")

    is_first_user = db.query(models.User).count() == 0
    role_name = ROLE_ADMIN if is_first_user else ROLE_VIEWER
    role = db.query(models.Role).filter(models.Role.name == role_name).first()
    if role is None:
        raise HTTPException(500, "Roles are not seeded")

    user = models.User(
        username=payload.username,
        email=email,
        hashed_password=hash_password(payload.password),
        role_id=role.id,
    )
    db.add(user)
    commit(db)
    db.refresh(user)
    return user


@auth_router.post("/login", response_model=schemas.TokenResponse)
def login(payload: schemas.LoginRequest, db: Session = Depends(get_db)):
    """JSON login for your frontend."""
    user = authenticate(db, str(payload.email), payload.password)
    if user is None:
        raise HTTPException(
            401, "Incorrect email or password", headers={"WWW-Authenticate": "Bearer"}
        )
    return token_for(user)


@auth_router.post("/token", response_model=schemas.TokenResponse)
def login_form(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """Form login used by Swagger's Authorize button. Put your EMAIL in 'username'."""
    user = authenticate(db, form.username, form.password)
    if user is None:
        raise HTTPException(
            401, "Incorrect email or password", headers={"WWW-Authenticate": "Bearer"}
        )
    return token_for(user)


@auth_router.get("/me", response_model=schemas.UserResponse)
def me(user: models.User = Depends(get_current_user)):
    return user


# ---------------------------------------------------------------------------
# Users (admin only)
# ---------------------------------------------------------------------------

@users_router.get("", response_model=list[schemas.UserResponse])
def list_users(db: Session = Depends(get_db)):
    return db.query(models.User).order_by(models.User.id).all()


@users_router.get("/{user_id}", response_model=schemas.UserResponse)
def get_user(user_id: int, db: Session = Depends(get_db)):
    return get_or_404(db, models.User, user_id)


@users_router.patch("/{user_id}/role", response_model=schemas.UserResponse)
def change_role(
    user_id: int,
    payload: schemas.UserRoleUpdate,
    db: Session = Depends(get_db),
    admin: models.User = Depends(require_admin),
):
    if user_id == admin.id:
        raise HTTPException(400, "You can't change your own role")
    user = get_or_404(db, models.User, user_id)
    get_or_404(db, models.Role, payload.role_id)
    user.role_id = payload.role_id
    commit(db)
    db.refresh(user)
    return user


@users_router.delete("/{user_id}", status_code=204)
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin: models.User = Depends(require_admin),
):
    if user_id == admin.id:
        raise HTTPException(400, "You can't delete your own account")
    user = get_or_404(db, models.User, user_id)
    db.delete(user)
    commit(db)


# ---------------------------------------------------------------------------
# Roles (read-only: the three roles are fixed because permission checks use their names)
# ---------------------------------------------------------------------------

@roles_router.get("", response_model=list[schemas.RoleResponse])
def list_roles(db: Session = Depends(get_db)):
    return db.query(models.Role).order_by(models.Role.id).all()
