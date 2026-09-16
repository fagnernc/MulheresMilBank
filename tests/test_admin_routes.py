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
        cls.ambiente_anterior = {chave: os.environ.get(chave) for chave in ("DATA_DIR", "CODIGO_ADMIN", "SENHA_PADRAO")}
        os.environ["DATA_DIR"] = cls.temporario.name
        os.environ["CODIGO_ADMIN"] = "codigo-teste"
        os.environ["SENHA_PADRAO"] = "senha-teste"
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

    def requisicao_http(self, metodo, caminho, corpo=None, cabecalhos_adicionais=None):
        conexao = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=2)
        cabecalhos = dict(cabecalhos_adicionais or {})
        if corpo is not None:
            cabecalhos["Content-Type"] = "application/x-www-form-urlencoded"
        conexao.request(metodo, caminho, body=corpo, headers=cabecalhos)
        resposta = conexao.getresponse()
        resposta.read()
        resultado = resposta.status, resposta.getheader("Location")
        conexao.close()
        return resultado

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
        token = "sessao-admin-http-teste"
        with self.app.lock:
            self.app.sessoes_admin[token] = datetime.now() + self.app.SESSAO_ADMIN_TTL
        caminho = f"/admin/turmas/{self.turma['id']}/alunas/criar"
        resposta = self.requisicao_http(
            "POST",
            caminho,
            urlencode({"nome": "Patrícia Souza"}),
            {"Cookie": f"sessao_admin={token}"},
        )
        alunas = self.app.storage.listar_alunas_turma(self.app.V2_DB, self.turma["id"])
        self.assertEqual(resposta, (303, f"/admin/turmas/{self.turma['id']}"))
        self.assertIn("Patrícia Souza", [aluna["nome"] for aluna in alunas])

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
