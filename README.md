# Media Library Manager

Aplicacao web para pesquisar filmes e series, consultar streams por protocolo Stremio e organizar arquivos locais ou `.strm` para uma biblioteca Jellyfin.

## Deploy no Coolify

O repositorio inclui `Dockerfile` multi-stage e `docker-compose.yml`. No Coolify, use a raiz `/` e o arquivo `/docker-compose.yml`; o servico escuta na porta interna `8000` e oferece `GET /health`.

O Compose monta `/home/ubuntu/jellyfin/tvshows:/media`, e a biblioteca de destino fica em `/media/stream media`. SQLite e jobs ficam no volume persistente `app_data`. A imagem base Python e Node oferece ARM64; o Coolify compila para a arquitetura do host. Para escrita, o diretorio da biblioteca precisa conceder acesso ao UID `10001` do container.

Configure no Coolify `JELLYFIN_URL`, `JELLYFIN_API_KEY` e `SESSION_SECRET` (valor aleatorio, com pelo menos 32 caracteres). Mantenha esse segredo estavel: os links dos `.strm` dinamicos dependem dele. `PUBLIC_BASE_URL` deve ser o dominio HTTPS publico do app. `COOKIE_SECURE=true` funciona atras do proxy HTTPS do Coolify. Veja `.env.example` para as demais opcoes.

## Funcionalidades

- Busca filmes e series em Cinemeta por IMDb ID, com poster, ano, sinopse, temporadas, episodios e elenco quando disponiveis.
- Login com as credenciais do Jellyfin. O token de autenticacao nao e persistido. Apenas administradores podem mudar addons, preferencias ou arquivos da biblioteca.
- Cadastro de Manifest URLs Stremio: leitura e validacao do manifest, resources, types e idPrefixes opcionais, seguida de chamadas aos endpoints JSON anunciados. O sistema nao faz scraping HTML nem tenta contornar protecoes externas.
- Consulta dos addons ativos com prioridade de idioma dublado, depois legendado; preferencias de qualidade e provider controlam a ordem subsequente. FrostStream vem primeiro na consulta; os outros addons ativos funcionam como fallback.
- Criacao de `.strm` dinamicos assinados. O Jellyfin acessa `/stream/...`, o backend consulta os addons novamente e redireciona para uma URL HTTPS direta. O proxy so e usado quando `behaviorHints.proxyHeaders` foi explicitamente fornecido pelo addon; headers `Range` sao encaminhados.
- Verificacao automatica dos `.strm` dinamicos a cada 5 minutos e logo apos a criacao. O backend consulta os addons de novo, testa ate cinco URLs com uma requisicao `Range: bytes=0-0` e registra estado, HTTP e horario; nao persiste URL temporaria. A tela mostra disponivel, stream invalido, sem fonte, arquivo ausente ou erro de verificacao.
- Importacao de `.mp4`, `.mkv` e `.webm` com limite configuravel (20 GB por padrao), progresso de upload e organizacao no formato de pastas Jellyfin.
- Jobs persistentes com progresso por SSE para adicionar temporada/serie, sincronizar metadata e importar arquivos. A sincronizacao detecta arquivos existentes e nunca os substitui.
- Verificacao de arquivos locais e `.strm`, protecao SSRF para hosts de addons/streams, validacao CSRF, cookies de sessao seguros e limite de requisicoes.
- Ao criar/importar arquivos, solicita atualizacao da biblioteca Jellyfin usando uma API key mantida apenas no backend.

## Endpoints principais

- `GET /health`
- `GET /api/search?query=Mr.%20Robot&media_type=series` (requer sessao)
- `GET /api/title/series/tt4158110` (requer sessao; metadata e estado local)
- `GET /api/library/checks/series/tt4158110` (requer sessao; ultimo estado das verificacoes dos links)
- `GET /api/streams/series/tt4158110/1/1` (requer sessao)
- `POST /api/library/add/series/tt4158110/1/1` (administrador + CSRF)
- `POST /api/library/add/series/tt4158110/season/1` (job para temporada)
- `POST /api/library/add/series/tt4158110` e `POST /api/library/sync/series/tt4158110` (jobs)
- `POST /api/jobs/import/movie/{imdb_id}` / `POST /api/jobs/import/series/{imdb_id}/{season}/{episode}` e `PUT /api/jobs/{job_id}/upload` (administrador + CSRF)
- `GET /api/jobs/{job_id}` e `GET /api/jobs/{job_id}/events` (sessao do dono ou administrador)
- `GET /stream/movie/{imdb_id}` e `GET /stream/series/{imdb_id}/{season}/{episode}` (links assinados incluidos nos `.strm`)

## Validacao feita em 2026-10-07

Frontend TypeScript, imagem Docker e Compose foram validados localmente; os testes cobrem autenticacao Jellyfin, cliente de addons, links dinamicos, verificacao curta de streams, progresso de jobs, sincronizacao sem sobrescrever arquivos e importacao organizada.

No site publicado, a busca por `Mr. Robot` retornou a serie de 2015 (`tt4158110`), quatro temporadas, 45 episodios, sinopse e elenco. A consulta ao S01E01 retornou uma fonte direta FenixFlix 1080p dublada. FrostStream e BestCine nao forneceram stream utilizavel no teste mais recente; o fallback FenixFlix funcionou. O PopPlay informado pelo usuario permanece indisponivel por falha de DNS, portanto nao foi adicionado como fonte funcional.

Os testes de upload usaram diretorios temporarios e nenhum arquivo foi adicionado a biblioteca de producao durante esta verificacao. Playback final e scan Jellyfin de um novo item dependem da `JELLYFIN_API_KEY` estar preenchida corretamente no Coolify.

## Testes locais

```sh
docker compose build
docker run --rm -v "$PWD:/workspace" -w /workspace -e PYTHONPATH=/workspace -e SESSION_SECRET=test-session-secret-value-at-least-32-characters -e DATABASE_URL=sqlite:////tmp/mlm-tests.sqlite3 --entrypoint python stremio_proprio-media-library-manager -m unittest discover -s tests -v
```
