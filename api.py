import hashlib
import time
import json
import re
import tempfile
import os
import secrets
import psycopg2
import requests
from datetime import datetime, timedelta, date
from calendar import monthrange
from fastapi import Depends, FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from openai import OpenAI
from auth import criptografar_senha, verificar_senha, criar_token, verificar_token
import PyPDF2

# ==================== CONFIGURAÇÃO ====================

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DATABASE_URL = os.environ.get("DATABASE_URL", "")
OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY", "")

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
        return psycopg2.connect(DATABASE_URL, sslmode="require")
    else:
        return psycopg2.connect(
            host="localhost", port=5432,
            database="solar_portal", user="postgres", password="991Bog31**"
        )

# ==================== AUTENTICAÇÃO ====================

def obter_integrador_atual(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    payload = verificar_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Token inválido ou expirado")
    return payload

# ==================== PDF ====================

def extrair_texto_pdf(caminho_pdf):
    texto = ""
    with open(caminho_pdf, "rb") as f:
        leitor = PyPDF2.PdfReader(f)
        for pagina in leitor.pages:
            texto += pagina.extract_text()
    return texto

# ==================== IA — ANÁLISE DE CONTA ====================

def analisar_conta(texto_pdf):
    cliente_ia = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=OPENROUTER_KEY
    )

    prompt = f"""Você é um especialista em contas de energia elétrica brasileiras com foco em Geração Distribuída (GD).

Analise o texto extraído da conta e retorne SOMENTE um JSON válido, sem texto adicional, sem explicações, sem markdown.

ATENÇÃO — regras importantes antes de extrair:

1. NOME DO CLIENTE: Nome da pessoa titular. Ignore prefixos de localidade como "B JARDIM".

2. MÊS DE REFERÊNCIA: É o mês e ano impressos no campo "REF: MÊS / ANO" ou "Referência" em destaque na fatura.
   ATENÇÃO: Use o mês do campo de referência principal da fatura, NÃO o mês de leitura anterior.
   Exemplos corretos: "Maio / 2026", "Abril / 2026", "Março / 2026"
   Se a fatura diz "Maio / 2026" no campo REF, retorne "Maio / 2026" mesmo que haja referências a abril em outros campos.

3. DATA DE VENCIMENTO: Campo "VENCIMENTO" em destaque. NÃO confundir com data de apresentação ou emissão.

4. CONSUMO BRUTO (kWh): Soma de todos os itens de consumo antes dos descontos da GD.
   Some "Consumo acima de 80kWh-BR" + "Consumo até 80kWh-BR".

5. CONSUMO FATURADO (kWh): Valor no histórico dos últimos 13 meses referente ao mês atual.
   NÃO usar a leitura bruta do medidor. NÃO confundir com "Consumo até 80kWh-BR".

6. ENERGIA INJETADA (kWh): Energia enviada à rede elétrica. Sempre em kWh, nunca em R$.
   Aparece como "Energia injetada", "Energia Ativa Inj." ou similar.

7. SALDO DE CRÉDITOS (kWh): Campo "Saldo Acumulado". Se zero, retornar 0.

8. CONSUMO DA REDE (kWh): Energia consumida da distribuidora (período noturno ou quando geração é insuficiente).
   É o consumo bruto que a conta registrou como consumo da rede. Aproximadamente igual ao consumo faturado em sistemas GD.

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
  "consumo_rede_kwh": 0.0,
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

Regras para calcular status_sistema, percentual_gerado e mensagem_cliente:
- status_sistema: Se energia_injetada_kwh >= consumo_kwh então "SUPERAVITÁRIO", senão "DEFICITÁRIO".
- percentual_gerado: (energia_injetada_kwh / consumo_kwh) x 100. NUNCA usar consumo_bruto_kwh.
- mensagem_cliente: Explique em linguagem simples. Máximo 3 linhas. NÃO copie textos técnicos.

Texto da conta:
{texto_pdf}"""

    resposta = cliente_ia.chat.completions.create(
        model="openrouter/auto",
        messages=[{"role": "user", "content": prompt}]
    )
    return resposta.choices[0].message.content

# ==================== CÁLCULO DE CONSUMO ====================

