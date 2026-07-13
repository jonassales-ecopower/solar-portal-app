/**
 * WEG Integration Frontend Examples
 *
 * Add these functions to portal.html or painel.html to enable
 * WEG monitoring in your solar portal.
 */

// WEG API endpoints
const WEG_ENDPOINTS = {
    login: (clienteId) => `${API}/clientes/${clienteId}/weg/login`,
    validate: (clienteId) => `${API}/clientes/${clienteId}/weg/validar-token`,
    plant: (clienteId, plantaId) => `${API}/clientes/${clienteId}/weg/planta/${plantaId}`,
    totals: (clienteId) => `${API}/clientes/${clienteId}/weg/totalizadores`,
    plants: (clienteId) => `${API}/clientes/${clienteId}/weg/plantas`,
    disconnect: (clienteId) => `${API}/clientes/${clienteId}/weg/desconectar`,
};

/**
 * 1. LOGIN WITH WEG CREDENTIALS
 */
async function wegLogin(clienteId, email, senha) {
    try {
        const resp = await fetch(WEG_ENDPOINTS.login(clienteId), {
            method: "POST",
            headers: { "Content-Type": "application/json", "Authorization": `Bearer ${token}` },
            body: JSON.stringify({ email, senha })
        });

        const data = await resp.json();
        if (resp.ok) {
            alert(`✅ Autenticado com WEG! ${data.plantas.length} usina(s) encontrada(s).`);
            // Store success and reload dashboard
            await carregarPlantasWEG(clienteId);
            return true;
        } else {
            alert(`❌ Erro: ${data.detail}`);
            return false;
        }
    } catch (e) {
        alert(`❌ Erro de conexão: ${e.message}`);
        return false;
    }
}

/**
 * 2. VALIDATE WEG TOKEN
 */
async function wegValidarToken(clienteId) {
    try {
        const resp = await fetch(WEG_ENDPOINTS.validate(clienteId), {
            method: "POST",
            headers: { "Authorization": `Bearer ${token}` }
        });

        const data = await resp.json();
        return data.sucesso && data.valido;
    } catch (e) {
        console.error("Erro ao validar token WEG:", e);
        return false;
    }
}

/**
 * 3. LOAD ALL WEG PLANTS
 */
async function carregarPlantasWEG(clienteId) {
    try {
        const resp = await fetch(WEG_ENDPOINTS.plants(clienteId), {
            headers: { "Authorization": `Bearer ${token}` }
        });

        if (!resp.ok) throw new Error("Failed to load plants");

        const data = await resp.json();
        if (!data.sucesso) throw new Error(data.detail);

        // Render plants in UI
        renderPlantasWEG(data.plantas);
        return data.plantas;
    } catch (e) {
        console.error("Erro ao carregar plantas WEG:", e);
        alert(`❌ Erro ao carregar plantas: ${e.message}`);
        return [];
    }
}

/**
 * 4. GET PLANT REAL-TIME DATA
 */
async function obterDadosPlantaWEG(clienteId, plantaId) {
    try {
        const resp = await fetch(WEG_ENDPOINTS.plant(clienteId, plantaId), {
            headers: { "Authorization": `Bearer ${token}` }
        });

        if (!resp.ok) throw new Error("Failed to load plant data");

        const data = await resp.json();
        if (!data.sucesso) throw new Error(data.detail);

        return data.planta;
    } catch (e) {
        console.error("Erro ao obter dados da planta WEG:", e);
        alert(`❌ Erro: ${e.message}`);
        return null;
    }
}

/**
 * 5. GET AGGREGATED TOTALS
 */
async function obterTotalizadoresWEG(clienteId) {
    try {
        const resp = await fetch(WEG_ENDPOINTS.totals(clienteId), {
            headers: { "Authorization": `Bearer ${token}` }
        });

        if (!resp.ok) throw new Error("Failed to load totals");

        const data = await resp.json();
        if (!data.sucesso) throw new Error(data.detail);

        return data.totalizadores;
    } catch (e) {
        console.error("Erro ao obter totalizadores WEG:", e);
        alert(`❌ Erro: ${e.message}`);
        return null;
    }
}

/**
 * 6. RENDER PLANTS LIST
 */
