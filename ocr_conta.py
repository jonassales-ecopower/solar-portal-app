import PyPDF2
import json
import re
import psycopg2
from openai import OpenAI
from datetime import datetime

# Configuração do banco de dados
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "database": "solar_portal",
    "user": "postgres",
    "password": "991Bog31**"
}

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
        api_key="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"
    )

    prompt = f"""Você é um especialista em contas de energia elétrica brasileiras com foco em Geração Distribuída (GD).

Analise o texto extraído da conta e retorne SOMENTE um JSON válido, sem texto adicional, sem explicações, sem markdown.

ATENÇÃO — regras importantes antes de extrair:

1. NOME DO CLIENTE: Nome da pessoa titular. Ignore prefixos de localidade como "B JARDIM".
2. MÊS DE REFERÊNCIA: Formato "Abril / 2026". NÃO confundir com referência dos indicadores de qualidade.
3. DATA DE VENCIMENTO: Campo "VENCIMENTO" em destaque na conta. NÃO confundir com data de apresentação.
4. CONSUMO FATURADO (kWh): Valor no histórico dos últimos 13 meses referente ao mês atual. NÃO usar leitura bruta do medidor.
5. ENERGIA INJETADA (kWh): Energia enviada à rede. Sempre em kWh, nunca em R$.
6. SALDO DE CRÉDITOS (kWh): Campo "Saldo Acumulado". Se zero, retornar 0.

REGRA ESPECIAL — LEITURA POR MÉDIA:
Se houver "FATURAMENTO PELA MÉDIA", "MÉDIA/MÍNIMO" ou "LEITURA INFORMADA PELO CLIENTE", a conta pode ter acúmulo de meses.
Neste caso estime o número de meses acumulados comparando o consumo com o histórico.

Retorne EXATAMENTE neste formato JSON:
{{
  "nome_cliente": "",
  "numero_uc": "",
  "distribuidora": "",
  "mes_referencia": "",
  "data_vencimento": "DD/MM/AAAA",
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

    # Converter data
    data_venc = None
    try:
        data_venc = datetime.strptime(dados["data_vencimento"], "%d/%m/%Y").date()
    except:
        pass

    cur.execute("""
        INSERT INTO contas (
            cliente_id, mes_referencia, data_vencimento,
            consumo_kwh, energia_injetada_kwh, saldo_acumulado_kwh,
            valor_fatura, modalidade_tarifaria, status_sistema,
            percentual_gerado, leitura_por_media, meses_acumulados,
            mensagem_cliente
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        cliente_id,
        dados.get("mes_referencia"),
        data_venc,
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

    conn.commit()
    cur.close()
    conn.close()
    print("✅ Dados salvos no banco com sucesso!")

# Programa principal
if __name__ == "__main__":
    caminho = input("Digite o caminho completo do PDF da conta de energia: ")

    print("\n🔍 Lendo o PDF...")
    texto = extrair_texto_pdf(caminho)

    print("🤖 Analisando com IA...\n")
    resultado_raw = analisar_conta(texto)

    # Limpar e converter JSON
    try:
        resultado_raw = re.sub(r"```json|```", "", resultado_raw).strip()
        dados = json.loads(resultado_raw)

        print("📊 Resultado:")
        print("-" * 40)
        for chave, valor in dados.items():
            print(f"- {chave}: {valor}")

        salvar = input("\n💾 Deseja salvar no banco? (s/n): ")
        if salvar.lower() == "s":
            cliente_id = int(input("Digite o ID do cliente: "))
            salvar_no_banco(dados, cliente_id)

    except json.JSONDecodeError as e:
        print(f"❌ Erro ao interpretar JSON: {e}")
        print("Resposta bruta da IA:")
        print(resultado_raw)