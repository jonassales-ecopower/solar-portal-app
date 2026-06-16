import hashlib
import time
import json
import re
import tempfile
import os
import secrets
import asyncio
import psycopg2
import requests
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, date
from calendar import monthrange
from fastapi import Depends, FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from openai import OpenAI
from auth import criptografar_senha, verificar_senha, criar_token, verificar_token
from extracao import (extrair_medicoes_medidor, normalizar_mes_referencia,
                      extrair_mes_referencia, extrair_datas_leitura, somar_geracao_periodo)
import PyPDF2

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DATABASE_URL = os.environ.get("DATABASE_URL", "")
OPENROUTER_KEY = os.environ.get("OPENROUTER_KEY", "")
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
ALERT_EMAIL_FROM = os.environ.get("ALERT_EMAIL_FROM", "alertas@solar-portal.com.br")

# ==================== OFFLINE ALERT ====================

def inicializar_banco():
    conn = conectar_banco()
    cur = conn.cursor()
    try:
        cur.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS ultima_leitura_em TIMESTAMP")
        cur.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS alerta_offline_em TIMESTAMP")
        cur.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS inversor_usuario TEXT")
        cur.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS inversor_senha TEXT")
        cur.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS senha_hash TEXT")
        cur.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS reset_token TEXT")
        cur.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS reset_token_exp TIMESTAMP")
        cur.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS tarifa_kwh DECIMAL(6,4)")
        cur.execute("ALTER TABLE clientes ADD COLUMN IF NOT EXISTS consumo_medio_antes_kwh DECIMAL(8,2)")
        cur.execute("ALTER TABLE integradores ADD COLUMN IF NOT EXISTS alertas_diario_ativo BOOLEAN DEFAULT TRUE")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rateio_beneficiarios (
                id SERIAL PRIMARY KEY,
                cliente_id INTEGER NOT NULL,
                nome TEXT NOT NULL,
                numero_uc TEXT,
                distribuidora TEXT,
                percentual DECIMAL(5,2) NOT NULL DEFAULT 0,
                ativo BOOLEAN DEFAULT TRUE,
                criado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS contas_beneficiario (
                id SERIAL PRIMARY KEY,
                beneficiario_id INTEGER NOT NULL,
                mes_referencia TEXT,
                creditos_recebidos_kwh DECIMAL(8,2),
                consumo_kwh DECIMAL(8,2),
                consumo_bruto_kwh DECIMAL(8,2),
                saldo_anterior_kwh DECIMAL(8,2),
                saldo_resultante_kwh DECIMAL(8,2),
                valor_fatura DECIMAL(10,2),
                criado_em TIMESTAMP DEFAULT NOW(),
                UNIQUE (beneficiario_id, mes_referencia)
            )
        """)
        # Migração: tabelas criadas por versões antigas não têm o UNIQUE acima
        # (CREATE TABLE IF NOT EXISTS não altera tabela existente) e o upsert
        # ON CONFLICT (beneficiario_id, mes_referencia) falha sem ele.
        # Remove duplicatas mantendo a mais recente e cria o índice único.
        cur.execute("""
            DELETE FROM contas_beneficiario a USING contas_beneficiario b
            WHERE a.id < b.id AND a.beneficiario_id = b.beneficiario_id
              AND a.mes_referencia IS NOT DISTINCT FROM b.mes_referencia
        """)
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS contas_beneficiario_unico
            ON contas_beneficiario (beneficiario_id, mes_referencia)
        """)
        # Tabela de cache de irradiância solar por cidade (PVGIS)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cidades_hsp (
                id SERIAL PRIMARY KEY,
                nome_cidade TEXT UNIQUE NOT NULL,
                latitude DECIMAL(10,6),
                longitude DECIMAL(10,6),
                hsp_mensal TEXT NOT NULL,
                media_anual DECIMAL(5,2),
                criado_em TIMESTAMP DEFAULT NOW(),
                atualizado_em TIMESTAMP DEFAULT NOW()
            )
        """)
        # Adiciona coluna cidade em clientes se não existir
        cur.execute("""ALTER TABLE clientes ADD COLUMN IF NOT EXISTS cidade TEXT""")
        conn.commit()
    except Exception:
        conn.rollback()
    finally:
        cur.close()
        conn.close()

# ==================== IRRADIÂNCIA SOLAR (PVGIS + FALLBACK) ====================

# Tabela de HSP por CIDADE específica (override de estado)
# Valores em plano inclinado (ângulo = latitude), validados com integrador
HSP_FALLBACK_CIDADE = {
    "EXTREMA - MG": [4.96, 5.16, 4.94, 4.92, 4.64, 4.57, 4.77, 5.31, 4.93, 4.93, 5.06, 4.72],  # Jan-Dez, plano inclinado
}

# Tabela de HSP médio anual por região brasileira (fallback quando PVGIS indisponível)
# Fonte: CRESESB/INPE, ABNT NBR ISO/IEC 61853-3
HSP_FALLBACK_ESTADO = {
    "AC": [4.5, 4.3, 4.4, 4.3, 4.2, 3.8, 3.9, 4.3, 4.5, 4.8, 4.7, 4.6],  # Acre
    "AL": [5.2, 4.8, 4.9, 4.6, 4.2, 3.9, 4.1, 4.5, 4.8, 5.1, 5.3, 5.4],  # Alagoas
    "AP": [3.9, 3.8, 4.0, 4.0, 4.0, 3.8, 3.9, 4.2, 4.5, 4.5, 4.2, 4.0],  # Amapá
    "AM": [3.8, 3.7, 3.8, 3.7, 3.8, 3.5, 3.6, 3.9, 4.2, 4.4, 4.1, 3.9],  # Amazonas
    "BA": [5.5, 5.0, 5.1, 4.6, 4.2, 3.9, 4.1, 4.6, 5.0, 5.4, 5.6, 5.7],  # Bahia
    "CE": [5.2, 4.8, 4.9, 4.5, 4.0, 3.7, 3.9, 4.4, 4.8, 5.2, 5.4, 5.5],  # Ceará
    "DF": [5.5, 5.1, 5.0, 4.5, 4.0, 3.7, 3.9, 4.5, 4.9, 5.3, 5.6, 5.7],  # Distrito Federal
    "ES": [5.2, 4.9, 4.9, 4.4, 3.9, 3.6, 3.8, 4.3, 4.8, 5.1, 5.4, 5.5],  # Espírito Santo
    "GO": [5.4, 5.0, 5.0, 4.4, 3.9, 3.6, 3.8, 4.4, 4.9, 5.3, 5.5, 5.6],  # Goiás
    "MA": [5.0, 4.7, 4.8, 4.5, 4.2, 3.9, 4.0, 4.4, 4.7, 5.0, 5.1, 5.2],  # Maranhão
    "MT": [5.2, 4.9, 4.9, 4.3, 3.9, 3.6, 3.7, 4.3, 4.8, 5.2, 5.4, 5.5],  # Mato Grosso
    "MS": [5.4, 5.1, 5.0, 4.4, 3.9, 3.5, 3.7, 4.4, 4.9, 5.3, 5.6, 5.7],  # Mato Grosso do Sul
    "MG": [5.3, 5.0, 4.9, 4.3, 3.8, 3.5, 3.7, 4.3, 4.8, 5.2, 5.5, 5.6],  # Minas Gerais (Extrema está aqui)
    "PA": [4.0, 3.9, 4.0, 4.0, 4.0, 3.8, 3.9, 4.2, 4.5, 4.6, 4.2, 4.0],  # Pará
    "PB": [5.2, 4.8, 4.9, 4.5, 4.0, 3.7, 3.9, 4.4, 4.8, 5.2, 5.4, 5.5],  # Paraíba
    "PR": [5.0, 4.7, 4.7, 4.1, 3.6, 3.2, 3.4, 4.0, 4.5, 4.9, 5.2, 5.3],  # Paraná
    "PE": [5.1, 4.7, 4.9, 4.5, 4.1, 3.8, 4.0, 4.5, 4.8, 5.1, 5.2, 5.3],  # Pernambuco
    "PI": [5.1, 4.8, 4.9, 4.5, 4.1, 3.8, 4.0, 4.4, 4.8, 5.1, 5.3, 5.4],  # Piauí
    "RJ": [5.0, 4.8, 4.8, 4.3, 3.8, 3.5, 3.7, 4.2, 4.7, 5.0, 5.3, 5.4],  # Rio de Janeiro
    "RN": [5.2, 4.8, 4.9, 4.5, 4.0, 3.7, 3.9, 4.4, 4.8, 5.2, 5.4, 5.5],  # Rio Grande do Norte
    "RS": [4.6, 4.4, 4.4, 3.9, 3.4, 2.9, 3.1, 3.7, 4.3, 4.7, 4.9, 5.0],  # Rio Grande do Sul
    "RO": [4.4, 4.2, 4.3, 4.2, 4.2, 3.8, 3.9, 4.3, 4.6, 4.9, 4.7, 4.6],  # Rondônia
    "RR": [3.9, 3.8, 3.9, 3.9, 4.0, 3.8, 3.9, 4.2, 4.5, 4.6, 4.3, 4.0],  # Roraima
    "SC": [4.8, 4.6, 4.6, 4.0, 3.5, 3.0, 3.2, 3.8, 4.4, 4.8, 5.0, 5.1],  # Santa Catarina
    "SP": [5.1, 4.8, 4.8, 4.2, 3.8, 3.4, 3.6, 4.2, 4.7, 5.0, 5.3, 5.4],  # São Paulo
    "SE": [5.2, 4.8, 4.9, 4.6, 4.2, 3.9, 4.1, 4.5, 4.8, 5.1, 5.3, 5.4],  # Sergipe
    "TO": [5.2, 4.8, 4.9, 4.5, 4.1, 3.8, 3.9, 4.4, 4.7, 5.1, 5.3, 5.4],  # Tocantins
}

def buscar_hsp_openmeteo(latitude: float, longitude: float):
    """Busca HSP via Open-Meteo (irradiância média diária em MJ/m²)."""
    try:
        url = f"https://archive-api.open-meteo.com/v1/archive?latitude={latitude}&longitude={longitude}&start_date=2023-01-01&end_date=2023-12-31&monthly=shortwave_radiation_sum&timezone=auto"
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return None

        data = r.json()
        if "error" in data or "monthly" not in data:
            return None

        # Converte MJ/m²/mês para kWh/m²/dia (HSP)
        monthly = data["monthly"].get("shortwave_radiation_sum", [])
        if not monthly or len(monthly) < 12:
            return None

        hsp = []
        dias_mes = [31, 28.25, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
        for i, mj in enumerate(monthly[:12]):
            if mj is None:
                return None
            kwh_m2_dia = (float(mj) / 3.6) / dias_mes[i]  # MJ → kWh, dividir por dias do mês
            hsp.append(round(kwh_m2_dia, 2))

        return hsp
    except Exception as e:
        print(f"[OpenMeteo] Erro: {e}")
        return None

def buscar_hsp_nasapower(latitude: float, longitude: float):
    """Busca HSP via NASA POWER (ALLSKY_SFC_SW_DWN em kWh/m²/dia)."""
    try:
        url = f"https://power.larc.nasa.gov/api/v1/monthly?latitude={latitude}&longitude={longitude}&parameters=ALLSKY_SFC_SW_DWN&format=json&start=2023&end=2023&community=SB"
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return None

        data = r.json()
        if "properties" not in data or "ALLSKY_SFC_SW_DWN" not in data["properties"]:
            return None

        monthly_data = data["properties"]["ALLSKY_SFC_SW_DWN"]
        if not monthly_data or len(monthly_data) < 12:
            return None

        # NASA POWER já retorna em kWh/m²/dia
        hsp = [round(float(monthly_data[f"{m+1:02d}"]), 2) for m in range(12)]
        return hsp
    except Exception as e:
        print(f"[NASA POWER] Erro: {e}")
        return None

def buscar_hsp_pvgis_raw(latitude: float, longitude: float):
    """Busca HSP via PVGIS (backup, cobertura limitada)."""
    try:
        url = "https://re.jrc.ec.europa.eu/api/v5_2/solarresource"
        params = {"lat": latitude, "lon": longitude, "outputformat": "json"}
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            return None

        data = r.json()
        monthly = data.get("monthly", [])
        if not monthly or len(monthly) < 12:
            return None

        hsp = []
        for m in monthly[:12]:
            rad = float(m.get("rad", 5.0))
            hsp.append(round(rad, 2))
        return hsp
    except Exception as e:
        print(f"[PVGIS] Erro: {e}")
        return None

def buscar_hsp_pvgis(cidade: str, latitude: float = None, longitude: float = None):
    """
    Busca HSP mensal (horas de pico solar) com múltiplas estratégias.
    Tenta: APIs externas → cache em BD → fallback de estado
    Retorna lista de 12 valores (jan-dez) ou None se falhar.
    """
    try:
        if latitude is None or longitude is None:
            # Tenta múltiplas variações de busca para melhor compatibilidade
            variacoes = [
                cidade,  # Original (ex: "EXTREMA - MG")
                cidade.replace(" - ", ", "),  # Ex: "EXTREMA, MG"
                cidade.replace(" - ", ", ") + ", Brasil",  # Ex: "EXTREMA, MG, Brasil"
                cidade.split(" - ")[0].strip(),  # Só a cidade (ex: "EXTREMA")
                cidade.split(" - ")[0].strip() + ", Brasil",  # Cidade + Brasil
            ]

            geo = None
            headers = {"User-Agent": "SolarPortal/1.0"}
            for var in variacoes:
                try:
                    geo_url = f"https://nominatim.openstreetmap.org/search?q={var}&format=json&countrycodes=br&limit=1"
                    geo_r = requests.get(geo_url, headers=headers, timeout=5)
                    if geo_r.status_code == 200 and geo_r.json():
                        geo = geo_r.json()[0]
                        print(f"[PVGIS] Geocodificação bem-sucedida para '{var}' (lat={geo['lat']}, lon={geo['lon']})")
                        break
                    else:
                        print(f"[PVGIS] Falha geocodificando '{var}': status {geo_r.status_code}")
                except Exception as e:
                    print(f"[PVGIS] Erro ao tentar '{var}': {e}")
                    continue

            if not geo:
                print(f"[PVGIS] Nenhuma variação funcionou para '{cidade}'")
                return None

            latitude, longitude = float(geo['lat']), float(geo['lon'])

        # 1. PRIORIDADE 1: Dados específicos de cidade (validados pelo integrador)
        cidade_upper = cidade.upper()
        if cidade_upper in HSP_FALLBACK_CIDADE:
            print(f"[HSP] Usando dados validados de cidade: {cidade_upper}")
            return HSP_FALLBACK_CIDADE[cidade_upper]

        # 2. PRIORIDADE 2: Tenta APIs externas (dados genéricos)
        apis = [
            ("Open-Meteo", lambda: buscar_hsp_openmeteo(latitude, longitude)),
            ("NASA POWER", lambda: buscar_hsp_nasapower(latitude, longitude)),
            ("PVGIS", lambda: buscar_hsp_pvgis_raw(latitude, longitude)),
        ]

        for api_name, api_func in apis:
            try:
                hsp = api_func()
                if hsp and len(hsp) == 12:
                    print(f"[HSP] Sucesso via {api_name} para ({latitude}, {longitude})")
                    return hsp
            except Exception as e:
                print(f"[HSP] {api_name} falhou: {e}")

        # 3. PRIORIDADE 3: Fallback de estado
        estado = None
        if " - " in cidade:
            estado = cidade.split(" - ")[-1].strip().upper()

        if estado and len(estado) == 2 and estado in HSP_FALLBACK_ESTADO:
            print(f"[HSP] Fallback para estado {estado}")
            return HSP_FALLBACK_ESTADO[estado]

        print(f"[HSP] Todas as estratégias falharam para '{cidade}'")
        return None
    except Exception as e:
        print(f"Erro ao buscar HSP de {cidade}: {e}")
        return None

def enviar_email_offline(integrador_email: str, integrador_nome: str, cliente_nome: str):
    if not SENDGRID_API_KEY:
        return
    html = f"""
        <div style="font-family:sans-serif;max-width:540px;margin:0 auto;">
        <h2 style="color:#dc2626;">⚠️ Sistema Solar Offline</h2>
        <p>Olá, <strong>{integrador_nome}</strong>!</p>
        <p>O sistema solar do cliente <strong>{cliente_nome}</strong> está <strong>offline há mais de 1 hora</strong>.</p>
        <p><strong>Possíveis causas:</strong></p>
        <ul>
            <li>Disjuntor CA desarmado</li>
            <li>Falha na comunicação Wi-Fi do inversor</li>
            <li>Problema técnico no inversor</li>
        </ul>
        <p style="margin-top:20px;">
            <a href="https://jonassales-ecopower.github.io/solar-portal-app/painel.html"
               style="background:#1e293b;color:white;padding:12px 24px;border-radius:6px;text-decoration:none;font-weight:bold;">
               Acessar Painel
            </a>
        </p>
        <p style="color:#888;font-size:12px;margin-top:24px;">Este alerta não será repetido nas próximas 24 horas.</p>
        </div>
    """
    try:
        requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            json={
                "personalizations": [{"to": [{"email": integrador_email}]}],
                "from": {"email": ALERT_EMAIL_FROM, "name": "Solar Portal"},
                "subject": f"⚠️ Alerta: {cliente_nome} está offline há +1h",
                "content": [{"type": "text/html", "value": html}]
            },
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
            timeout=10
        )
    except Exception:
        pass

def verificar_clientes_offline():
    # Inversor não gera à noite — só alertar entre 7h e 18h (horário de Brasília, UTC-3)
    hora_brt = (datetime.utcnow() - timedelta(hours=3)).hour
    if hora_brt < 7 or hora_brt >= 18:
        return

    conn = conectar_banco()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT c.id, c.nome, i.email, i.nome
            FROM clientes c
            JOIN integradores i ON i.id = c.integrador_id
            WHERE c.api_key_inversor IS NOT NULL
              AND c.api_key_inversor != ''
              AND c.ultima_leitura_em IS NOT NULL
              AND c.ultima_leitura_em < NOW() - INTERVAL '1 hour'
              AND (c.alerta_offline_em IS NULL OR c.alerta_offline_em < NOW() - INTERVAL '24 hours')
        """)
        clientes_offline = cur.fetchall()
        for cliente_id, cliente_nome, int_email, int_nome in clientes_offline:
            enviar_email_offline(int_email, int_nome, cliente_nome)
            cur.execute("UPDATE clientes SET alerta_offline_em = NOW() WHERE id = %s", (cliente_id,))
        conn.commit()
    except Exception:
        pass
    finally:
        cur.close()
        conn.close()

