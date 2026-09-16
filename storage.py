"""Camada SQLite da V2 do Mulheres Mil Bank.

Esta camada ainda não é usada pela interface V1. Ela cria e manipula um banco
novo em DATA_DIR sem importar ou alterar os arquivos CSV/JSON existentes.
"""

import hashlib
import hmac
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


TIMEZONE_PADRAO = "America/Maceio"
MAX_TENTATIVAS_CODIGO = 100


class ErroStorage(Exception):
    """Erro base da camada de persistência."""


class TurmaInativa(ErroStorage):
    pass


class SaldoInsuficiente(ErroStorage):
    pass


class TurmasDiferentes(ErroStorage):
    pass


class ContaNaoEncontrada(ErroStorage):
    pass


class ValorInvalido(ErroStorage):
    pass


class CodigoContaInvalido(ErroStorage):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS turmas (
    id INTEGER PRIMARY KEY,
    nome TEXT NOT NULL,
    descricao TEXT,
    inicio_em TEXT NOT NULL,
    validade_em TEXT NOT NULL,
    saldo_inicial INTEGER NOT NULL CHECK (saldo_inicial >= 0),
    senha TEXT NOT NULL,
    criada_em TEXT NOT NULL,
    encerrada_em TEXT,
    CHECK (inicio_em <= validade_em)
);