def calcular_analise_consumo(dados: dict, geracao_total_kwh: float = None) -> dict:
    """
    Calcula consumo instantâneo, consumo total e detecta aumento de consumo.

    Fórmulas:
    - Consumo Instantâneo = Gerado (pvPower) - Injetado
    - Consumo Total = Consumo Instantâneo + Consumo da Rede (Energisa)

    Se não tiver pvPower, estima pela conta:
    - Consumo Total ≈ Consumo Bruto da conta (antes dos descontos GD)
    """
    energia_injetada = float(dados.get("energia_injetada_kwh") or 0)
    consumo_rede = float(dados.get("consumo_rede_kwh") or dados.get("consumo_kwh") or 0)
    consumo_bruto = float(dados.get("consumo_bruto_kwh") or 0)

    resultado = {
        "geracao_total_kwh": None,
        "consumo_instantaneo_kwh": None,
        "consumo_total_kwh": None,
        "analise_consumo": None
    }

    if geracao_total_kwh and geracao_total_kwh > 0:
        # Temos dados do inversor — cálculo preciso
        consumo_instantaneo = round(geracao_total_kwh - energia_injetada, 2)
        consumo_total = round(consumo_instantaneo + consumo_rede, 2)
        resultado["geracao_total_kwh"] = geracao_total_kwh
        resultado["consumo_instantaneo_kwh"] = max(0, consumo_instantaneo)
        resultado["consumo_total_kwh"] = consumo_total
    else:
        # Sem dados do inversor — estimativa pela conta
        consumo_total = consumo_bruto if consumo_bruto > 0 else (consumo_rede + energia_injetada)
        resultado["consumo_total_kwh"] = round(consumo_total, 2)

    return resultado

def gerar_mensagem_consumo(dados_conta: dict, consumo_anterior: float = None) -> str:
    """
    Gera mensagem inteligente explicando o comportamento de consumo ao cliente.
    Detecta e explica aumento de consumo de forma clara e amigável.
    """
    energia_injetada = float(dados_conta.get("energia_injetada_kwh") or 0)
    consumo_total = float(dados_conta.get("consumo_total_kwh") or 0)
    geracao_total = float(dados_conta.get("geracao_total_kwh") or 0)
    consumo_instantaneo = float(dados_conta.get("consumo_instantaneo_kwh") or 0)
    consumo_rede = float(dados_conta.get("consumo_rede_kwh") or dados_conta.get("consumo_kwh") or 0)
    mes = dados_conta.get("mes_referencia", "este mês")

    linhas = []

    if geracao_total > 0:
        linhas.append(
            f"☀️ Em {mes}, seu sistema solar gerou {geracao_total:.1f} kWh no total. "
            f"Desses, {consumo_instantaneo:.1f} kWh foram consumidos instantaneamente na sua casa "
            f"e {energia_injetada:.1f} kWh foram injetados na rede elétrica como créditos."
        )
        linhas.append(
            f"🏠 Sua casa consumiu {consumo_total:.1f} kWh no total este mês "
            f"({consumo_instantaneo:.1f} kWh do solar + {consumo_rede:.1f} kWh da distribuidora)."
        )
    else:
        linhas.append(
            f"⚡ Em {mes}, seu sistema injetou {energia_injetada:.1f} kWh na rede elétrica. "
            f"Isso representa a sobra da geração solar após o consumo durante o dia."
        )

    if consumo_anterior and consumo_anterior > 0 and consumo_total > 0:
        variacao = ((consumo_total - consumo_anterior) / consumo_anterior) * 100
        if variacao > 20:
            economia_perdida = round((consumo_total - consumo_anterior) * 0.75, 2)
            linhas.append(
                f"📈 Atenção: seu consumo total aumentou {variacao:.0f}% comparado ao período anterior "
                f"({consumo_anterior:.1f} kWh antes vs {consumo_total:.1f} kWh agora). "
                f"Seu sistema solar está funcionando normalmente — o aumento na conta se deve ao maior consumo de energia, "
                f"não a uma falha no sistema. Se mantivesse o consumo anterior, economizaria cerca de R$ {economia_perdida:.2f} a mais."
            )
        elif variacao > 5:
            linhas.append(
                f"📊 Seu consumo aumentou {variacao:.0f}% em relação ao período anterior. "
                f"O sistema solar continua gerando normalmente — considere revisar quais equipamentos "
                f"estão consumindo mais energia."
            )
        elif variacao < -5:
            linhas.append(
                f"✅ Ótimo! Seu consumo reduziu {abs(variacao):.0f}% em relação ao período anterior. "
                f"O solar + eficiência energética estão fazendo efeito!"
            )

    return " ".join(linhas)

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

