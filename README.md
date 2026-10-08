# Media Library Manager

Aplicacao web para pesquisar filmes e series, consultar streams por protocolo Stremio e organizar arquivos locais ou `.strm` para uma biblioteca Jellyfin.

## Deploy no Coolify

O repositorio inclui `Dockerfile` multi-stage e `docker-compose.yml`. No Coolify, use a raiz `/` e o arquivo `/docker-compose.yml`; o servico escuta na porta interna `8000` e oferece `GET /health`.

O Compose monta `/home/ubuntu/jellyfin/tvshows:/media`, e a biblioteca de destino fica em `/media/stream media`. O volume persistente `app_data` mantém a origem SQLite e seu backup durante a migração para PostgreSQL. A imagem base Python e Node oferece ARM64; o Coolify compila para a arquitetura do host. Para escrita, o diretorio da biblioteca precisa conceder acesso ao UID `10001` do container.

Configure no Coolify `JELLYFIN_URL`, `JELLYFIN_API_KEY` e `SESSION_SECRET` (valor aleatorio, com pelo menos 32 caracteres). Mantenha esse segredo estavel: os links dos `.strm` dinamicos dependem dele. `PUBLIC_BASE_URL` deve ser o dominio HTTPS publico do app. `COOKIE_SECURE=true` funciona atras do proxy HTTPS do Coolify. Veja `.env.example` para as demais opcoes.

Em desenvolvimento local, SQLite continua sendo o padrão. Para produção no Coolify, PostgreSQL guarda addons, preferências, biblioteca e jobs; configure `DATABASE_URL` com a URL interna do PostgreSQL (formato `postgresql://...`). Na primeira inicialização, se o arquivo SQLite persistente existir em `SQLITE_MIGRATION_URL`, o app faz uma cópia de segurança `.pre-postgresql.bak` e importa os registros em uma transação. A origem não é apagada e a migração só roda uma vez. Mantenha o volume `/data` montado até conferir a migração. Redis é opcional e usado somente como cache compartilhado de pesquisa por 2 minutos, com fallback local; configure `REDIS_URL` com a URL interna do Redis.

Para as fileiras personalizadas da página inicial (baseadas no histórico assistido do Jellyfin), tendências, lançamentos e sinopses em português brasileiro, configure `TMDB_API_TOKEN` no Coolify com o **API Read Access Token** do TMDb. O token fica somente no backend. `TMDB_LANGUAGE` usa `pt-BR` por padrão. Sem esse token, a busca continua usando Cinemeta e os catálogos dos addons, mas o TMDb não consegue fornecer as recomendações nem garantir sinopses localizadas.

## Funcionalidades

