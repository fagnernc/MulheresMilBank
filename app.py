#!/usr/bin/env python3
"""
Pix Seguro - Simulador educativo de Pix para oficina do Mulheres Mil.

Como usar:
    1. Edite alunas.csv com o nome e o código de cada aluna (esse código
       vira o número da conta dela, e também a chave Pix).
    2. Rode:  python3 app.py
    3. O terminal vai mostrar o endereço para acessar pelo celular
       (algo como http://192.168.0.15:8000). Todo mundo precisa estar
       na mesma rede Wi-Fi do notebook.
    4. Cada aluna abre o endereço no navegador do celular e entra com:
       agência 001, o número da conta dela (o código da planilha) e a
       senha 123 (igual para todas). Pra confirmar um Pix, ela digita
       a senha de novo - igual em um banco de verdade.

Não usa nenhuma biblioteca externa - só o Python padrão. Não requer
internet para funcionar (a não ser que o navegador do celular exija
para abrir a página, o que não é o caso aqui).

Isto é só uma simulação para fins didáticos. Não é um sistema
bancário real e não deve ser usado com dinheiro ou dados reais.
"""

import csv
import hmac
import html
import json
import os
import secrets
import socket
import threading
from datetime import datetime, timedelta
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)

ALUNAS_CSV = os.path.join(DATA_DIR, "alunas.csv")
ESTADO_JSON = os.path.join(DATA_DIR, "estado.json")

SALDO_INICIAL = 1000.00
CODIGO_ADMIN = os.environ.get("CODIGO_ADMIN", "professor")
AGENCIA = "001"
SENHA_PADRAO = os.environ.get("SENHA_PADRAO", "123")
PORTA = int(os.environ.get("PORTA", "8000"))
SESSAO_ADMIN_TTL = timedelta(hours=4)


lock = threading.Lock()
sessoes = {}  # token -> codigo da aluna
sessoes_admin = {}  # token -> instante de expiração da sessão do professor


# ---------------------------------------------------------------------------
# Dados
# ---------------------------------------------------------------------------

def carregar_alunas():
    """A conta de cada aluna é o próprio código da planilha, e é também
    a chave Pix dela — igual a uma chave Pix do tipo 'celular' ou
    'aleatória' que na prática é só um número."""
    alunas = {}
    with open(ALUNAS_CSV, encoding="latin1") as f:
        for linha in csv.DictReader(f):
            nome = linha["nome"].strip()
            codigo = linha["codigo"].strip()
            if not nome or not codigo:
                continue
            alunas[codigo] = {"nome": nome, "chave": codigo}
    return alunas


def estado_inicial(alunas):
    return {
        "saldos": {c: SALDO_INICIAL for c in alunas},
        "extratos": {c: [] for c in alunas},
    }


def carregar_estado(alunas):
    if os.path.exists(ESTADO_JSON):
        with open(ESTADO_JSON, encoding="utf-8") as f:
            estado = json.load(f)
        mudou = False
        for codigo in alunas:
            if codigo not in estado["saldos"]:
                estado["saldos"][codigo] = SALDO_INICIAL
                estado["extratos"][codigo] = []
                mudou = True
        if mudou:
            salvar_estado(estado)
        return estado
    estado = estado_inicial(alunas)
    salvar_estado(estado)
    return estado


def salvar_estado(estado):
    with open(ESTADO_JSON, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)


ALUNAS = carregar_alunas()
ESTADO = carregar_estado(ALUNAS)


def formatar_reais(valor):
    return "R$ " + f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# ---------------------------------------------------------------------------
# HTML / CSS / JS (uma única página, com "telas" trocadas via JavaScript)
# ---------------------------------------------------------------------------

