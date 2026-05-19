import hashlib
import time
import json
import re
import tempfile
import os
import secrets
import psycopg2
import requests
from datetime import datetime, timedelta
from fastapi import Depends, FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from openai import OpenAI
from auth import criptografar_senha, verificar_senha, criar_token, verificar_token
import PyPDF2

# ==================== CONFIGURAÇÃO ====================

# Carregar .env se existir (desenvolvimento local)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Variáveis de ambiente — funcionam local e na nuvem
DATABASE_URL = os.environ.get("DATABASE_URL", "")
OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY", "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")

app = FastAPI(title="Solar Portal API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)

security = HTTPBearer()

# ==================== BANCO DE DADOS ====================

def conectar_banco():
    if DATABASE_URL:
        # Neon / Render / produção
        return psycopg2.connect(DATABASE_URL, sslmode="require")
    else:
        # Fallback local
        return psycopg2.connect(
            host="localhost",
            port=5432,
            database="solar_portal",
            user="postgres",
            password="991Bog31**"
        )

# ==================== AUTENTICAÇÃO ====================

def obter_integrador_atual(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    payload = verificar_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Token inválido ou expirado")
    return payload

# ==================== FUNÇÕES AUXILIARES ====================

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
4. CONSUMO BRUTO (kWh): É a soma de todos os itens de consumo antes dos descontos da GD. Some "Consumo acima de 80kWh-BR" + "Consumo até 80kWh-BR".
5. CONSUMO FATURADO (kWh): Procure no histórico dos últimos 13 meses o valor correspondente ao mês atual. NÃO usar leitura bruta do medidor.
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
- status_sistema: Se energia_injetada_kwh >= consumo_kwh então "SUPERAVITÁRIO", senão "DEFICITÁRIO". NUNCA compare com consumo_bruto_kwh.
- percentual_gerado: (energia_injetada_kwh / consumo_kwh) x 100. NUNCA usar consumo_bruto_kwh.
- mensagem_cliente: Explique em linguagem simples e amigável. Máximo 3 linhas. NÃO copie textos técnicos da conta.

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

# ==================== FOXESS API ====================

def foxess_chamar_api(api_key: str, path: str, body: dict):
    timestamp = str(int(time.time() * 1000))
    path_com_barra = f"/{path}"
    signature_raw = fr"{path_com_barra}\r\n{api_key}\r\n{timestamp}"
    signature = hashlib.md5(signature_raw.encode("utf-8")).hexdigest()

    headers = {
        "Token": api_key,
        "Lang": "en",
        "Timestamp": timestamp,
        "Signature": signature,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Content-Type": "application/json"
    }

    url = f"https://www.foxesscloud.com/{path}"
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=30)
        return resp.json()
    except requests.exceptions.RequestException as e:
        return {"errno": 99999, "msg": f"Erro de conexão: {str(e)}"}

def foxess_get_realtime_data(api_key: str, serial: str) -> dict:
    path = "op/v0/device/real/query"
    body = {"sn": serial, "variables": []}
    return foxess_chamar_api(api_key, path, body)

def salvar_dados_no_banco(cliente_id: int, dados_mensais: list):
    from calendar import monthrange
    conn = conectar_banco()
    cur = conn.cursor()
    total_inserido = 0
    for mes_dado in dados_mensais:
        ano = mes_dado["ano"]
        mes = mes_dado["mes_num"]
        total_mensal = mes_dado["geracao_kwh"]
        dias_no_mes = monthrange(ano, mes)[1]
        valor_diario = total_mensal / dias_no_mes
        for dia in range(1, dias_no_mes + 1):
            data = datetime(ano, mes, dia).date()
            cur.execute("""
                INSERT INTO historico_geracao (cliente_id, data, geracao_kwh)
                VALUES (%s, %s, %s)
                ON CONFLICT (cliente_id, data)
                DO UPDATE SET geracao_kwh = EXCLUDED.geracao_kwh
            """, (cliente_id, data, round(valor_diario, 2)))
            total_inserido += 1
    conn.commit()
    cur.close()
    conn.close()

def buscar_geracao_dia_especifico(cliente_id: int, data_busca):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("""
        SELECT SUM(geracao_kwh)
        FROM historico_geracao
        WHERE cliente_id = %s AND data = %s
    """, (cliente_id, data_busca))
    resultado = cur.fetchone()
    cur.close()
    conn.close()
    valor = resultado[0] if resultado[0] else 0
    return {"total_kwh": float(valor) if valor else 0}

def buscar_geracao_periodo(cliente_id: int, data_inicio, data_fim):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("""
        SELECT data, SUM(geracao_kwh) as total
        FROM historico_geracao
        WHERE cliente_id = %s AND data BETWEEN %s AND %s
        GROUP BY data
        ORDER BY data
    """, (cliente_id, data_inicio, data_fim))
    resultados = cur.fetchall()
    cur.close()
    conn.close()
    return [{"data": r[0], "total_kwh": float(r[1]) if r[1] else 0} for r in resultados]

# ==================== ROTAS ====================

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
    cur.execute("SELECT token_acesso FROM clientes WHERE id = %s AND integrador_id = %s", (cliente_id, integrador["id"]))
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
        "consumo_kwh": float(c[3] or 0), "energia_injetada_kwh": float(c[4] or 0),
        "saldo_acumulado_kwh": float(c[5] or 0), "valor_fatura": float(c[6] or 0),
        "status_sistema": c[7], "percentual_gerado": float(c[8] or 0),
        "mensagem_cliente": c[9]
    } for c in contas]