CREATE TABLE IF NOT EXISTS alunas (
    id INTEGER PRIMARY KEY,
    turma_id INTEGER NOT NULL,
    nome TEXT NOT NULL,
    codigo_conta TEXT NOT NULL UNIQUE CHECK (
        length(codigo_conta) = 6 AND codigo_conta GLOB '[1-9][0-9][0-9][0-9][0-9][0-9]'
    ),
    saldo INTEGER NOT NULL CHECK (saldo >= 0),
    criada_em TEXT NOT NULL,
    FOREIGN KEY (turma_id) REFERENCES turmas(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS transacoes (
    id INTEGER PRIMARY KEY,
    turma_id INTEGER NOT NULL,
    origem_aluna_id INTEGER NOT NULL,
    destino_aluna_id INTEGER NOT NULL,
    valor INTEGER NOT NULL CHECK (valor > 0),
    criada_em TEXT NOT NULL,
    CHECK (origem_aluna_id <> destino_aluna_id),
    FOREIGN KEY (turma_id) REFERENCES turmas(id) ON DELETE RESTRICT,
    FOREIGN KEY (origem_aluna_id) REFERENCES alunas(id) ON DELETE RESTRICT,
    FOREIGN KEY (destino_aluna_id) REFERENCES alunas(id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_alunas_turma_id ON alunas(turma_id);
CREATE INDEX IF NOT EXISTS idx_transacoes_turma_id ON transacoes(turma_id);
CREATE INDEX IF NOT EXISTS idx_transacoes_origem_id ON transacoes(origem_aluna_id);
CREATE INDEX IF NOT EXISTS idx_transacoes_destino_id ON transacoes(destino_aluna_id);
"""


def caminho_padrao_banco(data_dir=None):
    """Retorna DATA_DIR/mulheresmilbank.db sem criar ou migrar dados V1."""
    diretorio = data_dir or os.environ.get("DATA_DIR", os.path.dirname(__file__))
    return Path(diretorio) / "mulheresmilbank.db"


def timezone_aplicacao(nome=None):
    return ZoneInfo(nome or os.environ.get("APP_TIMEZONE", TIMEZONE_PADRAO))


def agora_local(agora=None, timezone_nome=None):
    fuso = timezone_aplicacao(timezone_nome)
    if agora is None:
        return datetime.now(fuso)
    if agora.tzinfo is None:
        return agora.replace(tzinfo=fuso)
    return agora.astimezone(fuso)


def _para_iso(instant, timezone_nome=None):
    return agora_local(instant, timezone_nome).isoformat()


def _de_iso(valor, timezone_nome=None):
    return agora_local(datetime.fromisoformat(valor), timezone_nome)


@contextmanager
def conexao(caminho_banco):
    banco = sqlite3.connect(str(caminho_banco))
    banco.row_factory = sqlite3.Row
    banco.execute("PRAGMA foreign_keys = ON")
    try:
        yield banco
    finally:
        banco.close()


def inicializar_banco(caminho_banco=None):
    """Cria o schema se necessário, preservando qualquer banco já existente."""
    caminho = Path(caminho_banco or caminho_padrao_banco())
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with conexao(caminho) as banco:
        banco.executescript(SCHEMA)
        banco.commit()
    return caminho


def _hash_senha(senha):
    if not senha:
        raise ValorInvalido("A senha da turma é obrigatória.")
    sal = secrets.token_bytes(16)
    derivada = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), sal, 310_000)
    return "pbkdf2_sha256${}${}".format(sal.hex(), derivada.hex())


def verificar_senha_turma(senha, senha_armazenada):
    try:
        algoritmo, sal_hex, hash_hex = senha_armazenada.split("$", 2)
        if algoritmo != "pbkdf2_sha256":
            return False
        derivada = hashlib.pbkdf2_hmac(
            "sha256", senha.encode("utf-8"), bytes.fromhex(sal_hex), 310_000
        )
        return hmac.compare_digest(derivada, bytes.fromhex(hash_hex))
    except (AttributeError, ValueError):
        return False


def criar_turma(
    caminho_banco,
    nome,
    inicio_em,
    validade_em,
    saldo_inicial_centavos,
    senha,
    descricao=None,
    agora=None,
    timezone_nome=None,
):
    if not nome or not nome.strip():
        raise ValorInvalido("O nome da turma é obrigatório.")
    if isinstance(saldo_inicial_centavos, bool) or not isinstance(saldo_inicial_centavos, int):
        raise ValorInvalido("O saldo inicial deve ser informado em centavos.")
    if saldo_inicial_centavos < 0:
        raise ValorInvalido("O saldo inicial não pode ser negativo.")
    inicio = agora_local(inicio_em, timezone_nome)
    validade = agora_local(validade_em, timezone_nome)
    if inicio > validade:
        raise ValorInvalido("O início não pode ser posterior à validade.")

    with conexao(caminho_banco) as banco:
        cursor = banco.execute(
            """INSERT INTO turmas
               (nome, descricao, inicio_em, validade_em, saldo_inicial, senha, criada_em)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                nome.strip(), descricao, _para_iso(inicio, timezone_nome),
                _para_iso(validade, timezone_nome), saldo_inicial_centavos,
                _hash_senha(senha), _para_iso(agora or datetime.now(), timezone_nome),
            ),
        )
        banco.commit()
        return buscar_turma(caminho_banco, cursor.lastrowid)


def buscar_turma(caminho_banco, turma_id):
    with conexao(caminho_banco) as banco:
        linha = banco.execute("SELECT * FROM turmas WHERE id = ?", (turma_id,)).fetchone()
    if linha is None:
        raise ContaNaoEncontrada("Turma não encontrada.")
    return dict(linha)


def listar_turmas(caminho_banco, agora=None, timezone_nome=None):
    """Lista turmas com a quantidade de alunas, sem consultas por turma."""
    with conexao(caminho_banco) as banco:
        linhas = banco.execute(
            """SELECT turmas.*, COUNT(alunas.id) AS quantidade_alunas
               FROM turmas
               LEFT JOIN alunas ON alunas.turma_id = turmas.id
               GROUP BY turmas.id
               ORDER BY turmas.criada_em DESC, turmas.id DESC"""
        ).fetchall()
    turmas = []
    prioridade = {"ATIVA": 0, "AGENDADA": 1, "EXPIRADA": 2, "ENCERRADA": 3}
    for linha in linhas:
        turma = dict(linha)
        turma["status"] = status_turma(turma, agora, timezone_nome)
        turmas.append(turma)
    # A consulta já traz as mais recentes primeiro; a ordenação estável mantém
    # essa ordem dentro de cada grupo de status.
    return sorted(turmas, key=lambda turma: prioridade[turma["status"]])


def contar_alunas_turma(caminho_banco, turma_id):
    with conexao(caminho_banco) as banco:
        linha = banco.execute(
            "SELECT COUNT(*) AS quantidade FROM alunas WHERE turma_id = ?", (turma_id,)
        ).fetchone()
    return linha["quantidade"]


def status_turma(turma, agora=None, timezone_nome=None):
    if turma["encerrada_em"] is not None:
        return "ENCERRADA"
    momento = agora_local(agora, timezone_nome)
    inicio = _de_iso(turma["inicio_em"], timezone_nome)
    validade = _de_iso(turma["validade_em"], timezone_nome)
    if momento < inicio:
        return "AGENDADA"
    if momento <= validade:
        return "ATIVA"
    return "EXPIRADA"


def encerrar_turma(caminho_banco, turma_id, agora=None, timezone_nome=None):
    with conexao(caminho_banco) as banco:
        cursor = banco.execute(
            "UPDATE turmas SET encerrada_em = ? WHERE id = ?",
            (_para_iso(agora or datetime.now(), timezone_nome), turma_id),
        )
        if cursor.rowcount != 1:
            raise ContaNaoEncontrada("Turma não encontrada.")
        banco.commit()
    return buscar_turma(caminho_banco, turma_id)


def alterar_validade_turma(caminho_banco, turma_id, validade_em, timezone_nome=None):
    """Altera somente a validade; uma turma encerrada permanece encerrada."""
    nova_validade = agora_local(validade_em, timezone_nome)
    with conexao(caminho_banco) as banco:
        turma = banco.execute("SELECT inicio_em FROM turmas WHERE id = ?", (turma_id,)).fetchone()
        if turma is None:
            raise ContaNaoEncontrada("Turma não encontrada.")
        inicio = _de_iso(turma["inicio_em"], timezone_nome)
        if nova_validade < inicio:
            raise ValorInvalido("A validade não pode ser anterior ao início.")
        banco.execute(
            "UPDATE turmas SET validade_em = ? WHERE id = ?",
            (_para_iso(nova_validade, timezone_nome), turma_id),
        )
        banco.commit()
    return buscar_turma(caminho_banco, turma_id)


def _validar_codigo(codigo):
    return isinstance(codigo, str) and len(codigo) == 6 and codigo.isascii() and codigo.isdigit() and codigo[0] != "0"


def gerar_codigo_conta(banco):
    """Gera um código globalmente único de seis dígitos, sem zero inicial."""
    for _ in range(MAX_TENTATIVAS_CODIGO):
        codigo = str(secrets.randbelow(900_000) + 100_000)
        existe = banco.execute("SELECT 1 FROM alunas WHERE codigo_conta = ?", (codigo,)).fetchone()
        if existe is None:
            return codigo
    raise ErroStorage("Não foi possível gerar um código de conta único.")


def criar_aluna(caminho_banco, turma_id, nome, codigo_conta=None, agora=None, timezone_nome=None):
    if not nome or not nome.strip():
        raise ValorInvalido("O nome da aluna é obrigatório.")
    if codigo_conta is not None and not _validar_codigo(codigo_conta):
        raise CodigoContaInvalido("O código da conta deve ter seis dígitos e não começar com zero.")

    with conexao(caminho_banco) as banco:
        turma = banco.execute("SELECT saldo_inicial FROM turmas WHERE id = ?", (turma_id,)).fetchone()
        if turma is None:
            raise ContaNaoEncontrada("Turma não encontrada.")
        codigo = codigo_conta or gerar_codigo_conta(banco)
        try:
            cursor = banco.execute(
                """INSERT INTO alunas (turma_id, nome, codigo_conta, saldo, criada_em)
                   VALUES (?, ?, ?, ?, ?)""",
                (turma_id, nome.strip(), codigo, turma["saldo_inicial"], _para_iso(agora or datetime.now(), timezone_nome)),
            )
        except sqlite3.IntegrityError as erro:
            if "UNIQUE constraint failed: alunas.codigo_conta" in str(erro):
                raise CodigoContaInvalido("O código da conta já existe.") from erro
            raise
        banco.commit()
        linha = banco.execute("SELECT * FROM alunas WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(linha)


def buscar_aluna(caminho_banco, aluna_id):
    with conexao(caminho_banco) as banco:
        linha = banco.execute("SELECT * FROM alunas WHERE id = ?", (aluna_id,)).fetchone()
    if linha is None:
        raise ContaNaoEncontrada("Conta não encontrada.")
    return dict(linha)


def listar_transacoes(caminho_banco, turma_id):
    with conexao(caminho_banco) as banco:
        linhas = banco.execute(
            "SELECT * FROM transacoes WHERE turma_id = ? ORDER BY id", (turma_id,)
        ).fetchall()
    return [dict(linha) for linha in linhas]


def executar_pix(caminho_banco, turma_id, origem_aluna_id, destino_aluna_id, valor_centavos, agora=None, timezone_nome=None):
    """Executa débito, crédito e registro de transação em uma única transação SQLite."""
    if isinstance(valor_centavos, bool) or not isinstance(valor_centavos, int) or valor_centavos <= 0:
        raise ValorInvalido("O valor deve ser um número inteiro positivo de centavos.")
    if origem_aluna_id == destino_aluna_id:
        raise ValorInvalido("A origem e o destino devem ser diferentes.")

    with conexao(caminho_banco) as banco:
        try:
            banco.execute("BEGIN IMMEDIATE")
            turma = banco.execute("SELECT * FROM turmas WHERE id = ?", (turma_id,)).fetchone()
            if turma is None:
                raise ContaNaoEncontrada("Turma não encontrada.")
            if status_turma(dict(turma), agora, timezone_nome) != "ATIVA":
                raise TurmaInativa("A turma não está ativa.")

            origem = banco.execute("SELECT * FROM alunas WHERE id = ?", (origem_aluna_id,)).fetchone()
            destino = banco.execute("SELECT * FROM alunas WHERE id = ?", (destino_aluna_id,)).fetchone()
            if origem is None or destino is None:
                raise ContaNaoEncontrada("Conta não encontrada.")
            if origem["turma_id"] != turma_id or destino["turma_id"] != turma_id:
                raise TurmasDiferentes("Pix entre turmas não é permitido.")

            debito = banco.execute(
                "UPDATE alunas SET saldo = saldo - ? WHERE id = ? AND saldo >= ?",
                (valor_centavos, origem_aluna_id, valor_centavos),
            )
            if debito.rowcount != 1:
                raise SaldoInsuficiente("Saldo insuficiente.")
            banco.execute(
                "UPDATE alunas SET saldo = saldo + ? WHERE id = ?",
                (valor_centavos, destino_aluna_id),
            )
            banco.execute(
                """INSERT INTO transacoes
                   (turma_id, origem_aluna_id, destino_aluna_id, valor, criada_em)
                   VALUES (?, ?, ?, ?, ?)""",
                (turma_id, origem_aluna_id, destino_aluna_id, valor_centavos, _para_iso(agora or datetime.now(), timezone_nome)),
            )
            banco.commit()
        except Exception:
            banco.rollback()
            raise