PAGINA = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Mulheres Mil Bank - Simulação de Pix</title>
<style>
  :root {
    --roxo: #5B2C82;
    --roxo-claro: #7A45A8;
    --laranja: #F2A93E;
    --creme: #FAF6EF;
    --linha: #E8DFEF;
    --texto: #2A1F35;
    --erro: #B23A2E;
    --sucesso: #1F7A4D;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    background: var(--creme);
    color: var(--texto);
    min-height: 100vh;
  }
  .faixa-sim {
    background: repeating-linear-gradient(45deg, var(--erro), var(--erro) 12px, #8f2c22 12px, #8f2c22 24px);
    color: #fff;
    text-align: center;
    font-size: 12.5px;
    font-weight: 700;
    letter-spacing: .04em;
    padding: 6px 10px;
    text-transform: uppercase;
  }
  .app {
    max-width: 460px;
    margin: 0 auto;
    min-height: 100vh;
    background: var(--creme);
    position: relative;
    padding-bottom: 40px;
  }
  header.topo {
    background: var(--roxo);
    color: #fff;
    padding: 22px 20px 26px;
    border-bottom: 4px solid var(--laranja);
  }
  header.topo.topo-login { text-align: center; padding: 26px 20px 28px; }
  .marca-logo {
    background: #fff;
    display: inline-block;
    border-radius: 12px;
    padding: 10px 16px 6px;
    box-shadow: 0 4px 12px rgba(0,0,0,.12);
  }
  .marca-logo img { display: block; width: 220px; max-width: 100%; height: auto; }
  header.topo.topo-login .sub { margin-top: 12px; }
  header.topo.topo-app { padding: 16px 20px 18px; }
  .topo-linha-superior { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
  .marca-mini-wrap {
    background: #fff;
    display: inline-block;
    border-radius: 8px;
    padding: 5px 9px 3px;
    line-height: 0;
  }
  .marca-mini { display: block; width: 108px; max-width: 40vw; height: auto; }
  .btn-inicio {
    background: rgba(255,255,255,.14);
    border: 1.5px solid rgba(255,255,255,.55);
    color: #fff;
    font-size: 12.5px;
    font-weight: 700;
    padding: 8px 13px;
    border-radius: 9px;
    cursor: pointer;
    white-space: nowrap;
  }
  .btn-inicio:active { background: rgba(255,255,255,.3); }
  .topo-usuaria { color: #fff; font-size: 15px; font-weight: 800; margin-top: 14px; }
  header.topo.topo-app .sub { margin-top: 1px; }
  header.topo .sub { font-size: 12.5px; opacity: .85; margin-top: 2px; }
  .cartao {
    background: #fff;
    margin: -16px 16px 0;
    border-radius: 14px;
    box-shadow: 0 6px 18px rgba(11,79,74,.10);
    padding: 20px;
    position: relative;
  }
  .bordado {
    height: 6px;
    margin: 0 16px;
    background-image: repeating-linear-gradient(
      to right, var(--laranja) 0 6px, transparent 6px 14px
    );
    opacity: .6;
  }
  .tela { padding: 18px 20px 10px; }
  h1.titulo { font-size: 17px; margin: 0 0 4px; color: var(--roxo); }
  p.legenda { font-size: 13px; color: #5B6E6A; margin: 0 0 18px; line-height: 1.4; }
  label { display:block; font-size: 12.5px; font-weight: 700; color: var(--roxo); margin: 14px 0 6px; }
  input, select {
    width: 100%;
    padding: 13px 14px;
    border-radius: 10px;
    border: 1.5px solid var(--linha);
    font-size: 16px;
    background: #fff;
    color: var(--texto);
  }
  input:focus, select:focus { outline: 2px solid var(--roxo-claro); border-color: var(--roxo-claro); }
  button.principal {
    width: 100%;
    margin-top: 22px;
    padding: 14px;
    background: var(--roxo);
    color: #fff;
    border: none;
    border-radius: 10px;
    font-size: 15.5px;
    font-weight: 700;
    cursor: pointer;
  }
  button.principal:active { background: var(--roxo-claro); }
  button.secundario {
    width: 100%;
    margin-top: 10px;
    padding: 12px;
    background: transparent;
    color: var(--roxo);
    border: 1.5px solid var(--roxo);
    border-radius: 10px;
    font-size: 14.5px;
    font-weight: 700;
    cursor: pointer;
  }
  .erro-msg {
    background: #FBEAE7; color: var(--erro); border: 1px solid #F1C6BE;
    padding: 10px 12px; border-radius: 8px; font-size: 13.5px; margin-top: 14px;
    display: none;
  }
  .saldo-box {
    margin: 18px 20px 0;
    background: var(--roxo);
    color: #fff;
    border-radius: 14px;
    padding: 20px;
  }
  .saldo-box .rotulo { font-size: 12px; opacity: .8; }
  .saldo-box .valor { font-size: 30px; font-weight: 800; margin-top: 4px; }
  .saldo-box .chave { font-size: 12px; opacity: .85; margin-top: 10px; }
  .acoes {
    display: flex; gap: 10px; margin: 18px 20px 4px;
  }
  .acoes button {
    flex: 1; padding: 14px 8px; border-radius: 12px; border: 1.5px solid var(--linha);
    background: #fff; font-weight: 700; font-size: 13.5px; color: var(--roxo);
  }
  .filtros-extrato { display:flex; gap:8px; margin-top: 16px; }
  .filtro-extrato {
    flex:1; padding: 9px 6px; border-radius: 20px; border: 1.5px solid var(--linha);
    background: #fff; color: var(--roxo); font-size: 12.5px; font-weight: 700; cursor: pointer;
  }
  .filtro-extrato.filtro-ativo { background: var(--roxo); color: #fff; border-color: var(--roxo); }
  .lista-extrato { margin: 6px 20px 0; }
  .item-extrato {
    display: flex; justify-content: space-between; align-items: center;
    padding: 12px 4px; border-bottom: 1px solid var(--linha); font-size: 13.5px;
  }
  .item-extrato .desc { font-weight: 700; }
  .item-extrato .quando { color: #7A8A86; font-size: 11.5px; }
  .item-extrato .valor.entrada { color: var(--sucesso); font-weight: 700; }
  .item-extrato .valor.saida { color: var(--erro); font-weight: 700; }
  .alerta {
    background: #FFF6E4; border: 1px solid #EAD59B; color: #6B4F12;
    border-radius: 10px; padding: 12px 14px; font-size: 12.8px; margin-top: 16px; line-height: 1.45;
  }
  .alerta b { display:block; margin-bottom:4px; }
  .conferencia {
    background: #F1EEE4; border-radius: 12px; padding: 16px; margin-top: 4px;
  }
  .conferencia .linha { display:flex; justify-content: space-between; padding: 7px 0; font-size: 13.5px; border-bottom: 1px dashed var(--linha); }
  .conferencia .linha:last-child { border-bottom: none; }
  .conferencia .linha b { color: var(--roxo); }
  .voltar { background:none; border:none; color: var(--roxo); font-size: 13.5px; font-weight:700; padding: 4px 0 0; cursor:pointer; }
  .rodape-app { text-align:center; font-size: 11px; color:#8A9A96; margin-top: 26px; padding: 0 20px; }
  .oculto { display: none !important; }
</style>
</head>
<body>
<div class="faixa-sim">Simulação educativa - não é um banco real</div>
<div class="app" id="app"></div>

<script>
const $ = (sel) => document.querySelector(sel);
const appEl = $("#app");
const LOGO_DATA_URI = "/assets/logo.png";
let EU = null;

function reais(v) {
  return "R$ " + v.toLocaleString("pt-BR", {minimumFractionDigits: 2, maximumFractionDigits: 2});
}

function topoApp(subtitulo) {
  return `
    <header class="topo topo-app">
      <div class="topo-linha-superior">
        <div class="marca-mini-wrap"><img class="marca-mini" src="${LOGO_DATA_URI}" alt="Mulheres Mil Bank"></div>
        <button class="btn-inicio" id="btn-inicio-header" type="button">&#8962; Início</button>
      </div>
      <div class="topo-usuaria">${EU ? EU.nome.split(" ")[0] : ""}</div>
      <div class="sub">${subtitulo}</div>
    </header>
  `;
}

function ligarBotaoInicio() {
  const botao = $("#btn-inicio-header");
  if (botao) botao.onclick = telaInicio;
}

async function api(caminho, opcoes = {}) {
  const resp = await fetch(caminho, {
    method: opcoes.method || "GET",
    headers: {"Content-Type": "application/json"},
    body: opcoes.body ? JSON.stringify(opcoes.body) : undefined,
    credentials: "same-origin",
  });
  const dados = await resp.json();
  if (!resp.ok) throw new Error(dados.erro || "Erro inesperado");
  return dados;
}

function telaLogin(mensagemErro) {
  appEl.innerHTML = `
    <header class="topo topo-login">
      <div class="marca-logo"><img src="${LOGO_DATA_URI}" alt="Mulheres Mil Bank"></div>
      <div class="sub">Ambiente de simulação da oficina de Pix</div>
    </header>
    <div class="cartao">
      <div class="tela">
        <h1 class="titulo">Entrar</h1>
        <p class="legenda">Entre como num banco de verdade: agência, conta e senha. O professor te entregou o número da sua conta.</p>
        <label for="campo-agencia">Agência</label>
        <input id="campo-agencia" value="001" disabled>
        <label for="campo-conta">Número da conta</label>
        <input id="campo-conta" inputmode="numeric" autocomplete="off">
        <label for="campo-senha">Senha</label>
        <input id="campo-senha" type="password" inputmode="numeric" autocomplete="off">
        <button class="principal" id="btn-entrar">Entrar</button>
        <div class="erro-msg" id="msg-erro" style="${mensagemErro ? 'display:block' : ''}">${mensagemErro || ""}</div>
      </div>
    </div>
    <p class="rodape-app">Nenhum dinheiro real é usado aqui. Tudo é fictício, feito para praticar Pix com segurança.</br>Fágner Nascimento Cunha</p>
  `;
  $("#btn-entrar").onclick = fazerLogin;
  $("#campo-senha").addEventListener("keydown", (e) => { if (e.key === "Enter") fazerLogin(); });
  $("#campo-conta").focus();
}

async function fazerLogin() {
  const conta = $("#campo-conta").value.trim();
  const senha = $("#campo-senha").value.trim();
  if (!conta || !senha) return;
  try {
    await api("/api/login", {method: "POST", body: {agencia: "001", conta, senha}});
    await telaInicio();
  } catch (e) {
    telaLogin(e.message);
  }
}

async function telaInicio() {
  EU = await api("/api/me");
  appEl.innerHTML = `
    ${topoApp("Minha conta")}
    <div class="saldo-box">
      <div class="rotulo">Saldo disponível (fictício)</div>
      <div class="valor">${reais(EU.saldo)}</div>
      <div class="chave">Agência 001 · Conta ${EU.chave} — esse número também é sua chave Pix</div>
    </div>
    <div class="acoes">
      <button id="btn-pix">Fazer um Pix</button>
      <button id="btn-extrato">Ver extrato</button>
      <button id="btn-sair">Sair</button>
    </div>
    <div class="alerta">
      <b>Lembrete de segurança</b>
      Antes de qualquer Pix de verdade: confira o nome de quem recebe, nunca tenha pressa, e nunca compartilhe seu código ou senha com ninguém - nem por telefone, nem por WhatsApp.
    </div>
    <p class="rodape-app">Simulação da oficina Mulheres Mil - dados e valores fictícios.</p>
    <p class="rodape-app">Fágner Nascimento Cunha.</p>
  `;
  $("#btn-pix").onclick = telaEscolherColega;
  $("#btn-extrato").onclick = telaExtrato;
  $("#btn-sair").onclick = async () => { await api("/api/sair", {method: "POST"}); telaLogin(); };
  ligarBotaoInicio();
}

async function telaEscolherColega() {
  appEl.innerHTML = `
    ${topoApp("Novo Pix")}
    <div class="cartao">
      <div class="tela">
        <button class="voltar" id="btn-voltar">&larr; Voltar</button>
        <h1 class="titulo">Para quem você vai enviar?</h1>
        <p class="legenda">Digite a chave Pix de quem vai receber, o valor e uma descrição. Na próxima tela você confere tudo antes de enviar.</p>
        <label for="campo-chave">Chave Pix (número da conta)</label>
        <input id="campo-chave" inputmode="numeric" autocomplete="off">
        <label for="campo-valor">Valor (R$)</label>
        <input id="campo-valor" inputmode="decimal">
        <label for="campo-desc">Descrição (opcional)</label>
        <input id="campo-desc">
        <button class="principal" id="btn-continuar">Continuar</button>
        <div class="erro-msg" id="msg-erro"></div>
      </div>
    </div>
  `;
  $("#btn-voltar").onclick = telaInicio;
  ligarBotaoInicio();
  $("#btn-continuar").onclick = async () => {
    const erroEl = $("#msg-erro");
    const chave = $("#campo-chave").value.trim();
    const valorTexto = $("#campo-valor").value.replace(",", ".").trim();
    const valor = parseFloat(valorTexto);
    if (!chave) {
      erroEl.textContent = "Digite a chave Pix de quem vai receber.";
      erroEl.style.display = "block";
      return;
    }
    if (!valor || valor <= 0) {
      erroEl.textContent = "Digite um valor válido.";
      erroEl.style.display = "block";
      return;
    }
    try {
      const colega = await api("/api/colega-por-chave?chave=" + encodeURIComponent(chave));
      telaConferencia(colega, valor, $("#campo-desc").value.trim());
    } catch (e) {
      erroEl.textContent = e.message;
      erroEl.style.display = "block";
    }
  };
}

function telaConferencia(colega, valor, desc) {
  appEl.innerHTML = `
    ${topoApp("Confira antes de enviar")}
    <div class="cartao">
      <div class="tela">
        <button class="voltar" id="btn-voltar">&larr; Voltar</button>
        <h1 class="titulo">Confira os dados do Pix</h1>
        <div class="conferencia">
          <div class="linha"><span>Nome de quem recebe</span><b>${colega.nome}</b></div>
          <div class="linha"><span>Agência</span><b>001</b></div>
          <div class="linha"><span>Chave Pix (conta)</span><b>${colega.chave}</b></div>
          <div class="linha"><span>Valor</span><b>${reais(valor)}</b></div>
          <div class="linha"><span>Descrição</span><b>${desc || "-"}</b></div>
        </div>
        <div class="alerta">
          <b>Antes de confirmar, pergunte-se:</b>
          O nome está correto? O valor está correto? Alguém te apressou para fazer esse Pix agora? Em caso de dúvida, pare e confira por outro meio antes de confirmar.
        </div>
        <label for="campo-senha-confirma">Digite sua senha para confirmar</label>
        <input id="campo-senha-confirma" type="password" inputmode="numeric" autocomplete="off">
        <button class="principal" id="btn-confirmar">Confirmar e enviar</button>
        <button class="secundario" id="btn-cancelar">Cancelar</button>
        <div class="erro-msg" id="msg-erro"></div>
      </div>
    </div>
  `;
  $("#btn-voltar").onclick = telaEscolherColega;
  $("#btn-cancelar").onclick = telaInicio;
  ligarBotaoInicio();
  $("#btn-confirmar").onclick = async () => {
    const erroEl = $("#msg-erro");
    const senha = $("#campo-senha-confirma").value.trim();
    if (!senha) {
      erroEl.textContent = "Digite sua senha para confirmar o Pix.";
      erroEl.style.display = "block";
      return;
    }
    try {
      await api("/api/pix", {method: "POST", body: {chave: colega.chave, valor, descricao: desc, senha}});
      telaSucesso(colega, valor);
    } catch (e) {
      erroEl.textContent = e.message;
      erroEl.style.display = "block";
    }
  };
}

function telaSucesso(colega, valor) {
  appEl.innerHTML = `
    ${topoApp("Pix enviado")}
    <div class="cartao">
      <div class="tela" style="text-align:center;">
        <h1 class="titulo" style="color: var(--sucesso);">Pix enviado com sucesso!</h1>
        <p class="legenda">${reais(valor)} enviados para ${colega.nome}.</p>
        <button class="principal" id="btn-ok">Voltar ao início</button>
      </div>
    </div>
  `;
  $("#btn-ok").onclick = telaInicio;
  ligarBotaoInicio();
}

let EXTRATO_ITENS = [];

function listaExtratoHtml(itens, filtro) {
  const filtrados = filtro === "todos" ? itens : itens.filter(i => i.tipo === filtro);
  if (filtrados.length === 0) {
    return '<p class="legenda" style="margin-top:14px;">Nenhuma movimentação aqui ainda.</p>';
  }
  return filtrados.map(i => `
    <div class="item-extrato">
      <div>
        <div class="desc">${i.tipo === "entrada" ? "Recebido de " : "Enviado para "}${i.contraparte}</div>
        <div class="quando">${i.quando}${i.descricao ? " - " + i.descricao : ""}</div>
      </div>
      <div class="valor ${i.tipo}">${i.tipo === "entrada" ? "+" : "-"} ${reais(i.valor)}</div>
    </div>
  `).join("");
}

function aplicarFiltroExtrato(filtro) {
  $("#lista-extrato").innerHTML = listaExtratoHtml(EXTRATO_ITENS, filtro);
  document.querySelectorAll(".filtro-extrato").forEach(btn => {
    btn.classList.toggle("filtro-ativo", btn.dataset.filtro === filtro);
  });
}

async function telaExtrato() {
  EXTRATO_ITENS = await api("/api/extrato");
  appEl.innerHTML = `
    ${topoApp("Extrato")}
    <div class="cartao">
      <div class="tela">
        <button class="voltar" id="btn-voltar">&larr; Voltar</button>
        <h1 class="titulo">Suas movimentações</h1>
        <div class="filtros-extrato">
          <button class="filtro-extrato filtro-ativo" data-filtro="todos">Todos</button>
          <button class="filtro-extrato" data-filtro="saida">Enviados</button>
          <button class="filtro-extrato" data-filtro="entrada">Recebidos</button>
        </div>
      </div>
    </div>
    <div class="lista-extrato" id="lista-extrato">
      ${listaExtratoHtml(EXTRATO_ITENS, "todos")}
    </div>
  `;
  $("#btn-voltar").onclick = telaInicio;
  ligarBotaoInicio();
  document.querySelectorAll(".filtro-extrato").forEach(btn => {
    btn.onclick = () => aplicarFiltroExtrato(btn.dataset.filtro);
  });
}

(async function iniciar() {
  try {
    await api("/api/me");
    telaInicio();
  } catch (e) {
    telaLogin();
  }
})();
</script>
</body>
</html>
"""


PAGINA_ADMIN = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Painel do professor - Mulheres Mil Bank</title>
<style>
  body {{ font-family: -apple-system, Arial, sans-serif; background:#FAF6EF; color:#1F2A28; margin:0; padding:20px; }}
  h1 {{ color:#5B2C82; font-size:20px; }}
  table {{ width:100%; border-collapse: collapse; background:#fff; border-radius:10px; overflow:hidden; margin-top:14px;}}
  th, td {{ text-align:left; padding:10px 12px; border-bottom:1px solid #E8DFEF; font-size:14px; }}
  th {{ background:#5B2C82; color:#fff; }}
  form {{ margin-top: 22px; }}
  button {{ background:#B23A2E; color:#fff; border:none; padding:10px 16px; border-radius:8px; font-weight:700; cursor:pointer; }}
  .aviso {{ font-size:12.5px; color:#5B6E6A; margin-top:6px; }}
</style>
</head>
<body>
  <h1>Painel do professor - Mulheres Mil Bank</h1>
  <table>
    <tr><th>Aluna</th><th>Agência</th><th>Conta (chave Pix)</th><th>Saldo</th><th>Transações</th></tr>
    {linhas}
  </table>
  <p class="aviso">A senha das alunas é a configurada pelo professor para esta atividade.</p>
  <form method="POST" action="/admin/resetar" onsubmit="return confirm('Isso zera todos os saldos e extratos. Confirmar?');">
    <button type="submit">Reiniciar todos os saldos e extratos</button>
    <div class="aviso">Use antes de repetir a atividade com outra turma.</div>
  </form>
  <form method="POST" action="/admin/sair">
    <button type="submit">Sair do painel</button>
  </form>
</body>
</html>
"""


PAGINA_LOGIN_ADMIN = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Login do professor - Mulheres Mil Bank</title>
<style>
  body { font-family: -apple-system, Arial, sans-serif; background:#FAF6EF; color:#1F2A28; margin:0; padding:20px; }
  main { max-width:360px; margin:40px auto; background:#fff; padding:24px; border-radius:12px; box-shadow:0 4px 12px rgba(0,0,0,.08); }
  h1 { color:#5B2C82; font-size:20px; margin-top:0; }
  label { display:block; margin:18px 0 6px; font-weight:700; font-size:14px; }
  input { width:100%; box-sizing:border-box; padding:11px; border:1px solid #E8DFEF; border-radius:8px; font-size:16px; }
  button { margin-top:20px; width:100%; background:#5B2C82; color:#fff; border:none; padding:11px; border-radius:8px; font-weight:700; cursor:pointer; }
  .erro { color:#B23A2E; font-size:14px; }
</style>
</head>
<body>
  <main>
    <h1>Login do professor</h1>
    {erro}
    <form method="POST" action="/admin/login">
      <label for="codigo">Código administrativo</label>
      <input id="codigo" name="codigo" type="password" autocomplete="current-password" required autofocus>
      <button type="submit">Entrar no painel</button>
    </form>
  </main>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Servidor
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "PixSeguro/1.0"

    def log_message(self, formato, *args):
        pass  # deixa o terminal limpo

    # -- utilidades -----------------------------------------------------

    def enviar_json(self, dados, status=200):
        corpo = json.dumps(dados, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def enviar_html(self, html, status=200):
        corpo = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)

    def ler_json(self):
        tamanho = int(self.headers.get("Content-Length", 0))
        if tamanho == 0:
            return {}
        return json.loads(self.rfile.read(tamanho).decode("utf-8"))

    def ler_formulario(self):
        tamanho = int(self.headers.get("Content-Length", 0))
        corpo = self.rfile.read(tamanho).decode("utf-8") if tamanho else ""
        return parse_qs(corpo)

    def pegar_token_cookie(self, nome):
        bruto = self.headers.get("Cookie")
        if not bruto:
            return None
        jar = cookies.SimpleCookie()
        jar.load(bruto)
        if nome not in jar:
            return None
        return jar[nome].value

    def configurar_cookie_sessao(self, nome, token, expirar=False):
        jar = cookies.SimpleCookie()
        jar[nome] = token
        jar[nome]["path"] = "/"
        jar[nome]["httponly"] = True
        jar[nome]["samesite"] = "Lax"
        if expirar:
            jar[nome]["max-age"] = 0
        self.send_header("Set-Cookie", jar[nome].OutputString())

    def pegar_codigo_sessao(self):
        token = self.pegar_token_cookie("sessao")
        if token is None:
            return None
        with lock:
            return sessoes.get(token)

    def tem_sessao_admin(self):
        token = self.pegar_token_cookie("sessao_admin")
        if token is None:
            return False
        with lock:
            expira_em = sessoes_admin.get(token)
            if expira_em is None:
                return False
            if expira_em <= datetime.now():
                sessoes_admin.pop(token, None)
                return False
            return True

    def exigir_login(self):
        codigo = self.pegar_codigo_sessao()
        if codigo is None or codigo not in ALUNAS:
            self.enviar_json({"erro": "Sessão inválida. Entre novamente."}, status=401)
            return None
        return codigo

    # -- rotas ------------------------------------------------------------

    def do_GET(self):
        caminho = urlparse(self.path).path

        if caminho == "/assets/logo.png":
            caminho_logo = os.path.join(BASE_DIR, "assets", "logo.png")
            try:
                with open(caminho_logo, "rb") as arquivo:
                    conteudo = arquivo.read()
            except FileNotFoundError:
                self.send_error(404, "Logo não encontrado")
                return

            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(conteudo)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(conteudo)
            return

        if caminho in ("/", "/app"):
            self.enviar_html(PAGINA)
            return

        if caminho == "/admin":
            if not self.tem_sessao_admin():
                self.redirecionar("/admin/login")
                return
            self.mostrar_admin()
            return

        if caminho == "/admin/login":
            if self.tem_sessao_admin():
                self.redirecionar("/admin")
                return
            self.enviar_html(PAGINA_LOGIN_ADMIN.replace("{erro}", ""))
            return

        if caminho == "/api/me":
            codigo = self.exigir_login()
            if codigo is None:
                return
            with lock:
                saldo = ESTADO["saldos"][codigo]
            self.enviar_json({
                "nome": ALUNAS[codigo]["nome"],
                "chave": ALUNAS[codigo]["chave"],
                "saldo": saldo,
            })
            return

        if caminho.startswith("/api/colega-por-chave"):
            codigo = self.exigir_login()
            if codigo is None:
                return
            query = parse_qs(urlparse(self.path).query)
            chave = (query.get("chave") or [""])[0].strip()
            destino_codigo = None
            for c, info in ALUNAS.items():
                if info["chave"] == chave:
                    destino_codigo = c
                    break
            if destino_codigo is None:
                self.enviar_json({"erro": "Chave Pix não encontrada."}, status=400)
                return
            if destino_codigo == codigo:
                self.enviar_json({"erro": "Você não pode enviar Pix para você mesma."}, status=400)
                return
            self.enviar_json({
                "nome": ALUNAS[destino_codigo]["nome"],
                "chave": ALUNAS[destino_codigo]["chave"],
            })
            return

        if caminho == "/api/extrato":
            codigo = self.exigir_login()
            if codigo is None:
                return
            with lock:
                itens = list(reversed(ESTADO["extratos"][codigo]))
            self.enviar_json(itens)
            return

        self.enviar_html("<h1>Página não encontrada</h1>", status=404)

    def do_POST(self):
        caminho = urlparse(self.path).path

        if caminho == "/api/login":
            dados = self.ler_json()
            agencia = str(dados.get("agencia", "")).strip()
            conta = str(dados.get("conta", "")).strip()
            senha = str(dados.get("senha", "")).strip()
            if agencia != AGENCIA:
                self.enviar_json({"erro": "Agência incorreta. A agência é 001."}, status=400)
                return
            if conta not in ALUNAS:
                self.enviar_json({"erro": "Conta não encontrada. Confira o número com o professor."}, status=400)
                return
            if senha != SENHA_PADRAO:
                self.enviar_json({"erro": "Senha incorreta."}, status=400)
                return
            codigo = conta
            token = secrets.token_urlsafe(32)
            with lock:
                sessoes[token] = codigo
            self.send_response(200)
            self.configurar_cookie_sessao("sessao", token)
            corpo = json.dumps({"ok": True}).encode("utf-8")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)
            return

        if caminho == "/api/sair":
            token = self.pegar_token_cookie("sessao")
            if token:
                with lock:
                    sessoes.pop(token, None)
            self.send_response(200)
            self.configurar_cookie_sessao("sessao", "", expirar=True)
            corpo = json.dumps({"ok": True}).encode("utf-8")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)
            return

        if caminho == "/api/pix":
            codigo = self.exigir_login()
            if codigo is None:
                return
            dados = self.ler_json()
            chave_destino = str(dados.get("chave", "")).strip()
            senha = str(dados.get("senha", "")).strip()
            try:
                valor = float(dados.get("valor"))
            except (TypeError, ValueError):
                self.enviar_json({"erro": "Valor inválido."}, status=400)
                return

            if senha != SENHA_PADRAO:
                self.enviar_json({"erro": "Senha incorreta."}, status=400)
                return

            destino_codigo = None
            for c, info in ALUNAS.items():
                if info["chave"] == chave_destino:
                    destino_codigo = c
                    break

            if destino_codigo is None:
                self.enviar_json({"erro": "Chave Pix não encontrada."}, status=400)
                return
            if destino_codigo == codigo:
                self.enviar_json({"erro": "Você não pode enviar Pix para você mesma."}, status=400)
                return
            if valor <= 0:
                self.enviar_json({"erro": "O valor precisa ser maior que zero."}, status=400)
                return

            with lock:
                if ESTADO["saldos"][codigo] < valor:
                    self.enviar_json({"erro": "Saldo insuficiente."}, status=400)
                    return
                agora = datetime.now().strftime("%d/%m %H:%M")
                ESTADO["saldos"][codigo] -= valor
                ESTADO["saldos"][destino_codigo] += valor
                descricao = str(dados.get("descricao", "")).strip()
                ESTADO["extratos"][codigo].append({
                    "tipo": "saida", "contraparte": ALUNAS[destino_codigo]["nome"],
                    "valor": valor, "quando": agora, "descricao": descricao,
                })
                ESTADO["extratos"][destino_codigo].append({
                    "tipo": "entrada", "contraparte": ALUNAS[codigo]["nome"],
                    "valor": valor, "quando": agora, "descricao": descricao,
                })
                salvar_estado(ESTADO)
                novo_saldo = ESTADO["saldos"][codigo]

            self.enviar_json({"ok": True, "saldo": novo_saldo})
            return

        if caminho == "/admin/login":
            campos = self.ler_formulario()
            codigo = (campos.get("codigo") or [""])[0]
            if not hmac.compare_digest(codigo, CODIGO_ADMIN):
                erro = '<p class="erro">Código incorreto.</p>'
                self.enviar_html(PAGINA_LOGIN_ADMIN.replace("{erro}", erro), status=403)
                return
            token = secrets.token_urlsafe(32)
            with lock:
                sessoes_admin[token] = datetime.now() + SESSAO_ADMIN_TTL
            self.send_response(303)
            self.configurar_cookie_sessao("sessao_admin", token)
            self.send_header("Location", "/admin")
            self.end_headers()
            return

        if caminho == "/admin/sair":
            token = self.pegar_token_cookie("sessao_admin")
            if token:
                with lock:
                    sessoes_admin.pop(token, None)
            self.send_response(303)
            self.configurar_cookie_sessao("sessao_admin", "", expirar=True)
            self.send_header("Location", "/admin/login")
            self.end_headers()
            return

        if caminho == "/admin/resetar":
            if not self.tem_sessao_admin():
                self.enviar_html("<h1>Acesso não autorizado</h1>", status=403)
                return
            with lock:
                novo = estado_inicial(ALUNAS)
                ESTADO["saldos"] = novo["saldos"]
                ESTADO["extratos"] = novo["extratos"]
                salvar_estado(ESTADO)
            self.redirecionar("/admin")
            return

        self.enviar_html("<h1>Rota não encontrada</h1>", status=404)

    def mostrar_admin(self):
        with lock:
            linhas = ""
            for codigo, info in sorted(ALUNAS.items(), key=lambda kv: kv[1]["nome"]):
                saldo = ESTADO["saldos"][codigo]
                n_transacoes = len(ESTADO["extratos"][codigo])
                linhas += (
                    f"<tr><td>{html.escape(str(info['nome']))}</td>"
                    f"<td>{html.escape(AGENCIA)}</td>"
                    f"<td>{html.escape(str(info['chave']))}</td>"
                    f"<td>{formatar_reais(saldo)}</td>"
                    f"<td>{n_transacoes}</td></tr>"
                )
        pagina = PAGINA_ADMIN.format(linhas=linhas)
        self.enviar_html(pagina)

    def redirecionar(self, destino):
        self.send_response(303)
        self.send_header("Location", destino)
        self.end_headers()


def descobrir_ip_local():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except OSError:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def main():
    ip = descobrir_ip_local()
    servidor = ThreadingHTTPServer(("0.0.0.0", PORTA), Handler)
    print("=" * 60)
    print(" Pix Seguro - simulador da oficina Mulheres Mil")
    print("=" * 60)
    print(f" {len(ALUNAS)} alunas carregadas de alunas.csv")
    print()
    print(" Alunas acessam pelo celular (mesma rede Wi-Fi) em:")
    print(f"   http://{ip}:{PORTA}")
    print()
    print(" Painel do professor (ver saldos e reiniciar):")
    print(f"   http://{ip}:{PORTA}/admin")
    print()
    print(" Pressione Ctrl+C para encerrar.")
    print("=" * 60)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrado.")


if __name__ == "__main__":
    main()
