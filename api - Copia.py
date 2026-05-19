import hashlib
import time
import json
import re
import tempfile
import os
import secrets
import psycopg2
from datetime import datetime
from fastapi import Depends, FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from openai import OpenAI
from auth import criptografar_senha, verificar_senha, criar_token, verificar_token
import PyPDF2

app = FastAPI(title="Solar Portal API")

# Permitir acesso do frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*", "http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)

# Configurações
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "database": "solar_portal",
    "user": "postgres",
    "password": "991Bog31**"
}

OPENROUTER_KEY = "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"

# Segurança
security = HTTPBearer()

def obter_integrador_atual(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    payload = verificar_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Token inválido ou expirado")
    return payload

def conectar_banco():
    return psycopg2.connect(**DB_CONFIG)

def extrair_texto_pdf(caminho_pdf):
    texto = ""
    with open(caminho_pdf, "rb") as f:
        leitor = PyPDF2.PdfReader(f)
        for pagina in leitor.pages:
            texto += pagina.extract_text()
    return texto

def analisar_conta(texto_pdf):
    cliente = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=OPENROUTER_KEY
    )

    prompt = f"""Você é um especialista em contas de energia elétrica brasileiras com foco em Geração Distribuída (GD).

Analise o texto extraído da conta e retorne SOMENTE um JSON válido, sem texto adicional, sem explicações, sem markdown.

ATENÇÃO — regras importantes antes de extrair:

1. NOME DO CLIENTE: Nome da pessoa titular. Ignore prefixos de localidade como "B JARDIM".
2. MÊS DE REFERÊNCIA: Formato "Abril / 2026". NÃO confundir com referência dos indicadores de qualidade.
3. DATA DE VENCIMENTO: Campo "VENCIMENTO" em destaque na conta. NÃO confundir com data de apresentação.
4. CONSUMO BRUTO (kWh): É a soma de todos os itens de consumo antes dos descontos da GD. Some "Consumo acima de 80kWh-BR" + "Consumo até 80kWh-BR". Nesta conta seria 1.417 + 80 = 1.497 kWh.
5. CONSUMO FATURADO (kWh): É o valor do consumo real cobrado após descontos da GD. Procure no histórico dos últimos 13 meses o valor correspondente ao mês atual (ex: ABR/26 = 420,63 kWh). NÃO confundir com "Consumo até 80kWh-BR" que é apenas uma faixa tarifária. NÃO usar a leitura bruta do medidor. O consumo faturado em sistemas GD é calculado assim: (Leitura atual - Leitura anterior) x Constante - Energia injetada.
6. SALDO DE CRÉDITOS (kWh): Campo "Saldo Acumulado". Se zero, retornar 0.

REGRA ESPECIAL — LEITURA POR MÉDIA:
Se houver "FATURAMENTO PELA MÉDIA", "MÉDIA/MÍNIMO" ou "LEITURA INFORMADA PELO CLIENTE", a conta pode ter acúmulo de meses.

Retorne EXATAMENTE neste formato JSON:
{{
  "nome_cliente": "",
  "numero_uc": "",
  "distribuidora": "",
  "mes_referencia": "",
  "data_vencimento": "DD/MM/AAAA",
  "consumo_bruto_kwh": 0.0,
  "consumo_kwh": 0.0,
  "energia_injetada_kwh": 0.0,
  "saldo_acumulado_kwh": 0.0,
  "valor_fatura": 0.0,
  "modalidade_tarifaria": "",
  "tipo_gd": "",
  "leitura_por_media": false,
  "meses_acumulados": 1,
  "status_sistema": "SUPERAVITÁRIO ou DEFICITÁRIO",
  "percentual_gerado": 0.0,
  "mensagem_cliente": ""
}}

Regras para preencher status_sistema, percentual_gerado e mensagem_cliente:
- status_sistema: Compare energia_injetada_kwh com consumo_kwh (FATURADO). Se energia_injetada_kwh >= consumo_kwh então "SUPERAVITÁRIO", senão "DEFICITÁRIO". NUNCA compare com consumo_bruto_kwh.
- percentual_gerado: (energia_injetada_kwh / consumo_kwh) x 100. NUNCA usar consumo_bruto_kwh.
- mensagem_cliente: Explique em linguagem simples e amigável o que aconteceu nesta conta. Mencione se houve leitura acumulada de meses, se o sistema está gerando bem e se há algum ponto de atenção. Máximo 3 linhas. NÃO copie textos técnicos da conta.

Texto da conta:
{texto_pdf}"""

    resposta = cliente.chat.completions.create(
        model="openrouter/auto",
        messages=[{"role": "user", "content": prompt}]
    )
    return resposta.choices[0].message.content

