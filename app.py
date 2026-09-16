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
from decimal import Decimal, InvalidOperation
import hmac
import html
import json
import os
import re
import secrets
import socket
import sys
import threading
from datetime import datetime, timedelta
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import storage

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)

ALUNAS_CSV = os.path.join(DATA_DIR, "alunas.csv")
ESTADO_JSON = os.path.join(DATA_DIR, "estado.json")

SALDO_INICIAL = 1000.00
CODIGO_ADMIN = os.environ.get("CODIGO_ADMIN", "professor")
AGENCIA = "001"
SENHA_PADRAO = os.environ.get("SENHA_PADRAO", "123")
PORTA = int(os.environ.get("PORTA", "8000"))
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").strip().lower() in {
    "1", "true", "yes", "on",
}
DOMINIO = os.environ.get("DOMINIO", "").strip()
SESSAO_ADMIN_TTL = timedelta(hours=4)
SESSAO_V2_TTL = timedelta(hours=2)
V2_DB = storage.caminho_padrao_banco(DATA_DIR)
V2_INICIALIZADA = False
V2_DISPONIVEL = False
V2_ERRO_INICIALIZACAO = None
lock_v2 = threading.Lock()


def inicializar_v2():
    """Inicializa o SQLite uma vez, sem impedir o funcionamento da V1."""
    global V2_INICIALIZADA, V2_DISPONIVEL, V2_ERRO_INICIALIZACAO
    with lock_v2:
        if V2_INICIALIZADA:
            return V2_DISPONIVEL
        try:
            storage.inicializar_banco(V2_DB)
        except Exception as erro:  # A V1 continua disponível se a V2 falhar.
            V2_ERRO_INICIALIZACAO = erro
            V2_DISPONIVEL = False
            print(f"Erro ao inicializar o SQLite da V2: {erro}", file=sys.stderr)
        else:
            V2_DISPONIVEL = True
        V2_INICIALIZADA = True
        return V2_DISPONIVEL


PADRAO_VALOR_COM_VIRGULA = re.compile(
    r"(?:0|[1-9]\d{0,2}(?:\.\d{3})*|[1-9]\d*)(?:,\d{1,2})?"
)
PADRAO_VALOR_COM_PONTO = re.compile(r"(?:0|[1-9]\d*)(?:\.\d{1,2})?")


def valor_para_centavos(texto):
    """Converte valor brasileiro ou decimal simples em centavos, sem float."""
    if not isinstance(texto, str):
        raise ValueError("Informe um saldo inicial válido.")
    valor = texto.strip()
    if valor.startswith("R$"):
        valor = valor[2:].strip()
    elif "R$" in valor:
        raise ValueError("Informe um saldo inicial válido.")

    if not valor:
        raise ValueError("Informe um saldo inicial válido.")
    if "," in valor:
        if not PADRAO_VALOR_COM_VIRGULA.fullmatch(valor):
            raise ValueError("Informe um saldo inicial válido.")
        normalizado = valor.replace(".", "").replace(",", ".")
    else:
        if not PADRAO_VALOR_COM_PONTO.fullmatch(valor):
            raise ValueError("Informe um saldo inicial válido.")
        normalizado = valor

    try:
        decimal = Decimal(normalizado)
    except InvalidOperation as erro:
        raise ValueError("Informe um saldo inicial válido.") from erro
    if not decimal.is_finite() or decimal < 0:
        raise ValueError("Informe um saldo inicial válido.")
    return int(decimal * 100)


lock = threading.Lock()
sessoes = {}  # token -> codigo da aluna
sessoes_admin = {}  # token -> instante de expiração da sessão do professor
sessoes_v2 = {}  # token -> (aluna_id, instante de expiração)


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
  <p><a href="/admin/turmas">Gerenciar turmas da V2</a></p>
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


ESTILO_TURMAS = """
<style>
  body { font-family:-apple-system,Arial,sans-serif; background:#FAF6EF; color:#1F2A28; margin:0; padding:20px; }
  main { max-width:980px; margin:0 auto; }
  h1,h2 { color:#5B2C82; } h1 { font-size:24px; } h2 { font-size:18px; }
  a { color:#5B2C82; font-weight:700; } .acoes { display:flex; flex-wrap:wrap; gap:10px; margin:16px 0; }
  .botao, button { display:inline-block; border:0; border-radius:8px; padding:10px 14px; background:#5B2C82; color:#fff; font-weight:700; cursor:pointer; text-decoration:none; }
  .perigo { background:#B23A2E; } .secundario { background:#fff; color:#5B2C82; border:1px solid #5B2C82; }
  .cartao { background:#fff; border-radius:12px; padding:18px; margin:14px 0; box-shadow:0 2px 8px rgba(0,0,0,.08); }
  .estatisticas { display:grid; grid-template-columns:repeat(4,minmax(120px,1fr)); gap:10px; } .indicador { background:#FAF6EF; border-radius:9px; padding:12px; } .indicador strong { display:block; color:#5B2C82; font-size:23px; margin-top:4px; }
  .dados { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:14px; } .rotulo { color:#5B6E6A; font-size:12px; }
  table { width:100%; border-collapse:collapse; background:#fff; } th,td { text-align:left; padding:10px; border-bottom:1px solid #E8DFEF; vertical-align:top; } th { background:#5B2C82; color:#fff; }
  label { display:block; margin:14px 0 5px; font-weight:700; } input,textarea { width:100%; box-sizing:border-box; padding:10px; border:1px solid #D7CBDD; border-radius:8px; font:inherit; }
  textarea { min-height:80px; } .linha { display:grid; grid-template-columns:1fr 1fr; gap:12px; } .status { display:inline-block; padding:4px 8px; border-radius:999px; font-size:12px; font-weight:700; }
  .ATIVA { background:#DDF3E5; color:#17643A; } .AGENDADA { background:#E8DFEF; color:#5B2C82; } .EXPIRADA,.ENCERRADA { background:#FBEAE7; color:#8E3026; }
  .erro { background:#FBEAE7; color:#8E3026; padding:10px; border-radius:8px; } .aviso { color:#5B6E6A; font-size:13px; }
  @media (max-width:600px) { body { padding:12px; } .linha { grid-template-columns:1fr; } .estatisticas { grid-template-columns:repeat(2,minmax(0,1fr)); } th,td { font-size:13px; padding:8px; } }
</style>
"""


PAGINA_TURMAS = """<!DOCTYPE html><html lang="pt-BR"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Turmas - Mulheres Mil Bank</title>__ESTILO__</head><body><main>
<p><a href="/admin">← Painel do professor</a></p><div class="acoes"><h1>Turmas</h1><a class="botao" href="/admin/turmas/nova">Nova turma</a></div>
__CONTEUDO__
</main></body></html>"""


PAGINA_NOVA_TURMA = """<!DOCTYPE html><html lang="pt-BR"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Nova turma - Mulheres Mil Bank</title>__ESTILO__</head><body><main>
<p><a href="/admin/turmas">← Turmas</a></p><h1>Nova turma</h1>__ERRO__
<form class="cartao" method="POST" action="/admin/turmas/criar"><label for="nome">Nome da turma *</label><input id="nome" name="nome" required value="__NOME__">
<label for="descricao">Descrição</label><textarea id="descricao" name="descricao">__DESCRICAO__</textarea>
<div class="linha"><div><label for="inicio_data">Data de início *</label><input id="inicio_data" name="inicio_data" type="date" required value="__INICIO_DATA__"></div><div><label for="inicio_hora">Hora de início *</label><input id="inicio_hora" name="inicio_hora" type="time" required value="__INICIO_HORA__"></div></div>
<div class="linha"><div><label for="validade_data">Data de validade *</label><input id="validade_data" name="validade_data" type="date" required value="__VALIDADE_DATA__"></div><div><label for="validade_hora">Hora de validade *</label><input id="validade_hora" name="validade_hora" type="time" required value="__VALIDADE_HORA__"></div></div>
<label for="saldo_inicial">Saldo inicial *</label><input id="saldo_inicial" name="saldo_inicial" inputmode="decimal" value="__SALDO__" required>
<label for="senha">Senha inicial das contas *</label><input id="senha" name="senha" type="password" required><label><input type="checkbox" onclick="document.getElementById('senha').type=this.checked?'text':'password'"> Mostrar senha</label>
<button type="submit">Criar turma</button></form></main></body></html>"""


