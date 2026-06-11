# -*- coding: utf-8 -*-
"""
Testes de regressão da extração determinística (extracao.py).

Os fixtures reproduzem o texto REAL que o PyPDF2 extrai das faturas Energisa
(DANF3E) usadas na validação de Junho/2026: ACADEMIA (geradora), RESTAURANTE
(beneficiária 80%) e CASA (beneficiária 20%). Cada bug corrigido em produção
tem um teste aqui — se um padrão da Energisa mudar ou uma nova distribuidora
quebrar um regex, estes testes acusam antes do cliente.

Rodar:  python -m pytest tests/ -v
   ou:  python tests/test_extracao.py
"""

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extracao import (_parse_br_num, extrair_medicoes_medidor,
                      normalizar_mes_referencia, extrair_mes_referencia,
                      extrair_datas_leitura, somar_geracao_periodo)

# ==================== FIXTURES (texto real das faturas) ====================

# ACADEMIA — UC geradora. Tabela RESERVADO AO FISCO da página 1:
# injetou 730 (6411-5681), consumiu da rede 505 (2369-1864) → excedente 225.
# Inclui a linha "Energia Atv Injetada GDII" dos Itens da Fatura (crédito
# APLICADO, não o injetado real) para garantir que o regex não a confunde.
TEXTO_ACADEMIA = """\
NOTA FISCAL DE ENERGIA ELÉTRICA - DANF3E
REF: Junho / 2026 VENCIMENTO 15/06/2026
Itens da Fatura Unid. Quant. Tarifa Valor
Consumo em kWh KWH 505,00 0,976610 493,19
Energia Atv Injetada GDII KWH 505,00 0,976610 -493,19
Ajuste GDII 37,15
RESERVADO AO FISCO
Medidor Grandeza Posto Anterior Atual Const. Consumo
N6201931610 Energia ativa em kWh Ponta 1864 2369 1 505
N6201931610 Energia injetada Ponta 5681 6411 1 730
Leitura Anterior:05/05/2026 Leitura Atual: 05/06/2026 Dias: 31
"""

# RESTAURANTE — beneficiária 80%, recebeu 180 kWh de crédito oUC.
TEXTO_RESTAURANTE = """\
NOTA FISCAL DE ENERGIA ELÉTRICA - DANF3E
REF: Junho / 2026 VENCIMENTO 15/06/2026
Itens da Fatura Unid. Quant. Tarifa Valor
Consumo em kWh KWH 666,00 0,976610 650,42
Energia Atv Injet. oUC GDII KWH 180,00 0,976610 -175,79
RESERVADO AO FISCO
Medidor Grandeza Posto Anterior Atual Const. Consumo
N6201931611 Energia ativa em kWh Ponta 22006 22672 1 666
Leitura Anterior:05/05/2026 Leitura Atual: 05/06/2026 Dias: 31
"""

# CASA — beneficiária 20%, recebeu 45 kWh oUC + sobras mUC do mês anterior
# (22+10), que devem ser ignoradas na verificação do rateio do mês.
TEXTO_CASA = """\
NOTA FISCAL DE ENERGIA ELÉTRICA - DANF3E
REF: Junho / 2026 VENCIMENTO 15/06/2026
Itens da Fatura Unid. Quant. Tarifa Valor
Consumo em kWh KWH 145,00 0,976610 141,60
Energia Atv Injet. oUC GDII KWH 45,00 0,976610 -43,95
Energia Atv Injet. mUC GDII KWH 22,00 0,976610 -21,49
Energia Atv Injet. mUC GDII KWH 10,00 0,976610 -9,77
RESERVADO AO FISCO
Medidor Grandeza Posto Anterior Atual Const. Consumo
N6201931612 Energia ativa em kWh Ponta 22750 22895 1 145
Leitura Anterior:05/05/2026 Leitura Atual: 05/06/2026 Dias: 31
"""

# ==================== _parse_br_num ====================

def test_parse_br_num_inteiro():
    assert _parse_br_num('505') == 505.0

def test_parse_br_num_milhar_decimal():
    assert _parse_br_num('1.864,00') == 1864.0

def test_parse_br_num_decimal():
    assert _parse_br_num('894,10') == 894.1

# ==================== extrair_medicoes_medidor ====================

def test_medidor_geradora():
    # ACADEMIA: consumo real 505, injetado real 730 — NÃO os 505 da linha GDII
    assert extrair_medicoes_medidor(TEXTO_ACADEMIA) == (505.0, 730.0)

def test_medidor_beneficiaria_restaurante():
    assert extrair_medicoes_medidor(TEXTO_RESTAURANTE) == (666.0, None)

def test_medidor_beneficiaria_casa():
    assert extrair_medicoes_medidor(TEXTO_CASA) == (145.0, None)