def salvar_no_banco(dados, cliente_id):
    conn = conectar_banco()
    cur = conn.cursor()

    data_venc = None
    try:
        data_venc = datetime.strptime(dados["data_vencimento"], "%d/%m/%Y").date()
    except:
        pass

    cur.execute("""
        INSERT INTO contas (
            cliente_id, mes_referencia, data_vencimento,
            consumo_bruto_kwh, consumo_kwh, energia_injetada_kwh, saldo_acumulado_kwh,
            valor_fatura, modalidade_tarifaria, status_sistema,
            percentual_gerado, leitura_por_media, meses_acumulados,
            mensagem_cliente
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (
        cliente_id,
        dados.get("mes_referencia"),
        data_venc,
        dados.get("consumo_bruto_kwh"),
        dados.get("consumo_kwh"),
        dados.get("energia_injetada_kwh"),
        dados.get("saldo_acumulado_kwh"),
        dados.get("valor_fatura"),
        dados.get("modalidade_tarifaria"),
        dados.get("status_sistema"),
        dados.get("percentual_gerado"),
        dados.get("leitura_por_media", False),
        dados.get("meses_acumulados", 1),
        dados.get("mensagem_cliente")
    ))

    conta_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return conta_id

# Rotas da API

@app.get("/")
def inicio():
    return {"status": "Solar Portal API funcionando!"}

@app.post("/auth/registro")
def registrar_integrador(dados: dict):
    conn = conectar_banco()
    cur = conn.cursor()
    try:
        senha_hash = criptografar_senha(dados["senha"])
        cur.execute("""
            INSERT INTO integradores (nome, email, telefone, senha_hash)
            VALUES (%s, %s, %s, %s)
            RETURNING id, nome, email
        """, (dados["nome"], dados["email"], dados.get("telefone"), senha_hash))
        integrador = cur.fetchone()
        conn.commit()
        return {"id": integrador[0], "nome": integrador[1], "email": integrador[2]}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        conn.close()

@app.post("/auth/login")
def login(dados: dict):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("SELECT id, nome, email, senha_hash FROM integradores WHERE email = %s AND ativo = TRUE", (dados["email"],))
    integrador = cur.fetchone()
    cur.close()
    conn.close()

    if not integrador or not verificar_senha(dados["senha"], integrador[3]):
        raise HTTPException(status_code=401, detail="Email ou senha incorretos")

    token = criar_token({"id": integrador[0], "nome": integrador[1], "email": integrador[2]})
    return {"token": token, "nome": integrador[1], "email": integrador[2]}

@app.get("/auth/me")
def meu_perfil(integrador: dict = Depends(obter_integrador_atual)):
    return integrador

@app.get("/clientes")
def listar_clientes(integrador: dict = Depends(obter_integrador_atual)):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("SELECT id, nome, numero_uc, distribuidora, tipo_gd FROM clientes WHERE integrador_id = %s", (integrador["id"],))
    clientes = cur.fetchall()
    cur.close()
    conn.close()
    return [{"id": c[0], "nome": c[1], "numero_uc": c[2], "distribuidora": c[3], "tipo_gd": c[4]} for c in clientes]

@app.post("/clientes")
def cadastrar_cliente(dados: dict, integrador: dict = Depends(obter_integrador_atual)):
    conn = conectar_banco()
    cur = conn.cursor()
    try:
        token_acesso = secrets.token_urlsafe(32)
        cur.execute("""
            INSERT INTO clientes (integrador_id, nome, email, telefone, numero_uc, distribuidora, tipo_gd, token_acesso)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, nome, token_acesso
        """, (
            integrador["id"],
            dados.get("nome"),
            dados.get("email"),
            dados.get("telefone"),
            dados.get("numero_uc"),
            dados.get("distribuidora"),
            dados.get("tipo_gd"),
            token_acesso
        ))
        cliente = cur.fetchone()
        conn.commit()
        return {"id": cliente[0], "nome": cliente[1], "token_acesso": cliente[2]}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        conn.close()

@app.get("/clientes/{cliente_id}/obter-token")
def obter_token_cliente(cliente_id: int, integrador: dict = Depends(obter_integrador_atual)):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("""
        SELECT token_acesso FROM clientes
        WHERE id = %s AND integrador_id = %s
    """, (cliente_id, integrador["id"]))
    cliente = cur.fetchone()
    cur.close()
    conn.close()
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")
    return {"token_acesso": cliente[0]}

@app.get("/clientes/{cliente_id}/contas")
def listar_contas(cliente_id: int, integrador: dict = Depends(obter_integrador_atual)):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("""
        SELECT c.id, c.mes_referencia, c.data_vencimento, c.consumo_kwh,
               c.energia_injetada_kwh, c.saldo_acumulado_kwh, c.valor_fatura,
               c.status_sistema, c.percentual_gerado, c.mensagem_cliente
        FROM contas c
        JOIN clientes cl ON cl.id = c.cliente_id
        WHERE c.cliente_id = %s AND cl.integrador_id = %s
        ORDER BY c.criado_em DESC
    """, (cliente_id, integrador["id"]))
    contas = cur.fetchall()
    cur.close()
    conn.close()
    return [{
        "id": c[0], "mes_referencia": c[1], "data_vencimento": str(c[2]),
        "consumo_kwh": c[3], "energia_injetada_kwh": c[4],
        "saldo_acumulado_kwh": c[5], "valor_fatura": c[6],
        "status_sistema": c[7], "percentual_gerado": c[8],
        "mensagem_cliente": c[9]
    } for c in contas]

@app.get("/portal/{token_acesso}")
def portal_cliente(token_acesso: str):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, nome, numero_uc, distribuidora, tipo_gd
        FROM clientes
        WHERE token_acesso = %s
    """, (token_acesso,))
    cliente = cur.fetchone()
    cur.close()
    conn.close()
    if not cliente:
        raise HTTPException(status_code=404, detail="Link inválido")
    return {"id": cliente[0], "nome": cliente[1], "numero_uc": cliente[2], "distribuidora": cliente[3], "tipo_gd": cliente[4]}

