# 🧪 WEG Integration Test Guide

## Pre-Requisites para Testar

### ✅ Checklist Antes de Começar

1. **Conta WEG Ativa**
   - Email e senha de uma conta com inversores registrados
   - Acesso a: https://sun.weg.net
   - Pelo menos 1 planta solar cadastrada

2. **Portal Atualizado**
   - Fez pull das alterações mais recentes
   - Database migrations foram executadas
   - Backend está rodando

3. **Ambiente Configurado**
   - `DATABASE_URL` está correto no `.env`
   - API backend acessível
   - Portal carregando normalmente

---

## Teste 1: Backend - Autenticação WEG

### Objetivo
Verificar se o backend consegue autenticar com a WEG API

### Passo 1: Testar endpoint de login via cURL

```bash
curl -X POST http://localhost:8000/weg/clientes/1/weg/login \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer SEU_TOKEN_AQUI" \
  -d '{
    "email": "seu.email@weg.com",
    "senha": "sua.senha"
  }'
```

**Resultado esperado:**
```json
{
  "sucesso": true,
  "mensagem": "Autenticado com sucesso. 3 usina(s) encontrada(s).",
  "plantas": [
    {
      "id": "12345",
      "nome": "Usina Solar - Projeto 1",
      "capacidade": "5.0",
      "energiaDia": "5.23 kWh"
    }
  ]
}
```

**Se falhar:**
- ❌ `"Authentication failed"` → Verifique email/senha
- ❌ `"No plants found"` → A conta WEG não tem plantas. Cadastre uma em https://sun.weg.net
- ❌ `"Token expired"` → Tente novamente em alguns segundos
- ❌ `Connection timeout` → Verifique se WEG API está online

---

## Teste 2: Backend - Listar Plantas

### Objetivo
Verificar se conseguimos listar todas as plantas cadastradas

### Passo 1: Fazer login primeiro (Teste 1)

### Passo 2: Testar endpoint de plantas

```bash
curl -X GET http://localhost:8000/weg/clientes/1/weg/plantas \
  -H "Authorization: Bearer SEU_TOKEN_AQUI"
```

**Resultado esperado:**
```json
{
  "sucesso": true,
  "plantas": [
    {
      "id": "12345",
      "nome": "Usina Solar - Julho",
      "numerUC": "1234567-89-0-1",
      "distribuidora": "Energisa",
      "capacidade": 5.0,
      "energiaDia": 5.23,
      "energiaMes": 156.8,
      "potencia": 3.45,
      "yieldDia": 1.044,
      "yieldMes": 31.36
    }
  ],
  "total": 1
}
```

**Se falhar:**
- ❌ `401 Unauthorized` → Token expirou. Faça login novamente
- ❌ `Empty plantas array` → Nenhuma planta encontrada na conta
- ❌ `Network error` → Verifique conectividade

---

## Teste 3: Backend - Totalizadores (Agregados)

### Objetivo
Verificar dados agregados de todas as plantas

### Passo 1: Testar endpoint de totalizadores

```bash
curl -X GET http://localhost:8000/weg/clientes/1/weg/totalizadores \
  -H "Authorization: Bearer SEU_TOKEN_AQUI"
```

**Resultado esperado:**
```json
{
  "sucesso": true,
  "totalizadores": {
    "energiaHoje": {
      "valor": 25.3,
      "unidade": "kWh",
      "raw": "25.3 kWh"
    },
    "energiaMes": {
      "valor": 890.5,
      "unidade": "kWh"
    },
    "potenciaAtiva": {
      "valor": 15.8,
      "unidade": "kW"
    },
    "economiaTotal": {
      "valor": 49750.50,
      "unidade": "R$"
    }
  }
}
```

---

## Teste 4: Frontend - Portal do Cliente

### Objetivo
Testar a integração visual no portal do cliente

### Passo 1: Abrir Portal
```
http://localhost:3000/portal.html?cliente_id=1
```
(ou seu domínio real)

### Passo 2: Procurar pela seção WEG
- Scroll down até encontrar "⚡ Monitoramento WEG"
- Verifique se a seção está visível
- Deve ter botão "🔗 Conectar WEG"

### Passo 3: Clicar em "Conectar WEG"
- Modal deve abrir
- Campos para email e senha visíveis
- Botão "🔗 Conectar" funcional

### Passo 4: Preencher Credenciais
```
Email: seu.email@weg.com
Senha: sua.senha
```

### Passo 5: Clicar Conectar
**Resultado esperado:**
- ✅ Alert: "✅ Autenticado com WEG! X usina(s) encontrada(s)."
- ✅ Modal fecha automaticamente
- ✅ Seção WEG mostra:
  - Status "✅ Conectado com WEG"
  - Lista de plantas
  - Cards com energia/potência
  - Botão "❌ Desconectar"

