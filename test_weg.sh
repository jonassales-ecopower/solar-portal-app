#!/bin/bash

# WEG Integration Test Script
# Configure abaixo antes de rodar

# ============================================
# CONFIGURAÇÃO - EDITE AQUI
# ============================================

# URL da sua API no Render
API_URL="https://seu-app.onrender.com"

# Seu token de autenticação
TOKEN="seu_token_aqui"

# ID do cliente para testar
CLIENTE_ID="1"

# Credenciais WEG
WEG_EMAIL="seu.email@weg.com"
WEG_SENHA="sua.senha"

# ============================================
# TESTES
# ============================================

echo "🧪 WEG Integration Test Suite"
echo "======================================"
echo ""

# Cores para output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Test 1: Login WEG
echo "${YELLOW}Teste 1: Autenticação WEG${NC}"
echo "URL: $API_URL/weg/clientes/$CLIENTE_ID/weg/login"
echo ""

RESPONSE=$(curl -s -X POST "$API_URL/weg/clientes/$CLIENTE_ID/weg/login" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d "{
    \"email\": \"$WEG_EMAIL\",
    \"senha\": \"$WEG_SENHA\"
  }")

echo "Response:"
echo "$RESPONSE" | jq '.' 2>/dev/null || echo "$RESPONSE"
echo ""

# Check if successful
if echo "$RESPONSE" | grep -q '"sucesso": true'; then
  echo -e "${GREEN}✅ PASSOU${NC}"
else
  echo -e "${RED}❌ FALHOU${NC}"
fi

echo ""
echo "======================================"
echo ""

# Test 2: Listar Plantas
echo "${YELLOW}Teste 2: Listar Plantas${NC}"
echo "URL: $API_URL/weg/clientes/$CLIENTE_ID/weg/plantas"
echo ""

RESPONSE=$(curl -s -X GET "$API_URL/weg/clientes/$CLIENTE_ID/weg/plantas" \
  -H "Authorization: Bearer $TOKEN")

echo "Response:"
echo "$RESPONSE" | jq '.' 2>/dev/null || echo "$RESPONSE"
echo ""

if echo "$RESPONSE" | grep -q '"sucesso": true'; then
  echo -e "${GREEN}✅ PASSOU${NC}"
else
  echo -e "${RED}❌ FALHOU${NC}"
fi

echo ""
echo "======================================"
echo ""

# Test 3: Totalizadores
echo "${YELLOW}Teste 3: Totalizadores (Agregados)${NC}"
echo "URL: $API_URL/weg/clientes/$CLIENTE_ID/weg/totalizadores"
echo ""

RESPONSE=$(curl -s -X GET "$API_URL/weg/clientes/$CLIENTE_ID/weg/totalizadores" \
  -H "Authorization: Bearer $TOKEN")

echo "Response:"
echo "$RESPONSE" | jq '.' 2>/dev/null || echo "$RESPONSE"
echo ""

if echo "$RESPONSE" | grep -q '"sucesso": true'; then
  echo -e "${GREEN}✅ PASSOU${NC}"
else
  echo -e "${RED}❌ FALHOU${NC}"
fi

echo ""
echo "======================================"
echo ""
echo "✅ Testes completos!"
echo ""
echo "Próximos passos:"
echo "1. Abra o portal: https://jonassales-ecopower.github.io/solar-portal-app/portal.html?cliente_id=$CLIENTE_ID"
echo "2. Procure pela seção '⚡ Monitoramento WEG'"
echo "3. Clique em 'Conectar WEG'"
echo "4. Use as mesmas credenciais"
echo ""