PAGINA_DETALHE_TURMA = """<!DOCTYPE html><html lang="pt-BR"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Gerenciar turma - Mulheres Mil Bank</title>__ESTILO__</head><body><main>
<p><a href="/admin/turmas">← Turmas</a></p><h1>__NOME__</h1>__ERRO__<div class="cartao"><span class="status __STATUS__">__STATUS__</span><p>__DESCRICAO__</p><div class="dados"><div><div class="rotulo">Início</div>__INICIO__</div><div><div class="rotulo">Validade</div>__VALIDADE__</div><div><div class="rotulo">Saldo inicial</div>__SALDO__</div><div><div class="rotulo">Alunas</div>__QUANTIDADE__</div><div><div class="rotulo">Criada em</div>__CRIADA__</div></div></div>
<div class="cartao"><h2>Resumo da atividade</h2><div class="estatisticas"><div class="indicador"><div class="rotulo">Participantes</div><strong>__EST_PARTICIPANTES__</strong></div><div class="indicador"><div class="rotulo">Fizeram Pix</div><strong>__EST_FIZERAM_PIX__</strong></div><div class="indicador"><div class="rotulo">Transações</div><strong>__EST_TRANSACOES__</strong></div><div class="indicador"><div class="rotulo">Movimentado</div><strong>__EST_MOVIMENTADO__</strong></div></div></div>
<div class="cartao"><h2>Alunas</h2><p>__QUANTIDADE__ cadastradas</p>__ACOES_ALUNAS____LISTA_ALUNAS__</div>
<div class="cartao"><h2>Alterar validade</h2><form method="POST" action="/admin/turmas/__ID__/validade"><div class="linha"><div><label for="validade_data">Data</label><input id="validade_data" name="validade_data" type="date" required value="__VALIDADE_DATA__"></div><div><label for="validade_hora">Hora</label><input id="validade_hora" name="validade_hora" type="time" required value="__VALIDADE_HORA__"></div></div><button type="submit">Alterar validade</button></form></div>
<div class="cartao"><h2>Encerrar turma</h2><p class="aviso">Uma turma encerrada não pode ser reaberta nesta etapa.</p><form method="POST" action="/admin/turmas/__ID__/encerrar" onsubmit="return confirm('Encerrar esta turma? As contas não poderão operar.');"><button class="perigo" type="submit">Encerrar turma</button></form></div>
</main></body></html>"""


PAGINA_NOVA_ALUNA = """<!DOCTYPE html><html lang="pt-BR"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Adicionar aluna - Mulheres Mil Bank</title>__ESTILO__</head><body><main>
<p><a href="/admin/turmas/__TURMA_ID__">← Gerenciar turma</a></p><h1>Adicionar aluna</h1>__ERRO__
<form class="cartao" method="POST" action="/admin/turmas/__TURMA_ID__/alunas/criar"><label for="nome">Nome da aluna *</label><input id="nome" name="nome" required value="__NOME__"><p class="aviso">A conta temporária será gerada automaticamente.</p><button type="submit">Cadastrar aluna</button></form>
</main></body></html>"""


PAGINA_LOTE_ALUNAS = """<!DOCTYPE html><html lang="pt-BR"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Adicionar alunas em lote - Mulheres Mil Bank</title>__ESTILO__</head><body><main>
<p><a href="/admin/turmas/__TURMA_ID__">← Gerenciar turma</a></p><h1>Adicionar alunas em lote</h1>__ERRO__
<form class="cartao" method="POST" action="/admin/turmas/__TURMA_ID__/alunas/lote"><label for="nomes">Nomes das alunas *</label><p class="aviso">Digite ou cole os nomes das alunas, um nome por linha. Limite de 100 por vez.</p><textarea id="nomes" name="nomes" required>__NOMES__</textarea><button type="submit">Cadastrar alunas</button></form>
</main></body></html>"""


PAGINA_PROJECAO = """<!DOCTYPE html><html lang="pt-BR"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>Contas da turma - Mulheres Mil Bank</title>
<style>
  :root { --roxo:#5B2C82; --laranja:#F2A93E; --creme:#FAF6EF; --texto:#1F2A28; --aviso:#8E3026; }
  * { box-sizing:border-box; } body { margin:0; background:var(--creme); color:var(--texto); font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif; }
  main { max-width:1600px; margin:0 auto; min-height:100vh; padding:clamp(20px,3vw,52px); display:flex; flex-direction:column; }
  header { text-align:center; border-bottom:4px solid var(--laranja); padding:0 0 clamp(18px,2vw,30px); } .marca { color:var(--roxo); font-size:clamp(20px,2.2vw,34px); font-weight:800; letter-spacing:.08em; }
  h1 { margin:10px 0 12px; color:var(--roxo); font-size:clamp(28px,4vw,58px); line-height:1.1; } .status { display:inline-block; border-radius:999px; padding:6px 12px; font-size:clamp(13px,1.5vw,20px); font-weight:800; background:#E8DFEF; color:var(--roxo); }
  .ATIVA { background:#DDF3E5; color:#17643A; } .EXPIRADA,.ENCERRADA { background:#FBEAE7; color:var(--aviso); } .validade { margin:12px 0 0; font-size:clamp(16px,1.8vw,25px); } .aviso { margin:12px auto 0; max-width:700px; color:var(--aviso); font-weight:700; font-size:clamp(15px,1.7vw,22px); }
  .contas { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:clamp(14px,2vw,28px); padding:clamp(26px,4vw,58px) 0; flex:1; align-content:start; }
  .conta { background:#fff; border:1px solid #E8DFEF; border-radius:16px; min-height:150px; padding:clamp(18px,2vw,30px); display:flex; flex-direction:column; justify-content:center; box-shadow:0 3px 10px rgba(31,42,40,.08); text-align:center; }
  .nome { font-weight:800; font-size:clamp(16px,1.8vw,27px); line-height:1.2; } .codigo { color:var(--roxo); font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:clamp(30px,4vw,62px); font-weight:800; letter-spacing:.06em; margin-top:10px; white-space:nowrap; }
  footer { border-top:2px solid #E8DFEF; padding-top:18px; text-align:center; font-size:clamp(16px,1.8vw,26px); } .endereco { color:var(--roxo); font-weight:800; word-break:break-word; } .tela-cheia { position:fixed; right:20px; bottom:20px; border:0; border-radius:999px; padding:12px 18px; background:var(--roxo); color:#fff; font:inherit; font-weight:800; cursor:pointer; } :fullscreen .tela-cheia { opacity:.18; } :fullscreen .tela-cheia:hover { opacity:1; }
  @media (max-width:1000px) { .contas { grid-template-columns:repeat(2,minmax(0,1fr)); } } @media (max-width:600px) { .contas { grid-template-columns:1fr; } main { padding:20px 14px 80px; } }
  @media print { .tela-cheia { display:none; } main { max-width:none; padding:12mm; } .contas { grid-template-columns:repeat(3,minmax(0,1fr)); gap:8mm; } .conta { box-shadow:none; break-inside:avoid; } }
</style></head><body><main>
<header><div class="marca">MULHERES MIL BANK</div><h1>__NOME_TURMA__</h1><span class="status __STATUS__">__STATUS__</span><p class="validade">Disponível até __VALIDADE__</p>__AVISO__</header>
<section class="contas" aria-label="Contas das participantes">__CONTAS__</section>
<footer>Acesse:<br><span class="endereco">__ENDERECO__</span></footer>
</main><button class="tela-cheia" type="button" onclick="document.documentElement.requestFullscreen && document.documentElement.requestFullscreen()">Tela cheia</button></body></html>"""