### Passo 6: Ver Detalhes de Planta
- Clique em uma das plantas da lista
- Modal "Usina Solar - [Nome]" deve abrir
- Mostrar: Energia, Potência, Capacity, Yield
- Fechar modal

### Passo 7: Verificar Auto-Refresh
- Espere 5 minutos
- Verifique se dados foram atualizados
- Abra console (F12) e procure por requests ao `/weg/clientes/`

### Passo 8: Desconectar
- Clique em "❌ Desconectar"
- Confirm dialog deve aparecer
- Clique confirmar
- ✅ Alert: "✅ Conta WEG desconectada"
- Seção volta ao estado inicial

**Possíveis Erros:**
- ❌ "Conta não identificada" → Verifique ID do cliente
- ❌ "Erro de conexão" → Verifique if API está rodando
- ❌ Dados não atualizam → Verifique browser console (F12 → Console)
- ❌ Modal não abre → Verificar JavaScript errors

---

## Teste 5: Frontend - Painel do Integrador

### Objetivo
Testar gerenciamento de WEG para clientes

### Passo 1: Abrir Painel
```
http://localhost:3000/painel.html
```

### Passo 2: Fazer Login como Integrador
- Use credenciais de um integrador
- Acesse painel com sucesso

### Passo 3: Abrir Cliente para Editar
- Clique em um cliente na tabela
- Modal de edição do cliente abre

### Passo 4: Navegue para Aba WEG
- Na modal, procure por abas no topo
- Clique em "⚡ WEG"

### Passo 5: Conectar Conta WEG
- Preencha email e senha da WEG
- Clique "🔗 Conectar com WEG"
- Aguarde status "Carregando..."

**Resultado esperado:**
- ✅ Campos desaparecem
- ✅ Status "✅ Conectado com WEG" aparece
- ✅ Lista de plantas conectadas mostra
- ✅ Botão "❌ Desconectar" disponível

### Passo 6: Verificar Dados da Planta
- Lista deve mostrar:
  - Nome da planta
  - Energia hoje (kWh)
  - Potência atual (kW)

### Passo 7: Salvar Cliente
- Clique "✅ Salvar" no final da modal
- Dados devem ser persistidos

### Passo 8: Desconectar WEG
- Abra cliente novamente
- Vá para aba WEG
- Clique "❌ Desconectar"
- Confirm e salve

**Possíveis Erros:**
- ❌ Aba WEG não aparece → Verifique se painel.html foi atualizado
- ❌ "Conectando..." travado → Verifique logs do backend
- ❌ Dados não carregam → Abra console (F12) e veja requests

---

## Teste 6: Dados e Valores

### Objetivo
Validar que os dados são precisos

### Verificação 1: Comparar com Portal WEG
1. Abra https://sun.weg.net
2. Faça login com mesma conta
3. Compare valores:
   - Energia hoje (deve bater)
   - Potência (deve estar próxima)
   - Plantas listadas (mesma quantidade)

### Verificação 2: Unidades
- Energia deve estar em kWh ✅
- Potência deve estar em kW ✅
- Valores decimais devem estar corretos ✅

### Verificação 3: Histórico
- Energia de dias anteriores deve estar diminuindo (ou igual)
- Totais mensais devem ser maiores que diários

---

## Teste 7: Casos de Erro

### Erro 1: Credenciais Inválidas
**O que fazer:**
- Teste com senha errada propositalmente
- **Resultado esperado:** ❌ "Erro: Autenticação falhou"
- Não deve travar o sistema

### Erro 2: Sem Plantas Cadastradas
**O que fazer:**
- Use conta WEG que não tem plantas
- **Resultado esperado:** ❌ "No plants found"
- Interface deve mostrar mensagem útil

### Erro 3: API WEG Offline
**O que fazer:**
- Desconecte internet (ou use VPN para simular)
- Tente conectar
- **Resultado esperado:** ❌ Timeout ou erro de conexão
- Sistema deve recuperar quando internet volta

### Erro 4: Token Expirado
**O que fazer:**
- Conecte com WEG
- Aguarde 24 horas (ou simule mock)
- Tente fazer nova requisição
- **Resultado esperado:** Token é refrescado automaticamente

---

## Teste 8: Performance

### Objetivo
Verificar se sistema é responsivo

### Métrica 1: Tempo de Login
- Conectar com WEG
- Medir tempo até aparecer plantas
- **Alvo:** < 3 segundos

