import { FormEvent, useEffect, useRef, useState } from "react";

type MediaType = "movie" | "series";
type SearchFilter = "all" | MediaType;

type SearchResult = {
  id: string;
  media_type: MediaType;
  tmdb_id: number | null;
  imdb_id: string | null;
  title: string;
  year: number | null;
  poster_url: string | null;
  overview: string;
};

type TitleDetails = SearchResult & {
  cast: { name: string; character: string; profile_url: string | null }[];
  genres: string[];
  tagline: string;
  season_count?: number;
  episode_count?: number;
  seasons?: { season_number: number; name: string; episode_count: number; air_date: string | null }[];
  episodes: { season_number: number; episode_number: number; title: string; overview: string; released: string | null }[];
  library_status?: { status?: "media" | "strm" | "missing"; path?: string | null; episodes?: { season_number: number; episode_number: number; status: "media" | "strm" | "missing"; path: string | null }[] };
};

type StreamOption = { name: string; title: string; provider: string; quality: string | null; language: "dubbed" | "subtitled" | "portuguese_unspecified" | "unknown"; url: string };
type AddonResource = string | { name?: string; types?: string[]; idPrefixes?: string[] };
type AddonItem = { id: string; name: string; manifest_url: string; enabled: boolean; manifest?: { resources?: AddonResource[]; types?: string[]; idPrefixes?: string[] } | null };
type Preferences = { preferred_quality: string; preferred_provider: string };
type JobSnapshot = { id: string; kind: string; status: "queued" | "uploading" | "running" | "completed" | "failed"; total: number; completed: number; failed: number; message: string; result: Record<string, unknown> };

function manifestTypes(manifest: AddonItem["manifest"]) {
  return Array.from(new Set([...(manifest?.types || []), ...(manifest?.resources || []).flatMap((resource) => typeof resource === "string" ? [] : resource.types || [])]));
}

function manifestPrefixes(manifest: AddonItem["manifest"]) {
  return Array.from(new Set([...(manifest?.idPrefixes || []), ...(manifest?.resources || []).flatMap((resource) => typeof resource === "string" ? [] : resource.idPrefixes || [])]));
}

function streamLanguageLabel(language: StreamOption["language"]) {
  return ({ dubbed: "Dublado", subtitled: "Legendado", portuguese_unspecified: "Português · tipo não informado", unknown: "Idioma não informado" })[language];
}

const FILTERS: { value: SearchFilter; label: string }[] = [
  { value: "all", label: "Tudo" },
  { value: "series", label: "Séries" },
  { value: "movie", label: "Filmes" },
];