async def verificar_offline_loop():
    while True:
        await asyncio.sleep(300)  # a cada 5 minutos
        try:
            verificar_clientes_offline()
        except Exception:
            pass

@asynccontextmanager
async def lifespan(app: FastAPI):
    inicializar_banco()
    asyncio.create_task(verificar_offline_loop())
    yield

# ==================== APP ====================

app = FastAPI(title="Solar Portal API", lifespan=lifespan)
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

4. CONSUMO BRUTO (kWh): Energia consumida desta UC no período de faturamento.

   MÉTODO PRIMÁRIO — Itens da Fatura:
   Procure a linha que começa com "Consumo em kWh" nos itens cobrados.
   A QUANTIDADE (primeiro número após a unidade KWH) é o consumo_bruto_kwh.
   Exemplos:
   "Consumo em kWh KWH 505,00 0,976610 493,19"  → consumo_bruto_kwh = 505
   "Consumo em kWh KWH 666,00 0,976610 650,42"  → consumo_bruto_kwh = 666
   "Consumo em kWh KWH 145,00 0,976610 141,60"  → consumo_bruto_kwh = 145
   NÃO confunda com "Energia Atv Injetada GDII" que pode ter o mesmo número com valor negativo.

   MÉTODO ALTERNATIVO (apenas se não houver linha "Consumo em kWh" nos itens):
   Tabela de medidores — linha "Energia ativa em kWh" — use o ÚLTIMO campo (Consumo_kWh).
   "N6201931610 Energia ativa em kWh Ponta 1864 2369 1 505" → consumo_bruto_kwh = 505
   • 1864 = Leitura Anterior ← NUNCA use como consumo
   • 2369 = Leitura Atual ← NUNCA use como consumo

   NUNCA use "Consumo até 80kWh-BR" isoladamente (é faixa tarifária, não consumo total).

5. CONSUMO FATURADO (kWh): Valor no histórico dos últimos 13 meses referente ao mês atual.
   É MENOR que consumo bruto em sistemas GD. Ex: ABR/26 = 420,63 kWh; MAI/26 = 277,45 kWh.

6. CONSUMO DA REDE (kWh): Mesmo valor que consumo_bruto_kwh.

7. ENERGIA INJETADA (kWh): DISTINÇÃO CRÍTICA para contas GD_II com rateio (Lei 14.300/22):

   ⚠️ ARMADILHA COMUM: a seção "Itens da Fatura" mostra "Energia Atv Injetada GDII = X kWh".
   Esse X é o crédito APLICADO ao consumo desta UC (limitado ao próprio consumo da geradora).
   Exemplo: se injetado real = 730 e consumo = 505, os itens mostram 505 (só abate o que consumiu).
   NÃO use esse valor — ele é MENOR que o injetado real quando há surplus para beneficiárias.

   ✅ USE SOMENTE a linha "Energia injetada" na TABELA DE MEDIDORES (parte inferior da fatura).
   Formato: Medidor | Grandeza | Posto | Leitura_Anterior | Leitura_Atual | Constante | Consumo_kWh
   O valor correto é o ÚLTIMO campo da linha.

   EXEMPLO EXATO (Energisa GD_II com rateio):
   • Itens da Fatura: "Energia Atv Injetada GDII 505,00 kWh" → NÃO use 505 (é crédito aplicado, não injetado real)
   • Tabela de medidores: "N6201931610 Energia injetada Ponta 5681 6411 1 730" → energia_injetada_kwh = 730 ✅
   • 5681 = Leitura Anterior ← NUNCA use
   • 6411 = Leitura Atual ← NUNCA use
   • 730 = ÚLTIMO campo = energia realmente injetada na rede ← USE ESTE

   Outros exemplos:
   "Energia injetada Ponta 7321 8016 1 695" → energia_injetada_kwh = 695
   "Energia injetada Ponta 8016 8350 1 334" → energia_injetada_kwh = 334
   Se não houver linha "Energia injetada" na tabela de medidores, retornar 0.

8. SALDO ACUMULADO (kWh): Campo "Saldo Acumulado". Se zero, retornar 0.

9. MESES ACUMULADOS: Se houver "FATURAMENTO PELA MÉDIA" ou "LEITURA INFORMADA PELO CLIENTE",
   a conta pode cobrir mais de um mês. Analise o período (Leitura Anterior → Leitura Atual) para
   determinar quantos meses foram acumulados. Ex: 13/03 a 13/04 = 1 mês. 13/02 a 13/04 = 2 meses.
   Se o consumo bruto for muito alto (acima de 1.000 kWh residencial), provavelmente é leitura acumulada.

10. DATAS DE LEITURA: Localize na tabela de medidores ou no cabeçalho da fatura os campos
    "Leitura Anterior" e "Leitura Atual" (ou "Data Leitura Anterior" / "Data Leitura Atual").
    São as datas em que o técnico foi ao local fazer a leitura do medidor.
    Exemplos: "13/04/2026" e "12/05/2026". Retornar no formato DD/MM/AAAA.

