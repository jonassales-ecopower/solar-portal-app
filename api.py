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

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DATABASE_URL = os.environ.get("DATABASE_URL", "")
OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY", "")

app = FastAPI(title="Solar Portal API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"], allow_credentials=False)
security = HTTPBearer()

# ==================== BANCO ====================

def conectar_banco():
    if DATABASE_URL:
        return psycopg2.connect(DATABASE_URL, sslmode="require")
    return psycopg2.connect(host="localhost", port=5432, database="solar_portal", user="postgres", password="991Bog31**")

def obter_integrador_atual(credentials: HTTPAuthorizationCredentials = Depends(security)):
    payload = verificar_token(credentials.credentials)
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

# ==================== IA ====================

def analisar_conta(texto_pdf):
    cliente_ia = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=OPENROUTER_KEY)
    prompt = f"""Você é um especialista em contas de energia elétrica brasileiras com foco em Geração Distribuída (GD).

Analise o texto extraído da conta e retorne SOMENTE um JSON válido, sem texto adicional, sem explicações, sem markdown.

REGRAS CRÍTICAS DE EXTRAÇÃO:

1. NOME DO CLIENTE: Nome da pessoa titular. Ignore prefixos de localidade como "B JARDIM".

2. MÊS DE REFERÊNCIA: Use EXATAMENTE o valor do campo "REF: MÊS / ANO" impresso na fatura.
   Exemplos: "Maio / 2026", "Abril / 2026". NUNCA use o mês de leitura anterior.

3. DATA DE VENCIMENTO: Campo "VENCIMENTO" em destaque. NÃO confundir com data de emissão.

4. CONSUMO BRUTO (kWh): ATENÇÃO CRÍTICA — é a leitura REAL DO MEDIDOR no período.
   Localize a tabela "ESTRUTURA DO CONSUMO" ou "DADOS DO CONSUMO" ou tabela de medidores.
   Use o campo "FATURADO" ou "Consumo kWh" da linha "Energia ativa em kWh".
   Exemplo: "Energia ativa em kWh Ponta | 8557 | 10054 | 1 | 1497" → consumo_bruto_kwh = 1497.
   Exemplo: "Energia ativa em kWh Ponta | 10054 | 10930 | 1 | 876" → consumo_bruto_kwh = 876.
   NUNCA use "Consumo até 80kWh-BR" — é faixa tarifária zerada pelo governo, NÃO é consumo real!
   O consumo bruto real é SEMPRE: (Leitura Atual - Leitura Anterior) × Constante.

5. CONSUMO FATURADO (kWh): Valor no histórico dos últimos 13 meses referente ao mês atual.
   É MENOR que consumo bruto em sistemas GD. Ex: ABR/26 = 420,63 kWh; MAI/26 = 277,45 kWh.

6. CONSUMO DA REDE (kWh): Mesmo valor que consumo_bruto_kwh.

7. ENERGIA INJETADA (kWh): SEMPRE em kWh, nunca em R$.
   Tabela medidores linha "Energia injetada": campo "Consumo kWh" ou "FATURADO".
   Exemplo: "Energia injetada Ponta | 7321 | 8016 | 1 | 695" → energia_injetada_kwh = 695.
   Exemplo: "Energia injetada Ponta | 8016 | 8350 | 1 | 334" → energia_injetada_kwh = 334.

8. SALDO ACUMULADO (kWh): Campo "Saldo Acumulado". Se zero, retornar 0.

9. MESES ACUMULADOS: Se houver "FATURAMENTO PELA MÉDIA" ou "LEITURA INFORMADA PELO CLIENTE",
   analise o período (Leitura Anterior → Leitura Atual) para determinar quantos meses foram acumulados.
   Período de 31 dias = 1 mês. Período de 60-62 dias = 2 meses. Etc.

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

- status_sistema: Se energia_injetada_kwh >= consumo_kwh → "SUPERAVITÁRIO", senão → "DEFICITÁRIO".
- percentual_gerado: (energia_injetada_kwh / consumo_kwh) × 100.
- mensagem_cliente: Máximo 2 linhas. Linguagem simples. Se leitura acumulada, mencionar.

Texto da conta:
{texto_pdf}"""
    resposta = cliente_ia.chat.completions.create(model="openrouter/auto", messages=[{"role": "user", "content": prompt}])
    return resposta.choices[0].message.content

# ==================== CÁLCULO DE CONSUMO ====================