@app.get("/clientes/{cliente_id}/contas-publico")
def listar_contas_publico(cliente_id: int):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, mes_referencia, data_vencimento, consumo_kwh,
               energia_injetada_kwh, saldo_acumulado_kwh, valor_fatura,
               status_sistema, percentual_gerado, mensagem_cliente,
               consumo_bruto_kwh
        FROM contas
        WHERE cliente_id = %s
        ORDER BY criado_em DESC
        LIMIT 12
    """, (cliente_id,))
    contas = cur.fetchall()
    cur.close()
    conn.close()
    return [{
        "id": c[0], "mes_referencia": c[1], "data_vencimento": str(c[2]),
        "consumo_kwh": float(c[3] or 0), "energia_injetada_kwh": float(c[4] or 0),
        "saldo_acumulado_kwh": float(c[5] or 0), "valor_fatura": float(c[6] or 0),
        "status_sistema": c[7], "percentual_gerado": float(c[8] or 0),
        "mensagem_cliente": c[9], "consumo_bruto_kwh": float(c[10] or 0)
    } for c in contas]

@app.get("/portal/{token_acesso}")
def portal_cliente(token_acesso: str):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("SELECT id, nome, numero_uc, distribuidora, tipo_gd FROM clientes WHERE token_acesso = %s", (token_acesso,))
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

@app.put("/clientes/{cliente_id}/inversor")
def atualizar_inversor(cliente_id: int, dados: dict, integrador: dict = Depends(obter_integrador_atual)):
    conn = conectar_banco()
    cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE clientes SET
                marca_inversor = %s,
                serial_inversor = %s,
                api_key_inversor = %s
            WHERE id = %s AND integrador_id = %s
            RETURNING id, nome
        """, (
            dados.get("marca_inversor"),
            dados.get("serial_inversor"),
            dados.get("api_key_inversor"),
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
    cur.execute("SELECT potencia_kwp, latitude, longitude, performance_ratio FROM clientes WHERE id = %s", (cliente_id,))
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
    meses = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]
    dias_mes = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    geracao = []
    for i in range(1, 13):
        hsp = hsp_mensal[i]
        dias = dias_mes[i-1]
        kwh_esperado = round(potencia_kwp * hsp * dias * pr, 2)
        geracao.append({"mes": meses[i-1], "mes_num": i, "hsp": hsp, "kwh_esperado": kwh_esperado})
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