function renderPlantasWEG(plantas) {
    const container = document.getElementById("weg-plantas-container");
    if (!container) return;

    container.innerHTML = plantas.map(p => `
        <div style="border:1px solid #e2e8f0; border-radius:10px; padding:16px; margin-bottom:12px;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
                <div>
                    <h3 style="margin:0 0 4px; color:#0F3E63; font-size:16px; font-weight:700;">${p.nome}</h3>
                    <p style="margin:0; font-size:12px; color:#666;">${p.numerUC} • ${p.distribuidora}</p>
                </div>
                <button onclick="abrirDetalheWEG('${p.id}', '${p.nome}')"
                        style="background:#0F3E63; color:white; border:none; border-radius:6px; padding:8px 16px; cursor:pointer; font-size:13px; font-weight:600;">
                    📊 Ver Detalhes
                </button>
            </div>
            <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px;">
                <div style="background:#f0f4f8; padding:10px; border-radius:6px;">
                    <span style="font-size:11px; color:#666; display:block; margin-bottom:4px;">Energia Hoje</span>
                    <span style="font-size:16px; font-weight:700; color:#0F3E63;">${p.energiaDia?.toFixed(2) || '—'} kWh</span>
                </div>
                <div style="background:#f0f4f8; padding:10px; border-radius:6px;">
                    <span style="font-size:11px; color:#666; display:block; margin-bottom:4px;">Energia Mês</span>
                    <span style="font-size:16px; font-weight:700; color:#0F3E63;">${p.energiaMes?.toFixed(1) || '—'} kWh</span>
                </div>
                <div style="background:#f0f4f8; padding:10px; border-radius:6px;">
                    <span style="font-size:11px; color:#666; display:block; margin-bottom:4px;">Potência</span>
                    <span style="font-size:16px; font-weight:700; color:#F7A81B;">${p.potencia?.toFixed(2) || '—'} kW</span>
                </div>
                <div style="background:#f0f4f8; padding:10px; border-radius:6px;">
                    <span style="font-size:11px; color:#666; display:block; margin-bottom:4px;">Capacidade</span>
                    <span style="font-size:16px; font-weight:700; color:#0F3E63;">${p.capacidade?.toFixed(2) || '—'} kW</span>
                </div>
            </div>
        </div>
    `).join("");
}

/**
 * 7. SHOW PLANT DETAILS MODAL
 */
async function abrirDetalheWEG(plantaId, plantaNome) {
    const idCliente = new URLSearchParams(window.location.search).get("cliente_id");

    // Create modal if not exists
    let modal = document.getElementById("weg-detalhes-modal");
    if (!modal) {
        modal = document.createElement("div");
        modal.id = "weg-detalhes-modal";
        modal.style.cssText = `
            display:none; position:fixed; top:0; left:0; width:100%; height:100%;
            background:rgba(0,0,0,0.5); z-index:1000; align-items:center; justify-content:center;
        `;
        document.body.appendChild(modal);
    }

    // Load plant data
    const planta = await obterDadosPlantaWEG(idCliente, plantaId);
    if (!planta) return;

    // Set modal content
    modal.innerHTML = `
        <div style="background:white; border-radius:12px; max-width:500px; width:90%; max-height:80vh; overflow-y:auto; padding:20px;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:20px;">
                <h2 style="margin:0; color:#0F3E63;">${plantaNome}</h2>
                <button onclick="document.getElementById('weg-detalhes-modal').style.display='none'"
                        style="background:none; border:none; font-size:24px; cursor:pointer;">✕</button>
            </div>

            <div style="space-y:12px;">
                <div style="background:#f0f4f8; padding:12px; border-radius:6px; margin-bottom:12px;">
                    <span style="font-size:12px; color:#666; display:block;">Energia Hoje</span>
                    <span style="font-size:24px; font-weight:700; color:#0F3E63;">${planta.energiaDia?.valor?.toFixed(2) || '—'} kWh</span>
                </div>

                <div style="background:#f0f4f8; padding:12px; border-radius:6px; margin-bottom:12px;">
                    <span style="font-size:12px; color:#666; display:block;">Energia Mês</span>
                    <span style="font-size:24px; font-weight:700; color:#0F3E63;">${planta.energiaMes?.valor?.toFixed(2) || '—'} kWh</span>
                </div>

                <div style="background:#f0f4f8; padding:12px; border-radius:6px; margin-bottom:12px;">
                    <span style="font-size:12px; color:#666; display:block;">Potência Atual</span>
                    <span style="font-size:24px; font-weight:700; color:#F7A81B;">${planta.potencia?.valor?.toFixed(2) || '—'} kW</span>
                </div>

                <div style="display:grid; grid-template-columns:1fr 1fr; gap:12px;">
                    <div style="background:#f0f4f8; padding:12px; border-radius:6px;">
                        <span style="font-size:12px; color:#666; display:block;">Capacidade</span>
                        <span style="font-size:18px; font-weight:700; color:#0F3E63;">${planta.capacidade?.valor?.toFixed(2) || '—'} kW</span>
                    </div>
                    <div style="background:#f0f4f8; padding:12px; border-radius:6px;">
                        <span style="font-size:12px; color:#666; display:block;">Yield Hoje</span>
                        <span style="font-size:18px; font-weight:700; color:#F7A81B;">${planta.yieldDia || '—'}</span>
                    </div>
                </div>
            </div>

            <button onclick="document.getElementById('weg-detalhes-modal').style.display='none'"
                    style="width:100%; background:#0F3E63; color:white; border:none; border-radius:6px; padding:12px; cursor:pointer; font-weight:600; margin-top:20px;">
                Fechar
            </button>
        </div>
    `;

    modal.style.display = "flex";
}

