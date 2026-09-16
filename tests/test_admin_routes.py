import importlib
import http.client
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from email.message import Message
from pathlib import Path
from urllib.parse import urlencode


class AdminTurmasRoutesTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporario = tempfile.TemporaryDirectory()
        diretorio = Path(cls.temporario.name)
        (diretorio / "alunas.csv").write_text("codigo,nome\n100001,ALUNA TESTE\n", encoding="latin1")
        cls.ambiente_anterior = {
            chave: os.environ.get(chave)
            for chave in ("DATA_DIR", "CODIGO_ADMIN", "SENHA_PADRAO", "DOMINIO")
        }
        os.environ["DATA_DIR"] = cls.temporario.name
        os.environ["CODIGO_ADMIN"] = "codigo-teste"
        os.environ["SENHA_PADRAO"] = "senha-teste"
        os.environ["DOMINIO"] = "projecao.exemplo.test"
        cls.app = importlib.import_module("app")
        if not cls.app.inicializar_v2():
            raise RuntimeError("Não foi possível inicializar o SQLite de teste da V2.")
        agora = datetime.now()
        cls.turma = cls.app.storage.criar_turma(
            cls.app.V2_DB,
            "Turma HTTP",
            agora - timedelta(hours=1),
            agora + timedelta(hours=1),
            100_000,
            "senha-da-turma",
        )
        cls.aluna_projecao = cls.app.storage.criar_aluna(
            cls.app.V2_DB, cls.turma["id"], "Nome Privado <script>"
        )
        cls.servidor = cls.app.ThreadingHTTPServer(("127.0.0.1", 0), cls.app.Handler)
        cls.porta = cls.servidor.server_address[1]
        cls.thread_servidor = threading.Thread(target=cls.servidor.serve_forever, daemon=True)
        cls.thread_servidor.start()

    @classmethod
    def tearDownClass(cls):
        cls.servidor.shutdown()
        cls.servidor.server_close()
        cls.thread_servidor.join(timeout=2)
        for chave, valor in cls.ambiente_anterior.items():
            if valor is None:
                os.environ.pop(chave, None)
            else:
                os.environ[chave] = valor
        cls.temporario.cleanup()

    def requisicao_http(self, metodo, caminho, corpo=None, cabecalhos_adicionais=None, com_corpo=False):
        conexao = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=2)
        cabecalhos = dict(cabecalhos_adicionais or {})
        if corpo is not None:
            cabecalhos["Content-Type"] = "application/x-www-form-urlencoded"
        conexao.request(metodo, caminho, body=corpo, headers=cabecalhos)
        resposta = conexao.getresponse()
        conteudo = resposta.read().decode("utf-8")
        resultado = resposta.status, resposta.getheader("Location")
        conexao.close()
        return (*resultado, conteudo) if com_corpo else resultado

    def cookie_admin(self):
        token = f"sessao-admin-http-{len(self.app.sessoes_admin)}"
        with self.app.lock:
            self.app.sessoes_admin[token] = datetime.now() + self.app.SESSAO_ADMIN_TTL
        return {"Cookie": f"sessao_admin={token}"}

    def test_listagem_de_turmas_redireciona_sem_sessao_administrativa(self):
        handler = object.__new__(self.app.Handler)
        handler.path = "/admin/turmas"
        handler.headers = Message()
        destinos = []
        handler.redirecionar = destinos.append

        handler.do_GET()

        self.assertEqual(destinos, ["/admin/login"])

    def test_http_listagem_de_turmas_redireciona_sem_sessao(self):
        self.assertEqual(self.requisicao_http("GET", "/admin/turmas"), (303, "/admin/login"))

    def test_http_formulario_de_turma_redireciona_sem_sessao(self):
        self.assertEqual(self.requisicao_http("GET", "/admin/turmas/nova"), (303, "/admin/login"))

    def test_http_nova_aluna_redireciona_sem_sessao(self):
        caminho = f"/admin/turmas/{self.turma['id']}/alunas/nova"
        self.assertEqual(self.requisicao_http("GET", caminho), (303, "/admin/login"))

    def test_http_lote_redireciona_sem_sessao(self):
        caminho = f"/admin/turmas/{self.turma['id']}/alunas/lote"
        self.assertEqual(self.requisicao_http("GET", caminho), (303, "/admin/login"))

    def test_http_criacao_de_turma_sem_sessao_nao_persiste(self):
        antes = len(self.app.storage.listar_turmas(self.app.V2_DB))
        resposta = self.requisicao_http(
            "POST",
            "/admin/turmas/criar",
            "nome=Tentativa&inicio_data=2026-09-15&inicio_hora=10%3A00"
            "&validade_data=2026-09-15&validade_hora=12%3A00"
            "&saldo_inicial=1000%2C00&senha=segredo",
        )
        depois = len(self.app.storage.listar_turmas(self.app.V2_DB))
        self.assertEqual(resposta, (303, "/admin/login"))
        self.assertEqual(depois, antes)

    def test_http_exclusao_sem_sessao_nao_remove_aluna(self):
        aluna = self.app.storage.criar_aluna(self.app.V2_DB, self.turma["id"], "Ana HTTP")
        caminho = f"/admin/turmas/{self.turma['id']}/alunas/{aluna['id']}/excluir"
        self.assertEqual(self.requisicao_http("POST", caminho), (303, "/admin/login"))
        self.assertEqual(self.app.storage.buscar_aluna(self.app.V2_DB, aluna["id"])["nome"], "Ana HTTP")

    def test_http_cadastro_autenticado_preserva_nome_utf8(self):
        caminho = f"/admin/turmas/{self.turma['id']}/alunas/criar"
        resposta = self.requisicao_http(
            "POST",
            caminho,
            urlencode({"nome": "Patrícia Souza"}),
            self.cookie_admin(),
        )
        alunas = self.app.storage.listar_alunas_turma(self.app.V2_DB, self.turma["id"])
        self.assertEqual(resposta, (303, f"/admin/turmas/{self.turma['id']}"))
        self.assertIn("Patrícia Souza", [aluna["nome"] for aluna in alunas])

    def test_http_projecao_sem_sessao_nao_expoe_dados(self):
        caminho = f"/admin/turmas/{self.turma['id']}/projetar"
        status, destino, conteudo = self.requisicao_http("GET", caminho, com_corpo=True)
        self.assertEqual((status, destino), (303, "/admin/login"))
        self.assertNotIn("Nome Privado", conteudo)
        self.assertNotIn(self.aluna_projecao["codigo_conta"], conteudo)

    def test_http_projecao_autenticada_escapa_nome_e_formata_codigo(self):
        caminho = f"/admin/turmas/{self.turma['id']}/projetar"
        status, destino, conteudo = self.requisicao_http(
            "GET", caminho, cabecalhos_adicionais=self.cookie_admin(), com_corpo=True
        )
        codigo = self.aluna_projecao["codigo_conta"]
        self.assertEqual((status, destino), (200, None))
        self.assertIn("Turma HTTP", conteudo)
        self.assertIn("Nome Privado &lt;script&gt;", conteudo)
        self.assertNotIn("Nome Privado <script>", conteudo)
        self.assertIn(f"{codigo[:3]} {codigo[3:]}", conteudo)
        self.assertNotIn(codigo, conteudo)
        self.assertEqual(len(codigo), 6)
        self.assertNotIn(" ", codigo)
        self.assertIn('name="robots" content="noindex,nofollow"', conteudo)
        self.assertIn("https://projecao.exemplo.test", conteudo)
        self.assertNotIn("https://projecao.exemplo.test/admin", conteudo)

    def test_http_projecao_indica_turma_expirada_e_encerrada(self):
        agora = datetime.now()
        expirada = self.app.storage.criar_turma(
            self.app.V2_DB, "Turma expirada", agora - timedelta(hours=2),
            agora + timedelta(hours=1), 100_000, "senha",
        )
        self.app.storage.criar_aluna(self.app.V2_DB, expirada["id"], "Ana")
        self.app.storage.alterar_validade_turma(self.app.V2_DB, expirada["id"], agora - timedelta(minutes=1))
        encerrada = self.app.storage.criar_turma(
            self.app.V2_DB, "Turma encerrada", agora - timedelta(hours=1),
            agora + timedelta(hours=1), 100_000, "senha",
        )
        self.app.storage.criar_aluna(self.app.V2_DB, encerrada["id"], "Bia")
        self.app.storage.encerrar_turma(self.app.V2_DB, encerrada["id"])
        cabecalhos = self.cookie_admin()
        for turma, status_esperado in ((expirada, "EXPIRADA"), (encerrada, "ENCERRADA")):
            with self.subTest(status=status_esperado):
                _, _, conteudo = self.requisicao_http(
                    "GET", f"/admin/turmas/{turma['id']}/projetar",
                    cabecalhos_adicionais=cabecalhos, com_corpo=True,
                )
                self.assertIn(status_esperado, conteudo)
                self.assertIn("As contas não estão disponíveis para operação.", conteudo)

    def test_valor_para_centavos_aceita_formatos_previstos(self):
        for valor in ("1000", "1000,00", "1.000,00", "1000.00", "R$ 1.000,00"):
            with self.subTest(valor=valor):
                self.assertEqual(self.app.valor_para_centavos(valor), 100_000)

    def test_valor_para_centavos_rejeita_formatos_invalidos_ou_ambiguos(self):
        for valor in (
            "-1", "1000,001", "1000.001", "NaN", "Infinity", "texto",
            "1.000", "1,000", "1,000.00", "1.00,00", "R$ -1", "1 000,00",
        ):
            with self.subTest(valor=valor):
                with self.assertRaises(ValueError):
                    self.app.valor_para_centavos(valor)


if __name__ == "__main__":
    unittest.main()