def test_medidor_nao_confunde_gdii_com_injetada():
    # 'Energia Atv Injetada GDII' (Itens da Fatura) não pode casar com o
    # padrão 'Energia injetada' da tabela do fisco (case-sensitive).
    texto = "Energia Atv Injetada GDII KWH 505,00 0,976610 -493,19"
    assert extrair_medicoes_medidor(texto) == (None, None)

def test_medidor_ausente():
    assert extrair_medicoes_medidor("fatura sem tabela do fisco") == (None, None)

# ==================== normalizar_mes_referencia ====================

def test_normalizar_minusculo():
    assert normalizar_mes_referencia('junho/2026') == 'Junho / 2026'

def test_normalizar_maiusculo_com_espacos():
    assert normalizar_mes_referencia('JUNHO / 2026') == 'Junho / 2026'

def test_normalizar_ja_canonico():
    assert normalizar_mes_referencia('Junho / 2026') == 'Junho / 2026'

def test_normalizar_com_acento():
    assert normalizar_mes_referencia('Março / 2026') == 'Março / 2026'

def test_normalizar_sem_barra():
    assert normalizar_mes_referencia('Dezembro 2025') == 'Dezembro / 2025'

def test_normalizar_vazio_e_none():
    assert normalizar_mes_referencia('') is None
    assert normalizar_mes_referencia(None) is None

def test_normalizar_formato_numerico_nao_casa():
    assert normalizar_mes_referencia('06/2026') is None

# ==================== extrair_mes_referencia ====================

def test_mes_referencia_nas_tres_faturas():
    # Chave de junção do rateio: precisa sair idêntica nas três contas.
    assert extrair_mes_referencia(TEXTO_ACADEMIA) == 'Junho / 2026'
    assert extrair_mes_referencia(TEXTO_RESTAURANTE) == 'Junho / 2026'
    assert extrair_mes_referencia(TEXTO_CASA) == 'Junho / 2026'

# ==================== extrair_datas_leitura ====================

def test_datas_leitura_geradora():
    assert extrair_datas_leitura(TEXTO_ACADEMIA) == ('05/05/2026', '05/06/2026')

def test_datas_leitura_sem_rotulo_nao_casa():
    # Datas soltas da página 1 (emissão, vencimento) não podem ser confundidas
    # com as datas de leitura — só o rótulo da página 2 é âncora válida.
    texto = "Emissão: 06/06/2026 VENCIMENTO 15/06/2026 Apresentação 07/06/2026"
    assert extrair_datas_leitura(texto) == (None, None)

# ==================== somar_geracao_periodo ====================

def _registros_diarios(inicio: date, fim_inclusive: date, kwh_dia: float = 1.0):
    dias = (fim_inclusive - inicio).days + 1
    return [{"data": str(inicio + timedelta(days=i)), "total_kwh": kwh_dia}
            for i in range(dias)]

def test_periodo_31_dias_nao_32():
    # Fatura 05/05 → 05/06, Dias: 31. O dia da leitura atual (05/06) fica fora.
    registros = _registros_diarios(date(2026, 5, 1), date(2026, 6, 30))
    total = somar_geracao_periodo(registros, date(2026, 5, 5), date(2026, 6, 5))
    assert total == 31.0

def test_contas_consecutivas_sem_dupla_contagem():
    # Conta A: 05/05→05/06 (31 dias). Conta B: 05/06→05/07 (30 dias).
    # O dia 05/06 só pode contar na conta B. Soma das duas = 61 dias únicos.
    registros = _registros_diarios(date(2026, 5, 1), date(2026, 7, 31))
    a = somar_geracao_periodo(registros, date(2026, 5, 5), date(2026, 6, 5))
    b = somar_geracao_periodo(registros, date(2026, 6, 5), date(2026, 7, 5))
    assert a == 31.0
    assert b == 30.0
    assert a + b == 61.0

def test_periodo_sem_registros():
    assert somar_geracao_periodo([], date(2026, 5, 5), date(2026, 6, 5)) == 0.0

def test_periodo_soma_valores_reais():
    registros = [
        {"data": "2026-05-05", "total_kwh": 28.4},
        {"data": "2026-05-20", "total_kwh": 31.7},
        {"data": "2026-06-04", "total_kwh": 25.9},
        {"data": "2026-06-05", "total_kwh": 30.0},  # dia da leitura atual: fora
    ]
    total = somar_geracao_periodo(registros, date(2026, 5, 5), date(2026, 6, 5))
    assert total == 86.0

# ==================== runner sem pytest ====================

if __name__ == "__main__":
    falhas = 0
    for nome, fn in sorted(globals().items()):
        if nome.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  OK   {nome}")
            except AssertionError as e:
                falhas += 1
                print(f"  FALHA {nome}: {e}")
    print(f"\n{falhas} falha(s)." if falhas else "\nTodos os testes passaram.")
    sys.exit(1 if falhas else 0)