/**
 * 8. DISPLAY AGGREGATED TOTALS DASHBOARD
 */
async function exibirTotalizadoresWEG(clienteId) {
    const totals = await obterTotalizadoresWEG(clienteId);
    if (!totals) return;

    const container = document.getElementById("weg-totalizadores-container");
    if (!container) return;

    container.innerHTML = `
        <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:12px;">
            <div style="background:linear-gradient(135deg, #0F3E63, #0B2C48); color:white; padding:16px; border-radius:10px;">
                <span style="font-size:12px; opacity:0.8; display:block; margin-bottom:8px;">Energia Hoje</span>
                <span style="font-size:24px; font-weight:700; display:block;">${totals.energiaHoje?.valor?.toFixed(2) || '—'} kWh</span>
            </div>

            <div style="background:linear-gradient(135deg, #0F3E63, #0B2C48); color:white; padding:16px; border-radius:10px;">
                <span style="font-size:12px; opacity:0.8; display:block; margin-bottom:8px;">Energia Mês</span>
                <span style="font-size:24px; font-weight:700; display:block;">${totals.energiaMes?.valor?.toFixed(2) || '—'} kWh</span>
            </div>

            <div style="background:linear-gradient(135deg, #0F3E63, #0B2C48); color:white; padding:16px; border-radius:10px;">
                <span style="font-size:12px; opacity:0.8; display:block; margin-bottom:8px;">Potência Total</span>
                <span style="font-size:24px; font-weight:700; display:block;">${totals.potenciaAtiva?.valor?.toFixed(2) || '—'} kW</span>
            </div>

            <div style="background:linear-gradient(135deg, #F7A81B, #F5821F); color:white; padding:16px; border-radius:10px;">
                <span style="font-size:12px; opacity:0.9; display:block; margin-bottom:8px;">Economia Hoje</span>
                <span style="font-size:24px; font-weight:700; display:block;">R$ ${totals.economiaHoje?.valor?.toFixed(2) || '—'}</span>
            </div>

            <div style="background:linear-gradient(135deg, #F7A81B, #F5821F); color:white; padding:16px; border-radius:10px;">
                <span style="font-size:12px; opacity:0.9; display:block; margin-bottom:8px;">Economia Total</span>
                <span style="font-size:24px; font-weight:700; display:block;">R$ ${totals.economiaTotal?.valor?.toFixed(2) || '—'}</span>
            </div>

            <div style="background:linear-gradient(135deg, #22c55e, #16a34a); color:white; padding:16px; border-radius:10px;">
                <span style="font-size:12px; opacity:0.9; display:block; margin-bottom:8px;">Árvores Plantadas</span>
                <span style="font-size:24px; font-weight:700; display:block;">${totals.arvoresPlantadas || '—'}</span>
            </div>
        </div>
    `;
}

/**
 * 9. DISCONNECT WEG ACCOUNT
 */
async function desconectarWEG(clienteId) {
    if (!confirm("Desconectar conta WEG? Isto removerá o acesso aos dados de monitoramento.")) return;

    try {
        const resp = await fetch(WEG_ENDPOINTS.disconnect(clienteId), {
            method: "DELETE",
            headers: { "Authorization": `Bearer ${token}` }
        });

        const data = await resp.json();
        if (resp.ok) {
            alert("✅ Conta WEG desconectada");
            location.reload();
        } else {
            alert(`❌ Erro: ${data.detail}`);
        }
    } catch (e) {
        alert(`❌ Erro de conexão: ${e.message}`);
    }
}

/**
 * 10. SETUP - CALL ON PAGE LOAD
 */
async function setupWEGMonitoring() {
    const clienteId = new URLSearchParams(window.location.search).get("cliente_id");
    if (!clienteId) return;

    // Load plants and totals on page load
    await carregarPlantasWEG(clienteId);
    await exibirTotalizadoresWEG(clienteId);

    // Refresh every 5 minutes
    setInterval(async () => {
        await carregarPlantasWEG(clienteId);
        await exibirTotalizadoresWEG(clienteId);
    }, 5 * 60 * 1000);
}

// Call on page load:
// document.addEventListener("DOMContentLoaded", setupWEGMonitoring);