@app.get("/clientes/{cliente_id}/monitoramento")
def monitoramento_foxess(cliente_id: int):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("SELECT marca_inversor, serial_inversor, api_key_inversor FROM clientes WHERE id = %s", (cliente_id,))
    cliente = cur.fetchone()
    cur.close()
    conn.close()
    if not cliente or not cliente[2]:
        raise HTTPException(status_code=404, detail="Inversor não configurado")
    marca, serial, api_key = cliente
    if marca.lower() != "foxess":
        raise HTTPException(status_code=400, detail="Marca não suportada ainda")
    data = foxess_get_realtime_data(api_key, serial)
    if data.get("errno") != 0:
        raise HTTPException(status_code=400, detail=f"Erro Foxess: {data.get('msg')} (errno: {data.get('errno')})")
    resultado = data.get("result", [])
    if not resultado:
        raise HTTPException(status_code=404, detail="Nenhum dado retornado")
    variaveis = {}
    for item in resultado[0].get("datas", []):
        valor = item.get("value", 0)
        if valor == "" or valor is None:
            valor = 0
        try:
            variaveis[item["variable"]] = float(valor)
        except (ValueError, TypeError):
            variaveis[item["variable"]] = 0.0
    return {
        "marca": marca,
        "serial": serial,
        "status": "online",
        "geracao_atual_kw": variaveis.get("pvPower", 0.0),
        "injetado_rede_kw": variaveis.get("feedinPower", 0.0),
        "consumo_rede_kw": variaveis.get("gridConsumptionPower", 0.0),
        "consumo_casa_kw": variaveis.get("loadsPower", 0.0),
        "potencia_total_kw": variaveis.get("generationPower", 0.0),
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/clientes/{cliente_id}/monitoramento/mensal")
def monitoramento_mensal(cliente_id: int):
    from datetime import date
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("SELECT nome, marca_inversor FROM clientes WHERE id = %s", (cliente_id,))
    cliente = cur.fetchone()
    if not cliente:
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail="Cliente não encontrado")
    nome_cliente, marca = cliente
    cur.execute("""
        SELECT
            EXTRACT(YEAR FROM data) as ano,
            EXTRACT(MONTH FROM data) as mes,
            SUM(geracao_kwh) as total_geracao
        FROM historico_geracao
        WHERE cliente_id = %s
          AND data >= CURRENT_DATE - INTERVAL '12 months'
        GROUP BY ano, mes
        ORDER BY ano DESC, mes DESC
    """, (cliente_id,))
    resultados = cur.fetchall()
    cur.close()
    conn.close()
    nomes_meses = {1: "Jan", 2: "Fev", 3: "Mar", 4: "Abr", 5: "Mai", 6: "Jun",
                   7: "Jul", 8: "Ago", 9: "Set", 10: "Out", 11: "Nov", 12: "Dez"}
    if resultados and len(resultados) > 0:
        mensal = []
        for ano, mes, total in resultados:
            if total and float(total) > 0:
                mensal.append({
                    "ano": int(ano),
                    "mes_num": int(mes),
                    "mes": nomes_meses[int(mes)],
                    "geracao_kwh": round(float(total), 2),
                    "fonte": "banco_local"
                })
        mensal.sort(key=lambda x: (x["ano"], x["mes_num"]))
        if len(mensal) > 12:
            mensal = mensal[-12:]
        return {
            "cliente_id": cliente_id,
            "cliente_nome": nome_cliente,
            "mensal": mensal,
            "total_periodo": round(sum(m["geracao_kwh"] for m in mensal), 2),
            "fonte": "banco_local"
        }
    if not marca or marca.lower() != "foxess":
        return {"cliente_id": cliente_id, "mensal": [], "total_periodo": 0, "fonte": "nenhuma"}
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("SELECT serial_inversor, api_key_inversor FROM clientes WHERE id = %s", (cliente_id,))
    dados_inversor = cur.fetchone()
    cur.close()
    conn.close()
    if not dados_inversor or not dados_inversor[1]:
        return {"cliente_id": cliente_id, "mensal": [], "total_periodo": 0, "fonte": "nenhuma"}
    serial, api_key = dados_inversor
    hoje = datetime.now()
    dados_por_mes = []
    for i in range(12):
        data_mes = hoje - timedelta(days=30 * i)
        ano = data_mes.year
        mes = data_mes.month
        path = "op/v0/device/report/query"
        body = {"sn": serial, "year": ano, "month": mes, "dimension": "month", "variables": ["generation"]}
        try:
            resposta = foxess_chamar_api(api_key, path, body)
            if resposta.get("errno") == 0:
                resultado = resposta.get("result", [])
                total_mes = 0
                if isinstance(resultado, list):
                    for item in resultado:
                        if isinstance(item, dict) and item.get("variable") == "generation":
                            valores = item.get("values", [])
                            if isinstance(valores, list):
                                total_mes = sum(valores)
                            break
                if total_mes > 0:
                    dados_por_mes.append({
                        "ano": ano, "mes_num": mes,
                        "mes": nomes_meses[mes],
                        "geracao_kwh": round(total_mes, 2)
                    })
        except Exception as e:
            continue
    if dados_por_mes:
        salvar_dados_no_banco(cliente_id, dados_por_mes)
        return {
            "cliente_id": cliente_id,
            "cliente_nome": nome_cliente,
            "mensal": sorted(dados_por_mes, key=lambda x: (x["ano"], x["mes_num"])),
            "total_periodo": round(sum(m["geracao_kwh"] for m in dados_por_mes), 2),
            "fonte": "foxess_api"
        }
    return {"cliente_id": cliente_id, "mensal": [], "total_periodo": 0, "fonte": "nenhuma"}