Retorne EXATAMENTE neste formato JSON:
{{
  "nome_cliente": "",
  "numero_uc": "",
  "distribuidora": "",
  "mes_referencia": "",
  "data_vencimento": "DD/MM/AAAA",
  "leitura_anterior_data": "DD/MM/AAAA",
  "leitura_atual_data": "DD/MM/AAAA",
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
- mensagem_cliente: Máximo 2 linhas. Linguagem simples. Se leitura acumulada, mencionar isso.

Texto da conta:
{texto_pdf}"""
    resposta = cliente_ia.chat.completions.create(model="openrouter/auto", messages=[{"role": "user", "content": prompt}])
    return resposta.choices[0].message.content

def analisar_conta_beneficiario(texto_pdf: str) -> str:
    cliente_ia = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=OPENROUTER_KEY)
    prompt = f"""Você é um especialista em contas de energia elétrica brasileiras com foco em Geração Distribuída (GD) e rateio de créditos (Lei 14.300/2022).

Esta conta pertence a uma UC BENEFICIÁRIA — ela recebe créditos de energia de uma UC geradora solar.

Analise o texto e retorne SOMENTE um JSON válido, sem texto adicional.

CAMPOS A EXTRAIR:

1. MÊS DE REFERÊNCIA: Campo "REF: MÊS / ANO" impresso na fatura. Ex: "Maio / 2026".

2. CRÉDITOS RECEBIDOS (kWh): Energia recebida via rateio da UC geradora NESTE MÊS.
   Procure pela linha que contenha "oUC" (origem UC) — representa créditos do mês atual enviados pela geradora.
   Padrões comuns na Energisa:
   - "Energia Atv Injetada GDII oUC 6/2026 mPT" → use o valor em kWh DESTA linha (ex: 45 ou 180 kWh)
   - "Energia Atv Injetada GDII oUC" → créditos do mês atual = valor kWh desta linha
   IGNORE linhas com "mUC" — são compensações de saldos de MESES ANTERIORES, não créditos novos recebidos.
   IGNORE linhas com "Ajuste GDII" — é apenas diferença tarifária em R$, não representa kWh recebidos.
   IGNORE linhas com "Ajuste GDII - TRF Reduzida" — idem, ajuste tarifário, não kWh.
   Outros padrões possíveis: "Energia Recebida em Transferência", "GD Energia Recebida", "Compensação GD".
   Se não encontrar nenhuma linha "oUC" ou equivalente, retornar 0.

3. CONSUMO BRUTO (kWh): Resultado da conta: (Leitura_Atual − Leitura_Anterior) × Constante.
   NUNCA use Leitura_Atual ou Leitura_Anterior diretamente — esses são os acumulados do medidor.
   Use somente a diferença calculada (tipicamente entre 100 e 600 kWh para uma residência).

4. CONSUMO FATURADO (kWh): Consumo após dedução dos créditos GD. Valor final cobrado.

5. SALDO ANTERIOR (kWh): Saldo de créditos trazido do mês anterior. Se não houver, 0.

6. SALDO RESULTANTE (kWh): Saldo de créditos restante após este mês. Se não houver, 0.

7. VALOR DA FATURA: Valor total a pagar (R$).

Retorne EXATAMENTE neste formato JSON:
{{
  "mes_referencia": "",
  "creditos_recebidos_kwh": 0.0,
  "consumo_bruto_kwh": 0.0,
  "consumo_kwh": 0.0,
  "saldo_anterior_kwh": 0.0,
  "saldo_resultante_kwh": 0.0,
  "valor_fatura": 0.0
}}

Texto da conta:
{texto_pdf}"""
    resposta = cliente_ia.chat.completions.create(
        model="openrouter/auto",
        messages=[{"role": "user", "content": prompt}]
    )
    return resposta.choices[0].message.content

# ==================== CÁLCULO DE CONSUMO ====================

def calcular_analise_consumo(dados: dict, geracao_total_kwh: float = None, geracao_ja_ajustada: bool = False) -> dict:
    energia_injetada = float(dados.get("energia_injetada_kwh") or 0)
    consumo_rede = float(dados.get("consumo_bruto_kwh") or dados.get("consumo_rede_kwh") or 0)
    meses = int(dados.get("meses_acumulados") or 1)

    resultado = {
        "geracao_total_kwh": None,
        "consumo_instantaneo_kwh": None,
        "consumo_total_kwh": None,
        "analise_consumo": None
    }

    geracao_ajustada = None
    if geracao_total_kwh and geracao_total_kwh > 0:
        if not geracao_ja_ajustada and meses > 1:
            # Geração mensal do inversor × meses (fallback sem datas exatas)
            geracao_ajustada = round(geracao_total_kwh * meses, 2)
        else:
            # Geração já soma o período exato de faturamento
            geracao_ajustada = geracao_total_kwh

    if geracao_ajustada and geracao_ajustada > 0:
        consumo_instantaneo = max(0, round(geracao_ajustada - energia_injetada, 2))
        consumo_total = round(consumo_instantaneo + consumo_rede, 2)
        resultado["geracao_total_kwh"] = geracao_ajustada
        resultado["consumo_instantaneo_kwh"] = consumo_instantaneo
        resultado["consumo_total_kwh"] = consumo_total
    else:
        # Sem pvPower: consumo total = leitura real do medidor
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
        partes.append(
            f"☀️ {periodo.capitalize()}, seu sistema solar gerou {geracao_total:.1f} kWh. "
            f"Desses, {consumo_instantaneo:.1f} kWh foram usados instantaneamente "
            f"e {energia_injetada:.1f} kWh foram injetados na rede como créditos."
        )
    else:
        partes.append(
            f"⚡ {periodo.capitalize()}, seu sistema injetou {energia_injetada:.1f} kWh na rede como créditos. "
            f"Sua casa consumiu {consumo_rede:.1f} kWh da distribuidora."
        )

    if energia_injetada > 0 and consumo_rede > 0:
        compensado = min(energia_injetada, consumo_rede)
        excedente = round(max(0.0, energia_injetada - consumo_rede), 1)
        if excedente > 0:
            partes.append(
                f"🔀 Dos {energia_injetada:.1f} kWh injetados, {compensado:.1f} kWh compensaram o consumo "
                f"desta unidade e {excedente:.1f} kWh viraram crédito para rateio/saldo."
            )

    if consumo_total > 0 and consumo_rede > 0:
        partes.append(
            f"📊 Consumo total da casa: {consumo_total:.1f} kWh "
            f"({consumo_instantaneo:.1f} kWh do solar + {consumo_rede:.1f} kWh da rede)."
        )

    if meses > 1:
        partes.append(
            f"⚠️ Esta conta cobre {meses} meses de leitura acumulada — os valores são referentes ao período completo."
        )

    if consumo_anterior and consumo_anterior > 0 and consumo_total > 0:
        consumo_anterior_ajustado = consumo_anterior * meses if meses > 1 else consumo_anterior
        variacao = ((consumo_total - consumo_anterior_ajustado) / consumo_anterior_ajustado) * 100
        if variacao > 20:
            partes.append(
                f"📈 ATENÇÃO: Seu consumo aumentou {variacao:.0f}% em relação ao período anterior "
                f"({consumo_anterior_ajustado:.1f} kWh → {consumo_total:.1f} kWh). "
                f"Seu sistema solar está funcionando normalmente — o aumento se deve ao maior consumo de energia, "
                f"não a falha no sistema. Verifique se novos equipamentos foram ligados."
            )
        elif variacao > 5:
            partes.append(f"📊 Consumo aumentou {variacao:.0f}% em relação ao período anterior.")
        elif variacao < -10:
            partes.append(f"✅ Consumo reduziu {abs(variacao):.0f}% — solar + eficiência energética funcionando!")

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
                    "cloudcover_pct": nuvens[i] if i < len(nuvens) else None,
                    "precipitation_mm": chuva[i] if i < len(chuva) else None,
                    "dia_nublado": (nuvens[i] > 70) if i < len(nuvens) and nuvens[i] is not None else False,
                    "dia_chuvoso": (chuva[i] > 5) if i < len(chuva) and chuva[i] is not None else False
                }
            return resultado
    except Exception:
        pass
    return {}

# ==================== FOXESS OLD API (login com credenciais) ====================

def foxess_old_login(usuario: str, senha: str) -> tuple:
    """Retorna (token, erro_detalhe)."""
    senha_md5 = hashlib.md5(senha.encode("utf-8")).hexdigest()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Origin": "https://www.foxesscloud.com",
        "Referer": "https://www.foxesscloud.com/",
        "timezone": "America/Sao_Paulo",
        "lang": "pt",
    }
    payload = {"user": usuario, "password": senha_md5, "lang": "pt", "appVersion": "1.3.0"}
    try:
        resp = requests.post(
            "https://www.foxesscloud.com/c/v0/user/login",
            json=payload, headers=headers, timeout=30
        )
        raw = resp.text[:300]
        try:
            d = resp.json()
        except Exception:
            return "", f"HTTP {resp.status_code} — resposta não-JSON: {raw}"
        if d.get("errno") == 0:
            return d.get("result", {}).get("token", ""), None
        return "", f"HTTP {resp.status_code} errno={d.get('errno')} msg={d.get('msg')}"
    except Exception as e:
        return "", f"Exceção de rede: {str(e)}"

def foxess_old_listar_dispositivos(token: str) -> list:
    try:
        resp = requests.post(
            "https://www.foxesscloud.com/c/v0/device/list",
            json={"currentPage": 1, "pageSize": 20, "queryDate": {"begin": 0, "end": 0}},
            headers={"token": token, "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Content-Type": "application/json"},
            timeout=30
        )
        d = resp.json()
        if d.get("errno") == 0:
            return [{"sn": dev.get("deviceSN"), "modelo": dev.get("productType", "Inversor")}
                    for dev in d.get("result", {}).get("devices", []) if dev.get("deviceSN")]
    except Exception:
        pass
    return []

def foxess_old_get_realtime(email: str, senha: str, sn: str) -> dict:
    """Monitoramento via credenciais (old API) — sem necessidade de API key."""
    token, erro = foxess_old_login(email, senha)
    if not token:
        return {"errno": 1, "msg": f"Falha no login FoxESS — {erro}"}
    try:
        resp = requests.post(
            "https://www.foxesscloud.com/c/v0/device/real/query",
            json={"deviceSN": sn, "variables": ["pvPower", "loadsPower", "feedinPower", "gridConsumptionPower", "generationPower"]},
            headers={"token": token, "Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=30
        )
        d = resp.json()
        if d.get("errno") == 0:
            variaveis = {}
            for item in (d.get("result") or [{}])[0].get("datas", []):
                try: variaveis[item["variable"]] = float(item.get("value") or 0)
                except: pass
            return {"errno": 0, "variaveis": variaveis}
        return {"errno": 1, "msg": d.get("msg", "Sem dados FoxESS")}
    except Exception as e:
        return {"errno": 1, "msg": str(e)}

# ==================== FOXESS ====================

def foxess_chamar_api(api_key: str, path: str, body: dict):
    timestamp = str(int(time.time() * 1000))
    path_com_barra = f"/{path}"
    signature_raw = fr"{path_com_barra}\r\n{api_key}\r\n{timestamp}"
    signature = hashlib.md5(signature_raw.encode("utf-8")).hexdigest()
    headers = {"Token": api_key, "Lang": "en", "Timestamp": timestamp, "Signature": signature,
               "Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        resp = requests.post(f"https://www.foxesscloud.com/{path}", json=body, headers=headers, timeout=30)
        return resp.json()
    except Exception as e:
        return {"errno": 99999, "msg": str(e)}

def foxess_get_realtime(api_key: str, serial: str) -> dict:
    return foxess_chamar_api(api_key, "op/v0/device/real/query", {"sn": serial, "variables": []})

def foxess_get_mensal(api_key: str, serial: str, ano: int, mes: int) -> float:
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

def foxess_get_diario(api_key: str, serial: str, ano: int, mes: int) -> list:
    body = {"sn": serial, "year": ano, "month": mes, "dimension": "month", "variables": ["generation"]}
    try:
        r = foxess_chamar_api(api_key, "op/v0/device/report/query", body)
        if r.get("errno") == 0:
            for item in r.get("result", []):
                if isinstance(item, dict) and item.get("variable") == "generation":
                    resultado = []
                    for i, v in enumerate(item.get("values", [])):
                        if v:
                            try:
                                dt = date(ano, mes, i + 1)
                                resultado.append({"data": str(dt), "total_kwh": round(float(v), 2)})
                            except ValueError:
                                pass
                    return resultado
    except Exception:
        pass
    return []

def foxess_get_geracao_periodo(api_key: str, serial: str, data_inicio: date, data_fim: date) -> float:
    """
    Soma a geração real da FoxESS dia a dia para o período exato de faturamento.

    Intervalo: [data_inicio, data_fim) — inclui o dia da leitura anterior e exclui
    o dia da leitura atual. Assim o total cobre exatamente os "Dias: N" da fatura
    e o dia da leitura atual entra apenas na conta seguinte (sem dupla contagem).
    """
    meses = set()
    d = data_inicio
    while d <= data_fim:
        meses.add((d.year, d.month))
        if d.month == 12:
            d = date(d.year + 1, 1, 1)
        else:
            d = date(d.year, d.month + 1, 1)
    registros = []
    for ano, mes in sorted(meses):
        registros.extend(foxess_get_diario(api_key, serial, ano, mes))
    return somar_geracao_periodo(registros, data_inicio, data_fim)

# ==================== GROWATT ====================

def growatt_get_realtime(api_key: str, serial: str) -> dict:
    try:
        headers = {"token": api_key, "Content-Type": "application/json"}
        resp = requests.post(
            "https://openapi.growatt.com/v1/device/inverter/last_inverter_info",
            headers=headers, json={"inverterSn": serial}, timeout=30
        )
        data = resp.json()
        if data.get("code") == 0:
            inv = data.get("data", {}).get("inverterData", {})
            pac_kw = round(float(inv.get("pac", 0) or 0) / 1000, 3)
            return {"errno": 0, "pac_kw": pac_kw, "status_code": int(inv.get("status", 0) or 0)}
        return {"errno": 1, "msg": data.get("msg", "Erro Growatt API")}
    except Exception as e:
        return {"errno": 1, "msg": str(e)}

def growatt_get_mensal(api_key: str, serial: str, ano: int, mes: int) -> float:
    try:
        headers = {"token": api_key, "Content-Type": "application/json"}
        resp = requests.post(
            "https://openapi.growatt.com/v1/device/inverter/month",
            headers=headers, json={"inverterSn": serial, "year": str(ano), "month": str(mes).zfill(2)},
            timeout=30
        )
        data = resp.json()
        if data.get("code") == 0:
            energy = data.get("data", {}).get("energy", [])
            return round(sum(float(d.get("energy", 0) or 0) for d in energy), 2)
    except Exception:
        pass
    return 0.0

# ==================== SOLARMAN / DEYE ====================

def solarman_get_token(app_id: str, app_secret: str) -> str:
    try:
        resp = requests.post(
            "https://globalapi.solarmanpv.com/account/v1.0/token",
            params={"language": "en"},
            json={"appId": app_id, "appSecret": app_secret,
                  "nonce": secrets.token_hex(8), "timestamp": int(time.time())},
            timeout=30
        )
        d = resp.json()
        if d.get("success"):
            return d.get("access_token", "")
    except Exception:
        pass
    return ""

def solarman_get_realtime(app_id: str, app_secret: str, serial: str) -> dict:
    token = solarman_get_token(app_id, app_secret)
    if not token:
        return {"errno": 1, "msg": "Token Solarman inválido — verifique App ID e App Secret"}
    try:
        resp = requests.post(
            "https://globalapi.solarmanpv.com/device/v1.0/currentData",
            params={"language": "en"},
            headers={"Authorization": f"Bearer {token}"},
            json={"deviceSn": serial},
            timeout=30
        )
        d = resp.json()
        if d.get("success"):
            vals = {i["key"]: float(i.get("value") or 0) for i in d.get("dataList", []) if i.get("key")}
            pac_kw = round(vals.get("AC_Active_Power", vals.get("Total_DC_Power", vals.get("DC_Power", 0))) / 1000, 3)
            return {"errno": 0, "pac_kw": pac_kw, "device_state": d.get("deviceState", 0)}
        return {"errno": 1, "msg": d.get("msg", "Erro Solarman")}
    except Exception as e:
        return {"errno": 1, "msg": str(e)}

def solarman_get_mensal(app_id: str, app_secret: str, serial: str, ano: int, mes: int) -> float:
    token = solarman_get_token(app_id, app_secret)
    if not token:
        return 0.0
    try:
        resp = requests.post(
            "https://globalapi.solarmanpv.com/device/v1.0/historicalData",
            params={"language": "en"},
            headers={"Authorization": f"Bearer {token}"},
            json={"deviceSn": serial, "timeType": 2, "time": f"{ano}-{str(mes).zfill(2)}"},
            timeout=30
        )
        d = resp.json()
        if d.get("success"):
            vals = {i["key"]: float(i.get("value") or 0) for i in d.get("dataList", []) if i.get("key")}
            return round(vals.get("E_Total", vals.get("Generating_Capacity_Month", 0)), 2)
    except Exception:
        pass
    return 0.0

# ==================== BANCO HELPERS ====================

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

def obter_seriais(serial_str: str) -> list:
    """Retorna lista de seriais — suporta múltiplos separados por vírgula."""
    if not serial_str:
        return []
    return [s.strip() for s in str(serial_str).split(',') if s.strip()]

def foxess_get_diario_multi(api_key: str, seriais: list, ano: int, mes: int) -> list:
    """Busca dados diários de múltiplos inversores FoxESS e soma por dia."""
    totais: dict = {}
    for sn in seriais:
        for item in foxess_get_diario(api_key, sn, ano, mes):
            totais[item["data"]] = round(totais.get(item["data"], 0) + item["total_kwh"], 2)
    return [{"data": d, "total_kwh": v} for d, v in sorted(totais.items())]

def foxess_get_mensal_multi(api_key: str, seriais: list, ano: int, mes: int) -> float:
    return round(sum(foxess_get_mensal(api_key, sn, ano, mes) for sn in seriais), 2)

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

def salvar_historico_banco(cliente_id: int, dados_mensais: list):
    """Salva estimativa mensal distribuída uniformemente por dia (fallback para inversores sem API diária)."""
    conn = conectar_banco()
    cur = conn.cursor()
    hoje_date = date.today()
    for m in dados_mensais:
        dias = monthrange(m["ano"], m["mes_num"])[1]
        val_dia = m["geracao_kwh"] / dias
        for d in range(1, dias + 1):
            dt = datetime(m["ano"], m["mes_num"], d).date()
            if dt > hoje_date:
                break
            cur.execute("INSERT INTO historico_geracao (cliente_id,data,geracao_kwh) VALUES (%s,%s,%s) ON CONFLICT (cliente_id,data) DO UPDATE SET geracao_kwh=EXCLUDED.geracao_kwh",
                        (cliente_id, dt, round(val_dia, 2)))
    conn.commit()
    cur.close()
    conn.close()

def salvar_historico_diario_banco(cliente_id: int, dados_diarios: list):
    """Salva dados diários REAIS da API do inversor (substitui estimativas)."""
    conn = conectar_banco()
    cur = conn.cursor()
    hoje_date = date.today()
    for item in dados_diarios:
        try:
            dt = date.fromisoformat(item["data"])
            if dt > hoje_date:
                continue
            cur.execute(
                "INSERT INTO historico_geracao (cliente_id,data,geracao_kwh) VALUES (%s,%s,%s) ON CONFLICT (cliente_id,data) DO UPDATE SET geracao_kwh=EXCLUDED.geracao_kwh",
                (cliente_id, dt, round(float(item["total_kwh"]), 2))
            )
        except Exception:
            pass
    conn.commit()
    cur.close()
    conn.close()

# ==================== ROTAS ====================

@app.get("/")
def inicio():
    return {"status": "Solar Portal API funcionando!"}

@app.post("/portal/login")
def portal_login(dados: dict):
    email = (dados.get("email") or "").strip().lower()
    senha = dados.get("senha") or ""
    if not email or not senha:
        raise HTTPException(status_code=400, detail="E-mail e senha são obrigatórios")
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, nome, numero_uc, distribuidora, tipo_gd, senha_hash FROM clientes WHERE LOWER(email)=%s",
        (email,)
    )
    c = cur.fetchone()
    cur.close()
    conn.close()
    if not c or not c[5] or not verificar_senha(senha, c[5]):
        raise HTTPException(status_code=401, detail="E-mail ou senha incorretos")
    token = criar_token({"cliente_id": c[0], "nome": c[1], "numero_uc": c[2], "distribuidora": c[3], "tipo": "cliente"})
    return {"token": token, "cliente_id": c[0], "nome": c[1], "numero_uc": c[2], "distribuidora": c[3]}

@app.get("/portal/me")
def portal_me(credentials: HTTPAuthorizationCredentials = Depends(security)):
    payload = verificar_token(credentials.credentials)
    if not payload or payload.get("tipo") != "cliente":
        raise HTTPException(status_code=401, detail="Token inválido ou expirado")
    return {"cliente_id": payload["cliente_id"], "nome": payload["nome"],
            "numero_uc": payload.get("numero_uc"), "distribuidora": payload.get("distribuidora")}

@app.get("/clientes/{cliente_id}/detalhes")
def detalhes_cliente(cliente_id: int, integrador: dict = Depends(obter_integrador_atual)):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, nome, email, telefone, numero_uc, distribuidora, tipo_gd,
               marca_inversor, serial_inversor, api_key_inversor,
               potencia_kwp, latitude, longitude, data_instalacao, performance_ratio, token_acesso,
               tarifa_kwh, consumo_medio_antes_kwh
        FROM clientes WHERE id=%s AND integrador_id=%s
    """, (cliente_id, integrador["id"]))
    c = cur.fetchone()
    cur.close()
    conn.close()
    if not c:
        raise HTTPException(status_code=404, detail="Cliente não encontrado")
    return {
        "id": c[0], "nome": c[1], "email": c[2], "telefone": c[3],
        "numero_uc": c[4], "distribuidora": c[5], "tipo_gd": c[6],
        "marca_inversor": c[7], "serial_inversor": c[8], "api_key_inversor": c[9],
        "potencia_kwp": float(c[10]) if c[10] else None,
        "latitude": float(c[11]) if c[11] else None,
        "longitude": float(c[12]) if c[12] else None,
        "data_instalacao": str(c[13]) if c[13] else None,
        "performance_ratio": float(c[14]) if c[14] else 0.80,
        "token_acesso": c[15],
        "tarifa_kwh": float(c[16]) if c[16] else None,
        "consumo_medio_antes_kwh": float(c[17]) if c[17] else None
    }

@app.put("/clientes/{cliente_id}")
def atualizar_cliente(cliente_id: int, dados: dict, integrador: dict = Depends(obter_integrador_atual)):
    conn = conectar_banco()
    cur = conn.cursor()
    try:
        cidade = dados.get("cidade", "").strip() if dados.get("cidade") else None
        cur.execute("""
            UPDATE clientes SET nome=%s, email=%s, telefone=%s, numero_uc=%s, distribuidora=%s, tipo_gd=%s, cidade=%s
            WHERE id=%s AND integrador_id=%s RETURNING id, nome, cidade
        """, (dados.get("nome"), dados.get("email"), dados.get("telefone"),
              dados.get("numero_uc"), dados.get("distribuidora"), dados.get("tipo_gd"),
              cidade, cliente_id, integrador["id"]))
        c = cur.fetchone()
        conn.commit()
        if not c:
            cur.close()
            conn.close()
            raise HTTPException(status_code=404, detail="Cliente não encontrado")

        # Se informou cidade, valida e popula HSP
        hsp_info = None
        if cidade:
            try:
                cur.execute("SELECT hsp_mensal FROM cidades_hsp WHERE LOWER(nome_cidade) = LOWER(%s)", (cidade,))
                r = cur.fetchone()
                if r:
                    hsp_info = {"fonte": "cache", "hsp_mensal": json.loads(r[0])}
            except:
                pass

        cur.close()
        conn.close()

        result = {"sucesso": True, "id": c[0], "nome": c[1], "cidade": c[2]}
        if hsp_info:
            result["hsp_info"] = hsp_info
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        try:
            cur.close()
            conn.close()
        except:
            pass

@app.delete("/clientes/{cliente_id}")
def excluir_cliente(cliente_id: int, integrador: dict = Depends(obter_integrador_atual)):
    conn = conectar_banco()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM clientes WHERE id=%s AND integrador_id=%s RETURNING id, nome",
                    (cliente_id, integrador["id"]))
        c = cur.fetchone()
        conn.commit()
        if not c:
            raise HTTPException(status_code=404, detail="Cliente não encontrado")
        return {"sucesso": True, "id": c[0], "nome": c[1]}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        conn.close()