def calcular_analise_consumo(dados: dict, geracao_total_kwh: float = None) -> dict:
    energia_injetada = float(dados.get("energia_injetada_kwh") or 0)
    consumo_rede = float(dados.get("consumo_bruto_kwh") or dados.get("consumo_rede_kwh") or 0)
    meses = int(dados.get("meses_acumulados") or 1)

    resultado = {"geracao_total_kwh": None, "consumo_instantaneo_kwh": None, "consumo_total_kwh": None, "analise_consumo": None}

    geracao_ajustada = None
    if geracao_total_kwh and geracao_total_kwh > 0:
        geracao_ajustada = geracao_total_kwh  # Geração já vem somada corretamente por todos os meses

    if geracao_ajustada and geracao_ajustada > 0:
        consumo_instantaneo = max(0, round(geracao_ajustada - energia_injetada, 2))
        consumo_total = round(consumo_instantaneo + consumo_rede, 2)
        resultado["geracao_total_kwh"] = geracao_ajustada
        resultado["consumo_instantaneo_kwh"] = consumo_instantaneo
        resultado["consumo_total_kwh"] = consumo_total
    else:
        consumo_instantaneo = max(0, round(consumo_rede - energia_injetada, 2)) if consumo_rede > energia_injetada else 0
        resultado["consumo_total_kwh"] = round(consumo_rede, 2)
        resultado["consumo_instantaneo_kwh"] = consumo_instantaneo

    return resultado

def gerar_mensagem_consumo(dados: dict, consumo_anterior: float = None) -> str:
    energia_injetada = float(dados.get("energia_injetada_kwh") or 0)
    consumo_total = float(dados.get("consumo_total_kwh") or 0)
    geracao_total = float(dados.get("geracao_total_kwh") or 0)
    consumo_instantaneo = float(dados.get("consumo_instantaneo_kwh") or 0)
    consumo_rede = float(dados.get("consumo_bruto_kwh") or 0)
    mes = dados.get("mes_referencia", "este mês")
    meses = int(dados.get("meses_acumulados") or 1)
    partes = []

    periodo = f"nos últimos {meses} meses" if meses > 1 else f"em {mes}"

    if geracao_total > 0:
        partes.append(f"☀️ {periodo.capitalize()}, seu sistema gerou {geracao_total:.1f} kWh. Desses, {consumo_instantaneo:.1f} kWh foram usados instantaneamente e {energia_injetada:.1f} kWh foram injetados na rede como créditos.")
    else:
        partes.append(f"⚡ {periodo.capitalize()}, seu sistema injetou {energia_injetada:.1f} kWh na rede como créditos. Sua casa consumiu {consumo_rede:.1f} kWh da distribuidora.")

    if consumo_total > 0 and consumo_rede > 0:
        partes.append(f"📊 Consumo total da casa: {consumo_total:.1f} kWh ({consumo_instantaneo:.1f} kWh do solar + {consumo_rede:.1f} kWh da rede).")

    if meses > 1:
        partes.append(f"⚠️ Esta conta cobre {meses} meses de leitura acumulada.")

    if consumo_anterior and consumo_anterior > 0 and consumo_total > 0:
        ant_ajustado = consumo_anterior * meses if meses > 1 else consumo_anterior
        variacao = ((consumo_total - ant_ajustado) / ant_ajustado) * 100
        if variacao > 20:
            partes.append(f"📈 ATENÇÃO: Consumo aumentou {variacao:.0f}% em relação ao período anterior ({ant_ajustado:.1f} → {consumo_total:.1f} kWh). O sistema solar está funcionando normalmente — verifique novos equipamentos ligados.")
        elif variacao < -10:
            partes.append(f"✅ Consumo reduziu {abs(variacao):.0f}% — ótimo!")

    return " ".join(partes)

# ==================== CLIMA ====================

def consultar_clima(latitude: float, longitude: float, data_inicio: str, data_fim: str) -> dict:
    try:
        url = (f"https://api.open-meteo.com/v1/forecast"
               f"?latitude={latitude}&longitude={longitude}"
               f"&daily=cloudcover_mean,precipitation_sum"
               f"&timezone=America/Sao_Paulo"
               f"&start_date={data_inicio}&end_date={data_fim}")
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            daily = data.get("daily", {})
            datas = daily.get("time", [])
            nuvens = daily.get("cloudcover_mean", [])
            chuva = daily.get("precipitation_sum", [])
            resultado = {}
            for i, d in enumerate(datas):
                resultado[d] = {
                    "dia_nublado": (nuvens[i] > 70) if i < len(nuvens) and nuvens[i] is not None else False,
                    "dia_chuvoso": (chuva[i] > 5) if i < len(chuva) and chuva[i] is not None else False
                }
            return resultado
    except Exception:
        pass
    return {}

