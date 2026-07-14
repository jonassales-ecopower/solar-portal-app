"""
WEG/SunWEG API Integration for FastAPI Backend

Provides endpoints for managing WEG accounts and monitoring solar plants.
"""

from fastapi import APIRouter, HTTPException, Body
from pydantic import BaseModel
import aiohttp
from weg_api import WEGClient, parse_numeric, WEGAuthError, WEGAPIError
import logging
import psycopg2
import os

_LOGGER = logging.getLogger(__name__)

# Request models
class WEGLoginRequest(BaseModel):
    email: str
    senha: str

DATABASE_URL = os.environ.get("DATABASE_URL", "")

def conectar_banco():
    """Connect to PostgreSQL database"""
    if DATABASE_URL:
        url = DATABASE_URL
        if "channel_binding=" in url:
            url = url.split("channel_binding=")[0].rstrip("&?")
        try:
            return psycopg2.connect(url)
        except psycopg2.Error as e:
            _LOGGER.error(f"Database connection error: {e}")
            raise
    else:
        raise RuntimeError("DATABASE_URL environment variable not set")

# Create a router for WEG endpoints
weg_router = APIRouter(prefix="/weg", tags=["weg"])


# ============================================================================
# 1. STORE WEG CREDENTIALS IN DATABASE
# ============================================================================
# Add these columns to your 'inversor_weg' or 'contas' table:
#
# ALTER TABLE contas ADD COLUMN (
#     weg_email VARCHAR(255),
#     weg_senha VARCHAR(255),
#     weg_token VARCHAR(1024),
#     weg_ultimo_sincronismo TIMESTAMP,
#     weg_ativo BOOLEAN DEFAULT FALSE
# );


# ============================================================================
# 2. AUTHENTICATE WITH WEG
# ============================================================================
@weg_router.post("/clientes/{cliente_id}/weg/login")
async def weg_login(cliente_id: int, request: WEGLoginRequest):
    """
    Authenticate a client with WEG and store the token.

    Args:
        cliente_id: Client ID in your system
        request: WEG login credentials (email, senha)

    Returns:
        Success status and list of available plants
    """
    conn = conectar_banco()
    cur = conn.cursor()

    try:
        # Create WEG client and authenticate
        async with aiohttp.ClientSession() as session:
            weg = WEGClient(session, email=request.email, password=request.senha)
            await weg.async_login()

            # Fetch available plants to verify access
            plants = await weg.async_get_all_plants()

            if not plants:
                raise HTTPException(status_code=400, detail="No plants found in WEG account")

            # Store token in database
            cur.execute("""
                UPDATE clientes
                SET weg_email = %s, weg_senha = %s, weg_token = %s, weg_ativo = TRUE
                WHERE id = %s
            """, (request.email, request.senha, weg.token, cliente_id))
            conn.commit()

            return {
                "sucesso": True,
                "mensagem": f"Autenticado com sucesso. {len(plants)} usina(s) encontrada(s).",
                "plantas": [
                    {
                        "id": str(p.get("id")),
                        "nome": p.get("nome"),
                        "capacidade": p.get("capacidade"),
                        "energiaDia": p.get("energiadia"),
                    }
                    for p in plants
                ]
            }

    except WEGAuthError as e:
        raise HTTPException(status_code=401, detail=f"Autenticação WEG falhou: {str(e)}")
    except WEGAPIError as e:
        raise HTTPException(status_code=500, detail=f"Erro na API WEG: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro desconhecido: {str(e)}")
    finally:
        cur.close()
        conn.close()


# ============================================================================
# 3. VALIDATE TOKEN
# ============================================================================
@weg_router.post("/clientes/{cliente_id}/weg/validar-token")
async def weg_validate_token(cliente_id: int):
    """
    Validate stored WEG token and refresh if necessary.

    Returns:
        Status of token validation
    """
    conn = conectar_banco()
    cur = conn.cursor()

    try:
        # Get stored token
        cur.execute("SELECT weg_token FROM contas WHERE id = %s", (cliente_id,))
        result = cur.fetchone()

        if not result or not result[0]:
            raise HTTPException(status_code=404, detail="WEG token not found")

        token = result[0]

        # Validate token
        async with aiohttp.ClientSession() as session:
            weg = WEGClient(session, token=token)
            is_valid = await weg.async_validate_token()

            if is_valid:
                return {"sucesso": True, "valido": True}
            else:
                # Mark as invalid
                cur.execute("UPDATE clientes SET weg_ativo = FALSE WHERE id = %s", (cliente_id,))
                conn.commit()
                raise HTTPException(status_code=401, detail="Token expirado")

    except WEGAuthError as e:
        raise HTTPException(status_code=401, detail=f"Token inválido: {str(e)}")
    except WEGAPIError as e:
        raise HTTPException(status_code=500, detail=f"Erro na API WEG: {str(e)}")
    finally:
        cur.close()
        conn.close()