@app.post("/portal/esqueci-senha")
def esqueci_senha(dados: dict):
    email = (dados.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="E-mail obrigatório")
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("SELECT id, nome, email FROM clientes WHERE LOWER(email)=%s", (email,))
    c = cur.fetchone()
    if not c:
        cur.close()
        conn.close()
        return {"mensagem": "Se o e-mail estiver cadastrado, você receberá um link em breve."}
    reset_tok = secrets.token_urlsafe(32)
    exp = datetime.utcnow() + timedelta(hours=2)
    cur.execute("UPDATE clientes SET reset_token=%s, reset_token_exp=%s WHERE id=%s", (reset_tok, exp, c[0]))
    conn.commit()
    cur.close()
    conn.close()
    link = f"https://jonassales-ecopower.github.io/solar-portal-app/portal.html?reset_token={reset_tok}"
    html_email = f"""<div style="font-family:sans-serif;max-width:540px;margin:0 auto;">
        <h2 style="color:#d97706;">☀️ Redefinição de Senha — Solar Portal</h2>
        <p>Olá, <strong>{c[1]}</strong>!</p>
        <p>Clique no botão abaixo para criar uma nova senha:</p>
        <p style="margin:24px 0;">
            <a href="{link}" style="background:#d97706;color:white;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:bold;">
               🔑 Criar Nova Senha
            </a>
        </p>
        <p style="color:#888;font-size:12px;">Este link expira em 2 horas.</p>
        </div>"""
    if SENDGRID_API_KEY:
        try:
            requests.post(
                "https://api.sendgrid.com/v3/mail/send",
                json={"personalizations":[{"to":[{"email":c[2]}]}],
                      "from":{"email":ALERT_EMAIL_FROM,"name":"Solar Portal"},
                      "subject":"🔑 Redefinição de Senha — Solar Portal",
                      "content":[{"type":"text/html","value":html_email}]},
                headers={"Authorization":f"Bearer {SENDGRID_API_KEY}","Content-Type":"application/json"},
                timeout=10
            )
        except Exception:
            pass
    return {"mensagem": "Se o e-mail estiver cadastrado, você receberá um link em breve."}

@app.post("/portal/redefinir-senha")
def redefinir_senha(dados: dict):
    reset_tok = (dados.get("token") or "").strip()
    senha = (dados.get("senha") or "").strip()
    if not reset_tok or len(senha) < 6:
        raise HTTPException(status_code=400, detail="Token e senha (mínimo 6 caracteres) são obrigatórios")
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("SELECT id FROM clientes WHERE reset_token=%s AND reset_token_exp > NOW()", (reset_tok,))
    c = cur.fetchone()
    if not c:
        cur.close()
        conn.close()
        raise HTTPException(status_code=400, detail="Link expirado ou inválido. Solicite um novo link.")
    cur.execute("UPDATE clientes SET senha_hash=%s, reset_token=NULL, reset_token_exp=NULL WHERE id=%s",
                (criptografar_senha(senha), c[0]))
    conn.commit()
    cur.close()
    conn.close()
    return {"sucesso": True, "mensagem": "Senha redefinida! Faça login."}

@app.put("/clientes/{cliente_id}/senha")
def definir_senha_cliente(cliente_id: int, dados: dict, integrador: dict = Depends(obter_integrador_atual)):
    senha = (dados.get("senha") or "").strip()
    if len(senha) < 6:
        raise HTTPException(status_code=400, detail="A senha deve ter no mínimo 6 caracteres")
    conn = conectar_banco()
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE clientes SET senha_hash=%s WHERE id=%s AND integrador_id=%s RETURNING id, nome, email",
            (criptografar_senha(senha), cliente_id, integrador["id"])
        )
        c = cur.fetchone()
        conn.commit()
        if not c:
            raise HTTPException(status_code=404, detail="Cliente não encontrado")
        return {"sucesso": True, "nome": c[1], "email": c[2]}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        conn.close()

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
    cur.execute("SELECT id,nome,numero_uc,distribuidora,tipo_gd,marca_inversor,serial_inversor,potencia_kwp FROM clientes WHERE integrador_id=%s", (integrador["id"],))
    clientes = cur.fetchall()
    cur.close()
    conn.close()
    return [{"id":c[0],"nome":c[1],"numero_uc":c[2],"distribuidora":c[3],"tipo_gd":c[4],"marca_inversor":c[5],"serial_inversor":c[6],"potencia_kwp":float(c[7]) if c[7] else None} for c in clientes]

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

@app.delete("/cache-cidade/{cidade}")
def limpar_cache_cidade(cidade: str, integrador: dict = Depends(obter_integrador_atual)):
    """Limpa cache HSP de uma cidade (força recalculação na próxima validação)."""
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("DELETE FROM cidades_hsp WHERE LOWER(nome_cidade) = LOWER(%s)", (cidade,))
    conn.commit()
    deletados = cur.rowcount
    cur.close()
    conn.close()
    return {"cidade": cidade, "deletados": deletados}