@app.get("/clientes/{cliente_id}/monitoramento/status")
def status_inversor(cliente_id: int):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("SELECT marca_inversor, serial_inversor, api_key_inversor FROM clientes WHERE id = %s", (cliente_id,))
    cliente = cur.fetchone()
    cur.close()
    conn.close()
    if not cliente or not cliente[2]:
        raise HTTPException(status_code=404, detail="Inversor não configurado")
    marca, serial, api_key = cliente
    if marca.lower() != "foxess":
        raise HTTPException(status_code=400, detail="Marca não suportada ainda")
    data = foxess_get_realtime_data(api_key, serial)
    if data.get("errno") != 0:
        return {"online": False, "error": data.get("msg"), "gerando": False}
    resultado = data.get("result", [])
    if not resultado:
        return {"online": False, "gerando": False}
    variaveis = {}
    for item in resultado[0].get("datas", []):
        variaveis[item["variable"]] = item.get("value", 0)
    pv_power = float(variaveis.get("pvPower", 0) or 0)
    return {
        "online": True,
        "gerando": pv_power > 0.05,
        "potencia_atual_kw": pv_power,
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/clientes/{cliente_id}/monitoramento-raw")
def monitoramento_raw(cliente_id: int):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("SELECT marca_inversor, serial_inversor, api_key_inversor FROM clientes WHERE id = %s", (cliente_id,))
    cliente = cur.fetchone()
    cur.close()
    conn.close()
    if not cliente or not cliente[2]:
        raise HTTPException(status_code=404, detail="Inversor não configurado")
    marca, serial, api_key = cliente
    if marca.lower() != "foxess":
        raise HTTPException(status_code=400, detail="Marca não suportada ainda")
    data = foxess_get_realtime_data(api_key, serial)
    return data

@app.get("/clientes/{cliente_id}/verificar-anomalias")
def verificar_anomalias(cliente_id: int):
    from datetime import date
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("SELECT nome FROM clientes WHERE id = %s", (cliente_id,))
    cliente = cur.fetchone()
    cur.close()
    conn.close()
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")
    hoje = date.today()
    ontem = hoje - timedelta(days=1)
    dados_ontem = buscar_geracao_dia_especifico(cliente_id, ontem)
    geracao_ontem = float(dados_ontem.get("total_kwh", 0))
    data_inicio = hoje - timedelta(days=8)
    data_fim = hoje - timedelta(days=1)
    ultimos_7_dias = buscar_geracao_periodo(cliente_id, data_inicio, data_fim)
    if ultimos_7_dias:
        soma = sum(d["total_kwh"] for d in ultimos_7_dias)
        media_7_dias = float(soma / len(ultimos_7_dias))
    else:
        media_7_dias = 0.0
    alertas = []
    if geracao_ontem < 0.5:
        alertas.append({
            "tipo": "urgente",
            "titulo": "Sistema Parado",
            "mensagem": f"Seu sistema não gerou energia significativa ontem ({geracao_ontem:.1f} kWh). Isso pode indicar disjuntor desarmado ou falha no inversor.",
            "acao": "Verificar disjuntor CA e entrar em contato com o técnico.",
            "icone": "🔴"
        })
    elif media_7_dias > 0 and geracao_ontem < media_7_dias * 0.4:
        queda = ((media_7_dias - geracao_ontem) / media_7_dias * 100)
        alertas.append({
            "tipo": "atencao",
            "titulo": "Geração Muito Abaixo do Normal",
            "mensagem": f"Ontem seu sistema gerou {geracao_ontem:.1f} kWh, enquanto a média dos últimos 7 dias foi {media_7_dias:.1f} kWh. Queda de {queda:.0f}%.",
            "acao": "Verificar sombreamento, sujeira nos painéis ou problema no inversor.",
            "icone": "⚠️"
        })
    elif media_7_dias > 0 and geracao_ontem < media_7_dias * 0.7:
        alertas.append({
            "tipo": "informativo",
            "titulo": "Geração Abaixo da Média",
            "mensagem": f"Ontem seu sistema gerou {geracao_ontem:.1f} kWh. A média dos últimos 7 dias é {media_7_dias:.1f} kWh.",
            "acao": "Monitorar nos próximos dias. Se continuar baixo, agendar limpeza preventiva.",
            "icone": "📉"
        })
    if not alertas and geracao_ontem > 0:
        alertas.append({
            "tipo": "normal",
            "titulo": "Sistema Operando Normalmente",
            "mensagem": f"Ontem seu sistema gerou {geracao_ontem:.1f} kWh.",
            "acao": "Nenhuma ação necessária. Continue monitorando!",
            "icone": "✅"
        })
    if media_7_dias == 0 and geracao_ontem == 0:
        alertas = [{
            "tipo": "informativo",
            "titulo": "Coletando Dados",
            "mensagem": "Seu sistema está coletando os primeiros dados de geração. Em alguns dias teremos estatísticas completas.",
            "acao": "Aguardar coleta de mais dias para gerar alertas precisos.",
            "icone": "📡"
        }]
    return {
        "cliente_id": cliente_id,
        "cliente_nome": cliente[0],
        "data_analise": hoje.isoformat(),
        "geracao_ontem_kwh": round(geracao_ontem, 1),
        "media_7_dias_kwh": round(media_7_dias, 1),
        "alertas": alertas
    }

@app.post("/clientes/{cliente_id}/atualizar-historico")
def atualizar_historico_banco(cliente_id: int):
    dados = monitoramento_mensal(cliente_id)
    if dados.get("fonte") == "foxess_api":
        return {"sucesso": True, "mensagem": "Histórico atualizado com sucesso da FoxESS", "dados": dados}
    elif dados.get("fonte") == "banco_local":
        return {"sucesso": True, "mensagem": "Dados já estavam no banco local", "dados": dados}
    else:
        return {"sucesso": False, "mensagem": dados.get("mensagem", "Erro ao atualizar histórico")}