def foxess_get_realtime(api_key: str, serial: str) -> dict:
    path = "op/v0/device/real/query"
    body = {"sn": serial, "variables": []}
    return foxess_chamar_api(api_key, path, body)

def foxess_get_mensal(api_key: str, serial: str, ano: int, mes: int) -> float:
    """Busca geração total do mês na FoxESS. Retorna kWh ou 0."""
    path = "op/v0/device/report/query"
    body = {"sn": serial, "year": ano, "month": mes, "dimension": "month", "variables": ["generation"]}
    try:
        resposta = foxess_chamar_api(api_key, path, body)
        if resposta.get("errno") == 0:
            resultado = resposta.get("result", [])
            if isinstance(resultado, list):
                for item in resultado:
                    if isinstance(item, dict) and item.get("variable") == "generation":
                        valores = item.get("values", [])
                        if isinstance(valores, list):
                            return round(sum(v for v in valores if v), 2)
    except Exception:
        pass
    return 0.0

# ==================== BANCO — HELPERS ====================

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
            percentual_gerado, leitura_por_media, meses_acumulados, mensagem_cliente,
            geracao_total_kwh, consumo_instantaneo_kwh, consumo_total_kwh,
            consumo_anterior_kwh, aumento_consumo_pct, analise_consumo
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
        dados.get("mensagem_cliente"),
        dados.get("geracao_total_kwh"),
        dados.get("consumo_instantaneo_kwh"),
        dados.get("consumo_total_kwh"),
        dados.get("consumo_anterior_kwh"),
        dados.get("aumento_consumo_pct"),
        dados.get("analise_consumo")
    ))

    conta_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return conta_id

def buscar_geracao_dia(cliente_id: int, data_busca):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("""
        SELECT COALESCE(SUM(geracao_kwh), 0)
        FROM historico_geracao
        WHERE cliente_id = %s AND data = %s
    """, (cliente_id, data_busca))
    resultado = cur.fetchone()
    cur.close()
    conn.close()
    return float(resultado[0]) if resultado[0] else 0.0

def buscar_geracao_periodo(cliente_id: int, data_inicio, data_fim):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("""
        SELECT data, SUM(geracao_kwh) as total
        FROM historico_geracao
        WHERE cliente_id = %s AND data BETWEEN %s AND %s
        GROUP BY data ORDER BY data
    """, (cliente_id, data_inicio, data_fim))
    resultados = cur.fetchall()
    cur.close()
    conn.close()
    return [{"data": str(r[0]), "total_kwh": float(r[1]) if r[1] else 0} for r in resultados]

def salvar_historico_banco(cliente_id: int, dados_mensais: list):
    conn = conectar_banco()
    cur = conn.cursor()
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
    conn.commit()
    cur.close()
    conn.close()

# ==================== ROTAS ====================

@app.get("/")
def inicio():
    return {"status": "Solar Portal API funcionando!"}

# ===== AUTH =====

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

# ===== CLIENTES =====