PAGINA_SENHA_TURMA = """<!DOCTYPE html><html lang="pt-BR"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Redefinir senha - Mulheres Mil Bank</title>__ESTILO__</head><body><main><p><a href="/admin/turmas/__ID__">← Gerenciar turma</a></p><h1>Redefinir senha da turma</h1>__ERRO__<form class="cartao" method="POST" action="/admin/turmas/__ID__/senha"><label for="senha">Nova senha *</label><input id="senha" name="senha" type="password" required><label for="confirmacao">Confirme a nova senha *</label><input id="confirmacao" name="confirmacao" type="password" required><p class="aviso">A senha atual não pode ser recuperada ou exibida.</p><button type="submit">Atualizar senha</button></form></main></body></html>"""


PAGINA_V2 = """<!DOCTYPE html><html lang="pt-BR"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Mulheres Mil Bank</title><style>:root{--r:#5B2C82;--c:#FAF6EF;--e:#B23A2E;--s:#17643A}*{box-sizing:border-box}body{margin:0;background:var(--c);font-family:-apple-system,Arial,sans-serif;color:#1F2A28}.app{max-width:520px;margin:auto;min-height:100vh;padding:20px}.marca{text-align:center;color:var(--r);font-weight:800;letter-spacing:.08em}.card{background:#fff;border-radius:14px;padding:20px;margin:16px 0;box-shadow:0 3px 12px #0001}h1{color:var(--r);font-size:22px}label{display:block;margin:12px 0 5px;font-weight:700}input{width:100%;padding:12px;border:1px solid #ddd;border-radius:9px;font:inherit}button{width:100%;margin-top:12px;padding:13px;border:0;border-radius:9px;background:var(--r);color:#fff;font-weight:800;font:inherit}.acoes{display:flex;gap:8px}.acoes button{font-size:13px}.saldo{font-size:30px;font-weight:800;color:var(--r)}.erro{color:var(--e);font-weight:700}.item{padding:12px 0;border-bottom:1px solid #eee}.mais{color:var(--s);font-weight:800}.menos{color:var(--e);font-weight:800}.oculto{display:none}</style></head><body><main class="app"><div class="marca">MULHERES MIL BANK</div><div id="app"></div></main><script>const q=s=>document.querySelector(s),e=s=>String(s).replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));let eu;async function api(p,o={}){let r=await fetch(p,{method:o.method||'GET',headers:{'Content-Type':'application/json'},body:o.body?JSON.stringify(o.body):undefined,credentials:'same-origin'}),d=await r.json();if(!r.ok)throw Error(d.erro||'Não foi possível concluir.');return d}function login(m=''){q('#app').innerHTML=`<div class="card"><h1>Entrar na atividade</h1><p>Use sua conta de seis dígitos e a senha informada pelo professor.</p><label>Conta</label><input id="conta" inputmode="numeric"><label>Senha</label><input id="senha" type="password"><button id="entrar">Entrar</button><p class="erro">${e(m)}</p></div>`;q('#entrar').onclick=async()=>{try{await api('/api/v2/login',{method:'POST',body:{conta:q('#conta').value.trim(),senha:q('#senha').value}});inicio()}catch(x){login(x.message)}}}async function inicio(){try{eu=await api('/api/v2/me')}catch(x){return login(x.message)}q('#app').innerHTML=`<div class="card"><h1>Olá, ${e(eu.nome)}</h1><p>Agência 001 · Conta ${e(eu.conta)}</p><p>Saldo fictício</p><div class="saldo">${e(eu.saldo)}</div></div><div class="acoes"><button id="pix">Fazer Pix</button><button id="extrato">Extrato</button><button id="sair">Sair</button></div>`;q('#pix').onclick=pix;q('#extrato').onclick=extrato;q('#sair').onclick=async()=>{await api('/api/v2/sair',{method:'POST'});login()}}function pix(m=''){q('#app').innerHTML=`<div class="card"><h1>Fazer Pix</h1><label>Conta de destino</label><input id="destino" inputmode="numeric"><label>Valor (R$)</label><input id="valor" inputmode="decimal"><button id="enviar">Confirmar Pix</button><button id="voltar">Voltar</button><p class="erro">${e(m)}</p></div>`;q('#voltar').onclick=inicio;q('#enviar').onclick=async()=>{if(!confirm('Confirmar este Pix?'))return;try{let r=await api('/api/v2/pix',{method:'POST',body:{conta_destino:q('#destino').value.trim(),valor:q('#valor').value.trim()}});q('#app').innerHTML=`<div class="card"><h1>Pix enviado</h1><p>${e(r.valor)} para ${e(r.nome)}.</p><button id="ok">Voltar</button></div>`;q('#ok').onclick=inicio}catch(x){pix(x.message)}}}async function extrato(){try{let itens=await api('/api/v2/extrato');q('#app').innerHTML=`<div class="card"><h1>Extrato</h1>${itens.map(i=>`<div class="item"><b>${i.tipo==='enviado'?'Pix enviado para':'Pix recebido de'} ${e(i.nome)}</b><br>Conta ${e(i.conta)} · ${e(i.quando)}<span class="${i.tipo==='enviado'?'menos':'mais'}"> ${i.sinal} ${e(i.valor)}</span></div>`).join('')||'<p>Nenhuma movimentação.</p>'}<button id="voltar">Voltar</button></div>`;q('#voltar').onclick=inicio}catch(x){login(x.message)}}(async()=>{try{await api('/api/v2/me');inicio()}catch(x){login()}})()</script></body></html>"""

# A camada V2 reaproveita a linguagem visual da V1 sem duplicar sua lógica.
PAGINA_V2 = PAGINA_V2.replace("</head>", """<style>
  :root { --r:#5B2C82; --rc:#7A45A8; --l:#F2A93E; --c:#FAF6EF; --linha:#E8DFEF; }
  body { background:var(--c); } .app { max-width:460px; padding:0 0 36px; background:var(--c); }
  .marca { min-height:142px; padding:28px 20px 20px; color:#fff; background:var(--r); border-bottom:4px solid var(--l); font-size:22px; letter-spacing:.04em; box-shadow:0 2px 8px #0002; }
  .marca::after { content:'Simulação educativa para praticar Pix com segurança'; display:block; margin-top:12px; font-size:12px; font-weight:500; letter-spacing:0; opacity:.9; }
  #app { padding:0 16px; } .card { margin:-22px 0 16px; padding:20px; border-radius:14px; box-shadow:0 6px 18px rgba(11,79,74,.10); }
  h1 { font-size:18px; margin-top:0; } p { line-height:1.45; } label { color:var(--r); font-size:13px; }
  input { border:1.5px solid var(--linha); border-radius:10px; } input:focus { outline:2px solid var(--rc); border-color:var(--rc); }
  button { border-radius:10px; background:var(--r); } button:active { background:var(--rc); }
  .acoes { margin:0 0 16px; } .acoes button { border:1.5px solid var(--linha); background:#fff; color:var(--r); border-radius:12px; }
  .saldo { margin-top:6px; padding:14px; border-radius:12px; color:#fff; background:var(--r); font-size:30px; }
  .erro { min-height:0; background:#FBEAE7; border-radius:8px; padding:8px 10px; } .erro:empty { display:none; }
  .item { font-size:14px; } .mais,.menos { display:block; margin-top:5px; }
</style></head>""")
PAGINA_V2 = PAGINA_V2.replace("</body>", """<script>
  const pixComMensagemSegura = pix;
  pix = function (mensagem = '') {
    return pixComMensagemSegura(typeof mensagem === 'string' ? mensagem : '');
  };
  new MutationObserver(() => {
    const botao = document.querySelector('#pix');
    if (botao) botao.onclick = () => pix();
  }).observe(document.querySelector('#app'), {childList:true, subtree:true});
</script></body>""")