@app.put("/clientes/{cliente_id}/projeto")
def atualizar_projeto(cliente_id: int, dados: dict, integrador: dict = Depends(obter_integrador_atual)):
    conn = conectar_banco()
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE clientes SET
                potencia_kwp = %s,
                latitude = %s,
                longitude = %s,
                data_instalacao = %s,
                performance_ratio = %s
            WHERE id = %s AND integrador_id = %s
            RETURNING id, nome
        """, (
            dados.get("potencia_kwp"),
            dados.get("latitude"),
            dados.get("longitude"),
            dados.get("data_instalacao"),
            dados.get("performance_ratio", 0.80),
            cliente_id,
            integrador["id"]
        ))
        cliente = cur.fetchone()
        conn.commit()
        if not cliente:
            raise HTTPException(status_code=404, detail="Cliente não encontrado")
        return {"sucesso": True, "id": cliente[0], "nome": cliente[1]}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        conn.close()

@app.get("/clientes/{cliente_id}/geracao-esperada")
def geracao_esperada(cliente_id: int):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("""
        SELECT potencia_kwp, latitude, longitude, performance_ratio
        FROM clientes WHERE id = %s
    """, (cliente_id,))
    cliente = cur.fetchone()
    cur.close()
    conn.close()

    if not cliente or not cliente[0]:
        raise HTTPException(status_code=404, detail="Projeto solar não cadastrado")

    potencia_kwp, latitude, longitude, pr = cliente
    potencia_kwp = float(potencia_kwp)
    pr = float(pr) if pr else 0.80

    hsp_mensal = {
        1: 5.2, 2: 5.4, 3: 5.1, 4: 4.8, 5: 4.5, 6: 4.3,
        7: 4.5, 8: 5.0, 9: 4.9, 10: 4.8, 11: 4.9, 12: 5.0
    }

    meses = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun",
             "Jul", "Ago", "Set", "Out", "Nov", "Dez"]
    dias_mes = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]

    geracao = []
    for i in range(1, 13):
        hsp = hsp_mensal[i]
        dias = dias_mes[i-1]
        kwh_esperado = round(potencia_kwp * hsp * dias * pr, 2)
        geracao.append({
            "mes": meses[i-1],
            "mes_num": i,
            "hsp": hsp,
            "kwh_esperado": kwh_esperado
        })

    return {
        "potencia_kwp": potencia_kwp,
        "performance_ratio": pr,
        "geracao_mensal": geracao,
        "total_anual": round(sum(g["kwh_esperado"] for g in geracao), 2)
    }

@app.post("/contas/upload/{cliente_id}")
async def upload_conta(cliente_id: int, arquivo: UploadFile = File(...)):
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(await arquivo.read())
        tmp_path = tmp.name

    try:
        texto = extrair_texto_pdf(tmp_path)
        resultado_raw = analisar_conta(texto)
        resultado_raw = re.sub(r"```json|```", "", resultado_raw).strip()
        dados = json.loads(resultado_raw)

        conta_id = salvar_no_banco(dados, cliente_id)
        dados["id"] = conta_id

        return {"sucesso": True, "conta": dados}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        os.unlink(tmp_path)

def foxess_request(api_key: str, path: str, body: dict = None):
    import requests
    timestamp = str(int(time.time() * 1000))
    signature_str = f"{path}\r\n{api_key}\r\n{timestamp}"
    signature = hashlib.md5(signature_str.encode("UTF-8")).hexdigest()

    headers = {
        "token": api_key,
        "timestamp": timestamp,
        "signature": signature,
        "lang": "en",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0"
    }

    url = f"https://www.foxesscloud.com{path}"
    if body:
        resp = requests.post(url, json=body, headers=headers, timeout=10)
    else:
        resp = requests.get(url, headers=headers, timeout=10)
    return resp.json()

@app.get("/clientes/{cliente_id}/monitoramento")
def monitoramento_foxess(cliente_id: int):
    import requests

    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("""
        SELECT marca_inversor, serial_inversor, api_key_inversor
        FROM clientes WHERE id = %s
    """, (cliente_id,))
    cliente = cur.fetchone()
    cur.close()
    conn.close()

    if not cliente or not cliente[2]:
        raise HTTPException(status_code=404, detail="Inversor não configurado")

    marca, serial, api_key = cliente

    if marca.lower() != "foxess":
        raise HTTPException(status_code=400, detail="Marca não suportada ainda")

    try:
        timestamp = str(round(time.time() * 1000))
        path = "op/v1/device/real/query"
        
        # Atenção: usar f-string raw para garantir \r\n literais
        signature_raw = f"{path}\\r\\n{api_key}\\r\\n{timestamp}"
        signature = hashlib.md5(signature_raw.encode("utf-8")).hexdigest()

        headers = {
            "token": api_key,
            "timestamp": timestamp,
            "signature": signature,
            "lang": "en",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36",
            "Content-Type": "application/json"
        }

        body = {
            "sn": serial,
            "variables": [
                "pvPower",
                "feedinPower",
                "gridConsumptionPower",
                "loadsPower",
                "generationPower"
            ]
        }

        resp = requests.post(
            f"https://www.foxesscloud.com/{path}",
            json=body,
            headers=headers,
            timeout=15
        )

        resultado = resp.json()

        if resultado.get("errno") != 0:
            raise HTTPException(
                status_code=400,
                detail=f"Erro Foxess: {resultado.get('msg')} (errno: {resultado.get('errno')})"
            )

        dados = resultado.get("result", [])
        variaveis = {}
        if dados:
            for item in dados[0].get("datas", []):
                variaveis[item["variable"]] = item.get("value", 0)

        return {
            "marca": marca,
            "serial": serial,
            "status": "online",
            "geracao_atual_kw": variaveis.get("pvPower", 0),
            "injetado_rede_kw": variaveis.get("feedinPower", 0),
            "consumo_rede_kw": variaveis.get("gridConsumptionPower", 0),
            "consumo_casa_kw": variaveis.get("loadsPower", 0),
            "potencia_total_kw": variaveis.get("generationPower", 0),
            "timestamp": datetime.utcnow().isoformat()
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))