@app.post("/validar-cidade")
def validar_cidade(cidade: str, latitude: float = None, longitude: float = None,
                   integrador: dict = Depends(obter_integrador_atual)):
    """
    Valida e retorna HSP mensal para uma cidade.
    SEMPRE recalcula (não usa cache, para garantir dados atualizados).
    """
    conn = conectar_banco()
    cur = conn.cursor()

    try:
        # 1. Remove cache antigo (força recalculação com dados atualizados)
        cur.execute("DELETE FROM cidades_hsp WHERE LOWER(nome_cidade) = LOWER(%s)", (cidade,))
        conn.commit()

        # 2. Busca HSP (prioridade: cidade específica > APIs > estado)
        hsp_mensal = buscar_hsp_pvgis(cidade, latitude, longitude)
        if not hsp_mensal:
            raise HTTPException(status_code=400, detail=f"Não foi possível encontrar dados de irradiância para {cidade}")

        media_anual = round(sum(hsp_mensal) / 12, 2)

        # 3. Salva no banco para próximas requisições
        cur.execute(
            """INSERT INTO cidades_hsp (nome_cidade, latitude, longitude, hsp_mensal, media_anual)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (nome_cidade) DO UPDATE SET
               hsp_mensal = EXCLUDED.hsp_mensal, atualizado_em = NOW()""",
            (cidade, latitude, longitude, json.dumps(hsp_mensal), media_anual)
        )
        conn.commit()

        return {
            "cidade": cidade,
            "hsp_mensal": hsp_mensal,
            "media_anual": media_anual,
            "fonte": "validado"
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao validar cidade: {str(e)}")
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
        cur.execute("""UPDATE clientes SET potencia_kwp=%s,latitude=%s,longitude=%s,data_instalacao=%s,
                    performance_ratio=%s,tarifa_kwh=%s,consumo_medio_antes_kwh=%s
                    WHERE id=%s AND integrador_id=%s RETURNING id,nome""",
                    (dados.get("potencia_kwp"),dados.get("latitude"),dados.get("longitude"),dados.get("data_instalacao"),
                     dados.get("performance_ratio",0.80),dados.get("tarifa_kwh"),dados.get("consumo_medio_antes_kwh"),
                     cliente_id,integrador["id"]))
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
        cur.execute("""
            UPDATE clientes SET
                marca_inversor=%s, serial_inversor=%s, api_key_inversor=%s,
                inversor_usuario=%s, inversor_senha=%s
            WHERE id=%s AND integrador_id=%s RETURNING id,nome
        """, (
            dados.get("marca_inversor"), dados.get("serial_inversor"), dados.get("api_key_inversor"),
            dados.get("inversor_usuario"), dados.get("inversor_senha"),
            cliente_id, integrador["id"]
        ))
        c = cur.fetchone()
        conn.commit()
        if not c:
            raise HTTPException(status_code=404, detail="Cliente não encontrado")
        return {"sucesso": True, "id": c[0], "nome": c[1]}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        cur.close()
        conn.close()

@app.post("/clientes/{cliente_id}/foxess/buscar")
def foxess_buscar_dispositivos(cliente_id: int, dados: dict, integrador: dict = Depends(obter_integrador_atual)):
    api_key = dados.get("api_key", "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="API Key é obrigatória")
    data = foxess_chamar_api(api_key, "op/v0/device/list", {"currentPage": 1, "pageSize": 20})
    if data.get("errno") != 0:
        raise HTTPException(status_code=401, detail=f"API Key inválida ou sem permissão: {data.get('msg')}")
    devices = data.get("result", {}).get("devices", [])
    if not devices:
        raise HTTPException(status_code=404, detail="Nenhum inversor encontrado nesta conta FoxESS")
    return {"dispositivos": [{"sn": d.get("deviceSN"), "modelo": d.get("productType", "Inversor"), "status": d.get("status", 0)} for d in devices if d.get("deviceSN")]}

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

        # Override com regex — mais confiável que IA para estes campos estruturados
        consumo_rx, injetado_rx = extrair_medicoes_medidor(texto)
        if consumo_rx is not None:
            dados["consumo_bruto_kwh"] = consumo_rx
            dados["consumo_rede_kwh"] = consumo_rx
        if injetado_rx is not None:
            dados["energia_injetada_kwh"] = injetado_rx
        # Status e percentual derivam de injetada/consumo — recalcula com os
        # valores corrigidos (a IA os computa com a extração dela, que pode errar)
        try:
            inj = float(dados.get("energia_injetada_kwh") or 0)
            cons = float(dados.get("consumo_bruto_kwh") or 0)
            if cons > 0:
                dados["status_sistema"] = "SUPERAVITÁRIO" if inj >= cons else "DEFICITÁRIO"
                dados["percentual_gerado"] = round(inj / cons * 100, 1)
        except (TypeError, ValueError):
            pass
        # Mesmo formato canônico nos dois lados da junção geradora ↔ beneficiárias
        mes_rx = extrair_mes_referencia(texto)
        if mes_rx:
            dados["mes_referencia"] = mes_rx
        else:
            dados["mes_referencia"] = normalizar_mes_referencia(dados.get("mes_referencia")) or dados.get("mes_referencia")
        # Datas de leitura definem o período exato da soma de geração do inversor
        ant_rx, atu_rx = extrair_datas_leitura(texto)
        if ant_rx and atu_rx:
            dados["leitura_anterior_data"] = ant_rx
            dados["leitura_atual_data"] = atu_rx

        conn = conectar_banco()
        cur = conn.cursor()
        cur.execute("SELECT marca_inversor,serial_inversor,api_key_inversor FROM clientes WHERE id=%s", (cliente_id,))
        inversor = cur.fetchone()
        cur.execute("SELECT consumo_total_kwh,consumo_bruto_kwh FROM contas WHERE cliente_id=%s ORDER BY criado_em DESC LIMIT 1", (cliente_id,))
        conta_ant = cur.fetchone()
        cur.close()
        conn.close()

        consumo_anterior = float(conta_ant[0] or conta_ant[1] or 0) if conta_ant else None

        # Buscar geração do período exato de faturamento
        geracao_total_kwh = None
        geracao_ja_ajustada = False
        if inversor and inversor[0] and inversor[0].lower() == "foxess" and inversor[2]:
            api_key_inv, serial_inv = inversor[2], inversor[1]
            try:
                # 1ª tentativa: período exato usando datas de leitura da conta
                ant_str = dados.get("leitura_anterior_data", "")
                atu_str = dados.get("leitura_atual_data", "")
                if ant_str and atu_str and ant_str != "DD/MM/AAAA" and atu_str != "DD/MM/AAAA":
                    data_ant = datetime.strptime(ant_str, "%d/%m/%Y").date()
                    data_atu = datetime.strptime(atu_str, "%d/%m/%Y").date()
                    g = foxess_get_geracao_periodo(api_key_inv, serial_inv, data_ant, data_atu)
                    if g > 0:
                        geracao_total_kwh = g
                        geracao_ja_ajustada = True
            except Exception:
                pass

            # 2ª tentativa: banco (historico_geracao) pelo período extraído
            if not geracao_total_kwh:
                try:
                    ant_str = dados.get("leitura_anterior_data", "")
                    atu_str = dados.get("leitura_atual_data", "")
                    if ant_str and atu_str and ant_str != "DD/MM/AAAA" and atu_str != "DD/MM/AAAA":
                        data_ant = datetime.strptime(ant_str, "%d/%m/%Y").date()
                        data_atu = datetime.strptime(atu_str, "%d/%m/%Y").date()
                        registros = buscar_geracao_periodo(cliente_id, data_ant, data_atu)
                        g = somar_geracao_periodo(registros, data_ant, data_atu)
                        if g > 0:
                            geracao_total_kwh = g
                            geracao_ja_ajustada = True
                except Exception:
                    pass

            # 3ª tentativa: total mensal pelo mês de referência (fallback original)
            if not geracao_total_kwh:
                try:
                    mes_ref = dados.get("mes_referencia", "")
                    meses_map = {"jan":1,"fev":2,"mar":3,"abr":4,"mai":5,"jun":6,"jul":7,"ago":8,"set":9,"out":10,"nov":11,"dez":12}
                    mes_num = next((n for k,n in meses_map.items() if k in mes_ref.lower()), None)
                    anos = re.findall(r'\d{4}', mes_ref)
                    ano_num = int(anos[0]) if anos else datetime.now().year
                    if mes_num:
                        g = foxess_get_mensal(api_key_inv, serial_inv, ano_num, mes_num)
                        if g > 0:
                            geracao_total_kwh = g
                except Exception:
                    pass

        analise = calcular_analise_consumo(dados, geracao_total_kwh, geracao_ja_ajustada)
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
    cur.execute("SELECT potencia_kwp,latitude,longitude,performance_ratio,tarifa_kwh,consumo_medio_antes_kwh,cidade FROM clientes WHERE id=%s", (cliente_id,))
    c = cur.fetchone()
    if not c or not c[0]:
        cur.close()
        conn.close()
        raise HTTPException(status_code=404, detail="Projeto solar não cadastrado")

    kwp = float(c[0])
    pr = float(c[3]) if c[3] else 0.80
    tarifa = float(c[4]) if c[4] else None
    consumo_antes = float(c[5]) if c[5] else None
    cidade = c[6] if c[6] else None

    # Tenta buscar HSP da cidade do banco
    hsp_mensal = None
    if cidade:
        cur.execute("SELECT hsp_mensal FROM cidades_hsp WHERE LOWER(nome_cidade) = LOWER(%s)", (cidade,))
        r = cur.fetchone()
        if r:
            hsp_mensal = json.loads(r[0]) if isinstance(r[0], str) else r[0]

    # Se não encontrou HSP da cidade, usa o padrão genérico (fallback)
    if not hsp_mensal:
        hsp_mensal = [5.2, 5.4, 5.1, 4.8, 4.5, 4.3, 4.5, 5.0, 4.9, 4.8, 4.9, 5.0]

    cur.close()
    conn.close()

    meses = ["Jan","Fev","Mar","Abr","Mai","Jun","Jul","Ago","Set","Out","Nov","Dez"]
    dias = [31,28,31,30,31,30,31,31,30,31,30,31]
    geracao = [{"mes":meses[i],"mes_num":i+1,"hsp":hsp_mensal[i],"kwh_esperado":round(kwp*hsp_mensal[i]*dias[i]*pr,2)} for i in range(12)]
    return {"potencia_kwp":kwp,"performance_ratio":pr,"tarifa_kwh":tarifa,"consumo_medio_antes_kwh":consumo_antes,
            "geracao_mensal":geracao,"total_anual":round(sum(g["kwh_esperado"] for g in geracao),2),"cidade":cidade,"fonte_hsp":"cidade" if cidade and hsp_mensal else "padrao"}

@app.get("/clientes/{cliente_id}/saldo-creditos")
def saldo_creditos(cliente_id: int):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("""
        SELECT mes_referencia, saldo_acumulado_kwh, energia_injetada_kwh,
               consumo_bruto_kwh, consumo_kwh, criado_em
        FROM contas WHERE cliente_id=%s
        ORDER BY criado_em ASC
    """, (cliente_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    if not rows:
        return {"dados": [], "saldo_atual": 0, "metodo": "sem_dados"}

    # Verificar se as contas têm saldo_acumulado_kwh extraído direto do PDF
    tem_saldo_direto = any(float(r[1] or 0) > 0 for r in rows)

    dados = []
    saldo_cumulativo = 0.0

    for r in rows:
        mes       = r[0]
        saldo_pdf = float(r[1] or 0)
        injetado  = float(r[2] or 0)
        consumo_bruto = float(r[3] or 0)   # leitura real do medidor
        consumo_fat   = float(r[4] or 0)   # consumo faturado (após créditos)
        data      = str(r[5])

        if tem_saldo_direto and saldo_pdf > 0:
            # Estratégia 1: usar o saldo que a IA extraiu diretamente do PDF
            saldo_mes = saldo_pdf
        else:
            # Estratégia 2: recalcular acumulando (injetado - consumo_bruto)
            # consumo_bruto = leitura do medidor de entrada (o que veio da rede)
            # injetado = leitura do medidor de saída (o que foi para a rede)
            saldo_cumulativo = max(0.0, round(saldo_cumulativo + injetado - consumo_bruto, 2))
            saldo_mes = saldo_cumulativo

        dados.append({
            "mes": mes,
            "saldo_kwh": saldo_mes,
            "injetado_kwh": injetado,
            "consumo_bruto_kwh": consumo_bruto,
            "delta_kwh": round(injetado - consumo_bruto, 2),
            "data": data
        })

    saldo_atual = dados[-1]["saldo_kwh"] if dados else 0
    metodo = "extraido_pdf" if tem_saldo_direto else "calculado_acumulado"
    return {"dados": dados, "saldo_atual": saldo_atual, "metodo": metodo}

@app.get("/clientes/{cliente_id}/previsao")
def previsao_geracao(cliente_id: int):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("SELECT latitude, longitude, potencia_kwp, performance_ratio FROM clientes WHERE id=%s", (cliente_id,))
    c = cur.fetchone()
    cur.close()
    conn.close()
    if not c or not c[0] or not c[2]:
        raise HTTPException(status_code=404, detail="Projeto não configurado")
    lat, lng, kwp, pr = float(c[0]), float(c[1]), float(c[2]), float(c[3] or 0.80)
    try:
        hoje = date.today()
        fim = hoje + timedelta(days=4)
        url = (f"https://api.open-meteo.com/v1/forecast"
               f"?latitude={lat}&longitude={lng}"
               f"&daily=shortwave_radiation_sum,cloudcover_mean,weathercode"
               f"&timezone=America%2FSao_Paulo"
               f"&start_date={hoje}&end_date={fim}")
        resp = requests.get(url, timeout=10)
        data = resp.json()
        daily = data.get("daily", {})
        datas = daily.get("time", [])
        radiacao = daily.get("shortwave_radiation_sum", [])
        nuvens = daily.get("cloudcover_mean", [])
        wcodes = daily.get("weathercode", [])
        semana = ["Seg","Ter","Qua","Qui","Sex","Sáb","Dom"]
        dias = []
        for i, d in enumerate(datas):
            hsp = round((radiacao[i] or 0) * 0.2778, 2) if i < len(radiacao) and radiacao[i] else 0
            kwh = round(kwp * hsp * pr, 2)
            wc = wcodes[i] if i < len(wcodes) else 0
            if wc == 0: icone = "☀️"
            elif wc in [1, 2]: icone = "🌤️"
            elif wc == 3: icone = "☁️"
            elif 51 <= wc <= 69: icone = "🌧️"
            elif 80 <= wc <= 99: icone = "⛈️"
            else: icone = "⛅"
            dt = date.fromisoformat(d)
            dias.append({"data": d, "hoje": d == str(hoje),
                         "dia_semana": "Hoje" if d == str(hoje) else semana[dt.weekday()],
                         "icone": icone, "geracao_kwh": kwh,
                         "cloudcover": round(nuvens[i] or 0) if i < len(nuvens) else 0})
        return {"dias": dias}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/clientes/{cliente_id}/monitoramento")
def monitoramento(cliente_id: int):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("SELECT marca_inversor,serial_inversor,api_key_inversor,inversor_usuario,inversor_senha FROM clientes WHERE id=%s", (cliente_id,))
    c = cur.fetchone()
    cur.close()
    conn.close()
    if not c or (not c[2] and not c[3]):
        raise HTTPException(status_code=404, detail="Inversor não configurado")
    marca, serial, api_key, inv_usuario, inv_senha = c
    marca_lower = marca.lower()

    geracao_kw = injetado_kw = consumo_rede_kw = consumo_casa_kw = potencia_kw = 0.0
    seriais = obter_seriais(serial)

    if marca_lower == "foxess":
        if not seriais:
            raise HTTPException(status_code=404, detail="Serial do inversor não configurado")
        variaveis: dict = {}
        # Prioridade: credenciais (email+senha) → dispensa API key
        if inv_usuario and inv_senha:
            for sn in seriais:
                data = foxess_old_get_realtime(inv_usuario, inv_senha, sn)
                if data.get("errno") != 0:
                    raise HTTPException(status_code=400, detail=f"Erro FoxESS ({sn}): {data.get('msg')}")
                for k, v in data.get("variaveis", {}).items():
                    variaveis[k] = variaveis.get(k, 0) + v
        elif api_key:
            for sn in seriais:
                raw = foxess_get_realtime(api_key, sn)
                if raw.get("errno") != 0:
                    raise HTTPException(status_code=400, detail=f"Erro FoxESS ({sn}): {raw.get('msg')}")
                resultado = raw.get("result", [])
                if not resultado:
                    raise HTTPException(status_code=404, detail=f"Sem dados FoxESS para {sn}")
                for item in resultado[0].get("datas", []):
                    try: variaveis[item["variable"]] = variaveis.get(item["variable"], 0) + float(item.get("value") or 0)
                    except: pass
        else:
            raise HTTPException(status_code=404, detail="Configure e-mail/senha ou API Key do FoxESS")
        geracao_kw      = variaveis.get("pvPower", 0.0)
        injetado_kw     = variaveis.get("feedinPower", 0.0)
        consumo_rede_kw = variaveis.get("gridConsumptionPower", 0.0)
        consumo_casa_kw = variaveis.get("loadsPower", 0.0)
        potencia_kw     = variaveis.get("generationPower", 0.0)

    elif marca_lower == "growatt":
        data = growatt_get_realtime(api_key, serial)
        if data.get("errno") != 0:
            raise HTTPException(status_code=400, detail=f"Erro Growatt: {data.get('msg')}")
        geracao_kw = potencia_kw = data.get("pac_kw", 0.0)

    elif marca_lower == "deye":
        try:
            creds = json.loads(api_key)
            app_id, app_secret = creds["app_id"], creds["app_secret"]
        except Exception:
            raise HTTPException(status_code=400, detail="Credenciais Deye inválidas — reconfigure o inversor com App ID e App Secret")
        data = solarman_get_realtime(app_id, app_secret, serial)
        if data.get("errno") != 0:
            raise HTTPException(status_code=400, detail=f"Erro Solarman/Deye: {data.get('msg')}")
        geracao_kw = potencia_kw = data.get("pac_kw", 0.0)

    else:
        raise HTTPException(status_code=400, detail=f"Marca '{marca}' ainda não suportada. Suportadas: FoxESS, Growatt, Deye")

    try:
        conn2 = conectar_banco()
        cur2 = conn2.cursor()
        cur2.execute("UPDATE clientes SET ultima_leitura_em = NOW() WHERE id = %s", (cliente_id,))
        conn2.commit()
        cur2.close()
        conn2.close()
    except Exception:
        pass

    return {"marca": marca, "serial": serial, "status": "online",
            "geracao_atual_kw": geracao_kw, "injetado_rede_kw": injetado_kw,
            "consumo_rede_kw": consumo_rede_kw, "consumo_casa_kw": consumo_casa_kw,
            "potencia_total_kw": potencia_kw, "timestamp": datetime.utcnow().isoformat()}

@app.get("/clientes/{cliente_id}/monitoramento/mensal")
def monitoramento_mensal(cliente_id: int):
    nomes = {1:"Jan",2:"Fev",3:"Mar",4:"Abr",5:"Mai",6:"Jun",7:"Jul",8:"Ago",9:"Set",10:"Out",11:"Nov",12:"Dez"}
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("SELECT nome,marca_inversor,serial_inversor,api_key_inversor FROM clientes WHERE id=%s", (cliente_id,))
    cliente = cur.fetchone()
    cur.execute("SELECT EXTRACT(YEAR FROM data)::int,EXTRACT(MONTH FROM data)::int,SUM(geracao_kwh) FROM historico_geracao WHERE cliente_id=%s AND data>=CURRENT_DATE-INTERVAL '12 months' GROUP BY 1,2 ORDER BY 1,2", (cliente_id,))
    resultados = cur.fetchall()
    cur.close()
    conn.close()

    # Monta dicionário com dados do banco indexados por (ano, mes)
    banco = {(int(r[0]), int(r[1])): round(float(r[2]), 2) for r in resultados if float(r[2]) > 0}
    hoje = datetime.now()

    # Sempre atualiza mês atual e mês anterior direto na FoxESS com dados DIÁRIOS REAIS
    if cliente and cliente[1] and cliente[1].lower() == "foxess" and cliente[3]:
        seriais_m = obter_seriais(cliente[2])
        api_key = cliente[3]
        prev = (hoje.replace(day=1) - timedelta(days=1))
        for ano, mes in [(hoje.year, hoje.month), (prev.year, prev.month)]:
            diarios = foxess_get_diario_multi(api_key, seriais_m, ano, mes)
            if diarios:
                salvar_historico_diario_banco(cliente_id, diarios)
                banco[(ano, mes)] = round(sum(d["total_kwh"] for d in diarios), 2)
            else:
                total = foxess_get_mensal_multi(api_key, seriais_m, ano, mes)
                if total > 0:
                    banco[(ano, mes)] = total
                    salvar_historico_banco(cliente_id, [{"ano": ano, "mes_num": mes, "geracao_kwh": total}])

        # Se temos menos de 10 meses, faz backfill dos meses faltantes (até 12 meses atrás)
        if len(banco) < 10:
            for i in range(2, 13):  # começa do 2 — mês atual e anterior já foram buscados acima
                dm = (hoje.replace(day=1) - timedelta(days=30 * i))
                key = (dm.year, dm.month)
                if key in banco:
                    continue  # já tem dados para este mês, pula
                diarios = foxess_get_diario_multi(api_key, seriais_m, dm.year, dm.month)
                if diarios:
                    salvar_historico_diario_banco(cliente_id, diarios)
                    banco[key] = round(sum(d["total_kwh"] for d in diarios), 2)
                else:
                    total = foxess_get_mensal_multi(api_key, seriais_m, dm.year, dm.month)
                    if total > 0:
                        banco[key] = total
                        salvar_historico_banco(cliente_id, [{"ano": dm.year, "mes_num": dm.month, "geracao_kwh": total}])

    if not banco:
        return {"cliente_id": cliente_id, "mensal": [], "total_periodo": 0, "fonte": "nenhuma"}

    mensal = sorted(
        [{"ano": k[0], "mes_num": k[1], "mes": nomes[k[1]], "geracao_kwh": v} for k, v in banco.items()],
        key=lambda x: (x["ano"], x["mes_num"])
    )
    mensal = mensal[-12:]
    return {"cliente_id": cliente_id, "mensal": mensal, "total_periodo": round(sum(m["geracao_kwh"] for m in mensal), 2), "fonte": "foxess"}

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

    # Média diária esperada baseada no projeto
    media_diaria_esperada = 0.0
    if potencia_kwp and performance_ratio:
        hsp = {1:5.2,2:5.4,3:5.1,4:4.8,5:4.5,6:4.3,7:4.5,8:5.0,9:4.9,10:4.8,11:4.9,12:5.0}
        media_diaria_esperada = float(potencia_kwp) * hsp.get(hoje.month, 5.0) * float(performance_ratio)

    # Alerta de 3 dias consecutivos abaixo da média
    alerta_consecutivo = None
    if media_diaria_esperada > 0 and len(ultimos7) >= 3:
        ultimos3 = ultimos7[-3:]
        dias_baixos = [d for d in ultimos3 if d["total_kwh"] < media_diaria_esperada * 0.7]
        if len(dias_baixos) == 3:
            data_inicio = ultimos3[0]["data"]
            data_fim = ultimos3[-1]["data"]
            clima = {}
            if latitude and longitude:
                clima = consultar_clima(float(latitude), float(longitude), data_inicio, data_fim)
            dias_ruins = sum(1 for d in ultimos3 if clima.get(d["data"], {}).get("dia_nublado", False) or clima.get(d["data"], {}).get("dia_chuvoso", False))
            media_3dias = sum(d["total_kwh"] for d in ultimos3) / 3

            if dias_ruins >= 2:
                alerta_consecutivo = {"tipo":"informativo","icone":"☁️",
                    "titulo":"Geração Baixa por Condições Climáticas",
                    "mensagem":f"Geração abaixo da média por 3 dias ({media_3dias:.1f} kWh/dia vs esperado {media_diaria_esperada:.1f} kWh/dia), mas o período teve muita nebulosidade ou chuva. Isso é normal!",
                    "acao":"Aguardar melhora climática. Se continuar após dias ensolarados, contate o integrador.",
                    "data_referencia": f"{data_inicio} a {data_fim}"}
            else:
                alerta_consecutivo = {"tipo":"atencao","icone":"⚠️",
                    "titulo":"3 Dias Consecutivos com Geração Abaixo da Média",
                    "mensagem":f"Geração abaixo do esperado por 3 dias consecutivos ({media_3dias:.1f} kWh/dia vs esperado {media_diaria_esperada:.1f} kWh/dia). O tempo estava bom — pode haver problema técnico.",
                    "acao":"Verificar sombra nos painéis, sujeira acumulada ou problema no inversor. Contate seu integrador.",
                    "data_referencia": f"{data_inicio} a {data_fim}"}

    alertas = []
    if alerta_consecutivo:
        alertas.append(alerta_consecutivo)

    dr = str(ontem)
    if g_ontem < 0.5:
        alertas.append({"tipo":"urgente","icone":"🔴","titulo":"Sistema Parado",
            "mensagem":f"Seu sistema não gerou energia significativa ontem ({g_ontem:.1f} kWh). Pode ser disjuntor desarmado ou falha no inversor.",
            "acao":"Verificar disjuntor CA e acionar o técnico imediatamente.",
            "data_referencia": dr})
    elif media7 > 0 and g_ontem < media7*0.4:
        clima_ontem = {}
        if latitude and longitude:
            clima_ontem = consultar_clima(float(latitude), float(longitude), str(ontem), str(ontem))
        dia_ruim = clima_ontem.get(str(ontem), {}).get("dia_nublado", False) or clima_ontem.get(str(ontem), {}).get("dia_chuvoso", False)
        if dia_ruim:
            alertas.append({"tipo":"informativo","icone":"☁️","titulo":"Geração Baixa — Dia Nublado/Chuvoso",
                "mensagem":f"Ontem seu sistema gerou apenas {g_ontem:.1f} kWh — condições climáticas desfavoráveis na sua região. Isso é normal!",
                "acao":"Nenhuma ação necessária. Monitorar nos próximos dias ensolarados.",
                "data_referencia": dr})
        else:
            alertas.append({"tipo":"atencao","icone":"⚠️","titulo":"Geração Muito Abaixo do Normal",
                "mensagem":f"Ontem: {g_ontem:.1f} kWh vs média 7 dias: {media7:.1f} kWh. Queda de {((media7-g_ontem)/media7*100):.0f}%. O tempo estava bom.",
                "acao":"Verificar sombreamento, sujeira nos painéis ou problema no inversor.",
                "data_referencia": dr})
    elif media7 > 0 and g_ontem < media7*0.7:
        alertas.append({"tipo":"informativo","icone":"📉","titulo":"Geração Abaixo da Média",
            "mensagem":f"Ontem: {g_ontem:.1f} kWh vs média: {media7:.1f} kWh.",
            "acao":"Monitorar nos próximos dias. Se continuar, agendar limpeza preventiva.",
            "data_referencia": dr})

    if not alertas and g_ontem > 0:
        alertas.append({"tipo":"normal","icone":"✅","titulo":"Sistema Operando Normalmente",
            "mensagem":f"Ontem seu sistema gerou {g_ontem:.1f} kWh.","acao":"Nenhuma ação necessária.",
            "data_referencia": dr})

    if media7 == 0 and g_ontem == 0:
        alertas = [{"tipo":"informativo","icone":"📡","titulo":"Coletando Dados",
            "mensagem":"Aguardando coleta de dados de geração.","acao":"Em alguns dias teremos estatísticas completas.",
            "data_referencia": None}]

    return {"cliente_id":cliente_id,"cliente_nome":nome,"data_analise":hoje.isoformat(),
            "geracao_ontem_kwh":round(g_ontem,1),"media_7_dias_kwh":round(media7,1),
            "media_diaria_esperada_kwh":round(media_diaria_esperada,1),"alertas":alertas}

@app.get("/clientes/{cliente_id}/geracao/diaria")
def geracao_diaria(cliente_id: int, dias: int = 30):
    fim = date.today() - timedelta(days=1)  # até ontem (hoje está incompleto)
    inicio = fim - timedelta(days=dias - 1)

    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("SELECT marca_inversor, serial_inversor, api_key_inversor FROM clientes WHERE id=%s", (cliente_id,))
    c = cur.fetchone()
    cur.close()
    conn.close()

    # FoxESS: busca dados diários reais — suporta múltiplos seriais
    if c and c[0] and c[0].lower() == "foxess" and c[2]:
        seriais_d = obter_seriais(c[1])
        api_key = c[2]
        dados_dict: dict = {}
        meses = set()
        d = inicio
        while d <= fim:
            meses.add((d.year, d.month))
            primeiro_prox = (d.replace(day=1) + timedelta(days=32)).replace(day=1)
            d = primeiro_prox
        for ano, mes in sorted(meses):
            for item in foxess_get_diario_multi(api_key, seriais_d, ano, mes):
                dados_dict[item["data"]] = item["total_kwh"]
        dados = [{"data": str(inicio + timedelta(days=i)),
                  "total_kwh": dados_dict.get(str(inicio + timedelta(days=i)), 0)}
                 for i in range((fim - inicio).days + 1)]
        return {"cliente_id": cliente_id, "dados": [d for d in dados if d["total_kwh"] > 0]}

    # Fallback: banco (Growatt, Deye, etc.)
    dados = buscar_geracao_periodo(cliente_id, inicio, fim)
    return {"cliente_id": cliente_id, "dados": dados}

# ==================== RELATÓRIOS ====================

HSP_MENSAIS = {1:5.2,2:5.4,3:5.1,4:4.8,5:4.5,6:4.3,7:4.5,8:5.0,9:4.9,10:4.8,11:4.9,12:5.0}
MESES_NOMES = ["Jan","Fev","Mar","Abr","Mai","Jun","Jul","Ago","Set","Out","Nov","Dez"]

def obter_performance_plantas(integrador_id: int, data_ref: date) -> list:
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("""
        SELECT c.id, c.nome, c.potencia_kwp, c.performance_ratio,
               c.marca_inversor, c.serial_inversor, c.api_key_inversor,
               c.ultima_leitura_em,
               COALESCE((SELECT SUM(geracao_kwh) FROM historico_geracao
                         WHERE cliente_id=c.id AND data=%s), 0) AS geracao_ontem,
               COALESCE((SELECT SUM(geracao_kwh) FROM historico_geracao
                         WHERE cliente_id=c.id
                           AND EXTRACT(YEAR FROM data)=%s
                           AND EXTRACT(MONTH FROM data)=%s), 0) AS geracao_mes
        FROM clientes c WHERE c.integrador_id=%s ORDER BY c.nome
    """, (data_ref, data_ref.year, data_ref.month, integrador_id))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    plantas = []
    for r in rows:
        cid, nome, kwp, pr, marca, serial, api_key, ultima_leitura, g_ontem, g_mes = r
        g_ontem = float(g_ontem or 0)
        g_mes = float(g_mes or 0)

        # FoxESS: sempre preferir dado real da API — suporta múltiplos seriais
        if marca and marca.lower() == "foxess" and api_key:
            try:
                seriais_p = obter_seriais(serial)
                for item in foxess_get_diario_multi(api_key, seriais_p, data_ref.year, data_ref.month):
                    if item["data"] == str(data_ref):
                        g_ontem = item["total_kwh"]
                        break
            except Exception:
                pass  # mantém valor do banco em caso de falha na API

        kwp_f = float(kwp) if kwp else 0.0
        pr_f = float(pr) if pr else 0.80
        esperado_dia = round(kwp_f * HSP_MENSAIS.get(data_ref.month, 5.0) * pr_f, 2) if kwp_f else 0.0

        pct = round(g_ontem / esperado_dia * 100) if esperado_dia > 0 else None

        if not kwp_f:
            status = "sem_config"
        elif g_ontem < 0.5:
            status = "offline"
        elif pct is not None and pct < 70:
            status = "alerta"
        else:
            status = "normal"

        plantas.append({
            "id": cid, "nome": nome, "potencia_kwp": kwp_f,
            "geracao_ontem_kwh": round(g_ontem, 1),
            "geracao_mes_kwh": round(g_mes, 1),
            "esperado_dia_kwh": esperado_dia,
            "pct_performance": pct,
            "status": status
        })
    return plantas


def gerar_html_email_diario(integrador_nome: str, plantas: list, data_ref: date) -> str:
    data_str = data_ref.strftime("%d/%m/%Y")
    normais = sum(1 for p in plantas if p["status"] == "normal")
    alertas_ct = sum(1 for p in plantas if p["status"] == "alerta")
    offline_ct = sum(1 for p in plantas if p["status"] in ["offline", "sem_config"])
    total_kwh = sum(p["geracao_ontem_kwh"] for p in plantas)

    ordem = {"offline": 0, "sem_config": 1, "alerta": 2, "normal": 3}
    plantas_ord = sorted(plantas, key=lambda p: ordem.get(p["status"], 9))

    linhas = ""
    for p in plantas_ord:
        pct = p["pct_performance"]
        if p["status"] == "normal":
            cor, txt = "#16a34a", f"✅ {pct}% do esperado" if pct else "✅ OK"
        elif p["status"] == "alerta":
            cor, txt = "#d97706", f"⚠️ {pct}% do esperado" if pct else "⚠️ Baixo"
        elif p["status"] == "offline":
            cor, txt = "#dc2626", "🔴 Sem geração"
        else:
            cor, txt = "#94a3b8", "⚙️ Sem config"
        linhas += f"""<tr style="border-bottom:1px solid #f1f5f9;">
            <td style="padding:10px 12px;font-size:13px;font-weight:600;">{p['nome']}</td>
            <td style="padding:10px 12px;text-align:center;font-size:13px;">{p['geracao_ontem_kwh']:.1f} kWh</td>
            <td style="padding:10px 12px;text-align:center;font-size:13px;color:#64748b;">{p['esperado_dia_kwh']:.1f} kWh</td>
            <td style="padding:10px 12px;text-align:center;font-size:13px;color:{cor};font-weight:600;">{txt}</td>
        </tr>"""

    return f"""<div style="font-family:'Segoe UI',sans-serif;max-width:600px;margin:0 auto;background:#f8fafc;border-radius:12px;overflow:hidden;">
  <div style="background:linear-gradient(135deg,#0f172a,#1e293b);padding:24px 28px;">
    <h1 style="color:#f1f5f9;font-size:20px;margin:0;">☀️ Relatório Diário — Solar Portal</h1>
    <p style="color:#94a3b8;font-size:13px;margin:6px 0 0;">Desempenho de ontem — {data_str}</p>
  </div>
  <div style="padding:24px 28px;">
    <p style="font-size:15px;color:#1e293b;margin:0 0 20px;">Olá, <strong>{integrador_nome}</strong>! Aqui está o resumo de ontem:</p>
    <div style="display:flex;gap:10px;margin-bottom:24px;">
      <div style="background:white;border-radius:10px;padding:14px 18px;border:1px solid #e2e8f0;flex:1;text-align:center;">
        <div style="font-size:26px;font-weight:800;color:#16a34a;">{normais}</div>
        <div style="font-size:11px;color:#64748b;margin-top:2px;">✅ Normais</div>
      </div>
      <div style="background:white;border-radius:10px;padding:14px 18px;border:1px solid #e2e8f0;flex:1;text-align:center;">
        <div style="font-size:26px;font-weight:800;color:#d97706;">{alertas_ct}</div>
        <div style="font-size:11px;color:#64748b;margin-top:2px;">⚠️ Alertas</div>
      </div>
      <div style="background:white;border-radius:10px;padding:14px 18px;border:1px solid #e2e8f0;flex:1;text-align:center;">
        <div style="font-size:26px;font-weight:800;color:#dc2626;">{offline_ct}</div>
        <div style="font-size:11px;color:#64748b;margin-top:2px;">🔴 Offline</div>
      </div>
      <div style="background:white;border-radius:10px;padding:14px 18px;border:1px solid #e2e8f0;flex:1;text-align:center;">
        <div style="font-size:26px;font-weight:800;color:#3b82f6;">{total_kwh:.0f}</div>
        <div style="font-size:11px;color:#64748b;margin-top:2px;">kWh Total</div>
      </div>
    </div>
    <table style="width:100%;border-collapse:collapse;background:white;border-radius:10px;overflow:hidden;border:1px solid #e2e8f0;">
      <thead><tr style="background:#f8fafc;">
        <th style="padding:10px 12px;text-align:left;font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.5px;">Usina</th>
        <th style="padding:10px 12px;text-align:center;font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.5px;">Gerado</th>
        <th style="padding:10px 12px;text-align:center;font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.5px;">Esperado</th>
        <th style="padding:10px 12px;text-align:center;font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.5px;">Status</th>
      </tr></thead>
      <tbody>{linhas}</tbody>
    </table>
    <div style="margin-top:24px;text-align:center;">
      <a href="https://jonassales-ecopower.github.io/solar-portal-app/painel.html"
         style="background:#f59e0b;color:white;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:700;font-size:14px;">
         Abrir Painel ☀️
      </a>
    </div>
  </div>
  <div style="padding:14px 28px;text-align:center;background:#f1f5f9;border-top:1px solid #e2e8f0;">
    <p style="font-size:11px;color:#94a3b8;margin:0;">Solar Portal — Monitoramento de Usinas Solares</p>
  </div>
</div>"""


@app.get("/relatorio-diario/preview")
def preview_relatorio_diario(integrador: dict = Depends(obter_integrador_atual)):
    ontem = date.today() - timedelta(days=1)
    plantas = obter_performance_plantas(integrador["id"], ontem)
    normais = sum(1 for p in plantas if p["status"] == "normal")
    alertas = sum(1 for p in plantas if p["status"] == "alerta")
    offline = sum(1 for p in plantas if p["status"] in ["offline", "sem_config"])
    return {
        "data_ref": str(ontem),
        "plantas": plantas,
        "resumo": {
            "total": len(plantas),
            "normais": normais,
            "alertas": alertas,
            "offline": offline,
            "total_kwh": round(sum(p["geracao_ontem_kwh"] for p in plantas), 1)
        }
    }


@app.api_route("/relatorio-diario/enviar", methods=["GET", "POST"])
def enviar_relatorio_diario():
    """Chamado por cron externo (sem auth). Envia email para todos os integradores ativos."""
    if not SENDGRID_API_KEY:
        return {"status": "erro", "detalhe": "SENDGRID_API_KEY não configurada", "enviados": 0}
    ontem = date.today() - timedelta(days=1)
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("SELECT id, nome, email FROM integradores WHERE ativo=TRUE")
    integradores = cur.fetchall()
    cur.close()
    conn.close()
    enviados = 0
    erros = []
    for int_id, int_nome, int_email in integradores:
        try:
            plantas = obter_performance_plantas(int_id, ontem)
            if not plantas:
                continue
            html = gerar_html_email_diario(int_nome, plantas, ontem)
            n_alertas = sum(1 for p in plantas if p["status"] in ["alerta", "offline"])
            assunto = (f"☀️ Relatório {ontem.strftime('%d/%m')} — {n_alertas} alerta(s) ⚠️"
                       if n_alertas else
                       f"☀️ Relatório {ontem.strftime('%d/%m')} — Tudo operando ✅")
            requests.post(
                "https://api.sendgrid.com/v3/mail/send",
                json={"personalizations": [{"to": [{"email": int_email}]}],
                      "from": {"email": ALERT_EMAIL_FROM, "name": "Solar Portal"},
                      "subject": assunto,
                      "content": [{"type": "text/html", "value": html}]},
                headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
                timeout=15
            )
            enviados += 1
        except Exception as e:
            erros.append({"integrador": int_nome, "erro": str(e)})
    return {"status": "ok", "enviados": enviados, "data_ref": str(ontem), "erros": erros}


@app.get("/relatorio/carteira")
def relatorio_carteira(integrador: dict = Depends(obter_integrador_atual)):
    hoje = datetime.now()
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("""
        SELECT c.id, c.nome, c.potencia_kwp, c.performance_ratio, c.data_instalacao,
               c.marca_inversor, c.ultima_leitura_em,
               COALESCE((SELECT SUM(geracao_kwh) FROM historico_geracao
                         WHERE cliente_id=c.id
                           AND EXTRACT(YEAR FROM data)=%s AND EXTRACT(MONTH FROM data)=%s), 0) AS g_mes,
               COALESCE((SELECT SUM(geracao_kwh) FROM historico_geracao
                         WHERE cliente_id=c.id AND data >= CURRENT_DATE - INTERVAL '365 days'), 0) AS g_ano
        FROM clientes c WHERE c.integrador_id=%s ORDER BY c.nome
    """, (hoje.year, hoje.month, integrador["id"]))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    dias_mes = monthrange(hoje.year, hoje.month)[1]
    hsp = HSP_MENSAIS[hoje.month]
    total_kwp = 0.0
    total_g_mes = 0.0
    plantas = []
    for r in rows:
        cid, nome, kwp, pr, dt_inst, marca, ultima_leitura, g_mes, g_ano = r
        kwp_f = float(kwp) if kwp else 0.0
        pr_f = float(pr) if pr else 0.80
        g_mes_f = round(float(g_mes), 1)
        g_ano_f = round(float(g_ano), 1)
        total_kwp += kwp_f
        total_g_mes += g_mes_f
        esperado_mes = round(kwp_f * hsp * dias_mes * pr_f, 1) if kwp_f else 0.0
        esperado_ate_hoje = round(kwp_f * hsp * hoje.day * pr_f, 1) if kwp_f else 0.0
        ritmo = round(g_mes_f / esperado_ate_hoje * 100) if esperado_ate_hoje > 0 else None
        offline_flag = bool(ultima_leitura and ultima_leitura < datetime.utcnow() - timedelta(hours=2))
        plantas.append({
            "id": cid, "nome": nome, "potencia_kwp": kwp_f,
            "data_instalacao": str(dt_inst) if dt_inst else None,
            "marca_inversor": marca,
            "geracao_mes_kwh": g_mes_f, "geracao_ano_kwh": g_ano_f,
            "esperado_mes_kwh": esperado_mes, "esperado_ate_hoje_kwh": esperado_ate_hoje,
            "ritmo_pct": ritmo, "offline": offline_flag
        })
    return {
        "plantas": plantas,
        "resumo": {
            "total_plantas": len(plantas),
            "total_kwp": round(total_kwp, 1),
            "total_geracao_mes_kwh": round(total_g_mes, 1),
            "mes_referencia": f"{MESES_NOMES[hoje.month-1]}/{hoje.year}"
        }
    }


@app.get("/relatorio/desempenho")
def relatorio_desempenho(integrador: dict = Depends(obter_integrador_atual)):
    hoje = datetime.now()
    # Gera lista dos últimos 12 meses
    meses = []
    for i in range(11, -1, -1):
        dt = hoje.replace(day=1) - timedelta(days=30 * i)
        meses.append({"ano": dt.year, "mes": dt.month,
                      "label": f"{MESES_NOMES[dt.month-1]}/{dt.year % 100:02d}"})

    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, nome, potencia_kwp, performance_ratio
        FROM clientes WHERE integrador_id=%s AND potencia_kwp IS NOT NULL ORDER BY nome
    """, (integrador["id"],))
    clientes = cur.fetchall()

    plantas_data = []
    for cid, nome, kwp, pr in clientes:
        kwp_f = float(kwp)
        pr_f = float(pr) if pr else 0.80
        meses_data = []
        for m in meses:
            esperado = round(kwp_f * HSP_MENSAIS[m["mes"]] * monthrange(m["ano"], m["mes"])[1] * pr_f, 1)
            cur.execute("""
                SELECT COALESCE(SUM(geracao_kwh), 0) FROM historico_geracao
                WHERE cliente_id=%s AND EXTRACT(YEAR FROM data)=%s AND EXTRACT(MONTH FROM data)=%s
            """, (cid, m["ano"], m["mes"]))
            realizado = round(float(cur.fetchone()[0] or 0), 1)
            meses_data.append({"label": m["label"], "esperado": esperado, "realizado": realizado})
        plantas_data.append({"id": cid, "nome": nome, "potencia_kwp": kwp_f, "meses": meses_data})

    cur.close()
    conn.close()
    return {"plantas": plantas_data, "labels": [m["label"] for m in meses]}


# ==================== RATEIO ====================

@app.get("/rateio/{cliente_id}")
def listar_beneficiarios(cliente_id: int, integrador: dict = Depends(obter_integrador_atual)):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, nome, numero_uc, distribuidora, percentual, ativo
        FROM rateio_beneficiarios WHERE cliente_id=%s AND ativo=TRUE ORDER BY id
    """, (cliente_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    beneficiarios = [{"id": r[0], "nome": r[1], "numero_uc": r[2],
                      "distribuidora": r[3], "percentual": float(r[4])} for r in rows]
    total_pct = round(sum(b["percentual"] for b in beneficiarios), 2)
    return {"beneficiarios": beneficiarios, "total_percentual": total_pct}

@app.post("/rateio/{cliente_id}")
def criar_beneficiario(cliente_id: int, dados: dict, integrador: dict = Depends(obter_integrador_atual)):
    nome = dados.get("nome", "").strip()
    if not nome:
        raise HTTPException(status_code=400, detail="Nome obrigatório")
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO rateio_beneficiarios (cliente_id, nome, numero_uc, distribuidora, percentual)
        VALUES (%s, %s, %s, %s, %s) RETURNING id
    """, (cliente_id, nome, dados.get("numero_uc"), dados.get("distribuidora"),
          float(dados.get("percentual", 0))))
    new_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return {"id": new_id, "ok": True}

@app.put("/rateio/beneficiario/{beneficiario_id}")
def atualizar_beneficiario(beneficiario_id: int, dados: dict, integrador: dict = Depends(obter_integrador_atual)):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("""
        UPDATE rateio_beneficiarios SET nome=%s, numero_uc=%s, distribuidora=%s, percentual=%s
        WHERE id=%s
    """, (dados.get("nome"), dados.get("numero_uc"), dados.get("distribuidora"),
          float(dados.get("percentual", 0)), beneficiario_id))
    conn.commit()
    cur.close()
    conn.close()
    return {"ok": True}

@app.delete("/rateio/beneficiario/{beneficiario_id}")
def excluir_beneficiario(beneficiario_id: int, integrador: dict = Depends(obter_integrador_atual)):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("UPDATE rateio_beneficiarios SET ativo=FALSE WHERE id=%s", (beneficiario_id,))
    conn.commit()
    cur.close()
    conn.close()
    return {"ok": True}

@app.get("/rateio/{cliente_id}/resumo")
def resumo_rateio(cliente_id: int, integrador: dict = Depends(obter_integrador_atual)):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, nome, numero_uc, distribuidora, percentual
        FROM rateio_beneficiarios WHERE cliente_id=%s AND ativo=TRUE ORDER BY id
    """, (cliente_id,))
    beneficiarios = [{"id": r[0], "nome": r[1], "numero_uc": r[2],
                      "distribuidora": r[3], "percentual": float(r[4])} for r in cur.fetchall()]

    cur.execute("""
        SELECT mes_referencia, energia_injetada_kwh, consumo_bruto_kwh
        FROM contas WHERE cliente_id=%s AND energia_injetada_kwh IS NOT NULL
        ORDER BY criado_em ASC
    """, (cliente_id,))
    contas = cur.fetchall()

    meses = []
    for c in contas:
        mes_ref = c[0]
        injetado = float(c[1] or 0)
        consumo_geradora = float(c[2] or 0)
        surplus = round(max(0.0, injetado - consumo_geradora), 2)
        dist = []
        for b in beneficiarios:
            kwh = round(surplus * b["percentual"] / 100, 2)
            cur.execute("""
                SELECT creditos_recebidos_kwh, consumo_kwh, saldo_resultante_kwh, valor_fatura
                FROM contas_beneficiario WHERE beneficiario_id=%s AND mes_referencia=%s
                ORDER BY criado_em DESC LIMIT 1
            """, (b["id"], mes_ref))
            conta_ben = cur.fetchone()
            dist.append({
                "beneficiario_id": b["id"],
                "nome": b["nome"],
                "percentual": b["percentual"],
                "creditos_calculados_kwh": kwh,
                "tem_conta": conta_ben is not None,
                "creditos_conta_kwh": float(conta_ben[0] or 0) if conta_ben else None,
                "consumo_kwh": float(conta_ben[1] or 0) if conta_ben else None,
                "saldo_resultante_kwh": float(conta_ben[2] or 0) if conta_ben else None,
                "valor_fatura": float(conta_ben[3] or 0) if conta_ben else None,
            })
        meses.append({
            "mes_referencia": mes_ref,
            "injetado_kwh": injetado,
            "consumo_geradora_kwh": consumo_geradora,
            "surplus_kwh": surplus,
            "distribuicao": dist,
        })

    cur.close()
    conn.close()
    return {"beneficiarios": beneficiarios, "meses": meses}

@app.post("/rateio/beneficiario/{beneficiario_id}/conta/upload")
async def upload_conta_beneficiario(beneficiario_id: int, arquivo: UploadFile = File(...)):
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(await arquivo.read())
        tmp_path = tmp.name
    try:
        texto = extrair_texto_pdf(tmp_path)
        resultado_raw = analisar_conta_beneficiario(texto)
        resultado_raw = re.sub(r"```json|```", "", resultado_raw).strip()
        dados = json.loads(resultado_raw)

        # Overrides determinísticos — regex é mais confiável que IA nestes campos.
        # creditos_recebidos_kwh NÃO entra aqui: o texto extraído embaralha os
        # valores das linhas oUC, e corrigi-lo pelo valor calculado anularia a
        # própria conferência.
        consumo_rx, _ = extrair_medicoes_medidor(texto)
        if consumo_rx is not None:
            dados["consumo_bruto_kwh"] = consumo_rx
        mes_rx = extrair_mes_referencia(texto)
        if mes_rx:
            dados["mes_referencia"] = mes_rx
        else:
            dados["mes_referencia"] = normalizar_mes_referencia(dados.get("mes_referencia")) or dados.get("mes_referencia")

        conn = conectar_banco()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO contas_beneficiario
                (beneficiario_id, mes_referencia, creditos_recebidos_kwh, consumo_kwh,
                 consumo_bruto_kwh, saldo_anterior_kwh, saldo_resultante_kwh, valor_fatura)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (beneficiario_id, mes_referencia) DO UPDATE SET
                creditos_recebidos_kwh = EXCLUDED.creditos_recebidos_kwh,
                consumo_kwh = EXCLUDED.consumo_kwh,
                consumo_bruto_kwh = EXCLUDED.consumo_bruto_kwh,
                saldo_anterior_kwh = EXCLUDED.saldo_anterior_kwh,
                saldo_resultante_kwh = EXCLUDED.saldo_resultante_kwh,
                valor_fatura = EXCLUDED.valor_fatura
            RETURNING id
        """, (beneficiario_id, dados.get("mes_referencia"),
              dados.get("creditos_recebidos_kwh"), dados.get("consumo_kwh"),
              dados.get("consumo_bruto_kwh"), dados.get("saldo_anterior_kwh"),
              dados.get("saldo_resultante_kwh"), dados.get("valor_fatura")))
        conta_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        return {"ok": True, "dados": dados, "id": conta_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro na análise: {str(e)}")
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

def _calcular_status_rateio(calculado: float, recebido) -> str:
    if calculado <= 0:
        return "sem_excedente"
    if recebido is None:
        return "aguardando"
    rec = float(recebido or 0)
    if rec >= calculado * 0.90:
        return "compensado"
    if rec > 0:
        return "parcial"
    return "nao_compensado"

@app.get("/clientes/{cliente_id}/rateio")
def rateio_publico(cliente_id: int):
    conn = conectar_banco()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, nome, numero_uc, distribuidora, percentual
        FROM rateio_beneficiarios WHERE cliente_id=%s AND ativo=TRUE ORDER BY id
    """, (cliente_id,))
    beneficiarios = [{"id": r[0], "nome": r[1], "numero_uc": r[2],
                      "distribuidora": r[3], "percentual": float(r[4])} for r in cur.fetchall()]

    if not beneficiarios:
        cur.close()
        conn.close()
        return {"beneficiarios": [], "meses": []}

    cur.execute("""
        SELECT mes_referencia, energia_injetada_kwh, consumo_bruto_kwh
        FROM contas WHERE cliente_id=%s AND energia_injetada_kwh IS NOT NULL
        ORDER BY criado_em ASC
    """, (cliente_id,))
    contas = cur.fetchall()

    meses = []
    for c in contas:
        mes_ref = c[0]
        injetado = float(c[1] or 0)
        consumo_geradora = float(c[2] or 0)
        # Surplus = o que sobra após cobrir o consumo da própria geradora
        surplus = round(max(0.0, injetado - consumo_geradora), 2)
        dist = []
        for b in beneficiarios:
            calculado = round(surplus * b["percentual"] / 100, 2)
            cur.execute("""
                SELECT creditos_recebidos_kwh, consumo_kwh, consumo_bruto_kwh,
                       saldo_anterior_kwh, saldo_resultante_kwh, valor_fatura
                FROM contas_beneficiario
                WHERE beneficiario_id=%s AND mes_referencia=%s
                ORDER BY criado_em DESC LIMIT 1
            """, (b["id"], mes_ref))
            row = cur.fetchone()
            conta_b = None
            if row:
                conta_b = {
                    "creditos_recebidos_kwh": float(row[0] or 0),
                    "consumo_kwh": float(row[1] or 0),
                    "consumo_bruto_kwh": float(row[2] or 0),
                    "saldo_anterior_kwh": float(row[3] or 0),
                    "saldo_resultante_kwh": float(row[4] or 0),
                    "valor_fatura": float(row[5] or 0),
                }
            status = _calcular_status_rateio(calculado, conta_b["creditos_recebidos_kwh"] if conta_b else None)
            dist.append({
                "beneficiario_id": b["id"],
                "nome": b["nome"],
                "percentual": b["percentual"],
                "creditos_calculados_kwh": calculado,
                "conta": conta_b,
                "status": status,
            })
        meses.append({
            "mes_referencia": mes_ref,
            "injetado_kwh": injetado,
            "consumo_geradora_kwh": consumo_geradora,
            "surplus_kwh": surplus,
            "distribuicao": dist,
        })

    cur.close()
    conn.close()
    return {"beneficiarios": beneficiarios, "meses": list(reversed(meses))}


@app.api_route("/admin/verificar-offline", methods=["GET", "POST"])
def admin_verificar_offline():
    verificar_clientes_offline()
    return {"status": "ok"}

@app.post("/admin/teste-email")
def admin_teste_email(dados: dict):
    email = dados.get("email", "")
    nome = dados.get("nome", "Integrador")
    if not email:
        raise HTTPException(status_code=400, detail="Email obrigatório")
    if not SENDGRID_API_KEY:
        raise HTTPException(status_code=500, detail="SENDGRID_API_KEY não configurada")
    html = f"<h2>✅ Teste Solar Portal</h2><p>Olá {nome}, o alerta de email está funcionando!</p>"
    resp = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        json={
            "personalizations": [{"to": [{"email": email}]}],
            "from": {"email": ALERT_EMAIL_FROM, "name": "Solar Portal"},
            "subject": "✅ Teste — Solar Portal alerta funcionando",
            "content": [{"type": "text/html", "value": html}]
        },
        headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
        timeout=10
    )
    return {"status_code": resp.status_code, "sendgrid_response": resp.text or "enviado", "enviado_para": email, "remetente": ALERT_EMAIL_FROM}