- Busca filmes e séries em Cinemeta, TMDb e catálogos dos addons que declaram `catalog.extra.search`; consulta nomes traduzidos e originais e mantém IMDb IDs para compatibilidade com addons. Sinopses de busca, títulos e episódios são localizados em `pt-BR` pelo TMDb quando configurado.
- Página inicial com fileiras reais de “Para você” (recomendações a partir de filmes e episódios assistidos no Jellyfin), “Lançamentos” para os próximos 90 dias e “Em alta”. A API key do TMDb permanece no servidor.
- Login com as credenciais do Jellyfin. O token de autenticacao nao e persistido. Apenas administradores podem mudar addons, preferencias ou arquivos da biblioteca.
- Cadastro de Manifest URLs Stremio: leitura e validacao do manifest, resources, types e idPrefixes opcionais, seguida de chamadas aos endpoints JSON anunciados. O sistema nao faz scraping HTML nem tenta contornar protecoes externas.
- Busca de catálogo segue o manifest: só consulta `catalog/{type}/{catalogId}/search=...json` quando aquele catálogo anuncia a propriedade `search` em `extra`. Catálogos sem essa declaração não são consultados por busca textual.
- Consulta dos addons ativos com prioridade de idioma dublado, depois legendado; preferencias de qualidade e provider controlam a ordem subsequente. FrostStream vem primeiro na consulta; os outros addons ativos funcionam como fallback.
- Criacao de `.strm` com a URL HTTPS direta devolvida no campo `url` pelo addon. O arquivo nao recebe uma URL do dominio do Media Library Manager. As rotas `/stream/...` continuam disponiveis para resolucao dinamica, e o proxy so usa `behaviorHints.proxyHeaders` que o proprio addon forneceu.
- Verificacao automatica a cada 5 minutos e logo apos a criacao. O backend consulta os addons de novo, testa ate cinco URLs com uma requisicao `Range: bytes=0-0` e registra estado, HTTP, horario e provider funcional. Quando encontra uma URL funcional, atualiza o `.strm` atomicamente com ela. Ao migrar um `.strm` legado do app, grava a primeira URL direta devolvida pelo addon mesmo se o teste falhar, mantendo o status como indisponivel; se nenhum addon devolver URL, conserva o conteudo existente. So atualiza arquivos gerenciados cujo conteudo ainda corresponde ao registro, preservando edicoes manuais. A tela mostra disponivel, stream invalido, sem fonte, arquivo ausente ou erro de verificacao.
- Importacao de `.mp4`, `.mkv` e `.webm` com limite configuravel (20 GB por padrao), progresso de upload e organizacao no formato de pastas Jellyfin.
- Jobs persistentes com progresso por SSE para adicionar temporada/serie, sincronizar metadata e importar arquivos. A sincronizacao detecta arquivos existentes e nunca os substitui.
- Verificacao de arquivos locais e `.strm`, protecao SSRF para hosts de addons/streams, validacao CSRF, cookies de sessao seguros e limite de requisicoes.
- Ao criar/importar arquivos, solicita atualizacao da biblioteca Jellyfin usando uma API key mantida apenas no backend.
- Streams HTTPS diretos continuam gerando `.strm`. Streams Stremio com `infoHash` e `fileIdx` podem ser baixados pelo administrador tanto para filmes quanto para episódios. O addon é consultado novamente antes de aceitar o job; o torrent não vira URL de site nem arquivo `.strm`.
- O download torrent usa `aria2` no container, pasta de staging oculta e limite rígido de 20 GiB (o valor de `MAX_TORRENT_BYTES` pode reduzir o limite, nunca aumentá-lo). O `videoSize` informado pelo addon é conferido antes do job; se não vier, o tamanho real é monitorado durante a transferência e o download é interrompido ao passar do limite. Opções acima do limite ficam desabilitadas na interface. O aria2 encerra o seeding assim que termina e desiste depois de 30 minutos sem velocidade de download.
- Filmes e episódios recebem a estrutura/nome Jellyfin e são registrados no banco configurado como temporários. A aplicação remove o arquivo após oito horas do download concluído; se o Jellyfin ainda estiver reproduzindo o arquivo, aguarda a sessão terminar. Só remove um arquivo de vídeo dentro da biblioteca que também esteja associado ao registro temporário; em seguida solicita novo scan Jellyfin. O monitor depende da API key Jellyfin configurada.
- O container não usa shell para executar dados do addon, não aceita trackers fornecidos pelo addon e limita a gravação ao staging. Use apenas torrents cujo conteúdo você tenha direito de baixar. Conteúdo ou resposta não é verificado automaticamente como domínio público.

### Brazuca Torrents

Manifest lido em 2026-10-07; resposta HTTP 200:

```json
{
  "id": "com.stremio.brazuca.addon",
  "version": "0.1.1",
  "name": "Brazuca Torrents",
  "catalogs": [],
  "resources": [{"name": "stream", "types": ["movie", "series", "anime"], "idPrefixes": ["tt", "kitsu"]}],
  "types": ["movie", "series", "anime", "other"],
  "behaviorHints": {"configurable": true, "configurationRequired": false, "p2p": true}
}
```

Decisão: esse manifest oferece somente streams P2P, não metadados nem busca de catálogo. O protocolo Stremio documenta `infoHash`, `fileIdx` e `videoSize` como propriedades de streams torrent; a integração consulta somente a rota `stream` anunciada e passa o hash/índice ao aria2 sem tratá-los como URL direta. Em 2026-10-07, a requisição padrão `GET /stream/movie/tt0063350.json` respondeu HTTP 200 com este JSON:

```json
{"streams":[],"cacheMaxAge":60,"staleRevalidate":14400,"staleError":604800}
```

A resposta veio marcada `HIT` e `STALE` pelo cache do addon, então nenhum objeto torrent real pôde ser validado naquela tentativa. Na revisão de 2026-10-07, tanto o manifest quanto a rota de teste responderam HTTP 403 neste ambiente; não foi feito bypass. A integração aceita opções quando o endpoint retornar objetos `infoHash` válidos e reconsulta o addon ao iniciar cada job.

## Endpoints principais

- `GET /health`
- `GET /api/search?query=Mr.%20Robot&media_type=series` (requer sessao)
- `GET /api/home/catalog` (requer sessao; recomendações, tendências e lançamentos)
- `GET /api/title/series/tt4158110` (requer sessao; metadata e estado local)
- `GET /api/library/checks/series/tt4158110` (requer sessao; ultimo estado das verificacoes dos links)
- `GET /api/streams/series/tt4158110/1/1` (requer sessao)
- `POST /api/library/add/series/tt4158110/1/1` (administrador + CSRF)
- `POST /api/library/add/series/tt4158110/season/1` (job para temporada)
- `POST /api/jobs/download/movie/{imdb_id}` com `{ "provider_id": "...", "info_hash": "...", "file_idx": 0 }` (administrador + CSRF; job e download temporário)
- `POST /api/jobs/download/series/{imdb_id}/{season}/{episode}` com `{ "provider_id": "...", "info_hash": "...", "file_idx": 0 }` (administrador + CSRF; episódio baixado temporariamente)
- `POST /api/library/add/series/tt4158110` e `POST /api/library/sync/series/tt4158110` (jobs)
- `POST /api/jobs/import/movie/{imdb_id}` / `POST /api/jobs/import/series/{imdb_id}/{season}/{episode}` e `PUT /api/jobs/{job_id}/upload` (administrador + CSRF)
- `GET /api/jobs/{job_id}` e `GET /api/jobs/{job_id}/events` (sessao do dono ou administrador)
- `GET /stream/movie/{imdb_id}` e `GET /stream/series/{imdb_id}/{season}/{episode}` (rotas internas assinadas, mantidas para compatibilidade)

## Validacao feita em 2026-10-07

Frontend TypeScript, imagem Docker e Compose foram validados localmente; os testes cobrem autenticacao Jellyfin, cliente de addons, links dinamicos, verificacao curta de streams, progresso de jobs, sincronizacao sem sobrescrever arquivos, importacao organizada, parsing de torrents e chamada segura do aria2.

No site publicado, a busca por `Mr. Robot` retornou a serie de 2015 (`tt4158110`), quatro temporadas, 45 episodios, sinopse e elenco. A consulta ao S01E01 retornou uma fonte direta FenixFlix 1080p dublada. FrostStream e BestCine nao forneceram stream utilizavel no teste mais recente; o fallback FenixFlix funcionou. O PopPlay informado pelo usuario permanece indisponivel por falha de DNS, portanto nao foi adicionado como fonte funcional.

O checker ativo no Coolify identificou 10/10 arquivos da temporada 1 existentes e testou os links: 3 responderam e 7 falharam com HTTP 400/404/408. O provider que passou e usado como preferencia na proxima resolucao dinâmica, que consulta o addon novamente para gerar URL atual.

`refresh_library()` foi executado no container de producao sem imprimir a chave; Jellyfin aceitou `POST /Library/Refresh` e a solicitacao de scan foi confirmada. O teste de criacao e verificacao de `.strm` usa armazenamento temporario, sem novo arquivo criado na biblioteca de producao. Playback integral de um novo item ainda nao foi validado.

## Testes locais

```sh
docker compose build
docker run --rm -v "$PWD:/workspace" -w /workspace -e PYTHONPATH=/workspace -e SESSION_SECRET=test-session-secret-value-at-least-32-characters -e DATABASE_URL=sqlite:////tmp/mlm-tests.sqlite3 --entrypoint python stremio_proprio-media-library-manager -m unittest discover -s tests -v
```
