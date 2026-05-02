from datetime import datetime, timedelta
from typing import Optional
from jose import JWTError, jwt
import hashlib
import hmac

# Configurações do token
SECRET_KEY = "solar-portal-secret-key-2026"
ALGORITHM = "HS256"
TOKEN_EXPIRA_HORAS = 24

def criptografar_senha(senha: str) -> str:
    return hashlib.sha256(senha.encode()).hexdigest()

def verificar_senha(senha: str, senha_hash: str) -> bool:
    return hashlib.sha256(senha.encode()).hexdigest() == senha_hash

def criar_token(dados: dict) -> str:
    dados_token = dados.copy()
    expira = datetime.utcnow() + timedelta(hours=TOKEN_EXPIRA_HORAS)
    dados_token.update({"exp": expira})
    return jwt.encode(dados_token, SECRET_KEY, algorithm=ALGORITHM)

def verificar_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None