#!/usr/bin/env python3
"""
WEG Integration Quick Test
Seu ambiente específico
"""

import requests
import json

# ✅ SUA CONFIGURAÇÃO
API_URL = "https://solar-portal-api.onrender.com"
TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjbGllbnRlX2lkIjo4LCJub21lIjoiQU5EUkVTU0EgSk9WQU5BIFZBU0NPTlNFTE9TIEZSQU5DTyIsIm51bWVyb191YyI6IjkvNDkxMzY3MS02IiwiZGlzdHJpYnVpZG9yYSI6IkVuZXJnaXNhIFN1bC1TdWRlc3RlIiwidGlwbyI6ImNsaWVudGUiLCJleHAiOjE3ODQwNjIwNTh9.0GcBRuiWlAYIYI-u-qfgTfCnZ59qhh9KUQ8zUe-xVuI"
CLIENTE_ID = "8"  # Seu cliente é 8, não 1
WEG_EMAIL = "henriquemedeiros54@gmail.com"
WEG_SENHA = "12345678"

print("=" * 60)
print("🧪 WEG Integration Test")
print("=" * 60)
print()

# ============================================================
# TESTE 1: LOGIN WEG
# ============================================================
print("📝 TESTE 1: Autenticação WEG")
print("-" * 60)

url_login = f"{API_URL}/weg/clientes/{CLIENTE_ID}/weg/login"
print(f"URL: POST {url_login}")
print(f"Email: {WEG_EMAIL}")
print()

headers = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {TOKEN}"
}

payload = {
    "email": WEG_EMAIL,
    "senha": WEG_SENHA
}

try:
    print("⏳ Conectando com WEG...")
    response = requests.post(url_login, json=payload, headers=headers, timeout=30)

    print(f"Status Code: {response.status_code}")
    print()

    data = response.json()
    print("📊 Response:")
    print(json.dumps(data, indent=2, ensure_ascii=False))
    print()

    if data.get("sucesso"):
        plantas = data.get("plantas", [])
        print(f"✅ PASSOU - {len(plantas)} planta(s) encontrada(s)!")
        print()
        for planta in plantas:
            print(f"   🌞 {planta.get('nome')}")
            print(f"      Capacidade: {planta.get('capacidade')} kW")
            print(f"      Energia hoje: {planta.get('energiaDia')}")
            print()
    else:
        print(f"❌ FALHOU - {data.get('detail', 'Erro desconhecido')}")

except Exception as e:
    print(f"❌ ERRO: {str(e)}")

# ============================================================
# TESTE 2: LISTAR PLANTAS
# ============================================================
print()
print("📝 TESTE 2: Listar Plantas")
print("-" * 60)

url_plants = f"{API_URL}/weg/clientes/{CLIENTE_ID}/weg/plantas"
print(f"URL: GET {url_plants}")
print()

try:
    print("⏳ Buscando plantas...")
    response = requests.get(url_plants, headers=headers, timeout=30)

    print(f"Status Code: {response.status_code}")
    print()

    data = response.json()
    print("📊 Response:")
    print(json.dumps(data, indent=2, ensure_ascii=False))
    print()

    if data.get("sucesso"):
        plantas = data.get("plantas", [])
        print(f"✅ PASSOU - {len(plantas)} planta(s) listada(s)!")
        print()
        for planta in plantas:
            print(f"   🌞 {planta.get('nome')} ({planta.get('id')})")
            print(f"      Energia hoje: {planta.get('energiaDia')} kWh")
            print(f"      Potência: {planta.get('potencia')} kW")
            print()
    else:
        print(f"❌ FALHOU - {data.get('detail', 'Erro desconhecido')}")

except Exception as e:
    print(f"❌ ERRO: {str(e)}")

# ============================================================
# TESTE 3: TOTALIZADORES
# ============================================================
print()
print("📝 TESTE 3: Totalizadores (Agregados)")
print("-" * 60)

url_totals = f"{API_URL}/weg/clientes/{CLIENTE_ID}/weg/totalizadores"
print(f"URL: GET {url_totals}")
print()

try:
    print("⏳ Buscando totalizadores...")
    response = requests.get(url_totals, headers=headers, timeout=30)

    print(f"Status Code: {response.status_code}")
    print()

    data = response.json()
    print("📊 Response:")
    print(json.dumps(data, indent=2, ensure_ascii=False))
    print()

    if data.get("sucesso"):
        t = data.get("totalizadores", {})
        energia_hoje = t.get("energiaHoje", {}).get("valor", 0)
        energia_mes = t.get("energiaMes", {}).get("valor", 0)
        potencia = t.get("potenciaAtiva", {}).get("valor", 0)
        economia = t.get("economiaTotal", {}).get("valor", 0)

        print(f"✅ PASSOU - Totalizadores carregados!")
        print()
        print(f"   ⚡ Energia hoje: {energia_hoje} kWh")
        print(f"   📊 Energia mês: {energia_mes} kWh")
        print(f"   💪 Potência ativa: {potencia} kW")
        print(f"   💰 Economia total: R$ {economia}")
        print()
    else:
        print(f"❌ FALHOU - {data.get('detail', 'Erro desconhecido')}")

except Exception as e:
    print(f"❌ ERRO: {str(e)}")

# ============================================================
# RESUMO
# ============================================================
print()
print("=" * 60)
print("🎉 TESTES COMPLETOS!")
print("=" * 60)
print()
print("Próximos passos:")
print()
print("1️⃣  Portal do Cliente:")
print("   https://jonassales-ecopower.github.io/solar-portal-app/portal.html?cliente_id=8")
print()
print("2️⃣  Painel do Integrador:")
print("   https://jonassales-ecopower.github.io/solar-portal-app/painel.html")
print()
print("✨ Procure pela seção '⚡ Monitoramento WEG' e teste!")
print()
