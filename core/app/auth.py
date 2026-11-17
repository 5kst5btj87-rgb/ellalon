from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings
from .db import SessionLocal, User

ROLE_RANK = {"viewer": 1, "operator": 2, "admin": 3}
bearer = HTTPBearer(auto_error=False)


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw: str, hashed: str) -> bool:
    return bcrypt.checkpw(pw.encode(), hashed.encode())


def issue_token(sub: str, role: str, ttl: timedelta | None = None) -> str:
    exp = datetime.now(timezone.utc) + (ttl or timedelta(minutes=settings.jwt_ttl_minutes))
    return jwt.encode({"sub": sub, "role": role, "exp": exp}, settings.jwt_secret, algorithm="HS256")


def module_token(name: str) -> str:
    return issue_token(f"module:{name}", "module", timedelta(days=settings.module_token_ttl_days))


def decode(token: str) -> dict:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token")


def current_principal(request: Request, creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> dict:
    token = creds.credentials if creds else request.cookies.get("ellalon_token", "")
    if not token:   # the cookie lets <video> playback links authenticate
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing token")
    return decode(token)


def require(min_role: str = "viewer", allow_module: bool = False):
    """Dependency: user with at least `min_role`, or (optionally) a module service token."""
    def dep(p: dict = Depends(current_principal)) -> dict:
        if p.get("role") == "module":
            if allow_module:
                return p
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Modules not allowed here")
        if ROLE_RANK.get(p.get("role"), 0) < ROLE_RANK[min_role]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
        return p
    return dep


def ws_principal(token: str = Query(...)) -> dict:
    return decode(token)


def ensure_admin():
    with SessionLocal() as s:
        if not s.query(User).filter_by(username=settings.admin_user).first():
            s.add(User(username=settings.admin_user,
                       password_hash=hash_password(settings.admin_password), role="admin"))
            s.commit()