export default function App() {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<SearchFilter>("all");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [searched, setSearched] = useState(false);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<SearchResult | null>(null);
  const [details, setDetails] = useState<TitleDetails | null>(null);
  const [detailsLoading, setDetailsLoading] = useState(false);
  const [detailsError, setDetailsError] = useState("");
  const [openSeason, setOpenSeason] = useState<number | null>(null);
  const [authState, setAuthState] = useState<"checking" | "login" | "ready">("checking");
  const [password, setPassword] = useState("");
  const [username, setUsername] = useState("");
  const [csrfToken, setCsrfToken] = useState("");
  const [isAdmin, setIsAdmin] = useState(false);
  const [currentUser, setCurrentUser] = useState("");
  const [view, setView] = useState<"library" | "settings">("library");
  const [addons, setAddons] = useState<AddonItem[]>([]);
  const [preferences, setPreferences] = useState<Preferences>({ preferred_quality: "1080p", preferred_provider: "automatic" });
  const [addonName, setAddonName] = useState("");
  const [manifestUrl, setManifestUrl] = useState("");
  const [settingsLoading, setSettingsLoading] = useState(false);
  const [settingsError, setSettingsError] = useState("");
  const [settingsMessage, setSettingsMessage] = useState("");
  const [authError, setAuthError] = useState("");
  const [streamState, setStreamState] = useState<{ key: string; loading: boolean; streams: StreamOption[]; error: string; saved: string }>({ key: "", loading: false, streams: [], error: "", saved: "" });
  const [jobState, setJobState] = useState<JobSnapshot | null>(null);
  const jobEvents = useRef<EventSource | null>(null);

  useEffect(() => {
    fetch("/api/session").then(async (response) => {
      if (!response.ok) throw new Error("login");
      const data = await response.json();
      setCsrfToken(data.csrf_token || "");
      setCurrentUser(data.user?.name || "");
      setIsAdmin(Boolean(data.user?.is_admin));
      setAuthState("ready");
    }).catch(() => setAuthState("login"));
  }, []);

  useEffect(() => () => jobEvents.current?.close(), []);

  async function handleLogin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setAuthError("");
    try {
      const response = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, password }) });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Não foi possível entrar.");
      setCsrfToken(data.csrf_token);
      setCurrentUser(data.user?.name || username);
      setIsAdmin(Boolean(data.user?.is_admin));
      setPassword("");
      setAuthState("ready");
    } catch (cause) {
      setAuthError(cause instanceof Error ? cause.message : "Não foi possível entrar.");
    }
  }

  async function handleLogout() {
    try {
      await fetch("/api/logout", { method: "POST", headers: { "X-CSRF-Token": csrfToken } });
    } finally {
      setCsrfToken("");
      setCurrentUser("");
      setIsAdmin(false);
      setView("library");
      setAuthState("login");
    }
  }

  async function loadSettings() {
    setSettingsLoading(true);
    setSettingsError("");
    try {
      const [addonResponse, preferenceResponse] = await Promise.all([fetch("/api/addons"), fetch("/api/preferences")]);
      const addonData = await addonResponse.json();
      const preferenceData = await preferenceResponse.json();
      if (!addonResponse.ok) throw new Error(addonData.detail || "Nao foi possivel carregar os addons.");
      if (!preferenceResponse.ok) throw new Error(preferenceData.detail || "Nao foi possivel carregar as preferencias.");
      setAddons(addonData.addons);
      setPreferences(preferenceData);
    } catch (cause) {
      setSettingsError(cause instanceof Error ? cause.message : "Falha ao carregar configuracoes.");
    } finally {
      setSettingsLoading(false);
    }
  }

  async function openSettings() {
    setView("settings");
    setSettingsMessage("");
    await loadSettings();
  }

  async function handleAddonSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSettingsError("");
    setSettingsMessage("");
    try {
      const response = await fetch("/api/addons", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({ name: addonName, manifest_url: manifestUrl }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Nao foi possivel cadastrar o manifest.");
      setAddonName("");
      setManifestUrl("");
      setSettingsMessage("Manifest validado e addon cadastrado.");
      await loadSettings();
    } catch (cause) {
      setSettingsError(cause instanceof Error ? cause.message : "Falha ao cadastrar addon.");
    }
  }

  async function toggleAddon(addon: AddonItem) {
    setSettingsError("");
    try {
      const response = await fetch(`/api/addons/${encodeURIComponent(addon.id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({ enabled: !addon.enabled }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Nao foi possivel atualizar o addon.");
      setAddons((current) => current.map((item) => item.id === addon.id ? { ...item, enabled: data.enabled } : item));
    } catch (cause) {
      setSettingsError(cause instanceof Error ? cause.message : "Falha ao atualizar addon.");
    }
  }

  async function savePreferences(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSettingsError("");
    setSettingsMessage("");
    try {
      const response = await fetch("/api/preferences", {
        method: "PATCH",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify(preferences),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Nao foi possivel salvar as preferencias.");
      setPreferences(data);
      setSettingsMessage("Preferencias salvas.");
    } catch (cause) {
      setSettingsError(cause instanceof Error ? cause.message : "Falha ao salvar preferencias.");
    }
  }


  async function loadEpisodeStreams(episode: TitleDetails["episodes"][number]) {
    const key = `${episode.season_number}:${episode.episode_number}`;
    setStreamState({ key, loading: true, streams: [], error: "", saved: "" });
    try {
      const response = await fetch(`/api/streams/series/${details?.imdb_id}/${episode.season_number}/${episode.episode_number}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Falha ao consultar os addons.");
      const addonErrors = (data.addon_errors || []).map((item: { provider: string; detail: string }) => `${item.provider}: ${item.detail}`).join(" · ");
      setStreamState({ key, loading: false, streams: data.streams, error: data.streams.length ? "" : addonErrors || "Nenhuma URL HTTPS direta foi retornada pelos addons.", saved: "" });
    } catch (cause) {
      setStreamState({ key, loading: false, streams: [], error: cause instanceof Error ? cause.message : "Falha ao consultar os addons.", saved: "" });
    }
  }

  async function loadMovieStreams() {
    setStreamState({ key: "movie", loading: true, streams: [], error: "", saved: "" });
    try {
      const response = await fetch(`/api/streams/movie/${details?.imdb_id}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Falha ao consultar os addons.");
      const addonErrors = (data.addon_errors || []).map((item: { provider: string; detail: string }) => `${item.provider}: ${item.detail}`).join(" · ");
      setStreamState({ key: "movie", loading: false, streams: data.streams, error: data.streams.length ? "" : addonErrors || "Nenhuma URL HTTPS direta foi retornada pelos addons.", saved: "" });
    } catch (cause) {
      setStreamState({ key: "movie", loading: false, streams: [], error: cause instanceof Error ? cause.message : "Falha ao consultar os addons.", saved: "" });
    }
  }

  async function addMovieStream(stream: StreamOption) {
    try {
      const response = await fetch(`/api/library/add/movie/${details?.imdb_id}`, {
        method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken }, body: JSON.stringify({ url: stream.url }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Nao foi possivel adicionar o filme.");
      setStreamState((current) => ({ ...current, key: "movie", saved: data.existing ? data.message || "Arquivo existente mantido." : data.message || "Arquivo .strm criado." }));
    } catch (cause) {
      setStreamState((current) => ({ ...current, key: "movie", error: cause instanceof Error ? cause.message : "Falha ao adicionar filme." }));
    }
  }

  async function addEpisodeStream(episode: TitleDetails["episodes"][number], stream: StreamOption) {
    const key = `${episode.season_number}:${episode.episode_number}`;
    try {
      const response = await fetch(`/api/library/add/series/${details?.imdb_id}/${episode.season_number}/${episode.episode_number}`, {
        method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken }, body: JSON.stringify({ url: stream.url }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Não foi possível adicionar o episódio.");
      setStreamState((current) => ({ ...current, key, saved: data.existing ? "Arquivo já existia; mantido sem alterações." : "Arquivo .strm criado." }));
    } catch (cause) {
      setStreamState((current) => ({ ...current, key, error: cause instanceof Error ? cause.message : "Não foi possível adicionar o episódio." }));
    }
  }

  async function refreshSelectedDetails() {
    if (!selected?.imdb_id) return;
    const response = await fetch(`/api/title/${selected.media_type}/${selected.imdb_id}`);
    const payload = await response.json();
    if (response.ok) setDetails(payload);
  }

  function watchJob(jobId: string) {
    jobEvents.current?.close();
    const events = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/events`);
    jobEvents.current = events;
    events.onmessage = (message) => {
      const job = JSON.parse(message.data) as JobSnapshot;
      setJobState(job);
      if (job.status === "completed" || job.status === "failed") {
        events.close();
        jobEvents.current = null;
        void refreshSelectedDetails();
      }
    };
    events.onerror = () => {
      setJobState((current) => current?.id === jobId ? { ...current, message: "Reconectando ao progresso do job..." } : current);
    };
  }

  async function startJob(endpoint: string) {
    setJobState(null);
    try {
      const response = await fetch(endpoint, { method: "POST", headers: { "X-CSRF-Token": csrfToken } });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Nao foi possivel iniciar o job.");
      setJobState({ id: data.job_id, kind: "library", status: "queued", total: data.total || 0, completed: 0, failed: 0, message: "Job iniciado.", result: {} });
      watchJob(data.job_id);
    } catch (cause) {
      setJobState({ id: "error", kind: "library", status: "failed", total: 0, completed: 0, failed: 1, message: cause instanceof Error ? cause.message : "Falha ao iniciar job.", result: {} });
    }
  }

  async function startImport(endpoint: string, file: File) {
    setJobState(null);
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({ filename: file.name, size: file.size }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Nao foi possivel iniciar a importacao.");
      setJobState({ id: data.job_id, kind: "import", status: "uploading", total: file.size, completed: 0, failed: 0, message: "Enviando arquivo...", result: {} });
      watchJob(data.job_id);
      const formData = new FormData();
      formData.append("file", file);
      const upload = new XMLHttpRequest();
      upload.open("PUT", data.upload_url);
      upload.withCredentials = true;
      upload.setRequestHeader("X-CSRF-Token", csrfToken);
      upload.upload.onprogress = (event) => {
        if (event.lengthComputable) setJobState((current) => {
          if (!current || current.id !== data.job_id) return current;
          return { ...current, total: event.total, completed: event.loaded, message: `Enviando arquivo: ${Math.floor((event.loaded / event.total) * 100)}%.` };
        });
      };
      upload.onerror = () => setJobState((current) => {
        if (!current || current.id !== data.job_id) return current;
        return { ...current, status: "failed", failed: 1, message: "A conexao caiu durante o upload." };
      });
      upload.onload = () => {
        if (upload.status < 200 || upload.status >= 300) {
          let detail = "Falha ao enviar arquivo.";
          try { detail = JSON.parse(upload.responseText).detail || detail; } catch { /* response may be empty */ }
          setJobState((current) => {
            if (!current || current.id !== data.job_id) return current;
            return { ...current, status: "failed", failed: 1, message: detail };
          });
        }
      };
      upload.send(formData);
    } catch (cause) {
      setJobState({ id: "error", kind: "import", status: "failed", total: 0, completed: 0, failed: 1, message: cause instanceof Error ? cause.message : "Falha ao iniciar importacao.", result: {} });
    }
  }

  if (authState === "checking") return <main className="auth-shell">Verificando sessão...</main>;
  if (authState === "login") return <main className="auth-shell"><form className="auth-card" onSubmit={handleLogin}><span className="eyebrow">MEDIA LIBRARY MANAGER</span><h1>Acesse sua biblioteca</h1><label htmlFor="jellyfin-username">Usuário Jellyfin</label><input id="jellyfin-username" type="text" autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} required /><label htmlFor="jellyfin-password">Senha Jellyfin</label><input id="jellyfin-password" type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required /><button type="submit">Entrar</button>{authError && <p className="error-message">{authError}</p>}</form></main>;

  async function handleSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const value = query.trim();
    if (!value) return;

    setLoading(true);
    setSearched(true);
    setError("");
    try {
      const params = new URLSearchParams({ query: value, media_type: filter });
      const response = await fetch(`/api/search?${params}`);
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "Falha na pesquisa.");
      setResults(payload.results);
    } catch (cause) {
      setResults([]);
      setError(cause instanceof Error ? cause.message : "Falha na pesquisa.");
    } finally {
      setLoading(false);
    }
  }

  async function openDetails(item: SearchResult) {
    setSelected(item);
    setDetails(null);
    setDetailsError("");
    setDetailsLoading(true);
    try {
      const response = await fetch(`/api/title/${item.media_type}/${item.imdb_id}`);
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "Não foi possível carregar os detalhes.");
      setDetails(payload);
    } catch (cause) {
      setDetailsError(cause instanceof Error ? cause.message : "Não foi possível carregar os detalhes.");
    } finally {
      setDetailsLoading(false);
    }
  }

  return (
    <main className="page-shell">
      <header className="topbar">
        <a className="brand" href="/" aria-label="Media Library Manager início">
          <span className="brand-mark">M</span>
          <span>Media Library <strong>Manager</strong></span>
        </a>
        <div className="topbar-actions">
          {currentUser && <span className="topbar-label">{currentUser}</span>}
          {isAdmin && <button className="nav-button" type="button" onClick={() => view === "settings" ? setView("library") : void openSettings()}>{view === "settings" ? "Biblioteca" : "Configuracoes"}</button>}
          {currentUser && <button className="nav-button" type="button" onClick={() => void handleLogout()}>Sair</button>}
        </div>
      </header>

      {view === "settings" ? (
        <section className="settings-page">
          <div className="settings-heading"><div><span className="eyebrow">ADMINISTRACAO</span><h1>Configuracoes</h1><p>Gerencie addons Stremio e preferencias de streams.</p></div><button className="filter-chip" type="button" onClick={() => void loadSettings()}>Atualizar</button></div>
          {settingsError && <div className="message error-message">{settingsError}</div>}
          {settingsMessage && <div className="message success-message">{settingsMessage}</div>}
          {settingsLoading ? <div className="message">Carregando configuracoes...</div> : <div className="settings-grid">
            <section className="settings-card">
              <span className="eyebrow">STREMIO ADDONS</span><h2>Addons cadastrados</h2>
              {addons.map((addon) => <article className="addon-card" key={addon.id}>
                <div className="addon-card-head"><div><strong>{addon.name}</strong><small>{addon.id}</small></div><button className={`filter-chip ${addon.enabled ? "active" : ""}`} type="button" onClick={() => void toggleAddon(addon)}>{addon.enabled ? "Ativo" : "Desativado"}</button></div>
                <a href={addon.manifest_url} target="_blank" rel="noreferrer">{addon.manifest_url}</a>
                {addon.manifest && <div className="manifest-facts"><span>Resources: {(addon.manifest.resources || []).map((resource) => typeof resource === "string" ? resource : resource.name || "resource").join(", ") || "-"}</span><span>Types: {manifestTypes(addon.manifest).join(", ") || "-"}</span><span>ID prefixes: {manifestPrefixes(addon.manifest).join(", ") || "all"}</span></div>}
              </article>)}
              <form className="settings-form" onSubmit={handleAddonSubmit}><h3>Adicionar addon</h3><label>Nome<input value={addonName} onChange={(event) => setAddonName(event.target.value)} maxLength={120} required /></label><label>Manifest URL<input type="url" value={manifestUrl} onChange={(event) => setManifestUrl(event.target.value)} placeholder="https://addon.example/manifest.json" required /></label><button type="submit" disabled={!addonName.trim() || !manifestUrl.trim()}>Validar e cadastrar</button></form>
            </section>
            <section className="settings-card"><span className="eyebrow">REPRODUCAO</span><h2>Preferencias de stream</h2><form className="settings-form" onSubmit={savePreferences}><label>Qualidade preferida<select value={preferences.preferred_quality} onChange={(event) => setPreferences((current) => ({ ...current, preferred_quality: event.target.value }))}>{["2160p", "1440p", "1080p", "720p", "480p"].map((quality) => <option key={quality}>{quality}</option>)}</select></label><label>Provider preferido<select value={preferences.preferred_provider} onChange={(event) => setPreferences((current) => ({ ...current, preferred_provider: event.target.value }))}><option value="automatic">Automatico (dublado primeiro)</option>{addons.filter((addon) => addon.enabled).map((addon) => <option value={addon.id} key={addon.id}>{addon.name}</option>)}</select></label><p>Dublado sempre vem primeiro; provider e qualidade definem a ordem entre opcoes do mesmo idioma.</p><button type="submit">Salvar preferencias</button></form></section>
          </div>}
        </section>
      ) : <>
      <section className="hero">
        <div className="eyebrow"><span className="eyebrow-line" /> BIBLIOTECA PESSOAL</div>
        <h1>Encontre o que<br /><span>você quer assistir.</span></h1>
        <p>Pesquise filmes e séries para organizar sua biblioteca Jellyfin.</p>
        <form className="search-form" onSubmit={handleSearch}>
          <span className="search-icon" aria-hidden="true">⌕</span>
          <input
            aria-label="Pesquisar filmes e séries"
            placeholder="Pesquisar filmes e séries..."
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <button type="submit" disabled={loading || !query.trim()}>
            {loading ? "Buscando..." : "Pesquisar"}
          </button>
        </form>
        <div className="filter-row" role="group" aria-label="Tipo de conteúdo">
          {FILTERS.map((option) => (
            <button
              className={`filter-chip ${filter === option.value ? "active" : ""}`}
              key={option.value}
              onClick={() => setFilter(option.value)}
              type="button"
            >
              {option.label}
            </button>
          ))}
        </div>
        <div className="search-hint">Experimente <button onClick={() => setQuery("Mr. Robot")}>Mr. Robot</button> ou <button onClick={() => setQuery("The Office")}>The Office</button></div>
      </section>

      <section className="results-section" aria-live="polite">
        {error && <div className="message error-message">{error}</div>}
        {!error && searched && !loading && results.length === 0 && (
          <div className="message">Nenhum resultado encontrado. Tente outro título.</div>
        )}
        {results.length > 0 && (
          <>
            <div className="section-heading">
              <div><span className="eyebrow">RESULTADOS</span><h2>{results.length} títulos encontrados</h2></div>
              <span className="result-count">{results.length} itens</span>
            </div>
            <div className="results-grid">
              {results.map((item) => (
                <article className="media-card" key={item.id}>
                  <div className="poster-wrap">
                    {item.poster_url ? (
                      <img src={item.poster_url} alt={`Poster de ${item.title}`} loading="lazy" />
                    ) : (
                      <div className="poster-placeholder"><span>ML</span></div>
                    )}
                    <span className="media-type">{item.media_type === "series" ? "SÉRIE" : "FILME"}</span>
                  </div>
                  <div className="card-copy">
                    <div className="card-title-row"><h3>{item.title}</h3>{item.year && <span className="year">{item.year}</span>}</div>
                    <p>{item.overview || "Sinopse indisponível."}</p>
                    <div className="card-footer">
                      <span>IMDb <b>{item.imdb_id}</b></span>
                      <button className="details-button" onClick={() => openDetails(item)} type="button">Ver elenco e detalhes <span aria-hidden="true">↗</span></button>
                    </div>
                  </div>
                </article>
              ))}
            </div>
          </>
        )}
      </section>

      </>}

      <footer className="footer"><span>MEDIA LIBRARY MANAGER</span><span>METADATA POR TMDb</span></footer>

      {selected && (
        <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && setSelected(null)}>
          <section className="detail-modal" role="dialog" aria-modal="true" aria-label={`Detalhes de ${selected.title}`}>
            <button className="modal-close" type="button" aria-label="Fechar detalhes" onClick={() => setSelected(null)}>×</button>
            {detailsLoading && <div className="modal-message">Carregando detalhes...</div>}
            {detailsError && <div className="modal-message error-message">{detailsError}</div>}
            {details && (
              <>
                <div className="detail-header">
                  <div className="detail-poster">
                    {details.poster_url && <img src={details.poster_url} alt={`Poster de ${details.title}`} />}
                  </div>
                  <div className="detail-heading">
                    <span className="eyebrow">{details.media_type === "series" ? "SÉRIE" : "FILME"}{details.year ? ` · ${details.year}` : ""}</span>
                    <h2>{details.title}</h2>
                    {details.tagline && <p className="tagline">{details.tagline}</p>}
                    {details.genres.length > 0 && <div className="genre-list">{details.genres.map((genre) => <span key={genre}>{genre}</span>)}</div>}
                    {details.imdb_id && <span className="imdb-id">IMDb {details.imdb_id}</span>}
                  </div>
                </div>
                <div className="detail-body">
                  {jobState && <div className={`message job-progress ${jobState.status === "failed" ? "error-message" : ""}`} aria-live="polite"><div><strong>{jobState.message}</strong><span>{jobState.total ? `${jobState.completed.toLocaleString()} / ${jobState.total.toLocaleString()}` : jobState.status}</span></div>{jobState.total > 0 && <progress max={jobState.total} value={Math.min(jobState.completed, jobState.total)} />}</div>}
                  <div className="detail-block"><h3>Sinopse</h3><p>{details.overview || "Sinopse indisponível para este título."}</p></div>
                  {details.media_type === "movie" && <div className="detail-block movie-add-block"><h3>Biblioteca</h3><p>{details.library_status?.status === "media" ? "Arquivo de video encontrado." : details.library_status?.status === "strm" ? "Arquivo STRM encontrado." : "Ainda nao adicionado."}</p>{details.library_status?.path && <small>{details.library_status.path}</small>}<button className="episode-action" type="button" onClick={() => void loadMovieStreams()}>Consultar addons</button>{isAdmin && <label className="episode-action upload-action">Importar arquivo local<input type="file" accept=".mp4,.mkv,.webm,video/mp4,video/x-matroska,video/webm" onChange={(event) => { const file = event.currentTarget.files?.[0]; event.currentTarget.value = ""; if (file && details.imdb_id) void startImport(`/api/jobs/import/movie/${details.imdb_id}`, file); }} /></label>}{streamState.key === "movie" && <div className="stream-results">{streamState.loading && <p>Consultando addons...</p>}{streamState.error && <p className="error-message">{streamState.error}</p>}{streamState.saved && <p className="success-message">{streamState.saved}</p>}{streamState.streams.map((stream) => <div className="stream-option" key={`${stream.provider}-${stream.url}`}><div><strong>{stream.provider} · {streamLanguageLabel(stream.language)}</strong><span>{stream.quality || stream.name}</span><small>{stream.title}</small></div>{isAdmin && <button type="button" onClick={() => void addMovieStream(stream)}>Adicionar</button>}</div>)}</div>}</div>}
                  {details.media_type === "series" && details.seasons && (
                    <div className="detail-block season-summary">
                      <div><h3>Temporadas</h3><span>{details.season_count ?? details.seasons.length} temporadas · {details.episode_count ?? "?"} episódios</span></div>
                      {isAdmin && <div className="series-actions"><button className="episode-action" type="button" onClick={() => void startJob(`/api/library/add/series/${details.imdb_id}`)}>Adicionar série inteira</button><button className="episode-action" type="button" onClick={() => void startJob(`/api/library/sync/series/${details.imdb_id}`)}>Sincronizar série</button></div>}
                      <div className="season-list">
                        {details.seasons.map((season) => {
                          const episodes = details.episodes.filter((episode) => episode.season_number === season.season_number);
                          return (
                            <div className="season-entry" key={season.season_number}>
                              <button className="season-toggle" type="button" onClick={() => setOpenSeason(openSeason === season.season_number ? null : season.season_number)}>
                                <span>{season.name}</span><span>{details.library_status?.episodes?.filter((item) => item.season_number === season.season_number && item.status !== "missing").length || 0} / {season.episode_count} <b>{openSeason === season.season_number ? "-" : "+"}</b></span>
                              </button>
                              {isAdmin && <button className="episode-action season-add" type="button" onClick={() => void startJob(`/api/library/add/series/${details.imdb_id}/season/${season.season_number}`)}>Adicionar temporada</button>}
                              {openSeason === season.season_number && <div className="episode-list">
                                {episodes.map((episode) => (
                                  <article className="episode-row" key={`${episode.season_number}-${episode.episode_number}`}>
                                    <span className="episode-number">S{String(episode.season_number).padStart(2, "0")}E{String(episode.episode_number).padStart(2, "0")}</span>
                                    <div className="episode-copy"><strong>{episode.title}</strong><small className={`library-state ${details.library_status?.episodes?.find((item) => item.season_number === episode.season_number && item.episode_number === episode.episode_number)?.status || "missing"}`}>{details.library_status?.episodes?.find((item) => item.season_number === episode.season_number && item.episode_number === episode.episode_number)?.status === "media" ? "Video local" : details.library_status?.episodes?.find((item) => item.season_number === episode.season_number && item.episode_number === episode.episode_number)?.status === "strm" ? "STRM" : "Nao adicionado"}</small><p>{episode.overview || "Sinopse indisponível."}</p><button className="episode-action" type="button" onClick={() => loadEpisodeStreams(episode)}>Consultar addons</button>{isAdmin && <label className="episode-action upload-action">Importar arquivo local<input type="file" accept=".mp4,.mkv,.webm,video/mp4,video/x-matroska,video/webm" onChange={(event) => { const file = event.currentTarget.files?.[0]; event.currentTarget.value = ""; if (file && details.imdb_id) void startImport(`/api/jobs/import/series/${details.imdb_id}/${episode.season_number}/${episode.episode_number}`, file); }} /></label>}</div>
                                  </article>
                                ))}
                                {streamState.key.startsWith(`${season.season_number}:`) && <div className="stream-results">
                                  {streamState.loading && <p>Consultando addons ativos...</p>}
                                  {streamState.error && <p className="error-message">{streamState.error}</p>}
                                  {streamState.saved && <p className="success-message">{streamState.saved}</p>}
                                  {streamState.streams.map((stream) => <div className="stream-option" key={`${stream.provider}-${stream.url}`}><div><strong>{stream.provider} · {streamLanguageLabel(stream.language)}</strong><span>{stream.quality || stream.name}</span><small>{stream.title}</small></div>{isAdmin && <button type="button" onClick={() => { const episode = details.episodes.find((item) => `${item.season_number}:${item.episode_number}` === streamState.key); if (episode) void addEpisodeStream(episode, stream); }}>Adicionar</button>}</div>)}
                                </div>}
                              </div>}
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  )}
                  <div className="detail-block">
                    <h3>Elenco principal</h3>
                    {details.cast.length > 0 ? (
                      <div className="cast-grid">
                        {details.cast.map((person, index) => (
                          <div className="cast-person" key={`${person.name}-${index}`}>
                            <div className="cast-photo">{person.profile_url ? <img src={person.profile_url} alt="" loading="lazy" /> : <span>{person.name.slice(0, 1)}</span>}</div>
                            <div><strong>{person.name}</strong><span>{person.character || "Elenco"}</span></div>
                          </div>
                        ))}
                      </div>
                    ) : <p className="muted-copy">O TMDb não retornou informações de elenco.</p>}
                  </div>
                </div>
              </>
            )}
          </section>
        </div>
      )}
    </main>
  );
}
