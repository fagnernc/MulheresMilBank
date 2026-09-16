import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import storage


FUSO = ZoneInfo("America/Maceio")


class StorageTestCase(unittest.TestCase):
    def setUp(self):
        self.temporario = tempfile.TemporaryDirectory()
        self.banco = Path(self.temporario.name) / "mulheresmilbank.db"
        storage.inicializar_banco(self.banco)
        self.agora = datetime.now(FUSO).replace(second=0, microsecond=0)

    def tearDown(self):
        self.temporario.cleanup()

    def criar_turma_ativa(self, **campos):
        parametros = {
            "nome": "Turma de teste",
            "inicio_em": self.agora - timedelta(hours=1),
            "validade_em": self.agora + timedelta(hours=1),
            "saldo_inicial_centavos": 100_000,
            "senha": "senha-da-turma",
        }
        parametros.update(campos)
        return storage.criar_turma(self.banco, **parametros)

    def test_cria_banco_e_tabelas(self):
        self.assertTrue(self.banco.exists())
        with storage.conexao(self.banco) as banco:
            tabelas = {
                linha["name"]
                for linha in banco.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        self.assertTrue({"turmas", "alunas", "transacoes"}.issubset(tabelas))

    def test_cria_turma_com_senha_hash(self):
        turma = self.criar_turma_ativa()
        self.assertEqual(turma["nome"], "Turma de teste")
        self.assertNotEqual(turma["senha"], "senha-da-turma")
        self.assertNotIn("senha-da-turma", turma["senha"])
        self.assertTrue(storage.verificar_senha_turma("senha-da-turma", turma["senha"]))
        self.assertFalse(storage.verificar_senha_turma("senha-incorreta", turma["senha"]))

    def test_status_agendada(self):
        turma = self.criar_turma_ativa(inicio_em=self.agora + timedelta(hours=1), validade_em=self.agora + timedelta(hours=2))
        self.assertEqual(storage.status_turma(turma, self.agora), "AGENDADA")

    def test_status_ativa(self):
        turma = self.criar_turma_ativa()
        self.assertEqual(storage.status_turma(turma, self.agora), "ATIVA")

    def test_status_e_ativo_exatamente_no_inicio(self):
        inicio = self.agora + timedelta(hours=1)
        turma = self.criar_turma_ativa(inicio_em=inicio, validade_em=inicio + timedelta(hours=1))
        self.assertEqual(storage.status_turma(turma, inicio), "ATIVA")

    def test_status_e_ativo_exatamente_na_validade(self):
        validade = self.agora + timedelta(hours=1)
        turma = self.criar_turma_ativa(inicio_em=self.agora, validade_em=validade)
        self.assertEqual(storage.status_turma(turma, validade), "ATIVA")

    def test_status_expira_imediatamente_apos_validade(self):
        validade = self.agora + timedelta(hours=1)
        turma = self.criar_turma_ativa(inicio_em=self.agora, validade_em=validade)
        self.assertEqual(storage.status_turma(turma, validade + timedelta(microseconds=1)), "EXPIRADA")

    def test_status_expirada(self):
        turma = self.criar_turma_ativa(inicio_em=self.agora - timedelta(hours=2), validade_em=self.agora - timedelta(seconds=1))
        self.assertEqual(storage.status_turma(turma, self.agora), "EXPIRADA")

    def test_status_encerrada(self):
        turma = self.criar_turma_ativa()
        encerrada = storage.encerrar_turma(self.banco, turma["id"], self.agora)
        self.assertEqual(storage.status_turma(encerrada, self.agora), "ENCERRADA")

    def test_lista_turmas_com_status_e_ordenacao(self):
        expirada = self.criar_turma_ativa(
            nome="Expirada", inicio_em=self.agora - timedelta(days=2),
            validade_em=self.agora - timedelta(days=1),
        )
        ativa = self.criar_turma_ativa(nome="Ativa")
        turmas = storage.listar_turmas(self.banco, self.agora)
        self.assertEqual([turma["id"] for turma in turmas], [ativa["id"], expirada["id"]])
        self.assertEqual(turmas[0]["status"], "ATIVA")
        self.assertEqual(turmas[0]["quantidade_alunas"], 0)

    def test_conta_alunas_da_turma(self):
        turma = self.criar_turma_ativa()
        storage.criar_aluna(self.banco, turma["id"], "Ana")
        storage.criar_aluna(self.banco, turma["id"], "Bia")
        self.assertEqual(storage.contar_alunas_turma(self.banco, turma["id"]), 2)

    def test_altera_validade_da_turma(self):
        turma = self.criar_turma_ativa()
        nova_validade = self.agora + timedelta(days=2)
        alterada = storage.alterar_validade_turma(self.banco, turma["id"], nova_validade)
        self.assertEqual(storage.status_turma(alterada, self.agora), "ATIVA")
        self.assertEqual(storage._de_iso(alterada["validade_em"]), nova_validade)

    def test_rejeita_validade_anterior_ao_inicio(self):
        turma = self.criar_turma_ativa()
        with self.assertRaises(storage.ValorInvalido):
            storage.alterar_validade_turma(self.banco, turma["id"], self.agora - timedelta(days=2))

    def test_alterar_validade_de_turma_encerrada_nao_reabre(self):
        turma = self.criar_turma_ativa()
        storage.encerrar_turma(self.banco, turma["id"], self.agora)
        alterada = storage.alterar_validade_turma(self.banco, turma["id"], self.agora + timedelta(days=2))
        self.assertIsNotNone(alterada["encerrada_em"])
        self.assertEqual(storage.status_turma(alterada, self.agora), "ENCERRADA")

    def test_cria_aluna_com_saldo_inicial(self):
        turma = self.criar_turma_ativa(saldo_inicial_centavos=12_345)
        aluna = storage.criar_aluna(self.banco, turma["id"], "Ana")
        self.assertEqual(aluna["turma_id"], turma["id"])
        self.assertEqual(aluna["saldo"], 12_345)

    def test_codigos_tem_seis_digitos_e_nao_duplicam(self):
        turma = self.criar_turma_ativa()
        codigos = {
            storage.criar_aluna(self.banco, turma["id"], f"Aluna {numero}")["codigo_conta"]
            for numero in range(30)
        }
        self.assertEqual(len(codigos), 30)
        self.assertTrue(all(len(codigo) == 6 and codigo.isdigit() and codigo[0] != "0" for codigo in codigos))

    def test_codigo_manual_duplicado_e_rejeitado_globalmente(self):
        primeira = self.criar_turma_ativa(nome="Primeira")
        segunda = self.criar_turma_ativa(nome="Segunda")
        storage.criar_aluna(self.banco, primeira["id"], "Ana", "123456")
        with self.assertRaises(storage.CodigoContaInvalido):
            storage.criar_aluna(self.banco, segunda["id"], "Bia", "123456")

    def test_pix_na_mesma_turma_debita_credia_e_registra(self):
        turma = self.criar_turma_ativa()
        origem = storage.criar_aluna(self.banco, turma["id"], "Ana")
        destino = storage.criar_aluna(self.banco, turma["id"], "Bia")
        storage.executar_pix(self.banco, turma["id"], origem["id"], destino["id"], 2_500, self.agora)
        self.assertEqual(storage.buscar_aluna(self.banco, origem["id"])["saldo"], 97_500)
        self.assertEqual(storage.buscar_aluna(self.banco, destino["id"])["saldo"], 102_500)
        transacoes = storage.listar_transacoes(self.banco, turma["id"])
        self.assertEqual(len(transacoes), 1)
        self.assertEqual(transacoes[0]["valor"], 2_500)

    def test_pix_rejeita_saldo_insuficiente_sem_alterar_saldos(self):
        turma = self.criar_turma_ativa(saldo_inicial_centavos=100)
        origem = storage.criar_aluna(self.banco, turma["id"], "Ana")
        destino = storage.criar_aluna(self.banco, turma["id"], "Bia")
        with self.assertRaises(storage.SaldoInsuficiente):
            storage.executar_pix(self.banco, turma["id"], origem["id"], destino["id"], 101, self.agora)
        self.assertEqual(storage.buscar_aluna(self.banco, origem["id"])["saldo"], 100)
        self.assertEqual(storage.buscar_aluna(self.banco, destino["id"])["saldo"], 100)

    def test_pix_rejeita_turmas_diferentes(self):
        primeira = self.criar_turma_ativa(nome="Primeira")
        segunda = self.criar_turma_ativa(nome="Segunda")
        origem = storage.criar_aluna(self.banco, primeira["id"], "Ana")
        destino = storage.criar_aluna(self.banco, segunda["id"], "Bia")
        with self.assertRaises(storage.TurmasDiferentes):
            storage.executar_pix(self.banco, primeira["id"], origem["id"], destino["id"], 100, self.agora)

    def test_pix_rejeita_turma_expirada(self):
        turma = self.criar_turma_ativa(validade_em=self.agora - timedelta(seconds=1))
        antes_da_expiracao = self.agora - timedelta(seconds=2)
        origem = storage.criar_aluna(self.banco, turma["id"], "Ana", agora=antes_da_expiracao)
        destino = storage.criar_aluna(self.banco, turma["id"], "Bia", agora=antes_da_expiracao)
        with self.assertRaises(storage.TurmaInativa):
            storage.executar_pix(self.banco, turma["id"], origem["id"], destino["id"], 100, self.agora)

    def test_pix_rejeita_turma_encerrada_sem_alterar_dados(self):
        turma = self.criar_turma_ativa()
        origem = storage.criar_aluna(self.banco, turma["id"], "Ana")
        destino = storage.criar_aluna(self.banco, turma["id"], "Bia")
        storage.encerrar_turma(self.banco, turma["id"], self.agora)
        with self.assertRaises(storage.TurmaInativa):
            storage.executar_pix(self.banco, turma["id"], origem["id"], destino["id"], 100, self.agora)
        self.assertEqual(storage.buscar_aluna(self.banco, origem["id"])["saldo"], 100_000)
        self.assertEqual(storage.buscar_aluna(self.banco, destino["id"])["saldo"], 100_000)
        self.assertEqual(storage.listar_transacoes(self.banco, turma["id"]), [])

    def test_lista_alunas_da_turma_ordenada_por_nome(self):
        turma = self.criar_turma_ativa()
        storage.criar_aluna(self.banco, turma["id"], "Bia", agora=self.agora)
        storage.criar_aluna(self.banco, turma["id"], "Ana", agora=self.agora)
        self.assertEqual(
            [aluna["nome"] for aluna in storage.listar_alunas_turma(self.banco, turma["id"])],
            ["Ana", "Bia"],
        )

    def test_cria_alunas_em_lote_ignora_linhas_vazias_e_permite_nomes_iguais(self):
        turma = self.criar_turma_ativa()
        alunas = storage.criar_alunas_em_lote(
            self.banco, turma["id"], "  Ana  \n\nBia\n Ana\n", agora=self.agora
        )
        self.assertEqual([aluna["nome"] for aluna in alunas], ["Ana", "Bia", "Ana"])
        self.assertEqual(len({aluna["codigo_conta"] for aluna in alunas}), 3)
        self.assertTrue(all(aluna["saldo"] == 100_000 for aluna in alunas))

    def test_lote_vazio_e_rejeitado(self):
        turma = self.criar_turma_ativa()
        with self.assertRaises(storage.ValorInvalido):
            storage.criar_alunas_em_lote(self.banco, turma["id"], " \n\n ", agora=self.agora)
        self.assertEqual(storage.listar_alunas_turma(self.banco, turma["id"]), [])

    def test_lote_acima_do_limite_e_rejeitado_integralmente(self):
        turma = self.criar_turma_ativa()
        nomes = [f"Aluna {numero}" for numero in range(storage.MAX_ALUNAS_POR_LOTE + 1)]
        with self.assertRaises(storage.ValorInvalido):
            storage.criar_alunas_em_lote(self.banco, turma["id"], nomes, agora=self.agora)
        self.assertEqual(storage.listar_alunas_turma(self.banco, turma["id"]), [])

    def test_falha_no_lote_faz_rollback_integral(self):
        turma = self.criar_turma_ativa()
        gerar_codigo = storage.gerar_codigo_conta
        chamadas = 0

        def falhar_na_segunda_chamada(banco):
            nonlocal chamadas
            chamadas += 1
            if chamadas == 2:
                raise storage.ErroStorage("falha simulada")
            return gerar_codigo(banco)

        with mock.patch.object(storage, "gerar_codigo_conta", falhar_na_segunda_chamada):
            with self.assertRaises(storage.ErroStorage):
                storage.criar_alunas_em_lote(
                    self.banco, turma["id"], ["Ana", "Bia"], agora=self.agora
                )
        self.assertEqual(storage.listar_alunas_turma(self.banco, turma["id"]), [])

    def test_cadastro_e_rejeitado_em_turma_expirada(self):
        turma = self.criar_turma_ativa(validade_em=self.agora - timedelta(seconds=1))
        with self.assertRaises(storage.TurmaNaoAceitaAlteracoes):
            storage.criar_aluna(self.banco, turma["id"], "Ana", agora=self.agora)

    def test_cadastro_e_rejeitado_em_turma_encerrada(self):
        turma = self.criar_turma_ativa()
        storage.encerrar_turma(self.banco, turma["id"], self.agora)
        with self.assertRaises(storage.TurmaNaoAceitaAlteracoes):
            storage.criar_aluna(self.banco, turma["id"], "Ana", agora=self.agora)

    def test_cadastro_e_permitido_em_turma_agendada(self):
        turma = self.criar_turma_ativa(
            inicio_em=self.agora + timedelta(hours=1),
            validade_em=self.agora + timedelta(hours=2),
        )
        aluna = storage.criar_aluna(self.banco, turma["id"], "Ana", agora=self.agora)
        self.assertEqual(aluna["turma_id"], turma["id"])

    def test_exclui_aluna_sem_transacoes(self):
        turma = self.criar_turma_ativa()
        aluna = storage.criar_aluna(self.banco, turma["id"], "Ana", agora=self.agora)
        excluida = storage.excluir_aluna(self.banco, turma["id"], aluna["id"], agora=self.agora)
        self.assertEqual(excluida["id"], aluna["id"])
        with self.assertRaises(storage.ContaNaoEncontrada):
            storage.buscar_aluna(self.banco, aluna["id"])

    def test_exclusao_e_rejeitada_quando_aluna_tem_transacoes(self):
        turma = self.criar_turma_ativa()
        origem = storage.criar_aluna(self.banco, turma["id"], "Ana", agora=self.agora)
        destino = storage.criar_aluna(self.banco, turma["id"], "Bia", agora=self.agora)
        storage.executar_pix(self.banco, turma["id"], origem["id"], destino["id"], 100, self.agora)
        with self.assertRaises(storage.AlunaComTransacoes):
            storage.excluir_aluna(self.banco, turma["id"], origem["id"], agora=self.agora)
        self.assertEqual(storage.buscar_aluna(self.banco, origem["id"])["nome"], "Ana")

    def test_exclusao_e_rejeitada_para_aluna_de_outra_turma(self):
        primeira = self.criar_turma_ativa(nome="Primeira")
        segunda = self.criar_turma_ativa(nome="Segunda")
        aluna = storage.criar_aluna(self.banco, segunda["id"], "Ana", agora=self.agora)
        with self.assertRaises(storage.AlunaNaoPertenceTurma):
            storage.excluir_aluna(self.banco, primeira["id"], aluna["id"], agora=self.agora)

    def test_exclusao_e_rejeitada_em_turma_expirada_ou_encerrada(self):
        expirada = self.criar_turma_ativa()
        aluna_expirada = storage.criar_aluna(self.banco, expirada["id"], "Ana", agora=self.agora)
        storage.alterar_validade_turma(
            self.banco, expirada["id"], self.agora - timedelta(seconds=1)
        )
        with self.assertRaises(storage.TurmaNaoAceitaAlteracoes):
            storage.excluir_aluna(self.banco, expirada["id"], aluna_expirada["id"], agora=self.agora)

        encerrada = self.criar_turma_ativa(nome="Encerrada")
        aluna_encerrada = storage.criar_aluna(self.banco, encerrada["id"], "Bia", agora=self.agora)
        storage.encerrar_turma(self.banco, encerrada["id"], self.agora)
        with self.assertRaises(storage.TurmaNaoAceitaAlteracoes):
            storage.excluir_aluna(self.banco, encerrada["id"], aluna_encerrada["id"], agora=self.agora)

    def test_autentica_aluna_com_senha_correta_e_rejeita_credenciais_invalidas(self):
        turma = self.criar_turma_ativa()
        aluna = storage.criar_aluna(self.banco, turma["id"], "Ana", agora=self.agora)
        autenticada = storage.autenticar_aluna(
            self.banco, aluna["codigo_conta"], "senha-da-turma", agora=self.agora
        )
        self.assertEqual(autenticada["id"], aluna["id"])
        for conta, senha in ((aluna["codigo_conta"], "errada"), ("999999", "senha-da-turma")):
            with self.subTest(conta=conta):
                with self.assertRaises(storage.AutenticacaoInvalida):
                    storage.autenticar_aluna(self.banco, conta, senha, agora=self.agora)

    def test_login_bloqueado_em_turma_nao_ativa(self):
        agendada = self.criar_turma_ativa(
            inicio_em=self.agora + timedelta(hours=1), validade_em=self.agora + timedelta(hours=2)
        )
        aluna_agendada = storage.criar_aluna(self.banco, agendada["id"], "Ana", agora=self.agora)
        for turma, aluna in ((agendada, aluna_agendada),):
            with self.assertRaises(storage.TurmaInativa):
                storage.autenticar_aluna(self.banco, aluna["codigo_conta"], "senha-da-turma", agora=self.agora)
        ativa = self.criar_turma_ativa(nome="Ativa")
        aluna = storage.criar_aluna(self.banco, ativa["id"], "Bia", agora=self.agora)
        storage.alterar_validade_turma(self.banco, ativa["id"], self.agora - timedelta(seconds=1))
        with self.assertRaises(storage.TurmaInativa):
            storage.autenticar_aluna(self.banco, aluna["codigo_conta"], "senha-da-turma", agora=self.agora)
        encerrada = self.criar_turma_ativa(nome="Encerrada")
        aluna_encerrada = storage.criar_aluna(self.banco, encerrada["id"], "Cia", agora=self.agora)
        storage.encerrar_turma(self.banco, encerrada["id"], self.agora)
        with self.assertRaises(storage.TurmaInativa):
            storage.autenticar_aluna(self.banco, aluna_encerrada["codigo_conta"], "senha-da-turma", agora=self.agora)

    def test_extrato_da_aluna_mostra_enviado_e_recebido(self):
        turma = self.criar_turma_ativa()
        ana = storage.criar_aluna(self.banco, turma["id"], "Ana", agora=self.agora)
        bia = storage.criar_aluna(self.banco, turma["id"], "Bia", agora=self.agora)
        storage.executar_pix(self.banco, turma["id"], ana["id"], bia["id"], 250, self.agora)
        enviado = storage.listar_extrato_aluna(self.banco, ana["id"])[0]
        recebido = storage.listar_extrato_aluna(self.banco, bia["id"])[0]
        self.assertEqual((enviado["tipo"], enviado["nome"], enviado["valor"]), ("enviado", "Bia", 250))
        self.assertEqual((recebido["tipo"], recebido["nome"], recebido["valor"]), ("recebido", "Ana", 250))

    def test_redefinir_senha_invalida_a_anterior(self):
        turma = self.criar_turma_ativa()
        aluna = storage.criar_aluna(self.banco, turma["id"], "Ana", agora=self.agora)
        storage.redefinir_senha_turma(self.banco, turma["id"], "nova-senha")
        with self.assertRaises(storage.AutenticacaoInvalida):
            storage.autenticar_aluna(self.banco, aluna["codigo_conta"], "senha-da-turma", agora=self.agora)
        self.assertEqual(
            storage.autenticar_aluna(self.banco, aluna["codigo_conta"], "nova-senha", agora=self.agora)["id"], aluna["id"]
        )

    def test_pix_concorrente_nao_gasta_acima_do_saldo(self):
        turma = self.criar_turma_ativa(saldo_inicial_centavos=100)
        origem = storage.criar_aluna(self.banco, turma["id"], "Ana", agora=self.agora)
        destino_a = storage.criar_aluna(self.banco, turma["id"], "Bia", agora=self.agora)
        destino_b = storage.criar_aluna(self.banco, turma["id"], "Cia", agora=self.agora)
        resultados = []

        def transferir(destino):
            try:
                storage.executar_pix(self.banco, turma["id"], origem["id"], destino["id"], 80, self.agora)
                resultados.append("ok")
            except storage.SaldoInsuficiente:
                resultados.append("insuficiente")

        threads = [threading.Thread(target=transferir, args=(destino,)) for destino in (destino_a, destino_b)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(resultados.count("ok"), 1)
        self.assertEqual(storage.buscar_aluna(self.banco, origem["id"])["saldo"], 20)

    def test_pix_e_bloqueado_se_turma_encerrar_apos_login(self):
        turma = self.criar_turma_ativa()
        origem = storage.criar_aluna(self.banco, turma["id"], "Ana", agora=self.agora)
        destino = storage.criar_aluna(self.banco, turma["id"], "Bia", agora=self.agora)
        storage.autenticar_aluna(self.banco, origem["codigo_conta"], "senha-da-turma", agora=self.agora)
        storage.encerrar_turma(self.banco, turma["id"], self.agora)
        with self.assertRaises(storage.TurmaInativa):
            storage.executar_pix(self.banco, turma["id"], origem["id"], destino["id"], 100, self.agora)

    def test_estatisticas_de_turma_sem_movimentacao_sao_zero(self):
        turma = self.criar_turma_ativa()
        storage.criar_aluna(self.banco, turma["id"], "Ana", agora=self.agora)
        self.assertEqual(
            storage.obter_estatisticas_turma(self.banco, turma["id"]),
            {"participantes": 1, "fizeram_pix": 0, "transacoes": 0, "movimentado": 0},
        )

    def test_estatisticas_contam_transacoes_origens_unicas_e_soma(self):
        turma = self.criar_turma_ativa()
        ana = storage.criar_aluna(self.banco, turma["id"], "Ana", agora=self.agora)
        bia = storage.criar_aluna(self.banco, turma["id"], "Bia", agora=self.agora)
        clara = storage.criar_aluna(self.banco, turma["id"], "Clara", agora=self.agora)
        storage.executar_pix(self.banco, turma["id"], ana["id"], bia["id"], 100, self.agora)
        storage.executar_pix(self.banco, turma["id"], ana["id"], clara["id"], 200, self.agora)
        estatisticas = storage.obter_estatisticas_turma(self.banco, turma["id"])
        self.assertEqual(estatisticas["participantes"], 3)
        self.assertEqual(estatisticas["transacoes"], 2)
        self.assertEqual(estatisticas["movimentado"], 300)
        self.assertEqual(estatisticas["fizeram_pix"], 1)

    def test_estatisticas_nao_misturam_turmas(self):
        primeira = self.criar_turma_ativa(nome="Primeira")
        segunda = self.criar_turma_ativa(nome="Segunda")
        origem = storage.criar_aluna(self.banco, primeira["id"], "Ana", agora=self.agora)
        destino = storage.criar_aluna(self.banco, primeira["id"], "Bia", agora=self.agora)
        storage.criar_aluna(self.banco, segunda["id"], "Cia", agora=self.agora)
        storage.executar_pix(self.banco, primeira["id"], origem["id"], destino["id"], 500, self.agora)
        self.assertEqual(
            storage.obter_estatisticas_turma(self.banco, segunda["id"]),
            {"participantes": 1, "fizeram_pix": 0, "transacoes": 0, "movimentado": 0},
        )

    def test_descricao_do_pix_aparece_no_extrato(self):
        turma = self.criar_turma_ativa()
        origem = storage.criar_aluna(self.banco, turma["id"], "Ana", agora=self.agora)
        destino = storage.criar_aluna(self.banco, turma["id"], "Bia", agora=self.agora)
        storage.executar_pix(
            self.banco, turma["id"], origem["id"], destino["id"], 100,
            agora=self.agora, descricao="Material da oficina",
        )
        self.assertEqual(storage.listar_extrato_aluna(self.banco, origem["id"])[0]["descricao"], "Material da oficina")

    def test_migracao_adiciona_descricao_a_banco_existente(self):
        banco_antigo = Path(self.temporario.name) / "antigo.db"
        with sqlite3.connect(banco_antigo) as banco:
            banco.executescript(storage.SCHEMA.replace("descricao TEXT NOT NULL DEFAULT '',\n    criada_em", "criada_em"))
        storage.inicializar_banco(banco_antigo)
        with storage.conexao(banco_antigo) as banco:
            colunas = {linha["name"] for linha in banco.execute("PRAGMA table_info(transacoes)")}
        self.assertIn("descricao", colunas)

    def test_pix_e_atomico_se_registro_da_transacao_falhar(self):
        turma = self.criar_turma_ativa()
        origem = storage.criar_aluna(self.banco, turma["id"], "Ana")
        destino = storage.criar_aluna(self.banco, turma["id"], "Bia")
        with storage.conexao(self.banco) as banco:
            banco.execute(
                """CREATE TRIGGER falhar_antes_da_transacao
                   BEFORE INSERT ON transacoes
                   BEGIN SELECT RAISE(ABORT, 'falha simulada'); END"""
            )
            banco.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            storage.executar_pix(self.banco, turma["id"], origem["id"], destino["id"], 100, self.agora)
        self.assertEqual(storage.buscar_aluna(self.banco, origem["id"])["saldo"], 100_000)
        self.assertEqual(storage.buscar_aluna(self.banco, destino["id"])["saldo"], 100_000)
        self.assertEqual(storage.listar_transacoes(self.banco, turma["id"]), [])


if __name__ == "__main__":
    unittest.main()
