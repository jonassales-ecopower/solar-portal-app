# ==================== EXTRAÇÃO DETERMINÍSTICA ====================
# Funções puras de extração de dados estruturais das faturas (regex sobre o
# texto extraído pelo PyPDF2) e da convenção de período de faturamento.
#
# Filosofia: IA para o que é variável, regex para o que é estrutural.
# Estas funções têm precedência sobre o resultado da IA nos endpoints de upload.
#
# Padrões atuais: Energisa (DANF3E). Ao adicionar outras distribuidoras
# (CEMIG, CPFL, Enel, Neoenergia...), estenda os padrões aqui — os endpoints
# já tratam None como "não encontrado" e mantêm o valor da IA como fallback.
#
# Testado por tests/test_extracao.py com fixtures de faturas reais.

import re
from datetime import date

# ==================== NÚMEROS ====================

def _parse_br_num(s: str) -> float:
    """Converte número BR ('1.864,00' ou '505') para float."""
    s = s.strip()
    if ',' in s:
        s = s.replace('.', '').replace(',', '.')
    return float(s)

# ==================== MEDIDOR (RESERVADO AO FISCO) ====================

def extrair_medicoes_medidor(texto: str):
    """
    Extrai consumo ativo e energia injetada diretamente do texto da fatura
    usando a tabela RESERVADO AO FISCO (Energisa e similares).

    Formato esperado (página 1, extraído limpo pelo PyPDF2):
      [Medidor] Energia ativa em kWh [Posto] [Anterior] [Atual] [K] [Consumo]
      [Medidor] Energia injetada     [Posto] [Anterior] [Atual] [K] [Injetado]

    Retorna (consumo_kwh, injetado_kwh) — None se não encontrado.
    """
    NUM = r'[\d.,]+'

    consumo = None
    m = re.search(
        r'Energia ativa em kWh\s+\w+\s+(' + NUM + r')\s+(' + NUM + r')\s+(' + NUM + r')\s+(' + NUM + r')',
        texto
    )
    if m:
        try:
            consumo = _parse_br_num(m.group(4))
        except ValueError:
            pass

    injetado = None
    m2 = re.search(
        r'Energia injetada\s+\w+\s+(' + NUM + r')\s+(' + NUM + r')\s+(' + NUM + r')\s+(' + NUM + r')',
        texto
    )
    if m2:
        try:
            injetado = _parse_br_num(m2.group(4))
        except ValueError:
            pass

    return consumo, injetado

# ==================== MÊS DE REFERÊNCIA ====================

_MESES_PT = ['Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
             'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro']

def normalizar_mes_referencia(valor):
    """
    Normaliza qualquer variação ('junho/2026', 'JUNHO / 2026', 'Junho/2026')
    para o formato canônico 'Junho / 2026'.

    Crítico: mes_referencia é a chave de junção entre a conta da geradora e as
    contas das beneficiárias no rateio — formatos divergentes quebram o vínculo.
    """
    if not valor:
        return None
    m = re.search(r'(' + '|'.join(_MESES_PT) + r')\s*/?\s*(\d{4})', str(valor), re.IGNORECASE)
    if not m:
        return None
    mes_canon = next((x for x in _MESES_PT if x.lower() == m.group(1).lower()), None)
    if not mes_canon:
        return None
    return f"{mes_canon} / {m.group(2)}"

def extrair_mes_referencia(texto):
    """
    Extrai o mês de referência direto do texto da fatura (campo 'REF: MÊS / ANO').
    O primeiro nome de mês por extenso seguido de ano é sempre o REF — o histórico
    de consumo usa abreviações (JUN/26) que não casam com o padrão.
    """
    return normalizar_mes_referencia(texto)

# ==================== DATAS DE LEITURA ====================

def extrair_datas_leitura(texto):
    """
    Extrai as datas de leitura direto do texto da fatura.
    Âncora (página 2, Energisa): 'Leitura Anterior:05/05/2026 Leitura Atual: 05/06/2026 Dias: 31'
    Essas datas definem o período exato para somar a geração do inversor —
    a conta cobre leitura a leitura (ex.: 13/04 a 14/05), não o mês-calendário.

    Retorna (anterior, atual) em DD/MM/AAAA, ou (None, None) se não encontrar.
    """
    m = re.search(
        r'Leitura\s+Anterior:\s*(\d{2}/\d{2}/\d{4})\s+Leitura\s+Atual:\s*(\d{2}/\d{2}/\d{4})',
        texto
    )
    if m:
        return m.group(1), m.group(2)
    return None, None

# ==================== PERÍODO DE FATURAMENTO ====================

def somar_geracao_periodo(registros: list, data_inicio: date, data_fim: date) -> float:
    """
    Soma total_kwh dos registros diários dentro do período de faturamento.

    Intervalo: [data_inicio, data_fim) — inclui o dia da leitura anterior e exclui
    o dia da leitura atual. Assim o total cobre exatamente os "Dias: N" da fatura
    e o dia da leitura atual entra apenas na conta seguinte (sem dupla contagem).

    registros: lista de {"data": "YYYY-MM-DD", "total_kwh": float}
    """
    total = 0.0
    for item in registros:
        d = date.fromisoformat(item["data"])
        if data_inicio <= d < data_fim:
            total += float(item["total_kwh"])
    return round(total, 2)
