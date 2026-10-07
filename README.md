# Media Library Manager

Aplicação FastAPI + React para pesquisar metadata, consultar addons Stremio e organizar arquivos `.strm` para Jellyfin.

## Coolify

Use o Docker Compose do repositório e publique a porta interna `8000`. Mantenha o volume persistente `app_data` e configure no Coolify `JELLYFIN_URL`, `JELLYFIN_API_KEY` e `SESSION_SECRET` (segredo aleatório com pelo menos 32 caracteres). `COOKIE_SECURE=true` deve permanecer ligado atrás de HTTPS. O healthcheck é `GET /health`.

O Compose monta `/home/ubuntu/jellyfin/tvshows` em `/media`; os `.strm` são organizados em `/media/stream media`. O Compose não fixa arquitetura: o Coolify constrói para o host de destino.

## Implementado

- Busca de filmes e séries no Cinemeta, com IMDb IDs, poster, ano e sinopse.
- Detalhes com elenco, temporadas e episódios.
- Cliente Stremio que lê e valida o manifest antes de chamar `/stream/{type}/{videoID}.json`; respeita recursos, tipos e prefixos anunciados.
- FrostStream primeiro e BestCine como fallback quando o primeiro não retorna URL HTTPS direta. Respostas P2P sem URL direta não viram `.strm`.
- Login validado pelas contas e senhas do Jellyfin. A senha nao e armazenada; o token da resposta de autenticacao e descartado. Apenas administradores Jellyfin podem adicionar arquivos ou alterar addons.
- Criação de `.strm` sem sobrescrever arquivo existente, com gravação sob `/media/stream media`.
- SQLite persistente e imagem Docker multi-stage.
- Tela administrativa para cadastrar manifests, inspecionar recursos/tipos/prefixos e ativar/desativar addons.
- Preferencias persistentes de provider e qualidade; consulta tenta o provider escolhido, depois FrostStream e os demais addons ativos.
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
