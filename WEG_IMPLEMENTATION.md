# 🔌 WEG/SunWEG Integration Implementation Guide

## Overview

This guide walks through integrating WEG solar monitoring into your SunCheck portal. WEG provides real-time energy generation data for solar installations.

**Files created:**
- `weg_api.py` - Python async client for WEG API
- `weg_integration_example.py` - FastAPI endpoint examples
- `weg_frontend_example.js` - JavaScript for portal/painel
- `WEG_API_Integration.md` - Complete API documentation

---

## Step 1: Database Schema Update

Add WEG account columns to your `contas` table:

```sql
ALTER TABLE contas ADD COLUMN (
    weg_email VARCHAR(255),
    weg_senha VARCHAR(255),
    weg_token VARCHAR(1024),
    weg_ultimo_sincronismo TIMESTAMP,
    weg_ativo BOOLEAN DEFAULT FALSE
);
```

---

## Step 2: Add WEG Router to FastAPI

In your `api.py`, add the import at the top:

```python
from weg_integration_example import weg_router
```

Then include the router after creating your FastAPI app:

```python
app = FastAPI()
# ... existing routes ...

# Include WEG endpoints
app.include_router(weg_router)
```

This will expose endpoints like:
- `POST /weg/clientes/{cliente_id}/weg/login`
- `GET /weg/clientes/{cliente_id}/weg/plantas`
- `GET /weg/clientes/{cliente_id}/weg/totalizadores`
- etc.

---

## Step 3: Add WEG UI to Portal

In `portal.html`, add a new section for WEG monitoring:

```html
<!-- Add inside your dashboard HTML -->
<div id="weg-container" style="margin-top:20px;">
    <h3>📊 Monitoramento WEG</h3>
    
    <!-- Login section -->
    <div id="weg-login" style="display:none;">
        <input type="email" id="weg-email" placeholder="Email WEG">
        <input type="password" id="weg-senha" placeholder="Senha WEG">
        <button onclick="autenticarWEG()">Conectar com WEG</button>
    </div>
    
    <!-- Plants list -->
    <div id="weg-plantas-container"></div>
    
    <!-- Totals dashboard -->
    <div id="weg-totalizadores-container"></div>
    
    <!-- Disconnect button -->
    <button id="weg-desconectar-btn" style="display:none;" 
            onclick="desconectarWEG()">
        Desconectar WEG
    </button>
</div>
```

Include the JavaScript file:

```html
<script src="weg_frontend_example.js"></script>
```

Then initialize on page load:

```javascript
<script>
document.addEventListener("DOMContentLoaded", async function() {
    // ... existing code ...
    await setupWEGMonitoring();
});
</script>
```

---

## Step 4: Add WEG UI to Integrador Painel

In `painel.html`, add similar section for integrador to manage WEG accounts:

```html
<div id="painel-weg" style="margin-top:20px; padding:20px; background:#f0f4f8; border-radius:10px;">
    <h3>⚙️ Configuração WEG</h3>
    
    <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:12px;">
        <input type="email" id="weg-email-integrador" placeholder="Email WEG da usina">
        <input type="password" id="weg-senha-integrador" placeholder="Senha WEG">
    </div>
    
    <button onclick="configurarWEG()"
            style="background:#0F3E63; color:white; border:none; padding:10px 20px; border-radius:6px; cursor:pointer; margin-right:8px;">
        🔗 Conectar WEG
    </button>
    
    <div id="weg-plantas-integrador" style="margin-top:16px;"></div>
</div>
```

---

## Step 5: Create WEG Configuration Form

Add modal for WEG login in portal:

```html
<div id="modal-weg-login" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.5); z-index:1000; align-items:center; justify-content:center;">
    <div style="background:white; padding:24px; border-radius:12px; max-width:400px; width:90%;">
        <h2 style="margin-top:0;">Conectar com WEG</h2>
        <p style="color:#666; font-size:14px; margin-bottom:16px;">
            Faça login com sua conta WEG para monitorar sua geração solar em tempo real.
        </p>
        
        <input type="email" id="weg-email-modal" placeholder="Email WEG" style="width:100%; padding:10px; margin-bottom:12px; border:1px solid #ccc; border-radius:6px;">
        <input type="password" id="weg-senha-modal" placeholder="Senha WEG" style="width:100%; padding:10px; margin-bottom:12px; border:1px solid #ccc; border-radius:6px;">
        
        <button onclick="autenticarWEGModal()" 
                style="width:100%; background:#0F3E63; color:white; border:none; padding:10px; border-radius:6px; cursor:pointer; font-weight:600; margin-bottom:8px;">
            Conectar
        </button>
        
        <button onclick="document.getElementById('modal-weg-login').style.display='none'"
                style="width:100%; background:#f0f4f8; border:1px solid #e2e8f0; padding:10px; border-radius:6px; cursor:pointer;">
            Cancelar
        </button>
    </div>
</div>
```

---

## Step 6: Add JavaScript Helper Functions

Add these functions to portal.html or painel.html:

```javascript
// Get client ID from URL
function getClienteId() {
    return new URLSearchParams(window.location.search).get("cliente_id");
}

// Show WEG login modal
function mostrarLoginWEG() {
    document.getElementById("modal-weg-login").style.display = "flex";
}

// Authenticate and setup WEG monitoring
async function autenticarWEGModal() {
    const email = document.getElementById("weg-email-modal").value;
    const senha = document.getElementById("weg-senha-modal").value;
    
    if (!email || !senha) {
        alert("Por favor preencha email e senha");
        return;
    }
    
    const clienteId = getClienteId();
    const success = await wegLogin(clienteId, email, senha);
    
    if (success) {
        document.getElementById("modal-weg-login").style.display = "none";
        document.getElementById("weg-login").style.display = "none";
        document.getElementById("weg-desconectar-btn").style.display = "block";
    }
}

// Simple WEG login wrapper
async function autenticarWEG() {
    const email = document.getElementById("weg-email")?.value;
    const senha = document.getElementById("weg-senha")?.value;
    
    if (!email || !senha) {
        alert("Por favor preencha email e senha");
        return;
    }
    
    const clienteId = getClienteId();
    await wegLogin(clienteId, email, senha);
}

// Configure WEG for integrador
async function configurarWEG() {
    const email = document.getElementById("weg-email-integrador").value;
    const senha = document.getElementById("weg-senha-integrador").value;
    
    if (!email || !senha) {
        alert("Por favor preencha email e senha");
        return;
    }
    
    const clienteId = getClienteId();
    const success = await wegLogin(clienteId, email, senha);
    
    if (success) {
        document.getElementById("weg-email-integrador").value = "";
        document.getElementById("weg-senha-integrador").value = "";
    }
}
```

---

## Step 7: Refresh Data Periodically

The JavaScript setup automatically refreshes WEG data every 5 minutes:

```javascript
// In weg_frontend_example.js setupWEGMonitoring():
setInterval(async () => {
    await carregarPlantasWEG(clienteId);
    await exibirTotalizadoresWEG(clienteId);
}, 5 * 60 * 1000); // 5 minutes
```

To customize the interval, modify the interval in `setupWEGMonitoring()`.

---

## Step 8: Error Handling

The API client automatically handles token refresh:

1. When token expires (401/403), it automatically re-authenticates if credentials are stored
2. If no credentials available, returns auth error
3. All errors are caught and user-friendly messages shown

---

## Step 9: Data Display Examples

**Real-time plant data:**
```
📊 Planta Solar - Julho
├─ Energia Hoje: 5.23 kWh
├─ Energia Mês: 156.8 kWh
├─ Potência: 3.45 kW
├─ Capacidade: 5.0 kW
├─ Yield Hoje: 1.044
└─ Yield Mês: 31.36
```

**Aggregated totals:**
```
📈 Resumo Geral
├─ Energia Hoje: 25.3 kWh
├─ Energia Mês: 890.5 kWh
├─ Potência Total: 15.8 kW
├─ Economia Hoje: R$ 101.20
└─ Economia Total: R$ 49,750.50
```

---

## Testing Checklist

- [ ] Database columns added to `contas` table
- [ ] WEG router imported in `api.py`
- [ ] Test POST `/weg/clientes/{id}/weg/login` with valid WEG credentials
- [ ] Verify token is stored in database
- [ ] Test GET `/weg/clientes/{id}/weg/plantas` endpoint
- [ ] Test GET `/weg/clientes/{id}/weg/totalizadores` endpoint
- [ ] HTML sections added to portal.html
- [ ] JavaScript functions included
- [ ] WEG login modal works
- [ ] Plant data displays correctly
- [ ] Totals display correctly
- [ ] Auto-refresh works (check network tab every 5min)
- [ ] Token refresh on expiration works
- [ ] Disconnect button works

---

## Common Issues & Solutions

### "Token expirado" error
The token has expired. The system will automatically attempt to refresh it using stored credentials. If credentials aren't available, user must login again.

### No plants found
Verify WEG credentials are correct and the account has at least one plant registered on sun.weg.net portal.

### "HTTP 500" from API
Check WEG API status at https://status.weg.net. May be temporary API issue.

### Numeric parsing errors
Some values from WEG API include units (e.g., "5.23 kWh"). The `parse_numeric()` function handles this automatically. Check browser console for specific values if parsing fails.

---

## API Reference

See `WEG_API_Integration.md` for complete API documentation including:
- Authentication flow
- All endpoints and parameters
- Response formats
- Error codes
- Rate limiting (5-minute intervals recommended)

---

## Performance & Caching

- **Update interval**: 5 minutes (configurable)
- **Token lifetime**: As per WEG API (typically 24 hours)
- **Automatic refresh**: Yes, on 401/403 errors
- **Credential storage**: Encrypted in database (recommended)
- **No caching**: Always fetch latest data from WEG API

---

## Next Steps

1. Implement the database changes
2. Add WEG router to FastAPI
3. Add UI elements to portal and painel
4. Test with WEG credentials
5. Deploy to production
6. Monitor for errors in logs

For questions, refer to `WEG_API_Integration.md` or check the source comments in the implementation files.
