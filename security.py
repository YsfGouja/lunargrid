from passlib.context import CryptContext
from jose import jwt,JWTError
from datetime import datetime,timedelta,timezone
import os

SECRET_KEY=os.getenv("SECRET_KEY")
ALGORITHM="HS256"
pwd_context=CryptContext(schemes=["bcrypt"],deprecated="auto")

def hash_password(password:str)->str:
    return pwd_context.hash(password)

def verify_password(plain_password:str,hashed_password:str)->bool:
    return pwd_context.verify(plain_password,hashed_password)


def create_access_token(user_id:int):
    payload= {
        "sub":str(user_id),
        "exp":datetime.now(timezone.utc)+timedelta(minutes=30)
              }
    token=jwt.encode(payload,SECRET_KEY,algorithm=ALGORITHM)
    return token

def verify_access_token(token:str):
    try:
        payload=jwt.decode(token,SECRET_KEY,algorithms=[ALGORITHM])
        user_id=payload.get("sub")
        if user_id is None:
            return None
        return user_id
    except JWTError as e:
        print("JWT ERROR:", e)
        return None
    