### Métrica 2: Tempo de Carregamento de Plantas
- Dados devem carregar sem delay notável
- **Alvo:** < 1 segundo

### Métrica 3: Auto-Refresh
- Não deve haver lag durante refresh
- Deve ser silencioso (sem reload visual)
- **Alvo:** < 500ms

### Métrica 4: Mobile Performance
- Testar em device real (não só browser)
- Verificar se scroll é suave
- **Alvo:** 60 FPS

---

## Teste 9: Segurança

### Checklist de Segurança
- [ ] Senha não aparece no console (F12)
- [ ] Token não é armazenado em localStorage
- [ ] Token é salvo seguramente no servidor
- [ ] Logout remove credenciais
- [ ] Não há dados sensíveis em requests

### Como Verificar:
1. Abra F12 → Network
2. Monitore requests ao `/weg/`
3. Verifique headers:
   - Authorization header deve estar presente
   - Senha não deve aparecer em request body
   - Response não deve conter token

---

## Teste 10: Responsividade Mobile

### Testar em iPhone/Android
1. Abra portal em mobile
2. Navegue até "⚡ Monitoramento WEG"
3. Teste cada funcionalidade:
   - ✅ Conectar modal abre bem
   - ✅ Campos são legíveis
   - ✅ Botão é clicável
   - ✅ Lista de plantas scroll suave
   - ✅ Modal de detalhes redimensiona

### Breakpoints a Testar:
- 320px (iPhone pequeno)
- 375px (iPhone padrão)
- 414px (iPhone Plus)
- 768px (iPad)
- 1024px (iPad Pro)

---

## Checklist de Teste Final

### Backend
- [ ] Login retorna token válido
- [ ] Plantas listam com dados corretos
- [ ] Totalizadores agregam corretamente
- [ ] Token refresh funciona
- [ ] Erros tratados gracefully
- [ ] Nenhum SQL injection possível
- [ ] Performance aceitável

### Frontend - Portal
- [ ] Seção WEG renderiza
- [ ] Modal de login abre/fecha
- [ ] Autenticação funciona
- [ ] Plantas listam com dados
- [ ] Modal de detalhes mostra métricas
- [ ] Auto-refresh funciona (5 min)
- [ ] Desconectar limpa dados
- [ ] Mobile responsivo
- [ ] Sem console errors

### Frontend - Painel
- [ ] Aba WEG aparece
- [ ] Formulário WEG funciona
- [ ] Dados persistem após salvar
- [ ] Desconectar funciona
- [ ] Interface é intuitiva
- [ ] Sem erros visuais

### Cross-Browser
- [ ] Chrome ✅
- [ ] Firefox ✅
- [ ] Safari ✅
- [ ] Edge ✅

---

## Relatório de Teste

Após completar os testes, preencha:

```markdown
## Teste WEG Integration - [DATA]

### Status Geral
- [ ] ✅ PASSOU (Pronto para produção)
- [ ] ⚠️ PASSOU COM RESSALVAS (Corrigir antes de produção)
- [ ] ❌ FALHOU (Bloqueadores encontrados)

### Backend
- Autenticação: [PASSED/FAILED]
- Listagem de Plantas: [PASSED/FAILED]
- Totalizadores: [PASSED/FAILED]
- Tratamento de Erros: [PASSED/FAILED]

### Frontend Portal
- Renderização: [PASSED/FAILED]
- Autenticação: [PASSED/FAILED]
- Exibição de Dados: [PASSED/FAILED]
- Auto-Refresh: [PASSED/FAILED]
- Mobile: [PASSED/FAILED]

### Frontend Painel
- Tab WEG: [PASSED/FAILED]
- Gerenciamento: [PASSED/FAILED]
- Persistência: [PASSED/FAILED]

### Problemas Encontrados
1. [Descrição do problema]
   - Severidade: [CRÍTICA/ALTA/MÉDIA/BAIXA]
   - Status: [ABERTO/RESOLVIDO]

### Observações
[Qualquer feedback ou melhoria sugerida]

### Assinatura
Testado por: [Nome]
Data: [Data]
Aprovado para produção: [SIM/NÃO]
```

---

## Próximos Passos Após Teste

### Se ✅ PASSOU
1. Deploy para produção
2. Monitorar logs por 24h
3. Coletar feedback de usuários

### Se ⚠️ PASSOU COM RESSALVAS
1. Corrigir issues identificados
2. Re-testar aquele específico fluxo
3. Então deploy

### Se ❌ FALHOU
1. Debugar issues
2. Abrir issues no GitHub
3. Re-testar após correções

---

**Bom teste! Me manda os resultados depois! 🚀**
