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

def _extrair_consumo_grandeza(texto: str, rotulo: str):
    """
    Localiza a linha da grandeza na tabela do medidor e devolve o valor do
    período. O PyPDF2 emite as colunas em ordens diferentes conforme a
    regional da Energisa:

      Layout A (ex.: Energisa MS):
        [Medidor] [Grandeza] [Posto] [Anterior] [Atual] [K] [Consumo]
        'N6201931610 Energia injetada Ponta 5681 6411 1 730'
      Layout B (ex.: Energisa Sul-Sudeste/MG):
        [Medidor] [Posto] [Grandeza] [K] [Consumo] [Atual] [Anterior]
        'N6175974810 Ponta Energia injetada 1 365 6723 6358'

    O layout é confirmado pela aritmética do medidor:
    consumo == (atual − anterior) × K. Um candidato validado tem prioridade;
    sem validação (leitura por média, virada de medidor), vale o primeiro
    candidato estrutural. Retorna float ou None.
    """
    NUM = r'([\d.,]+)'
    QUATRO_NUMS = NUM + r'\s+' + NUM + r'\s+' + NUM + r'\s+' + NUM

    candidatos = []
    m = re.search(rotulo + r'\s+\w+\s+' + QUATRO_NUMS, texto)
    if m:
        candidatos.append(('A', m.groups()))
    m = re.search(r'\w+\s+' + rotulo + r'\s+' + QUATRO_NUMS, texto)
    if m:
        candidatos.append(('B', m.groups()))

    fallback = None
    for layout, grupos in candidatos:
        try:
            n = [_parse_br_num(g) for g in grupos]
        except ValueError:
            continue
        if layout == 'A':
            anterior, atual, k, consumo = n
        else:
            k, consumo, atual, anterior = n
        if fallback is None:
            fallback = consumo
        if abs((atual - anterior) * k - consumo) < 0.5:
            return consumo
    return fallback

def extrair_medicoes_medidor(texto: str):
    """
    Extrai consumo ativo e energia injetada diretamente do texto da fatura
    usando a tabela RESERVADO AO FISCO (Energisa e similares).

    Retorna (consumo_kwh, injetado_kwh) — None se não encontrado.
    """
    consumo = _extrair_consumo_grandeza(texto, r'Energia ativa em kWh')
    injetado = _extrair_consumo_grandeza(texto, r'Energia injetada')
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

    Layouts conhecidos (mesma divergência regional da tabela do medidor):
      A — rótulo antes da data:
        'Leitura Anterior:05/05/2026 Leitura Atual: 05/06/2026 Dias: 31'
      B — data antes do rótulo, ordem invertida:
        '08/06/2026 Leitura Atual: 07/05/2026 Leitura Anterior: Dias: 32...'

    Retorna (anterior, atual) em DD/MM/AAAA, ou (None, None) se não encontrar.
    """
    DATA = r'(\d{2}/\d{2}/\d{4})'
    m = re.search(r'Leitura\s+Anterior:\s*' + DATA + r'\s+Leitura\s+Atual:\s*' + DATA, texto)
    if m:
        return m.group(1), m.group(2)
    m = re.search(DATA + r'\s*Leitura\s+Atual:\s*' + DATA + r'\s*Leitura\s+Anterior:', texto)
    if m:
        return m.group(2), m.group(1)
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