PAGINA_V2_RENOVADA = r'''<!DOCTYPE html><html lang="pt-BR"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1"><title>Mulheres Mil Bank</title><style>
:root{--roxo:#5B2C82;--roxo-claro:#7A45A8;--laranja:#F2A93E;--creme:#FAF6EF;--linha:#E8DFEF;--texto:#2A1F35;--erro:#B23A2E;--sucesso:#1F7A4D}*{box-sizing:border-box}body{margin:0;background:var(--creme);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;color:var(--texto)}.faixa{background:repeating-linear-gradient(45deg,var(--erro),var(--erro) 12px,#8f2c22 12px,#8f2c22 24px);color:#fff;text-align:center;font-size:12px;font-weight:800;letter-spacing:.04em;padding:7px 10px}.app{max-width:460px;margin:auto;min-height:100vh}.topo{background:var(--roxo);color:#fff;padding:24px 20px;border-bottom:4px solid var(--laranja);text-align:center}.topo.conta{text-align:left;padding:15px 20px}.logo{background:#fff;border-radius:12px;padding:10px 15px 6px;display:inline-block;box-shadow:0 4px 12px #0002}.logo img{display:block;width:210px;max-width:100%;height:auto}.mini{width:108px}.sub{font-size:12.5px;opacity:.9;margin-top:10px}.nome{font-size:15px;font-weight:800;margin-top:12px}.card{background:#fff;margin:-16px 16px 0;border-radius:14px;box-shadow:0 6px 18px rgba(11,79,74,.1);padding:20px}.tela{padding:18px 20px 12px}h1{font-size:18px;color:var(--roxo);margin:0 0 6px}p{line-height:1.45}.legenda{font-size:13px;color:#5B6E6A}.rotulo{display:block;font-size:12.5px;color:var(--roxo);font-weight:800;margin:14px 0 6px}input{width:100%;padding:13px 14px;border:1.5px solid var(--linha);border-radius:10px;font:inherit;color:var(--texto)}input:focus{outline:2px solid var(--roxo-claro);border-color:var(--roxo-claro)}button{width:100%;margin-top:12px;padding:14px;border:0;border-radius:10px;background:var(--roxo);color:#fff;font:inherit;font-weight:800;cursor:pointer}.secundario{background:#fff;color:var(--roxo);border:1.5px solid var(--roxo)}.erro{background:#FBEAE7;color:var(--erro);border:1px solid #F1C6BE;padding:10px 12px;border-radius:8px;font-size:13px}.erro:empty{display:none}.saldo{margin:18px 20px 0;background:var(--roxo);color:#fff;border-radius:14px;padding:20px}.saldo .valor{font-size:30px;font-weight:800}.saldo .chave{font-size:12px;opacity:.9;margin-top:10px}.acoes{display:flex;gap:10px;margin:18px 20px 4px}.acoes button{flex:1;padding:14px 7px;margin:0;background:#fff;color:var(--roxo);border:1.5px solid var(--linha);font-size:13px}.alerta{background:#FFF6E4;border:1px solid #EAD59B;color:#6B4F12;border-radius:10px;padding:12px 14px;font-size:12.8px;margin:16px 20px;line-height:1.45}.alerta b{display:block;margin-bottom:4px}.conferencia{background:#F1EEE4;border-radius:12px;padding:15px}.linha{display:flex;justify-content:space-between;gap:12px;padding:8px 0;border-bottom:1px dashed var(--linha);font-size:13px}.linha:last-child{border:0}.linha b{text-align:right;color:var(--roxo)}.voltar{width:auto;margin:0 0 12px;padding:0;background:none;border:0;color:var(--roxo);text-align:left}.lista{margin:8px 20px}.item{display:flex;justify-content:space-between;gap:10px;padding:12px 4px;border-bottom:1px solid var(--linha);font-size:13px}.quando{font-size:11px;color:#7A8A86}.mais{color:var(--sucesso);font-weight:800}.menos{color:var(--erro);font-weight:800}.rodape{text-align:center;font-size:11px;color:#8A9A96;margin:25px 20px}.oculto{display:none!important}
</style></head><body><div class="faixa">SIMULAÇÃO EDUCATIVA — NÃO É UM BANCO REAL</div><main class="app" id="app"></main><script>
const q=s=>document.querySelector(s);let eu=null,rascunho={};const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const logo='/assets/logo.png';function topo(sub=''){return `<header class="topo conta"><div class="logo"><img class="mini" src="${logo}" alt="Mulheres Mil Bank"></div><div class="nome">${eu?esc(eu.nome):''}</div><div class="sub">${sub}</div></header>`}async function api(url,opt={}){const r=await fetch(url,{method:opt.method||'GET',headers:{'Content-Type':'application/json'},body:opt.body?JSON.stringify(opt.body):undefined,credentials:'same-origin'});const d=await r.json();if(!r.ok)throw Error(d.erro||'Não foi possível concluir esta ação.');return d}function rodape(){return '<p class="rodape">Nenhum dinheiro real é usado aqui. Contas e valores são fictícios, para praticar Pix com segurança.</p>'}
function login(msg=''){q('#app').innerHTML=`<header class="topo"><div class="logo"><img src="${logo}" alt="Mulheres Mil Bank"></div><div class="sub">Ambiente de simulação da oficina de Pix</div></header><section class="card"><div class="tela"><h1>Entrar na atividade</h1><p class="legenda">Use a conta de seis dígitos entregue pelo professor e a senha da turma.</p><label class="rotulo">Conta</label><input id="conta" inputmode="numeric" autocomplete="off"><label class="rotulo">Senha</label><input id="senha" type="password" autocomplete="current-password"><button id="entrar">Entrar</button><p class="erro">${esc(msg)}</p></div></section><div class="alerta"><b>Esta é uma simulação</b>Não é um banco real e não utiliza dinheiro real. A atividade é exclusivamente educacional.</div>${rodape()}`;q('#entrar').onclick=async()=>{try{await api('/api/v2/login',{method:'POST',body:{conta:q('#conta').value.trim(),senha:q('#senha').value}});inicio()}catch(e){login(e.message)}};q('#senha').onkeydown=e=>{if(e.key==='Enter')q('#entrar').click()}}
async function inicio(){try{eu=await api('/api/v2/me')}catch(e){return login(e.message)}q('#app').innerHTML=`${topo('Minha conta')}<section class="saldo"><div>Saldo disponível (fictício)</div><div class="valor">${esc(eu.saldo)}</div><div class="chave">Agência 001 · Conta ${esc(eu.conta)} — esse número também é sua chave Pix</div></section><div class="acoes"><button id="pix">Fazer Pix</button><button id="extrato">Ver extrato</button><button id="sair">Sair</button></div><div class="alerta"><b>Lembrete de segurança</b>Antes de qualquer Pix de verdade: confira o nome de quem recebe, nunca tenha pressa e nunca compartilhe seu código ou senha.</div>${rodape()}`;q('#pix').onclick=()=>dadosPix();q('#extrato').onclick=()=>extrato();q('#sair').onclick=async()=>{await api('/api/v2/sair',{method:'POST'});login()}}
function dadosPix(msg=''){q('#app').innerHTML=`${topo('Novo Pix')}<section class="card"><div class="tela"><button class="voltar" id="voltar">← Voltar</button><h1>Para quem você vai enviar?</h1><p class="legenda">Digite a conta da colega, o valor e uma descrição. Você conferirá os dados antes de enviar.</p><label class="rotulo">Conta da destinatária</label><input id="destino" inputmode="numeric" value="${esc(rascunho.conta||'')}"><label class="rotulo">Valor (R$)</label><input id="valor" inputmode="decimal" value="${esc(rascunho.valor||'')}"><label class="rotulo">Descrição (opcional)</label><input id="descricao" value="${esc(rascunho.descricao||'')}"><button id="avancar">Avançar</button><p class="erro">${esc(msg)}</p></div></section>${rodape()}`;q('#voltar').onclick=()=>inicio();q('#avancar').onclick=async()=>{rascunho={conta:q('#destino').value.trim(),valor:q('#valor').value.trim(),descricao:q('#descricao').value.trim()};if(!rascunho.conta||!rascunho.valor)return dadosPix('Informe a conta e o valor.');try{const d=await api('/api/v2/destinataria?conta='+encodeURIComponent(rascunho.conta));conferencia(d)}catch(e){dadosPix(e.message)}}}
function conferencia(destino,msg=''){q('#app').innerHTML=`${topo('Confira antes de enviar')}<section class="card"><div class="tela"><button class="voltar" id="voltar">← Voltar e corrigir</button><h1>Confira os dados do Pix</h1><div class="conferencia"><div class="linha"><span>Quem recebe</span><b>${esc(destino.nome)}</b></div><div class="linha"><span>Conta</span><b>${esc(destino.conta)}</b></div><div class="linha"><span>Valor</span><b>${esc(rascunho.valor)}</b></div><div class="linha"><span>Descrição</span><b>${esc(rascunho.descricao||'-')}</b></div><div class="linha"><span>Remetente</span><b>${esc(eu.nome)}<br>${esc(eu.conta)}</b></div></div><div class="alerta"><b>Operação simulada</b>Confira nome, conta e valor. Em caso de dúvida, pare e peça ajuda ao professor.</div><label class="rotulo">Digite a senha da turma para confirmar</label><input id="senha-pix" type="password" autocomplete="off"><button id="confirmar">Confirmar Pix</button><p class="erro">${esc(msg)}</p></div></section>${rodape()}`;q('#voltar').onclick=()=>dadosPix();q('#confirmar').onclick=async()=>{const senha=q('#senha-pix').value;if(!senha)return conferencia(destino,'Digite a senha para confirmar o Pix.');try{const r=await api('/api/v2/pix',{method:'POST',body:{conta_destino:rascunho.conta,valor:rascunho.valor,descricao:rascunho.descricao,senha}});comprovante(r)}catch(e){conferencia(destino,e.message)}}}
function comprovante(r){q('#app').innerHTML=`${topo('Comprovante')}<section class="card"><div class="tela"><h1 style="color:var(--sucesso)">Pix realizado com sucesso!</h1><div class="conferencia"><div class="linha"><span>Remetente</span><b>${esc(r.remetente)}<br>${esc(r.conta_remetente)}</b></div><div class="linha"><span>Destinatária</span><b>${esc(r.nome)}<br>${esc(r.conta)}</b></div><div class="linha"><span>Valor</span><b>${esc(r.valor)}</b></div><div class="linha"><span>Descrição</span><b>${esc(r.descricao||'-')}</b></div><div class="linha"><span>Data e hora</span><b>${esc(r.quando)}</b></div></div><div class="alerta"><b>OPERAÇÃO SIMULADA</b>Este comprovante é fictício e foi criado para a atividade educativa.</div><button id="inicio">Voltar para minha conta</button></div></section>${rodape()}`;q('#inicio').onclick=()=>inicio()}
async function extrato(){try{const itens=await api('/api/v2/extrato');q('#app').innerHTML=`${topo('Extrato')}<section class="card"><div class="tela"><button class="voltar" id="voltar">← Voltar</button><h1>Suas movimentações</h1><p class="legenda">Veja o que entrou e saiu da sua conta fictícia.</p></div></section><section class="lista">${itens.map(i=>`<div class="item"><div><b>${i.tipo==='enviado'?'Enviado para':'Recebido de'} ${esc(i.nome)}</b><br><span class="quando">Conta ${esc(i.conta)} · ${esc(i.quando)}${i.descricao?' · '+esc(i.descricao):''}</span></div><div class="${i.tipo==='enviado'?'menos':'mais'}">${esc(i.sinal)} ${esc(i.valor)}</div></div>`).join('')||'<p class="legenda">Nenhuma movimentação aqui ainda.</p>'}</section>${rodape()}`;q('#voltar').onclick=()=>inicio()}catch(e){login(e.message)}}
(async()=>{try{await api('/api/v2/me');inicio()}catch(e){login()}})();</script></body></html>'''


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
        if COOKIE_SECURE:
            jar[nome]["secure"] = True
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

    def exigir_admin_html(self):
        if self.tem_sessao_admin():
            return True
        self.redirecionar("/admin/login")
        return False

    def exigir_v2_disponivel(self):
        if inicializar_v2():
            return True
        self.enviar_html(
            "<h1>Gestão de turmas indisponível</h1>"
            "<p>Não foi possível inicializar os dados da V2. "
            "Verifique o servidor e tente novamente.</p>",
            status=503,
        )
        return False

    def pegar_sessao_v2(self):
        token = self.pegar_token_cookie("sessao_v2")
        if token is None:
            return None
        with lock:
            sessao = sessoes_v2.get(token)
            if sessao is None or sessao[1] <= datetime.now():
                sessoes_v2.pop(token, None)
                return None
            return sessao[0]

    def exigir_aluna_v2(self):
        aluna_id = self.pegar_sessao_v2()
        if aluna_id is None:
            self.enviar_json({"erro": "Sessão inválida. Entre novamente."}, status=401)
            return None
        try:
            aluna = storage.buscar_aluna(V2_DB, aluna_id)
            turma = storage.buscar_turma(V2_DB, aluna["turma_id"])
        except storage.ErroStorage:
            self.enviar_json({"erro": "Sessão inválida. Entre novamente."}, status=401)
            return None
        if storage.status_turma(turma) != "ATIVA":
            self.enviar_json({"erro": "Esta turma não está disponível para operação."}, status=403)
            return None
        return aluna

    def data_hora_formulario(self, campos, prefixo):
        data = (campos.get(f"{prefixo}_data") or [""])[0]
        hora = (campos.get(f"{prefixo}_hora") or [""])[0]
        try:
            return datetime.strptime(f"{data} {hora}", "%Y-%m-%d %H:%M")
        except ValueError as erro:
            raise ValueError("Informe uma data e hora válidas.") from erro

    def formatar_data_hora(self, valor):
        return datetime.fromisoformat(valor).strftime("%d/%m/%Y às %H:%M")

    def formatar_centavos(self, valor):
        reais, centavos = divmod(valor, 100)
        numero = f"{reais:,}".replace(",", "X").replace(".", ",").replace("X", ".")
        return f"R$ {numero},{centavos:02d}"

    def formatar_conta(self, codigo):
        return f"{codigo[:3]} {codigo[3:]}"

    def id_turma_da_rota(self, caminho):
        partes = caminho.strip("/").split("/")
        if len(partes) >= 3 and partes[:2] == ["admin", "turmas"] and partes[2].isdigit():
            return int(partes[2])
        return None

    def ids_exclusao_aluna_da_rota(self, caminho):
        partes = caminho.strip("/").split("/")
        if (
            len(partes) == 6
            and partes[:2] == ["admin", "turmas"]
            and partes[2].isdigit()
            and partes[3] == "alunas"
            and partes[4].isdigit()
            and partes[5] == "excluir"
        ):
            return int(partes[2]), int(partes[4])
        return None

    def endereco_acesso(self):
        if DOMINIO:
            return f"https://{DOMINIO}"
        host = self.headers.get("Host", "").strip()
        return f"http://{host or 'localhost'}"

    # -- rotas ------------------------------------------------------------

    def do_GET(self):
        caminho = urlparse(self.path).path

        if caminho == "/health":
            self.enviar_json({"ok": True})
            return

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

        if caminho == "/":
            self.enviar_html(PAGINA_V2_RENOVADA)
            return

        if caminho in ("/app", "/v1"):
            self.enviar_html(PAGINA)
            return

        if caminho == "/v2":
            self.enviar_html(PAGINA_V2_RENOVADA)
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

        if caminho == "/admin/turmas":
            if not self.exigir_admin_html():
                return
            if not self.exigir_v2_disponivel():
                return
            self.mostrar_turmas()
            return

        if caminho == "/admin/turmas/nova":
            if not self.exigir_admin_html():
                return
            if not self.exigir_v2_disponivel():
                return
            self.mostrar_formulario_turma()
            return

        turma_id = self.id_turma_da_rota(caminho)
        if turma_id is not None and caminho == f"/admin/turmas/{turma_id}":
            if not self.exigir_admin_html():
                return
            if not self.exigir_v2_disponivel():
                return
            self.mostrar_detalhe_turma(turma_id)
            return

        if turma_id is not None and caminho == f"/admin/turmas/{turma_id}/alunas/nova":
            if not self.exigir_admin_html():
                return
            if not self.exigir_v2_disponivel():
                return
            self.mostrar_formulario_aluna(turma_id)
            return

        if turma_id is not None and caminho == f"/admin/turmas/{turma_id}/alunas/lote":
            if not self.exigir_admin_html():
                return
            if not self.exigir_v2_disponivel():
                return
            self.mostrar_formulario_lote(turma_id)
            return

        if turma_id is not None and caminho == f"/admin/turmas/{turma_id}/projetar":
            if not self.exigir_admin_html():
                return
            if not self.exigir_v2_disponivel():
                return
            self.mostrar_projecao(turma_id)
            return

        if turma_id is not None and caminho == f"/admin/turmas/{turma_id}/senha":
            if not self.exigir_admin_html():
                return
            if not self.exigir_v2_disponivel():
                return
            self.mostrar_formulario_senha_turma(turma_id)
            return

        if caminho == "/api/v2/me":
            if not self.exigir_v2_disponivel():
                return
            aluna = self.exigir_aluna_v2()
            if aluna is None:
                return
            self.enviar_json({
                "nome": aluna["nome"], "conta": self.formatar_conta(aluna["codigo_conta"]),
                "saldo": self.formatar_centavos(aluna["saldo"]),
            })
            return

        if caminho == "/api/v2/extrato":
            if not self.exigir_v2_disponivel():
                return
            aluna = self.exigir_aluna_v2()
            if aluna is None:
                return
            itens = []
            for item in storage.listar_extrato_aluna(V2_DB, aluna["id"]):
                itens.append({
                    "tipo": item["tipo"], "nome": item["nome"],
                    "conta": self.formatar_conta(item["codigo_conta"]),
                    "quando": self.formatar_data_hora(item["criada_em"]),
                    "valor": self.formatar_centavos(item["valor"]),
                    "sinal": "-" if item["tipo"] == "enviado" else "+",
                    "descricao": item["descricao"],
                })
            self.enviar_json(itens)
            return

        if caminho == "/api/v2/destinataria":
            if not self.exigir_v2_disponivel():
                return
            origem = self.exigir_aluna_v2()
            if origem is None:
                return
            conta = (parse_qs(urlparse(self.path).query).get("conta") or [""])[0].strip()
            try:
                destino = storage.buscar_aluna_por_codigo(V2_DB, conta)
                if destino["turma_id"] != origem["turma_id"] or destino["id"] == origem["id"]:
                    raise storage.ContaNaoEncontrada("Conta indisponível.")
            except storage.ContaNaoEncontrada:
                self.enviar_json({"erro": "Não foi possível localizar essa conta na sua turma."}, status=400)
                return
            self.enviar_json({"nome": destino["nome"], "conta": self.formatar_conta(destino["codigo_conta"])})
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

        if caminho == "/api/v2/login":
            if not self.exigir_v2_disponivel():
                return
            dados = self.ler_json()
            conta = str(dados.get("conta", "")).strip()
            senha = str(dados.get("senha", ""))
            try:
                aluna = storage.autenticar_aluna(V2_DB, conta, senha)
            except storage.AutenticacaoInvalida:
                self.enviar_json({"erro": "Conta ou senha incorreta."}, status=401)
                return
            except storage.TurmaInativa as erro:
                self.enviar_json({"erro": str(erro)}, status=403)
                return
            token = secrets.token_urlsafe(32)
            with lock:
                sessoes_v2[token] = (aluna["id"], datetime.now() + SESSAO_V2_TTL)
            self.send_response(200)
            self.configurar_cookie_sessao("sessao_v2", token)
            corpo = json.dumps({"ok": True}).encode("utf-8")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)
            return

        if caminho == "/api/v2/sair":
            token = self.pegar_token_cookie("sessao_v2")
            if token:
                with lock:
                    sessoes_v2.pop(token, None)
            self.send_response(200)
            self.configurar_cookie_sessao("sessao_v2", "", expirar=True)
            corpo = b'{"ok": true}'
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)
            return

        if caminho == "/api/v2/pix":
            if not self.exigir_v2_disponivel():
                return
            origem = self.exigir_aluna_v2()
            if origem is None:
                return
            dados = self.ler_json()
            try:
                senha = str(dados.get("senha", ""))
                if not storage.validar_senha_turma(V2_DB, origem["turma_id"], senha):
                    self.enviar_json({"erro": "Senha incorreta. Confira e tente novamente."}, status=403)
                    return
                destino = storage.buscar_aluna_por_codigo(
                    V2_DB, str(dados.get("conta_destino", "")).strip()
                )
                valor = valor_para_centavos(str(dados.get("valor", "")))
                resultado = storage.executar_pix(
                    V2_DB, origem["turma_id"], origem["id"], destino["id"], valor,
                    descricao=str(dados.get("descricao", "")),
                )
            except storage.ContaNaoEncontrada:
                self.enviar_json({"erro": "Não foi possível realizar este Pix."}, status=400)
                return
            except (storage.ErroStorage, ValueError) as erro:
                self.enviar_json({"erro": str(erro)}, status=400)
                return
            self.enviar_json({
                "ok": True, "remetente": origem["nome"], "conta_remetente": self.formatar_conta(origem["codigo_conta"]),
                "nome": destino["nome"], "conta": self.formatar_conta(destino["codigo_conta"]),
                "valor": self.formatar_centavos(valor), "descricao": str(dados.get("descricao", "")).strip(),
                "quando": self.formatar_data_hora(resultado["criada_em"]),
            })
            return

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
            if not hmac.compare_digest(
                codigo.encode("utf-8"),
                CODIGO_ADMIN.encode("utf-8"),
            ):
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

        turma_id = self.id_turma_da_rota(caminho)
        if turma_id is not None and caminho == f"/admin/turmas/{turma_id}/senha":
            if not self.exigir_admin_html():
                return
            if not self.exigir_v2_disponivel():
                return
            campos = self.ler_formulario()
            senha = (campos.get("senha") or [""])[0]
            confirmacao = (campos.get("confirmacao") or [""])[0]
            if senha != confirmacao:
                self.mostrar_formulario_senha_turma(turma_id, "As senhas não conferem.", status=400)
                return
            try:
                storage.redefinir_senha_turma(V2_DB, turma_id, senha)
            except storage.ErroStorage as erro:
                self.mostrar_formulario_senha_turma(turma_id, str(erro), status=400)
                return
            self.redirecionar(f"/admin/turmas/{turma_id}")
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

        if caminho == "/admin/turmas/criar":
            if not self.exigir_admin_html():
                return
            if not self.exigir_v2_disponivel():
                return
            campos = self.ler_formulario()
            try:
                nome = (campos.get("nome") or [""])[0]
                descricao = (campos.get("descricao") or [""])[0].strip() or None
                inicio = self.data_hora_formulario(campos, "inicio")
                validade = self.data_hora_formulario(campos, "validade")
                saldo = valor_para_centavos((campos.get("saldo_inicial") or [""])[0])
                senha = (campos.get("senha") or [""])[0]
                if not senha.strip():
                    raise ValueError("Informe a senha inicial das contas.")
                turma = storage.criar_turma(V2_DB, nome, inicio, validade, saldo, senha, descricao)
            except (ValueError, storage.ErroStorage) as erro:
                self.mostrar_formulario_turma(str(erro), campos, status=400)
                return
            self.redirecionar(f"/admin/turmas/{turma['id']}")
            return

        turma_id = self.id_turma_da_rota(caminho)
        if turma_id is not None and caminho == f"/admin/turmas/{turma_id}/encerrar":
            if not self.exigir_admin_html():
                return
            if not self.exigir_v2_disponivel():
                return
            try:
                storage.encerrar_turma(V2_DB, turma_id)
            except storage.ErroStorage as erro:
                self.mostrar_detalhe_turma(turma_id, str(erro), status=400)
                return
            self.redirecionar(f"/admin/turmas/{turma_id}")
            return

        if turma_id is not None and caminho == f"/admin/turmas/{turma_id}/validade":
            if not self.exigir_admin_html():
                return
            if not self.exigir_v2_disponivel():
                return
            campos = self.ler_formulario()
            try:
                validade = self.data_hora_formulario(campos, "validade")
                storage.alterar_validade_turma(V2_DB, turma_id, validade)
            except (ValueError, storage.ErroStorage) as erro:
                self.mostrar_detalhe_turma(turma_id, str(erro), status=400)
                return
            self.redirecionar(f"/admin/turmas/{turma_id}")
            return

        if turma_id is not None and caminho == f"/admin/turmas/{turma_id}/alunas/criar":
            if not self.exigir_admin_html():
                return
            if not self.exigir_v2_disponivel():
                return
            campos = self.ler_formulario()
            try:
                nome = (campos.get("nome") or [""])[0]
                storage.criar_aluna(V2_DB, turma_id, nome)
            except (ValueError, storage.ErroStorage) as erro:
                self.mostrar_formulario_aluna(turma_id, str(erro), campos, status=400)
                return
            self.redirecionar(f"/admin/turmas/{turma_id}")
            return

        if turma_id is not None and caminho == f"/admin/turmas/{turma_id}/alunas/lote":
            if not self.exigir_admin_html():
                return
            if not self.exigir_v2_disponivel():
                return
            campos = self.ler_formulario()
            try:
                nomes = (campos.get("nomes") or [""])[0].splitlines()
                storage.criar_alunas_em_lote(V2_DB, turma_id, nomes)
            except (ValueError, storage.ErroStorage) as erro:
                self.mostrar_formulario_lote(turma_id, str(erro), campos, status=400)
                return
            self.redirecionar(f"/admin/turmas/{turma_id}")
            return

        ids_exclusao = self.ids_exclusao_aluna_da_rota(caminho)
        if ids_exclusao is not None:
            turma_id, aluna_id = ids_exclusao
            if not self.exigir_admin_html():
                return
            if not self.exigir_v2_disponivel():
                return
            try:
                storage.excluir_aluna(V2_DB, turma_id, aluna_id)
            except storage.ErroStorage as erro:
                self.mostrar_detalhe_turma(turma_id, str(erro), status=400)
                return
            self.redirecionar(f"/admin/turmas/{turma_id}")
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

    def mostrar_turmas(self):
        turmas = storage.listar_turmas(V2_DB)
        if not turmas:
            conteudo = '<div class="cartao"><p>Nenhuma turma criada ainda.</p></div>'
        else:
            linhas = ""
            for turma in turmas:
                descricao = html.escape(turma["descricao"] or "—")
                status = turma["status"]
                linhas += (
                    "<tr>"
                    f"<td><strong>{html.escape(turma['nome'])}</strong><br><span class=\"aviso\">{descricao}</span></td>"
                    f"<td><span class=\"status {status}\">{status}</span></td>"
                    f"<td>{turma['quantidade_alunas']}</td>"
                    f"<td>{self.formatar_centavos(turma['saldo_inicial'])}</td>"
                    f"<td>{self.formatar_data_hora(turma['inicio_em'])}</td>"
                    f"<td>{self.formatar_data_hora(turma['validade_em'])}</td>"
                    f"<td><a class=\"botao secundario\" href=\"/admin/turmas/{turma['id']}\">Gerenciar</a></td>"
                    "</tr>"
                )
            conteudo = (
                "<div class=\"cartao\"><table><tr><th>Turma</th><th>Status</th><th>Alunas</th>"
                f"<th>Saldo inicial</th><th>Início</th><th>Validade</th><th>Ação</th></tr>{linhas}</table></div>"
            )
        pagina = PAGINA_TURMAS.replace("__ESTILO__", ESTILO_TURMAS).replace("__CONTEUDO__", conteudo)
        self.enviar_html(pagina)

    def mostrar_formulario_turma(self, erro=None, campos=None, status=200):
        campos = campos or {}
        agora = storage.agora_local()
        inicio = agora.replace(second=0, microsecond=0)
        validade = inicio + timedelta(hours=2)

        def valor(nome, padrao=""):
            return html.escape((campos.get(nome) or [padrao])[0])

        mensagem = f'<p class="erro">{html.escape(erro)}</p>' if erro else ""
        pagina = PAGINA_NOVA_TURMA
        substituicoes = {
            "__ESTILO__": ESTILO_TURMAS,
            "__ERRO__": mensagem,
            "__NOME__": valor("nome"),
            "__DESCRICAO__": valor("descricao"),
            "__INICIO_DATA__": valor("inicio_data", inicio.strftime("%Y-%m-%d")),
            "__INICIO_HORA__": valor("inicio_hora", inicio.strftime("%H:%M")),
            "__VALIDADE_DATA__": valor("validade_data", validade.strftime("%Y-%m-%d")),
            "__VALIDADE_HORA__": valor("validade_hora", validade.strftime("%H:%M")),
            "__SALDO__": valor("saldo_inicial", "1000,00"),
        }
        for marcador, conteudo in substituicoes.items():
            pagina = pagina.replace(marcador, conteudo)
        self.enviar_html(pagina, status=status)

    def mostrar_detalhe_turma(self, turma_id, erro=None, status=200):
        try:
            turma = storage.buscar_turma(V2_DB, turma_id)
        except storage.ContaNaoEncontrada:
            self.enviar_html("<h1>Turma não encontrada</h1>", status=404)
            return
        status_turma = storage.status_turma(turma)
        validade = datetime.fromisoformat(turma["validade_em"])
        mensagem = f'<p class="erro">{html.escape(erro)}</p>' if erro else ""
        descricao = html.escape(turma["descricao"] or "Sem descrição.")
        pode_alterar_alunas = status_turma in ("AGENDADA", "ATIVA")
        alunas = storage.listar_alunas_turma(V2_DB, turma_id)
        estatisticas = storage.obter_estatisticas_turma(V2_DB, turma_id)
        if alunas:
            linhas_alunas = ""
            for aluna in alunas:
                nome = html.escape(aluna["nome"])
                acao = ""
                if pode_alterar_alunas:
                    acao = (
                        f'<form method="POST" action="/admin/turmas/{turma_id}/alunas/{aluna["id"]}/excluir" '
                        'onsubmit="return confirm(\'Excluir esta aluna? Esta ação removerá a conta.\');">'
                        '<button class="perigo" type="submit">Excluir</button></form>'
                    )
                else:
                    acao = '<span class="aviso">Alterações indisponíveis</span>'
                linhas_alunas += (
                    "<tr>"
                    f"<td>{nome}</td><td>{aluna['codigo_conta']}</td>"
                    f"<td>{self.formatar_centavos(aluna['saldo'])}</td><td>{acao}</td>"
                    "</tr>"
                )
            lista_alunas = (
                "<table><tr><th>Nome</th><th>Conta</th><th>Saldo</th><th>Ação</th></tr>"
                f"{linhas_alunas}</table>"
            )
        else:
            lista_alunas = '<p class="aviso">Nenhuma aluna cadastrada nesta turma.</p>'
        botao_projecao = ""
        if alunas:
            botao_projecao = (
                f'<a class="botao secundario" href="/admin/turmas/{turma_id}/projetar">'
                'Projetar contas</a>'
            )
        if pode_alterar_alunas:
            acoes_alunas = (
                f'<div class="acoes"><a class="botao" href="/admin/turmas/{turma_id}/alunas/nova">'
                '+ Adicionar aluna</a>'
                f'<a class="botao secundario" href="/admin/turmas/{turma_id}/alunas/lote">'
                f'Adicionar em lote</a>{botao_projecao}</div>'
            )
        else:
            acoes_alunas = (
                '<p class="aviso">Cadastros e exclusões estão indisponíveis para esta turma.</p>'
                f'<div class="acoes">{botao_projecao}</div>'
            )
        acoes_alunas += (
            f'<div class="acoes"><a class="botao secundario" href="/admin/turmas/{turma_id}/senha">'
            'Redefinir senha</a></div>'
        )
        pagina = PAGINA_DETALHE_TURMA
        substituicoes = {
            "__ESTILO__": ESTILO_TURMAS,
            "__ERRO__": mensagem,
            "__NOME__": html.escape(turma["nome"]),
            "__DESCRICAO__": descricao,
            "__STATUS__": status_turma,
            "__INICIO__": self.formatar_data_hora(turma["inicio_em"]),
            "__VALIDADE__": self.formatar_data_hora(turma["validade_em"]),
            "__SALDO__": self.formatar_centavos(turma["saldo_inicial"]),
            "__QUANTIDADE__": str(storage.contar_alunas_turma(V2_DB, turma_id)),
            "__CRIADA__": self.formatar_data_hora(turma["criada_em"]),
            "__ID__": str(turma_id),
            "__VALIDADE_DATA__": validade.strftime("%Y-%m-%d"),
            "__VALIDADE_HORA__": validade.strftime("%H:%M"),
            "__EST_PARTICIPANTES__": str(estatisticas["participantes"]),
            "__EST_FIZERAM_PIX__": str(estatisticas["fizeram_pix"]),
            "__EST_TRANSACOES__": str(estatisticas["transacoes"]),
            "__EST_MOVIMENTADO__": self.formatar_centavos(estatisticas["movimentado"]),
            "__ACOES_ALUNAS__": acoes_alunas,
            "__LISTA_ALUNAS__": lista_alunas,
        }
        for marcador, conteudo in substituicoes.items():
            pagina = pagina.replace(marcador, conteudo)
        self.enviar_html(pagina, status=status)

    def mostrar_projecao(self, turma_id):
        try:
            turma = storage.buscar_turma(V2_DB, turma_id)
        except storage.ContaNaoEncontrada:
            self.enviar_html("<h1>Turma não encontrada</h1>", status=404)
            return
        status_turma = storage.status_turma(turma)
        alunas = storage.listar_alunas_turma(V2_DB, turma_id)
        contas = ""
        for aluna in alunas:
            codigo = aluna["codigo_conta"]
            contas += (
                '<article class="conta">'
                f'<div class="nome">{html.escape(aluna["nome"])}</div>'
                f'<div class="codigo">{codigo[:3]} {codigo[3:]}</div>'
                '</article>'
            )
        if not contas:
            contas = '<p class="aviso">Nenhuma conta cadastrada nesta turma.</p>'
        aviso = ""
        if status_turma in ("EXPIRADA", "ENCERRADA"):
            aviso = '<p class="aviso">As contas não estão disponíveis para operação.</p>'
        pagina = PAGINA_PROJECAO
        substituicoes = {
            "__NOME_TURMA__": html.escape(turma["nome"]),
            "__STATUS__": status_turma,
            "__VALIDADE__": self.formatar_data_hora(turma["validade_em"]),
            "__AVISO__": aviso,
            "__CONTAS__": contas,
            "__ENDERECO__": html.escape(self.endereco_acesso()),
        }
        for marcador, conteudo in substituicoes.items():
            pagina = pagina.replace(marcador, conteudo)
        self.enviar_html(pagina)

    def mostrar_formulario_senha_turma(self, turma_id, erro=None, status=200):
        try:
            storage.buscar_turma(V2_DB, turma_id)
        except storage.ContaNaoEncontrada:
            self.enviar_html("<h1>Turma não encontrada</h1>", status=404)
            return
        mensagem = f'<p class="erro">{html.escape(erro)}</p>' if erro else ""
        pagina = PAGINA_SENHA_TURMA.replace("__ESTILO__", ESTILO_TURMAS)
        pagina = pagina.replace("__ID__", str(turma_id)).replace("__ERRO__", mensagem)
        self.enviar_html(pagina, status=status)

    def mostrar_formulario_aluna(self, turma_id, erro=None, campos=None, status=200):
        try:
            turma = storage.buscar_turma(V2_DB, turma_id)
        except storage.ContaNaoEncontrada:
            self.enviar_html("<h1>Turma não encontrada</h1>", status=404)
            return
        campos = campos or {}
        mensagem = f'<p class="erro">{html.escape(erro)}</p>' if erro else ""
        if storage.status_turma(turma) not in ("AGENDADA", "ATIVA"):
            self.mostrar_detalhe_turma(
                turma_id,
                "Cadastros estão indisponíveis para esta turma.",
                status=400,
            )
            return
        pagina = PAGINA_NOVA_ALUNA
        pagina = pagina.replace("__ESTILO__", ESTILO_TURMAS)
        pagina = pagina.replace("__TURMA_ID__", str(turma_id))
        pagina = pagina.replace("__ERRO__", mensagem)
        pagina = pagina.replace("__NOME__", html.escape((campos.get("nome") or [""])[0]))
        self.enviar_html(pagina, status=status)

    def mostrar_formulario_lote(self, turma_id, erro=None, campos=None, status=200):
        try:
            turma = storage.buscar_turma(V2_DB, turma_id)
        except storage.ContaNaoEncontrada:
            self.enviar_html("<h1>Turma não encontrada</h1>", status=404)
            return
        campos = campos or {}
        mensagem = f'<p class="erro">{html.escape(erro)}</p>' if erro else ""
        if storage.status_turma(turma) not in ("AGENDADA", "ATIVA"):
            self.mostrar_detalhe_turma(
                turma_id,
                "Cadastros estão indisponíveis para esta turma.",
                status=400,
            )
            return
        pagina = PAGINA_LOTE_ALUNAS
        pagina = pagina.replace("__ESTILO__", ESTILO_TURMAS)
        pagina = pagina.replace("__TURMA_ID__", str(turma_id))
        pagina = pagina.replace("__ERRO__", mensagem)
        pagina = pagina.replace("__NOMES__", html.escape((campos.get("nomes") or [""])[0]))
        self.enviar_html(pagina, status=status)

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
    inicializar_v2()
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
