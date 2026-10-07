# Media Library Manager

Aplicação FastAPI + React para pesquisar metadata, consultar addons Stremio e organizar arquivos `.strm` para Jellyfin.

## Coolify

Use o Docker Compose do repositório e publique a porta interna `8000`. Mantenha o volume persistente `app_data` e configure no Coolify `JELLYFIN_URL`, `JELLYFIN_API_KEY` e `SESSION_SECRET` (segredo aleatório com pelo menos 32 caracteres). `COOKIE_SECURE=true` deve permanecer ligado atrás de HTTPS. O healthcheck é `GET /health`.

O Compose monta `/home/ubuntu/jellyfin/tvshows` em `/media`; os `.strm` são organizados em `/media/stream media`. A aplicação atende HTTP na porta interna `8000`. Para Cloudflare Tunnel, o host publica essa porta apenas em `127.0.0.1:8001`; configure a rota Tunnel para `http://localhost:8001`. O Compose não fixa arquitetura: o Coolify constrói para o host de destino.

## Implementado

- Busca de filmes e séries no Cinemeta, com IMDb IDs, poster, ano e sinopse.
- Detalhes com elenco, temporadas e episódios.
- Cliente Stremio que lê e valida o manifest antes de chamar `/stream/{type}/{videoID}.json`; respeita recursos, tipos e prefixos anunciados.
- FrostStream primeiro e BestCine como fallback quando o primeiro não retorna URL HTTPS direta. Respostas P2P sem URL direta não viram `.strm`.
- Login validado pelas contas e senhas do Jellyfin. A senha nao e armazenada; o token da resposta de autenticacao e descartado. Apenas administradores Jellyfin podem adicionar arquivos ou alterar addons.
- Criação de `.strm` sem sobrescrever arquivo existente, com gravação sob `/media/stream media`.
- SQLite persistente e imagem Docker multi-stage.
- Tela administrativa para cadastrar manifests, inspecionar recursos/tipos/prefixos e ativar/desativar addons.
- Prefer?ncias persistentes de provider e qualidade; a ordena??o prioriza dublado globalmente, depois legendado, qualidade preferida e provider.
- Status local por filme/episodio detecta `.strm` e videos `.mp4`, `.mkv`, `.webm`; arquivos existentes sao preservados ao adicionar.
- Apos criar um novo `.strm`, solicita scan pelo endpoint Jellyfin `POST /Library/Refresh`; `JELLYFIN_API_KEY` fica somente no backend.

## API principal

- `GET /health`
- `GET /api/search?query=Mr.%20Robot&media_type=series`
- `GET /api/title/series/tt4158110` (requer sessao; inclui status local da biblioteca)
- `GET /api/streams/series/tt4158110/1/1` (requer sessão)
- `POST /api/library/add/series/tt4158110/1/1` (requer sessao de administrador, CSRF e `JELLYFIN_API_KEY` para solicitar scan)
- `GET/POST/PATCH /api/addons` (requer sessao; alteracoes exigem sessao de administrador e CSRF)
- `GET/PATCH /api/preferences` (requer sessao; alteracoes exigem administrador e CSRF)
- Testes locais: `python -m unittest discover -s tests -v` (executar em ambiente com dependencias instaladas).

## Validação e limite atual

O escritor `.strm` foi testado isoladamente. O host Oracle confirmou ACL de escrita para UID 10001; a consulta de streams FrostStream retornou HTTP 403 tambem no host. Nao se usa User-Agent arbitrario nem se contornam bloqueios. O deploy, teste real de login Jellyfin e teste real de scan ainda dependem das variaveis configuradas no Coolify.


### Verifica??o dos addons (2026-10-07)

Teste feito pelo protocolo JSON Stremio para Mr. Robot S01E01 (`tt4158110:1:1`). FrostStream (`2.2.8`), BestCine (`13.0.0`) e FenixFlix (`1.2.0`) tiveram manifest v?lido e responderam ao endpoint `/stream/series/{videoID}.json` via cliente PowerShell. Os testes retornaram 5, 20 e 1 resultado, respectivamente. Amostras JSON (URLs omitidas):

```json
{"streams":[{"name":"FrostStream 1080p","title":"... Legendado"}]}
{"streams":[{"name":"BestCine 720p","title":"... Dublado"}]}
{"streams":[{"name":"FenixFlix 1080p","title":"... Dublado"}]}
```

No teste feito dentro da imagem Docker local (mesmo cliente HTTP usado pelo backend), FrostStream e BestCine responderam HTTP 403; FenixFlix respondeu HTTP 2xx e entregou 1 stream marcado como dublado. Nenhum User-Agent foi mascarado nem houve tentativa de contornar bloqueios. Isso confirma que FenixFlix est? tecnicamente consult?vel neste ambiente; a reprodu??o pelo Jellyfin ainda depende de validar a URL e do acesso no servidor Coolify. PopPlay n?o chegou ao manifest: o hostname comunit?rio testado (`site--popplay--rg2h4m5nr425.code.run`) falhou na resolu??o DNS. Ele pode ser cadastrado em Configura??es quando houver um Manifest URL acess?vel.