# ============================================================================
# 4. GET REAL-TIME PLANT DATA
# ============================================================================
@weg_router.get("/clientes/{cliente_id}/weg/planta/{planta_id}")
async def weg_get_plant_data(cliente_id: int, planta_id: str):
    """
    Fetch real-time data for a specific WEG plant.

    Returns:
        Current energy generation, power output, and yield metrics
    """
    conn = conectar_banco()
    cur = conn.cursor()

    try:
        # Get stored token
        cur.execute("SELECT weg_token FROM clientes WHERE id = %s AND weg_ativo = TRUE", (cliente_id,))
        result = cur.fetchone()

        if not result:
            raise HTTPException(status_code=404, detail="WEG token not configured")

        token = result[0]

        # Fetch plant data
        async with aiohttp.ClientSession() as session:
            weg = WEGClient(session, token=token)
            planta = await weg.async_get_plant_summary(planta_id)

            if not planta:
                raise HTTPException(status_code=404, detail="Plant not found")

            return {
                "sucesso": True,
                "planta": {
                    "id": planta.get("id"),
                    "nome": planta.get("nome"),
                    "energiaDia": {
                        "valor": parse_numeric(planta.get("energiadia")),
                        "unidade": "kWh",
                        "raw": planta.get("energiadia")
                    },
                    "energiaMes": {
                        "valor": parse_numeric(planta.get("energia_mes")),
                        "unidade": "kWh",
                        "raw": planta.get("energia_mes")
                    },
                    "potencia": {
                        "valor": parse_numeric(planta.get("potencia"), {"W": 0.001, "kW": 1.0, "MW": 1000.0}),
                        "unidade": "kW",
                        "raw": planta.get("potencia")
                    },
                    "capacidade": {
                        "valor": parse_numeric(planta.get("capacidade")),
                        "unidade": "kW",
                        "raw": planta.get("capacidade")
                    },
                    "yieldDia": planta.get("yield_dia"),
                    "yieldMes": planta.get("yield_mes"),
                }
            }

    except WEGAuthError as e:
        raise HTTPException(status_code=401, detail=f"Token inválido: {str(e)}")
    except WEGAPIError as e:
        raise HTTPException(status_code=500, detail=f"Erro na API WEG: {str(e)}")
    finally:
        cur.close()
        conn.close()


# ============================================================================
# 5. GET AGGREGATED TOTALS (ALL PLANTS)
# ============================================================================
@weg_router.get("/clientes/{cliente_id}/weg/totalizadores")
async def weg_get_totals(cliente_id: int):
    """
    Fetch aggregated totals across all WEG plants for this client.

    Returns:
        Total energy generation, power, carbon reduction, financial savings
    """
    conn = conectar_banco()
    cur = conn.cursor()

    try:
        # Get stored token
        cur.execute("SELECT weg_token FROM clientes WHERE id = %s AND weg_ativo = TRUE", (cliente_id,))
        result = cur.fetchone()

        if not result:
            raise HTTPException(status_code=404, detail="WEG token not configured")

        token = result[0]

        # Fetch totals
        async with aiohttp.ClientSession() as session:
            weg = WEGClient(session, token=token)
            totals = await weg.async_get_totalizadores()

            if not totals:
                raise HTTPException(status_code=500, detail="Could not fetch totals")

            return {
                "sucesso": True,
                "totalizadores": {
                    "energiaHoje": {
                        "valor": parse_numeric(totals.get("energia_gerada_hoje"), {"MWh": 1000.0, "kWh": 1.0}),
                        "unidade": "kWh",
                        "raw": totals.get("energia_gerada_hoje")
                    },
                    "energiaMes": {
                        "valor": parse_numeric(totals.get("energia_gerada_mes"), {"MWh": 1000.0, "kWh": 1.0}),
                        "unidade": "kWh",
                        "raw": totals.get("energia_gerada_mes")
                    },
                    "energiaTotal": {
                        "valor": parse_numeric(totals.get("energia_gerada_total"), {"MWh": 1000.0, "kWh": 1.0}),
                        "unidade": "kWh",
                        "raw": totals.get("energia_gerada_total")
                    },
                    "potenciaAtiva": {
                        "valor": parse_numeric(totals.get("potencia_ativa_total"), {"W": 0.001, "kW": 1.0, "MW": 1000.0}),
                        "unidade": "kW",
                        "raw": totals.get("potencia_ativa_total")
                    },
                    "capacidade": {
                        "valor": parse_numeric(totals.get("capacidade_usinas")),
                        "unidade": "kW",
                        "raw": totals.get("capacidade_usinas")
                    },
                    "arvoresPlantadas": parse_numeric(totals.get("arvores_plantadas")),
                    "kmEletrico": parse_numeric(totals.get("km_rodado_eletrico")),
                    "reduzcarbono": {
                        "valor": parse_numeric(totals.get("reduz_carbono_total")),
                        "unidade": "t",
                        "raw": totals.get("reduz_carbono_total")
                    },
                    "economiaHoje": {
                        "valor": parse_numeric(totals.get("total_economizado_hoje")),
                        "unidade": "R$",
                        "raw": totals.get("total_economizado_hoje")
                    },
                    "economiaTotal": {
                        "valor": parse_numeric(totals.get("total_economizado_acumulado")),
                        "unidade": "R$",
                        "raw": totals.get("total_economizado_acumulado")
                    },
                    "quantidadeUsinas": parse_numeric(totals.get("quantidade_usinas")),
                }
            }

    except WEGAuthError as e:
        raise HTTPException(status_code=401, detail=f"Token inválido: {str(e)}")
    except WEGAPIError as e:
        raise HTTPException(status_code=500, detail=f"Erro na API WEG: {str(e)}")
    finally:
        cur.close()
        conn.close()


