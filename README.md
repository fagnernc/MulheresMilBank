# Mulheres Mil Bank

O Mulheres Mil Bank é um simulador educacional de operações bancárias e Pix para atividades de inclusão digital realizadas no contexto do Programa Mulheres Mil. Ele permite praticar acesso a uma conta fictícia, consulta de saldo e extrato, além de transferências simuladas entre participantes.

> Este projeto é somente um recurso educacional. Não é banco, não movimenta dinheiro e não possui integração com Pix, bancos, instituições financeiras, IFS, MEC ou Programa Mulheres Mil.

O objetivo é criar um ambiente seguro para aprender, experimentar e conversar sobre segurança digital sem utilizar dinheiro ou credenciais reais.

## O que pode ser trabalhado

- uso de aplicativos e serviços digitais;
- consulta de saldo e extrato;
- conferência de dados antes de uma transferência;
- noções básicas de Pix e de segurança digital;
- prevenção a golpes, autonomia e confiança no uso da tecnologia.

## Antes de começar

Use somente dados fictícios para a atividade. Nunca inclua no repositório nomes reais, senhas pessoais, dados bancários ou qualquer dado sensível das participantes.

O arquivo de exemplo [alunas.example.csv](alunas.example.csv) mostra o formato esperado:

```csv
codigo,nome
100001,MARIA DA SILVA
100002,ANA DOS SANTOS
```

Os arquivos `alunas.csv`, `alunas_*.csv`, `estado.json`, `.env` e o diretório `data/` são ignorados pelo Git para ajudar a evitar a publicação acidental de dados usados em aula.

## Execução local com Python

Requisitos: Python 3 e uma rede Wi-Fi compartilhada entre o computador do professor e os celulares das participantes.

Na pasta do projeto, prepare uma cópia local da lista de participantes:

```bash
cp alunas.example.csv alunas.csv
```

Edite `alunas.csv` com os dados fictícios da turma e inicie a aplicação:

```bash
python3 app.py
```

O terminal mostrará um endereço como `http://192.168.0.15:8000`. Abra esse endereço nos celulares conectados à mesma rede.

### Configuração local opcional

Na execução direta, as configurações são recebidas como variáveis de ambiente. O aplicativo não carrega o arquivo `.env` automaticamente nessa modalidade. Para definir valores próprios, use por exemplo:

```bash
export PORTA=8000
export CODIGO_ADMIN='defina-um-codigo-proprio'
export SENHA_PADRAO='defina-uma-senha-propria'
python3 app.py
```

Não use senhas bancárias, senhas pessoais ou credenciais reais.

## Execução com Docker Compose

Requisitos: Docker com Docker Compose.

O Compose usa `.env` para receber as credenciais da simulação e mantém os arquivos de atividade em `data/`, montado no contêiner como `/data`.

```bash
cp .env.example .env
mkdir -p data
cp alunas.example.csv data/alunas.csv
docker compose up --build
```

Antes de iniciar, edite `.env` e substitua os valores de exemplo por valores próprios para:

- `CODIGO_ADMIN`: código usado pelo professor para entrar no painel administrativo;
- `SENHA_PADRAO`: senha fictícia usada pelas participantes na atividade.

`PORTA` também está disponível no `.env.example`; a configuração atual do Compose publica temporariamente `8000:8000` para testes locais. O simulador ficará disponível em `http://localhost:8000` no computador que executa o Docker. Para encerrar, use `Ctrl+C`.

## Painel do professor

O painel está em `/admin`, por exemplo `http://localhost:8000/admin`. Ele exige login em `/admin/login` com `CODIGO_ADMIN` e permite acompanhar a simulação e reiniciar saldos e extratos para uma nova turma.

O código administrativo não é enviado para o navegador. O acesso é mantido por uma sessão administrativa temporária no servidor. Ao finalizar a atividade, use o botão de sair do painel.

## Estrutura principal

| Caminho | Finalidade |
| --- | --- |
| `app.py` | Aplicação Python baseada apenas na biblioteca padrão. |
| `assets/` | Recursos visuais versionados, incluindo `assets/logo.png`. |
| `alunas.example.csv` | Modelo fictício para a lista de participantes. |
| `Dockerfile` | Imagem para executar a aplicação em Docker. |
| `compose.yml` | Execução local com Docker Compose e volume persistente. |
| `.env.example` | Modelo de variáveis de configuração, sem segredos reais. |
| `data/` | Dados persistentes da atividade no Docker; não é versionado. |

## Privacidade e segurança

O Mulheres Mil Bank é uma simulação. Ele não realiza Pix verdadeiro, não se conecta a bancos ou instituições financeiras e não deve ser usado como sistema financeiro real.

Mantenha `.env`, `alunas.csv`, `estado.json` e `data/` fora do repositório. Antes de compartilhar o projeto publicamente, confira se não há arquivos com dados de participantes ou credenciais locais no diretório de trabalho.

## Contribuições

Educadores, desenvolvedores e pessoas interessadas em inclusão digital podem estudar, adaptar e contribuir com o projeto. Sugestões de atividades pedagógicas são bem-vindas.

## 🤖 Inteligência Artificial no desenvolvimento

O Mulheres Mil Bank foi idealizado e desenvolvido por Fágner Nascimento Cunha com apoio de ferramentas de Inteligência Artificial. A IA foi utilizada como ferramenta de apoio na estruturação e revisão de código, documentação, análise de segurança, preparação do ambiente Docker e aperfeiçoamento da aplicação.

As decisões sobre a proposta pedagógica, as funcionalidades, a experiência de uso e a aplicação em sala de aula foram conduzidas pelo autor a partir das necessidades observadas nas atividades do Programa Mulheres Mil.

## Autoria

Projeto idealizado e desenvolvido por Fágner Nascimento Cunha, com apoio de ferramentas de Inteligência Artificial no processo de desenvolvimento de software.

## Licença

Este projeto é distribuído sob a [Licença MIT](LICENSE).

Copyright © 2026 Fágner Nascimento Cunha.