# ==================== FOXESS ====================

def foxess_chamar_api(api_key: str, path: str, body: dict):
    timestamp = str(int(time.time() * 1000))
    signature_raw = fr"/{path}\r\n{api_key}\r\n{timestamp}"
    signature = hashlib.md5(signature_raw.encode("utf-8")).hexdigest()
    headers = {"Token": api_key, "Lang": "en", "Timestamp": timestamp, "Signature": signature,
               "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.post(f"https://www.foxesscloud.com/{path}", json=body, headers=headers, timeout=30)
        return resp.json()
    except Exception as e:
        return {"errno": 99999, "msg": str(e)}

def foxess_get_realtime(api_key: str, serial: str) -> dict:
    return foxess_chamar_api(api_key, "op/v0/device/real/query", {"sn": serial, "variables": []})

def foxess_get_mensal(api_key: str, serial: str, ano: int, mes: int) -> float:
    """Retorna geração total real do mês diretamente da FoxESS — sem estimativa por dia."""
    body = {"sn": serial, "year": ano, "month": mes, "dimension": "month", "variables": ["generation"]}
    try:
        r = foxess_chamar_api(api_key, "op/v0/device/report/query", body)
        if r.get("errno") == 0:
            for item in r.get("result", []):
                if isinstance(item, dict) and item.get("variable") == "generation":
                    return round(sum(v for v in item.get("values", []) if v), 2)
    except Exception:
        pass
    return 0.0

# ==================== BANCO HELPERS ====================

def salvar_geracao_mensal(cliente_id: int, ano: int, mes: int, geracao_kwh: float, fonte: str = "foxess"):
    """
    CORREÇÃO: Salva o total mensal REAL diretamente, sem distribuir por dia.
    Isso garante que o sistema mostre o mesmo valor que o FoxESS Cloud.
    """
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO historico_geracao_mensal (cliente_id, ano, mes, geracao_kwh, fonte)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (cliente_id, ano, mes)
        DO UPDATE SET geracao_kwh = EXCLUDED.geracao_kwh, fonte = EXCLUDED.fonte
    """, (cliente_id, ano, mes, geracao_kwh, fonte))
    conn.commit()
    cur.close()
    conn.close()

def buscar_geracao_dia(cliente_id: int, data_busca):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(SUM(geracao_kwh),0) FROM historico_geracao WHERE cliente_id=%s AND data=%s", (cliente_id, data_busca))
    r = cur.fetchone()
    cur.close()
    conn.close()
    return float(r[0]) if r[0] else 0.0

def buscar_geracao_periodo(cliente_id: int, data_inicio, data_fim):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("SELECT data, SUM(geracao_kwh) FROM historico_geracao WHERE cliente_id=%s AND data BETWEEN %s AND %s GROUP BY data ORDER BY data", (cliente_id, data_inicio, data_fim))
    r = cur.fetchall()
    cur.close()
    conn.close()
    return [{"data": str(x[0]), "total_kwh": float(x[1])} for x in r]

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
        cliente_id, dados.get("mes_referencia"), data_venc,
        dados.get("consumo_bruto_kwh"), dados.get("consumo_kwh"),
        dados.get("energia_injetada_kwh"), dados.get("saldo_acumulado_kwh"),
        dados.get("valor_fatura"), dados.get("modalidade_tarifaria"),
        dados.get("status_sistema"), dados.get("percentual_gerado"),
        dados.get("leitura_por_media", False), dados.get("meses_acumulados", 1),
        dados.get("mensagem_cliente"), dados.get("geracao_total_kwh"),
        dados.get("consumo_instantaneo_kwh"), dados.get("consumo_total_kwh"),
        dados.get("consumo_anterior_kwh"), dados.get("aumento_consumo_pct"),
        dados.get("analise_consumo")
    ))
    conta_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return conta_id

# ==================== ROTAS ====================

@app.get("/")
def inicio():
    return {"status": "Solar Portal API funcionando!"}

@app.post("/auth/registro")
def registrar_integrador(dados: dict):
    conn = conectar_banco()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO integradores (nome,email,telefone,senha_hash) VALUES (%s,%s,%s,%s) RETURNING id,nome,email",
                    (dados["nome"], dados["email"], dados.get("telefone"), criptografar_senha(dados["senha"])))
        i = cur.fetchone()
        conn.commit()
        return {"id": i[0], "nome": i[1], "email": i[2]}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        conn.close()

@app.post("/auth/login")
def login(dados: dict):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("SELECT id,nome,email,senha_hash FROM integradores WHERE email=%s AND ativo=TRUE", (dados["email"],))
    i = cur.fetchone()
    cur.close()
    conn.close()
    if not i or not verificar_senha(dados["senha"], i[3]):
        raise HTTPException(status_code=401, detail="Email ou senha incorretos")
    return {"token": criar_token({"id": i[0], "nome": i[1], "email": i[2]}), "nome": i[1], "email": i[2]}

@app.get("/auth/me")
def meu_perfil(integrador: dict = Depends(obter_integrador_atual)):
    return integrador

@app.get("/clientes")
def listar_clientes(integrador: dict = Depends(obter_integrador_atual)):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("SELECT id,nome,numero_uc,distribuidora,tipo_gd,marca_inversor,serial_inversor FROM clientes WHERE integrador_id=%s", (integrador["id"],))
    clientes = cur.fetchall()
    cur.close()
    conn.close()
    return [{"id":c[0],"nome":c[1],"numero_uc":c[2],"distribuidora":c[3],"tipo_gd":c[4],"marca_inversor":c[5],"serial_inversor":c[6]} for c in clientes]

@app.post("/clientes")
def cadastrar_cliente(dados: dict, integrador: dict = Depends(obter_integrador_atual)):
    conn = conectar_banco()
    cur = conn.cursor()
    try:
        token_acesso = secrets.token_urlsafe(32)
        cur.execute("INSERT INTO clientes (integrador_id,nome,email,telefone,numero_uc,distribuidora,tipo_gd,token_acesso) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id,nome,token_acesso",
                    (integrador["id"],dados.get("nome"),dados.get("email"),dados.get("telefone"),dados.get("numero_uc"),dados.get("distribuidora"),dados.get("tipo_gd"),token_acesso))
        c = cur.fetchone()
        conn.commit()
        return {"id":c[0],"nome":c[1],"token_acesso":c[2]}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        conn.close()

@app.get("/clientes/{cliente_id}/obter-token")
def obter_token(cliente_id: int, integrador: dict = Depends(obter_integrador_atual)):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("SELECT token_acesso FROM clientes WHERE id=%s AND integrador_id=%s", (cliente_id, integrador["id"]))
    c = cur.fetchone()
    cur.close()
    conn.close()
    if not c:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")
    return {"token_acesso": c[0]}

@app.get("/clientes/{cliente_id}/contas")
def listar_contas(cliente_id: int, integrador: dict = Depends(obter_integrador_atual)):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("""
        SELECT c.id,c.mes_referencia,c.data_vencimento,c.consumo_kwh,c.energia_injetada_kwh,
               c.saldo_acumulado_kwh,c.valor_fatura,c.status_sistema,c.percentual_gerado,
               c.mensagem_cliente,c.geracao_total_kwh,c.consumo_instantaneo_kwh,
               c.consumo_total_kwh,c.analise_consumo,c.consumo_bruto_kwh,c.criado_em,
               c.leitura_por_media,c.meses_acumulados
        FROM contas c JOIN clientes cl ON cl.id=c.cliente_id
        WHERE c.cliente_id=%s AND cl.integrador_id=%s ORDER BY c.criado_em DESC
    """, (cliente_id, integrador["id"]))
    contas = cur.fetchall()
    cur.close()
    conn.close()
    return [{"id":c[0],"mes_referencia":c[1],"data_vencimento":str(c[2]),"consumo_kwh":float(c[3] or 0),
             "energia_injetada_kwh":float(c[4] or 0),"saldo_acumulado_kwh":float(c[5] or 0),
             "valor_fatura":float(c[6] or 0),"status_sistema":c[7],"percentual_gerado":float(c[8] or 0),
             "mensagem_cliente":c[9],"geracao_total_kwh":float(c[10] or 0),"consumo_instantaneo_kwh":float(c[11] or 0),
             "consumo_total_kwh":float(c[12] or 0),"analise_consumo":c[13],"consumo_bruto_kwh":float(c[14] or 0),
             "criado_em":str(c[15]),"leitura_por_media":c[16],"meses_acumulados":c[17]} for c in contas]

@app.get("/clientes/{cliente_id}/contas-publico")
def listar_contas_publico(cliente_id: int):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("""
        SELECT id,mes_referencia,data_vencimento,consumo_kwh,energia_injetada_kwh,
               saldo_acumulado_kwh,valor_fatura,status_sistema,percentual_gerado,
               mensagem_cliente,consumo_bruto_kwh,geracao_total_kwh,
               consumo_instantaneo_kwh,consumo_total_kwh,analise_consumo,
               consumo_anterior_kwh,aumento_consumo_pct,criado_em,
               leitura_por_media,meses_acumulados
        FROM contas WHERE cliente_id=%s ORDER BY criado_em DESC LIMIT 12
    """, (cliente_id,))
    contas = cur.fetchall()
    cur.close()
    conn.close()
    return [{"id":c[0],"mes_referencia":c[1],"data_vencimento":str(c[2]),"consumo_kwh":float(c[3] or 0),
             "energia_injetada_kwh":float(c[4] or 0),"saldo_acumulado_kwh":float(c[5] or 0),
             "valor_fatura":float(c[6] or 0),"status_sistema":c[7],"percentual_gerado":float(c[8] or 0),
             "mensagem_cliente":c[9],"consumo_bruto_kwh":float(c[10] or 0),"geracao_total_kwh":float(c[11] or 0),
             "consumo_instantaneo_kwh":float(c[12] or 0),"consumo_total_kwh":float(c[13] or 0),
             "analise_consumo":c[14],"consumo_anterior_kwh":float(c[15] or 0),
             "aumento_consumo_pct":float(c[16] or 0),"criado_em":str(c[17]),
             "leitura_por_media":c[18],"meses_acumulados":c[19]} for c in contas]

@app.delete("/contas/{conta_id}")
def excluir_conta(conta_id: int):
    conn = conectar_banco()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM contas WHERE id=%s RETURNING id", (conta_id,))
        deletado = cur.fetchone()
        conn.commit()
        if not deletado:
            raise HTTPException(status_code=404, detail="Conta não encontrada")
        return {"sucesso": True, "id": conta_id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        conn.close()

@app.get("/portal/{token_acesso}")
def portal_cliente(token_acesso: str):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("SELECT id,nome,numero_uc,distribuidora,tipo_gd FROM clientes WHERE token_acesso=%s", (token_acesso,))
    c = cur.fetchone()
    cur.close()
    conn.close()
    if not c:
        raise HTTPException(status_code=404, detail="Link inválido")
    return {"id":c[0],"nome":c[1],"numero_uc":c[2],"distribuidora":c[3],"tipo_gd":c[4]}

@app.put("/clientes/{cliente_id}/projeto")
def atualizar_projeto(cliente_id: int, dados: dict, integrador: dict = Depends(obter_integrador_atual)):
    conn = conectar_banco()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE clientes SET potencia_kwp=%s,latitude=%s,longitude=%s,data_instalacao=%s,performance_ratio=%s WHERE id=%s AND integrador_id=%s RETURNING id,nome",
                    (dados.get("potencia_kwp"),dados.get("latitude"),dados.get("longitude"),dados.get("data_instalacao"),dados.get("performance_ratio",0.80),cliente_id,integrador["id"]))
        c = cur.fetchone()
        conn.commit()
        if not c:
            raise HTTPException(status_code=404, detail="Cliente não encontrado")
        return {"sucesso":True,"id":c[0],"nome":c[1]}
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
        cur.execute("UPDATE clientes SET marca_inversor=%s,serial_inversor=%s,api_key_inversor=%s WHERE id=%s AND integrador_id=%s RETURNING id,nome",
                    (dados.get("marca_inversor"),dados.get("serial_inversor"),dados.get("api_key_inversor"),cliente_id,integrador["id"]))
        c = cur.fetchone()
        conn.commit()
        if not c:
            raise HTTPException(status_code=404, detail="Cliente não encontrado")
        return {"sucesso":True,"id":c[0],"nome":c[1]}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        conn.close()

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

        conn = conectar_banco()
        cur = conn.cursor()
        cur.execute("SELECT marca_inversor,serial_inversor,api_key_inversor FROM clientes WHERE id=%s", (cliente_id,))
        inversor = cur.fetchone()
        cur.execute("SELECT consumo_total_kwh,consumo_bruto_kwh FROM contas WHERE cliente_id=%s ORDER BY criado_em DESC LIMIT 1", (cliente_id,))
        conta_ant = cur.fetchone()
        cur.close()
        conn.close()

        consumo_anterior = float(conta_ant[0] or conta_ant[1] or 0) if conta_ant else None

        geracao_total_kwh = None
        if inversor and inversor[0] and inversor[0].lower() == "foxess" and inversor[2]:
            try:
                mes_ref = dados.get("mes_referencia", "")
                meses_map = {"jan":1,"fev":2,"mar":3,"abr":4,"mai":5,"jun":6,"jul":7,"ago":8,"set":9,"out":10,"nov":11,"dez":12}
                mes_num = next((n for k,n in meses_map.items() if k in mes_ref.lower()), None)
                anos = re.findall(r'\d{4}', mes_ref)
                ano_num = int(anos[0]) if anos else datetime.now().year
                meses_acumulados = int(dados.get("meses_acumulados") or 1)

                if mes_num:
                    geracao_total = 0.0

                    # Buscar geração de cada mês acumulado separadamente
                    for i in range(meses_acumulados):
                        # Calcular mês e ano retroativamente
                        mes_busca = mes_num - i
                        ano_busca = ano_num
                        if mes_busca <= 0:
                            mes_busca += 12
                            ano_busca -= 1

                        g = foxess_get_mensal(inversor[2], inversor[1], ano_busca, mes_busca)
                        if g > 0:
                            geracao_total += g
                            salvar_geracao_mensal(cliente_id, ano_busca, mes_busca, g)

                    if geracao_total > 0:
                        geracao_total_kwh = round(geracao_total, 2)

            except Exception:
                pass

        analise = calcular_analise_consumo(dados, geracao_total_kwh)
        dados.update(analise)

        if consumo_anterior and consumo_anterior > 0 and dados.get("consumo_total_kwh"):
            aumento_pct = ((dados["consumo_total_kwh"] - consumo_anterior) / consumo_anterior) * 100
            dados["consumo_anterior_kwh"] = consumo_anterior
            dados["aumento_consumo_pct"] = round(aumento_pct, 1)

        dados["analise_consumo"] = gerar_mensagem_consumo(dados, consumo_anterior)
        conta_id = salvar_no_banco(dados, cliente_id)
        dados["id"] = conta_id
        return {"sucesso": True, "conta": dados}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        os.unlink(tmp_path)

@app.get("/clientes/{cliente_id}/geracao-esperada")
def geracao_esperada(cliente_id: int):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("SELECT potencia_kwp,latitude,longitude,performance_ratio FROM clientes WHERE id=%s", (cliente_id,))
    c = cur.fetchone()
    cur.close()
    conn.close()
    if not c or not c[0]:
        raise HTTPException(status_code=404, detail="Projeto solar não cadastrado")
    kwp = float(c[0])
    pr = float(c[3]) if c[3] else 0.80
    hsp = {1:5.2,2:5.4,3:5.1,4:4.8,5:4.5,6:4.3,7:4.5,8:5.0,9:4.9,10:4.8,11:4.9,12:5.0}
    meses = ["Jan","Fev","Mar","Abr","Mai","Jun","Jul","Ago","Set","Out","Nov","Dez"]
    dias = [31,28,31,30,31,30,31,31,30,31,30,31]
    geracao = [{"mes":meses[i],"mes_num":i+1,"hsp":hsp[i+1],"kwh_esperado":round(kwp*hsp[i+1]*dias[i]*pr,2)} for i in range(12)]
    return {"potencia_kwp":kwp,"performance_ratio":pr,"geracao_mensal":geracao,"total_anual":round(sum(g["kwh_esperado"] for g in geracao),2)}

@app.get("/clientes/{cliente_id}/monitoramento")
def monitoramento(cliente_id: int):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("SELECT marca_inversor,serial_inversor,api_key_inversor FROM clientes WHERE id=%s", (cliente_id,))
    c = cur.fetchone()
    cur.close()
    conn.close()
    if not c or not c[2]:
        raise HTTPException(status_code=404, detail="Inversor não configurado")
    marca, serial, api_key = c
    if marca.lower() != "foxess":
        raise HTTPException(status_code=400, detail="Marca não suportada ainda")
    data = foxess_get_realtime(api_key, serial)
    if data.get("errno") != 0:
        raise HTTPException(status_code=400, detail=f"Erro Foxess: {data.get('msg')}")
    resultado = data.get("result", [])
    if not resultado:
        raise HTTPException(status_code=404, detail="Sem dados")
    variaveis = {}
    for item in resultado[0].get("datas", []):
        try:
            variaveis[item["variable"]] = float(item.get("value") or 0)
        except:
            variaveis[item["variable"]] = 0.0
    return {"marca":marca,"serial":serial,"status":"online",
            "geracao_atual_kw":variaveis.get("pvPower",0.0),
            "injetado_rede_kw":variaveis.get("feedinPower",0.0),
            "consumo_rede_kw":variaveis.get("gridConsumptionPower",0.0),
            "consumo_casa_kw":variaveis.get("loadsPower",0.0),
            "potencia_total_kw":variaveis.get("generationPower",0.0),
            "timestamp":datetime.utcnow().isoformat()}

@app.get("/clientes/{cliente_id}/monitoramento/mensal")
def monitoramento_mensal(cliente_id: int):
    """
    CORRIGIDO: Busca totais mensais reais da tabela historico_geracao_mensal.
    Isso garante que o valor mostrado seja igual ao FoxESS Cloud.
    """
    nomes = {1:"Jan",2:"Fev",3:"Mar",4:"Abr",5:"Mai",6:"Jun",7:"Jul",8:"Ago",9:"Set",10:"Out",11:"Nov",12:"Dez"}
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("SELECT nome,marca_inversor,serial_inversor,api_key_inversor FROM clientes WHERE id=%s", (cliente_id,))
    cliente = cur.fetchone()

    # Primeiro tenta tabela mensal correta
    cur.execute("""
        SELECT ano, mes, geracao_kwh FROM historico_geracao_mensal
        WHERE cliente_id=%s AND (ano > EXTRACT(YEAR FROM CURRENT_DATE)-2)
        ORDER BY ano, mes
    """, (cliente_id,))
    resultados = cur.fetchall()
    cur.close()
    conn.close()

    if resultados:
        mensal = [{"ano":r[0],"mes_num":r[1],"mes":nomes[r[1]],"geracao_kwh":float(r[2])} for r in resultados if float(r[2])>0]
        if mensal:
            return {"cliente_id":cliente_id,"mensal":mensal[-12:],"total_periodo":round(sum(m["geracao_kwh"] for m in mensal),2),"fonte":"banco"}

    # Fallback: buscar direto da FoxESS e salvar na tabela correta
    if not cliente or not cliente[1] or cliente[1].lower()!="foxess" or not cliente[3]:
        return {"cliente_id":cliente_id,"mensal":[],"total_periodo":0,"fonte":"nenhuma"}

    serial, api_key = cliente[2], cliente[3]
    hoje = datetime.now()
    dados_por_mes = []

    for i in range(12):
        dm = hoje - timedelta(days=30*i)
        total = foxess_get_mensal(api_key, serial, dm.year, dm.month)
        if total > 0:
            salvar_geracao_mensal(cliente_id, dm.year, dm.month, total)
            dados_por_mes.append({"ano":dm.year,"mes_num":dm.month,"mes":nomes[dm.month],"geracao_kwh":total})

    if dados_por_mes:
        dados_por_mes = sorted(dados_por_mes, key=lambda x:(x["ano"],x["mes_num"]))
        return {"cliente_id":cliente_id,"mensal":dados_por_mes,"total_periodo":round(sum(m["geracao_kwh"] for m in dados_por_mes),2),"fonte":"foxess"}

    return {"cliente_id":cliente_id,"mensal":[],"total_periodo":0,"fonte":"nenhuma"}

@app.get("/clientes/{cliente_id}/verificar-anomalias")
def verificar_anomalias(cliente_id: int):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("SELECT nome,latitude,longitude,potencia_kwp,performance_ratio FROM clientes WHERE id=%s", (cliente_id,))
    cliente = cur.fetchone()
    cur.close()
    conn.close()
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")

    nome, latitude, longitude, potencia_kwp, performance_ratio = cliente
    hoje = date.today()
    ontem = hoje - timedelta(days=1)
    g_ontem = buscar_geracao_dia(cliente_id, ontem)
    ultimos7 = buscar_geracao_periodo(cliente_id, hoje-timedelta(days=8), ontem)
    media7 = sum(d["total_kwh"] for d in ultimos7)/len(ultimos7) if ultimos7 else 0.0

    media_diaria_esperada = 0.0
    if potencia_kwp and performance_ratio:
        hsp = {1:5.2,2:5.4,3:5.1,4:4.8,5:4.5,6:4.3,7:4.5,8:5.0,9:4.9,10:4.8,11:4.9,12:5.0}
        media_diaria_esperada = float(potencia_kwp) * hsp.get(hoje.month, 5.0) * float(performance_ratio)

    alerta_consecutivo = None
    if media_diaria_esperada > 0 and len(ultimos7) >= 3:
        ultimos3 = ultimos7[-3:]
        dias_baixos = [d for d in ultimos3 if d["total_kwh"] < media_diaria_esperada * 0.7]
        if len(dias_baixos) == 3:
            clima = {}
            if latitude and longitude:
                clima = consultar_clima(float(latitude), float(longitude), ultimos3[0]["data"], ultimos3[-1]["data"])
            dias_ruins = sum(1 for d in ultimos3 if clima.get(d["data"], {}).get("dia_nublado") or clima.get(d["data"], {}).get("dia_chuvoso"))
            media_3d = sum(d["total_kwh"] for d in ultimos3) / 3
            if dias_ruins >= 2:
                alerta_consecutivo = {"tipo":"informativo","icone":"☁️",
                    "titulo":"Geração Baixa por Condições Climáticas",
                    "mensagem":f"Geração abaixo da média por 3 dias ({media_3d:.1f} kWh/dia vs esperado {media_diaria_esperada:.1f} kWh/dia). O período teve nebulosidade ou chuva — isso é normal!",
                    "acao":"Aguardar melhora climática. Se continuar após dias ensolarados, contate o integrador."}
            else:
                alerta_consecutivo = {"tipo":"atencao","icone":"⚠️",
                    "titulo":"3 Dias Consecutivos com Geração Abaixo da Média",
                    "mensagem":f"Geração abaixo do esperado por 3 dias ({media_3d:.1f} kWh/dia vs esperado {media_diaria_esperada:.1f} kWh/dia). O tempo estava bom — pode ser problema técnico.",
                    "acao":"Verificar sombra, sujeira nos painéis ou problema no inversor. Contate seu integrador."}

    alertas = []
    if alerta_consecutivo:
        alertas.append(alerta_consecutivo)

    if g_ontem < 0.5:
        alertas.append({"tipo":"urgente","icone":"🔴","titulo":"Sistema Parado",
            "mensagem":f"Seu sistema não gerou energia significativa ontem ({g_ontem:.1f} kWh).",
            "acao":"Verificar disjuntor CA e acionar o técnico imediatamente."})
    elif media7 > 0 and g_ontem < media7*0.4:
        clima_ontem = {}
        if latitude and longitude:
            clima_ontem = consultar_clima(float(latitude), float(longitude), str(ontem), str(ontem))
        dia_ruim = clima_ontem.get(str(ontem), {}).get("dia_nublado") or clima_ontem.get(str(ontem), {}).get("dia_chuvoso")
        if dia_ruim:
            alertas.append({"tipo":"informativo","icone":"☁️","titulo":"Geração Baixa — Dia Nublado/Chuvoso",
                "mensagem":f"Ontem seu sistema gerou {g_ontem:.1f} kWh — condições climáticas desfavoráveis. Normal!",
                "acao":"Monitorar nos próximos dias ensolarados."})
        else:
            alertas.append({"tipo":"atencao","icone":"⚠️","titulo":"Geração Muito Abaixo do Normal",
                "mensagem":f"Ontem: {g_ontem:.1f} kWh vs média 7 dias: {media7:.1f} kWh. Queda de {((media7-g_ontem)/media7*100):.0f}%.",
                "acao":"Verificar sombreamento, sujeira ou problema no inversor."})
    elif media7 > 0 and g_ontem < media7*0.7:
        alertas.append({"tipo":"informativo","icone":"📉","titulo":"Geração Abaixo da Média",
            "mensagem":f"Ontem: {g_ontem:.1f} kWh vs média: {media7:.1f} kWh.",
            "acao":"Monitorar. Se continuar, agendar limpeza preventiva."})

    if not alertas and g_ontem > 0:
        alertas.append({"tipo":"normal","icone":"✅","titulo":"Sistema Operando Normalmente",
            "mensagem":f"Ontem seu sistema gerou {g_ontem:.1f} kWh.","acao":"Nenhuma ação necessária."})

    if media7 == 0 and g_ontem == 0:
        alertas = [{"tipo":"informativo","icone":"📡","titulo":"Coletando Dados",
            "mensagem":"Aguardando coleta de dados de geração.","acao":"Em alguns dias teremos estatísticas."}]

    return {"cliente_id":cliente_id,"cliente_nome":nome,"data_analise":hoje.isoformat(),
            "geracao_ontem_kwh":round(g_ontem,1),"media_7_dias_kwh":round(media7,1),
            "media_diaria_esperada_kwh":round(media_diaria_esperada,1),"alertas":alertas}