# ============================================================================
# 6. LIST ALL PLANTS
# ============================================================================
@weg_router.get("/clientes/{cliente_id}/weg/plantas")
async def weg_list_plants(cliente_id: int):
    """
    List all plants accessible through WEG account.

    Returns:
        List of all plants with basic metrics
    """
    conn = conectar_banco()
    cur = conn.cursor()

    try:
        # Get stored token
        cur.execute("SELECT weg_token FROM clientes WHERE id = %s AND weg_ativo = TRUE", (cliente_id,))
        result = cur.fetchone()

        if not result:
            raise HTTPException(status_code=404, detail="WEG token not configured")

        token = result[0]

        # Fetch plants
        async with aiohttp.ClientSession() as session:
            weg = WEGClient(session, token=token)
            plants = await weg.async_get_all_plants()

            return {
                "sucesso": True,
                "plantas": [
                    {
                        "id": str(p.get("id")),
                        "nome": p.get("nome"),
                        "numerUC": p.get("numero_uc"),
                        "distribuidora": p.get("distribuidora"),
                        "capacidade": parse_numeric(p.get("capacidade")),
                        "energiaDia": parse_numeric(p.get("energiadia")),
                        "energiaMes": parse_numeric(p.get("energia_mes")),
                        "potencia": parse_numeric(p.get("potencia"), {"W": 0.001, "kW": 1.0, "MW": 1000.0}),
                        "yieldDia": parse_numeric(p.get("yield_dia")),
                        "yieldMes": parse_numeric(p.get("yield_mes")),
                    }
                    for p in plants
                ],
                "total": len(plants)
            }

    except WEGAuthError as e:
        raise HTTPException(status_code=401, detail=f"Token inválido: {str(e)}")
    except WEGAPIError as e:
        raise HTTPException(status_code=500, detail=f"Erro na API WEG: {str(e)}")
    finally:
        cur.close()
        conn.close()


# ============================================================================
# 7. DISCONNECT WEG ACCOUNT
# ============================================================================
@weg_router.delete("/clientes/{cliente_id}/weg/desconectar")
async def weg_disconnect(cliente_id: int):
    """
    Disconnect WEG account from client profile.

    Removes stored credentials and token.
    """
    conn = conectar_banco()
    cur = conn.cursor()

    try:
        cur.execute("""
            UPDATE clientes
            SET weg_email = NULL, weg_senha = NULL, weg_token = NULL, weg_ativo = FALSE
            WHERE id = %s
        """, (cliente_id,))
        conn.commit()

        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Client not found")

        return {"sucesso": True, "mensagem": "Conta WEG desconectada com sucesso"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao desconectar: {str(e)}")
    finally:
        cur.close()
        conn.close()


# ============================================================================
# HOW TO ADD TO YOUR API
# ============================================================================
# In your main FastAPI app file (api.py), add:
#
# from weg_integration_example import weg_router
#
# app = FastAPI()
# app.include_router(weg_router)
#
# This will expose the endpoints:
# - POST /weg/clientes/{cliente_id}/weg/login
# - POST /weg/clientes/{cliente_id}/weg/validar-token
# - GET  /weg/clientes/{cliente_id}/weg/planta/{planta_id}
# - GET  /weg/clientes/{cliente_id}/weg/totalizadores
# - GET  /weg/clientes/{cliente_id}/weg/plantas
# - DELETE /weg/clientes/{cliente_id}/weg/desconectar
