#!/usr/bin/env python3
"""
WEG Integration Test Script
Run: python3 test_weg.py
"""

import requests
import json
import sys

# ============================================
# CONFIGURAÇÃO - EDITE AQUI
# ============================================

API_URL = "https://seu-app.onrender.com"
TOKEN = "seu_token_aqui"
CLIENTE_ID = "1"
WEG_EMAIL = "seu.email@weg.com"
WEG_SENHA = "sua.senha"

# ============================================
# TESTES
# ============================================

class Colors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'

def print_header(text):
    print(f"\n{Colors.BOLD}{Colors.WARNING}{text}{Colors.ENDC}")
    print("=" * 50)

def print_success(text):
    print(f"{Colors.OKGREEN}✅ {text}{Colors.ENDC}")

def print_error(text):
    print(f"{Colors.FAIL}❌ {text}{Colors.ENDC}")

def print_info(text):
    print(f"{Colors.OKCYAN}ℹ️  {text}{Colors.ENDC}")

def test_login():
    print_header("Teste 1: Autenticação WEG")

    url = f"{API_URL}/weg/clientes/{CLIENTE_ID}/weg/login"
    print_info(f"URL: {url}")
    print_info(f"Email: {WEG_EMAIL}")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TOKEN}"
    }

    payload = {
        "email": WEG_EMAIL,
        "senha": WEG_SENHA
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        data = response.json()

        print("\n📊 Response:")
        print(json.dumps(data, indent=2, ensure_ascii=False))

        if data.get("sucesso"):
            print_success(f"Autenticação funcionou! {len(data.get('plantas', []))} planta(s) encontrada(s)")
            return True
        else:
            print_error(f"Erro: {data.get('detail', 'Falha desconhecida')}")
            return False

    except requests.exceptions.Timeout:
        print_error("Timeout na requisição. Verifique se a API está online")
        return False
    except requests.exceptions.ConnectionError:
        print_error("Erro de conexão. Verifique a URL da API")
        return False
    except Exception as e:
        print_error(f"Erro: {str(e)}")
        return False

def test_plants():
    print_header("Teste 2: Listar Plantas")

    url = f"{API_URL}/weg/clientes/{CLIENTE_ID}/weg/plantas"
    print_info(f"URL: {url}")

    headers = {
        "Authorization": f"Bearer {TOKEN}"
    }

    try:
        response = requests.get(url, headers=headers, timeout=30)
        data = response.json()

        print("\n📊 Response:")
        print(json.dumps(data, indent=2, ensure_ascii=False))

        if data.get("sucesso"):
            plants = data.get("plantas", [])
            print_success(f"Plantas listadas! Total: {len(plants)}")
            for plant in plants:
                print(f"  • {plant.get('nome')}: {plant.get('energiaDia')} kWh hoje")
            return True
        else:
            print_error(f"Erro: {data.get('detail', 'Falha desconhecida')}")
            return False

    except Exception as e:
        print_error(f"Erro: {str(e)}")
        return False

def test_totals():
    print_header("Teste 3: Totalizadores (Agregados)")

    url = f"{API_URL}/weg/clientes/{CLIENTE_ID}/weg/totalizadores"
    print_info(f"URL: {url}")

    headers = {
        "Authorization": f"Bearer {TOKEN}"
    }

    try:
        response = requests.get(url, headers=headers, timeout=30)
        data = response.json()

        print("\n📊 Response:")
        print(json.dumps(data, indent=2, ensure_ascii=False))

        if data.get("sucesso"):
            total = data.get("totalizadores", {})
            energia_hoje = total.get("energiaHoje", {}).get("valor", 0)
            economia_total = total.get("economiaTotal", {}).get("valor", 0)

            print_success("Totalizadores carregados!")
            print(f"  • Energia hoje: {energia_hoje} kWh")
            print(f"  • Economia total: R$ {economia_total}")
            return True
        else:
            print_error(f"Erro: {data.get('detail', 'Falha desconhecida')}")
            return False

    except Exception as e:
        print_error(f"Erro: {str(e)}")
        return False

def main():
    print(f"\n{Colors.BOLD}{Colors.HEADER}")
    print("🧪 WEG Integration Test Suite")
    print(Colors.ENDC)

    print_info(f"API URL: {API_URL}")
    print_info(f"Cliente ID: {CLIENTE_ID}")
    print_info(f"Token: {TOKEN[:20]}..." if len(TOKEN) > 20 else f"Token: {TOKEN}")

    results = []

    # Run tests
    results.append(("Login WEG", test_login()))
    results.append(("Listar Plantas", test_plants()))
    results.append(("Totalizadores", test_totals()))

    # Summary
    print_header("📋 Resumo dos Testes")

    for test_name, passed in results:
        status = f"{Colors.OKGREEN}✅ PASSOU{Colors.ENDC}" if passed else f"{Colors.FAIL}❌ FALHOU{Colors.ENDC}"
        print(f"{test_name}: {status}")

    total_passed = sum(1 for _, p in results if p)
    total_tests = len(results)

    print(f"\nResultado: {total_passed}/{total_tests} testes passaram")

    if total_passed == total_tests:
        print_success("Tudo funcionando perfeitamente! 🎉")
        print_info("Próximos passos:")
        print(f"  1. Portal: https://jonassales-ecopower.github.io/solar-portal-app/portal.html?cliente_id={CLIENTE_ID}")
        print(f"  2. Painel: https://jonassales-ecopower.github.io/solar-portal-app/painel.html")
        return 0
    else:
        print_error("Alguns testes falharam. Verifique as configurações acima.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