@app.get("/clientes")
def listar_clientes(integrador: dict = Depends(obter_integrador_atual)):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, nome, numero_uc, distribuidora, tipo_gd,
               marca_inversor, serial_inversor
        FROM clientes WHERE integrador_id = %s
    """, (integrador["id"],))
    clientes = cur.fetchall()
    cur.close()
    conn.close()
    return [{
        "id": c[0], "nome": c[1], "numero_uc": c[2],
        "distribuidora": c[3], "tipo_gd": c[4],
        "marca_inversor": c[5], "serial_inversor": c[6]
    } for c in clientes]

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
            integrador["id"], dados.get("nome"), dados.get("email"),
            dados.get("telefone"), dados.get("numero_uc"),
            dados.get("distribuidora"), dados.get("tipo_gd"), token_acesso
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
               c.status_sistema, c.percentual_gerado, c.mensagem_cliente,
               c.geracao_total_kwh, c.consumo_instantaneo_kwh, c.consumo_total_kwh,
               c.analise_consumo
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
        "mensagem_cliente": c[9], "geracao_total_kwh": float(c[10] or 0),
        "consumo_instantaneo_kwh": float(c[11] or 0), "consumo_total_kwh": float(c[12] or 0),
        "analise_consumo": c[13]
    } for c in contas]

@app.get("/clientes/{cliente_id}/contas-publico")
def listar_contas_publico(cliente_id: int):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, mes_referencia, data_vencimento, consumo_kwh,
               energia_injetada_kwh, saldo_acumulado_kwh, valor_fatura,
               status_sistema, percentual_gerado, mensagem_cliente,
               consumo_bruto_kwh, geracao_total_kwh, consumo_instantaneo_kwh,
               consumo_total_kwh, analise_consumo
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
        "mensagem_cliente": c[9], "consumo_bruto_kwh": float(c[10] or 0),
        "geracao_total_kwh": float(c[11] or 0), "consumo_instantaneo_kwh": float(c[12] or 0),
        "consumo_total_kwh": float(c[13] or 0), "analise_consumo": c[14]
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
            UPDATE clientes SET potencia_kwp=%s, latitude=%s, longitude=%s,
                data_instalacao=%s, performance_ratio=%s
            WHERE id=%s AND integrador_id=%s
            RETURNING id, nome
        """, (
            dados.get("potencia_kwp"), dados.get("latitude"), dados.get("longitude"),
            dados.get("data_instalacao"), dados.get("performance_ratio", 0.80),
            cliente_id, integrador["id"]
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
            UPDATE clientes SET marca_inversor=%s, serial_inversor=%s, api_key_inversor=%s
            WHERE id=%s AND integrador_id=%s
            RETURNING id, nome
        """, (
            dados.get("marca_inversor"), dados.get("serial_inversor"),
            dados.get("api_key_inversor"), cliente_id, integrador["id"]
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

# ===== UPLOAD DE CONTA =====

@app.post("/contas/upload/{cliente_id}")
async def upload_conta(cliente_id: int, arquivo: UploadFile = File(...)):
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(await arquivo.read())
        tmp_path = tmp.name

    try:
        # 1. Extrair texto do PDF
        texto = extrair_texto_pdf(tmp_path)

        # 2. Analisar com IA
        resultado_raw = analisar_conta(texto)
        resultado_raw = re.sub(r"```json|```", "", resultado_raw).strip()
        dados = json.loads(resultado_raw)

        # 3. Buscar geração total do inversor (se configurado)
        conn = conectar_banco()
        cur = conn.cursor()
        cur.execute("SELECT marca_inversor, serial_inversor, api_key_inversor FROM clientes WHERE id = %s", (cliente_id,))
        inversor = cur.fetchone()

        # Buscar conta anterior para comparar consumo
        cur.execute("""
            SELECT consumo_total_kwh, consumo_bruto_kwh
            FROM contas WHERE cliente_id = %s
            ORDER BY criado_em DESC LIMIT 1
        """, (cliente_id,))
        conta_anterior = cur.fetchone()
        cur.close()
        conn.close()

        consumo_anterior = None
        if conta_anterior:
            consumo_anterior = float(conta_anterior[0] or conta_anterior[1] or 0)

        # 4. Buscar geração total do mês via FoxESS
        geracao_total_kwh = None
        if inversor and inversor[0] and inversor[0].lower() == "foxess" and inversor[2]:
            try:
                mes_ref = dados.get("mes_referencia", "")
                meses_map = {
                    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
                    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12
                }
                mes_num = None
                ano_num = datetime.now().year
                for nome, num in meses_map.items():
                    if nome in mes_ref.lower():
                        mes_num = num
                        break
                partes = re.findall(r'\d{4}', mes_ref)
                if partes:
                    ano_num = int(partes[0])

                if mes_num:
                    geracao_total_kwh = foxess_get_mensal(inversor[2], inversor[1], ano_num, mes_num)
                    if geracao_total_kwh == 0:
                        geracao_total_kwh = None
            except Exception:
                geracao_total_kwh = None

        # 5. Calcular consumo instantâneo e total
        analise = calcular_analise_consumo(dados, geracao_total_kwh)
        dados.update(analise)

        # 6. Calcular aumento de consumo
        if consumo_anterior and consumo_anterior > 0 and dados.get("consumo_total_kwh"):
            aumento_pct = ((dados["consumo_total_kwh"] - consumo_anterior) / consumo_anterior) * 100
            dados["consumo_anterior_kwh"] = consumo_anterior
            dados["aumento_consumo_pct"] = round(aumento_pct, 1)

        # 7. Gerar análise inteligente de consumo
        dados["analise_consumo"] = gerar_mensagem_consumo(dados, consumo_anterior)

        # 8. Salvar no banco
        conta_id = salvar_no_banco(dados, cliente_id)
        dados["id"] = conta_id

        return {"sucesso": True, "conta": dados}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        os.unlink(tmp_path)

# ===== GERAÇÃO ESPERADA =====

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

    potencia_kwp = float(cliente[0])
    pr = float(cliente[3]) if cliente[3] else 0.80

    hsp_mensal = {
        1: 5.2, 2: 5.4, 3: 5.1, 4: 4.8, 5: 4.5, 6: 4.3,
        7: 4.5, 8: 5.0, 9: 4.9, 10: 4.8, 11: 4.9, 12: 5.0
    }
    meses = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]
    dias_mes = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]

    geracao = []
    for i in range(1, 13):
        kwh_esperado = round(potencia_kwp * hsp_mensal[i] * dias_mes[i-1] * pr, 2)
        geracao.append({"mes": meses[i-1], "mes_num": i, "hsp": hsp_mensal[i], "kwh_esperado": kwh_esperado})

    return {
        "potencia_kwp": potencia_kwp,
        "performance_ratio": pr,
        "geracao_mensal": geracao,
        "total_anual": round(sum(g["kwh_esperado"] for g in geracao), 2)
    }

# ===== MONITORAMENTO =====

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

    data = foxess_get_realtime(api_key, serial)

    if data.get("errno") != 0:
        raise HTTPException(status_code=400, detail=f"Erro Foxess: {data.get('msg')} (errno: {data.get('errno')})")

    resultado = data.get("result", [])
    if not resultado:
        raise HTTPException(status_code=404, detail="Nenhum dado retornado")

    variaveis = {}
    for item in resultado[0].get("datas", []):
        try:
            variaveis[item["variable"]] = float(item.get("value") or 0)
        except:
            variaveis[item["variable"]] = 0.0

    return {
        "marca": marca, "serial": serial, "status": "online",
        "geracao_atual_kw": variaveis.get("pvPower", 0.0),
        "injetado_rede_kw": variaveis.get("feedinPower", 0.0),
        "consumo_rede_kw": variaveis.get("gridConsumptionPower", 0.0),
        "consumo_casa_kw": variaveis.get("loadsPower", 0.0),
        "potencia_total_kw": variaveis.get("generationPower", 0.0),
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/clientes/{cliente_id}/monitoramento/mensal")
def monitoramento_mensal(cliente_id: int):
    nomes_meses = {
        1: "Jan", 2: "Fev", 3: "Mar", 4: "Abr", 5: "Mai", 6: "Jun",
        7: "Jul", 8: "Ago", 9: "Set", 10: "Out", 11: "Nov", 12: "Dez"
    }

    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("SELECT nome, marca_inversor, serial_inversor, api_key_inversor FROM clientes WHERE id = %s", (cliente_id,))
    cliente = cur.fetchone()

    # Tentar dados do banco local primeiro
    cur.execute("""
        SELECT EXTRACT(YEAR FROM data) as ano, EXTRACT(MONTH FROM data) as mes,
               SUM(geracao_kwh) as total
        FROM historico_geracao
        WHERE cliente_id = %s AND data >= CURRENT_DATE - INTERVAL '12 months'
        GROUP BY ano, mes ORDER BY ano, mes
    """, (cliente_id,))
    resultados = cur.fetchall()
    cur.close()
    conn.close()

    if resultados:
        mensal = [{"ano": int(r[0]), "mes_num": int(r[1]), "mes": nomes_meses[int(r[1])],
                   "geracao_kwh": round(float(r[2]), 2), "fonte": "banco"} for r in resultados if float(r[2]) > 0]
        if mensal:
            return {"cliente_id": cliente_id, "mensal": mensal[-12:],
                    "total_periodo": round(sum(m["geracao_kwh"] for m in mensal), 2), "fonte": "banco"}

    # Fallback FoxESS
    if not cliente or not cliente[1] or cliente[1].lower() != "foxess" or not cliente[3]:
        return {"cliente_id": cliente_id, "mensal": [], "total_periodo": 0, "fonte": "nenhuma"}

    nome, marca, serial, api_key = cliente
    hoje = datetime.now()
    dados_por_mes = []

    for i in range(12):
        data_mes = hoje - timedelta(days=30 * i)
        total = foxess_get_mensal(api_key, serial, data_mes.year, data_mes.month)
        if total > 0:
            dados_por_mes.append({
                "ano": data_mes.year, "mes_num": data_mes.month,
                "mes": nomes_meses[data_mes.month], "geracao_kwh": total
            })

    if dados_por_mes:
        salvar_historico_banco(cliente_id, dados_por_mes)
        return {
            "cliente_id": cliente_id,
            "mensal": sorted(dados_por_mes, key=lambda x: (x["ano"], x["mes_num"])),
            "total_periodo": round(sum(m["geracao_kwh"] for m in dados_por_mes), 2),
            "fonte": "foxess"
        }

    return {"cliente_id": cliente_id, "mensal": [], "total_periodo": 0, "fonte": "nenhuma"}

# ===== ALERTAS E ANOMALIAS =====

@app.get("/clientes/{cliente_id}/verificar-anomalias")
def verificar_anomalias(cliente_id: int):
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
    geracao_ontem = buscar_geracao_dia(cliente_id, ontem)

    ultimos_7 = buscar_geracao_periodo(cliente_id, hoje - timedelta(days=8), ontem)
    media_7_dias = sum(d["total_kwh"] for d in ultimos_7) / len(ultimos_7) if ultimos_7 else 0.0

    alertas = []

    if geracao_ontem < 0.5:
        alertas.append({
            "tipo": "urgente", "icone": "🔴",
            "titulo": "Sistema Parado",
            "mensagem": f"Seu sistema não gerou energia significativa ontem ({geracao_ontem:.1f} kWh). Isso pode indicar disjuntor desarmado ou falha no inversor.",
            "acao": "Verificar disjuntor CA e entrar em contato com o técnico."
        })
    elif media_7_dias > 0 and geracao_ontem < media_7_dias * 0.4:
        queda = ((media_7_dias - geracao_ontem) / media_7_dias * 100)
        alertas.append({
            "tipo": "atencao", "icone": "⚠️",
            "titulo": "Geração Muito Abaixo do Normal",
            "mensagem": f"Ontem seu sistema gerou {geracao_ontem:.1f} kWh, enquanto a média dos últimos 7 dias foi {media_7_dias:.1f} kWh. Queda de {queda:.0f}%.",
            "acao": "Verificar sombreamento, sujeira nos painéis ou problema no inversor."
        })
    elif media_7_dias > 0 and geracao_ontem < media_7_dias * 0.7:
        alertas.append({
            "tipo": "informativo", "icone": "📉",
            "titulo": "Geração Abaixo da Média",
            "mensagem": f"Ontem seu sistema gerou {geracao_ontem:.1f} kWh. A média dos últimos 7 dias é {media_7_dias:.1f} kWh.",
            "acao": "Monitorar nos próximos dias. Se continuar baixo, agendar limpeza preventiva."
        })

    if not alertas and geracao_ontem > 0:
        alertas.append({
            "tipo": "normal", "icone": "✅",
            "titulo": "Sistema Operando Normalmente",
            "mensagem": f"Ontem seu sistema gerou {geracao_ontem:.1f} kWh.",
            "acao": "Nenhuma ação necessária. Continue monitorando!"
        })

    if media_7_dias == 0 and geracao_ontem == 0:
        alertas = [{
            "tipo": "informativo", "icone": "📡",
            "titulo": "Coletando Dados",
            "mensagem": "Seu sistema está coletando os primeiros dados de geração. Em alguns dias teremos estatísticas completas.",
            "acao": "Aguardar coleta de mais dias para gerar alertas precisos."
        }]

    return {
        "cliente_id": cliente_id, "cliente_nome": cliente[0],
        "data_analise": hoje.isoformat(),
        "geracao_ontem_kwh": round(geracao_ontem, 1),
        "media_7_dias_kwh": round(media_7_dias, 1),
        "alertas": alertas
    }
