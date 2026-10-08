import { FormEvent, Suspense, lazy, useEffect, useRef, useState } from "react";

const ShaderBackground = lazy(() => import("./ShaderBackground"));

type MediaType = "movie" | "series";
type SearchFilter = "all" | MediaType;
type LinkCheckState = { status: "pending" | "checking" | "available" | "unavailable" | "no_source" | "invalid" | "missing_file" | "error"; checked_at?: string | null; provider?: string | null; quality?: string | null; http_status?: number | null; message?: string };

type SearchResult = {
  id: string;
  media_type: MediaType;
  tmdb_id: number | null;
  imdb_id: string | null;
  title: string;
  year: number | null;
  poster_url: string | null;
  overview: string;
  availability?: "available" | "unavailable" | "unknown";
  available_providers?: string[];
  catalog_providers?: string[];
  catalog_search_errors?: { provider: string; detail: string }[];
};

type ResumeItem = { id: string; title: string; subtitle: string; media_type: MediaType; progress: number; image_url: string; open_url: string };
type HomeSection = { id: string; title: string; items: SearchResult[] };

type TitleDetails = SearchResult & {
  cast: { name: string; character: string; profile_url: string | null }[];
  genres: string[];
  tagline: string;
  season_count?: number;
  episode_count?: number;
  seasons?: { season_number: number; name: string; episode_count: number; air_date: string | null }[];
  episodes: { season_number: number; episode_number: number; title: string; overview: string; released: string | null }[];
  library_status?: { status?: "media" | "strm" | "missing"; path?: string | null; link_check?: LinkCheckState | null; episodes?: { season_number: number; episode_number: number; status: "media" | "strm" | "missing"; path: string | null; link_check?: LinkCheckState | null }[] };
};

type StreamOption = { name: string; title: string; provider: string; quality: string | null; language: "dubbed" | "subtitled" | "portuguese_unspecified" | "unknown"; url: string };
type TorrentOption = { name: string; title: string; provider: string; provider_id: string; quality: string | null; language: StreamOption["language"]; info_hash: string; file_idx: number | null };
type AddonResource = string | { name?: string; types?: string[]; idPrefixes?: string[] };
type AddonItem = { id: string; name: string; manifest_url: string; enabled: boolean; manifest?: { resources?: AddonResource[]; types?: string[]; idPrefixes?: string[]; catalogs?: { id?: string; type?: string; name?: string; extra?: { name?: string }[] }[]; behaviorHints?: { p2p?: boolean } } | null };
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

function linkCheckLabel(item: { status: "media" | "strm" | "missing"; link_check?: LinkCheckState | null }) {
  if (item.status === "missing") return { label: "Não adicionado", state: "missing" };
  if (item.status === "media") return { label: "Vídeo local", state: "available" };
  const check = item.link_check;
  if (!check) return { label: "STRM · aguardando verificação", state: "pending" };
  const labels: Record<LinkCheckState["status"], string> = {
    pending: "STRM · aguardando verificação", checking: "STRM · verificando",
    available: `Disponível${check.quality ? ` · ${check.quality}` : ""}`,
    unavailable: `Stream inválido${check.http_status ? ` · HTTP ${check.http_status}` : ""}`, error: "Falha na verificação",
    no_source: "Sem fonte ativa", invalid: "STRM inválido", missing_file: "Arquivo STRM ausente",
  };
  return { label: labels[check.status], state: check.status };
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
  const [catalogSearchErrors, setCatalogSearchErrors] = useState<{ provider: string; detail: string }[]>([]);
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
  const [expandedRow, setExpandedRow] = useState<string | null>(null);
  const [resumeItems, setResumeItems] = useState<ResumeItem[]>([]);
  const [resumeLoading, setResumeLoading] = useState(false);
  const [resumeError, setResumeError] = useState("");
  const [homeSections, setHomeSections] = useState<HomeSection[]>([]);
  const [homeLoading, setHomeLoading] = useState(false);
  const [homeError, setHomeError] = useState("");
  const [homeWarning, setHomeWarning] = useState("");
  const [tmdbConfigured, setTmdbConfigured] = useState<boolean | null>(null);
  const [renderShader, setRenderShader] = useState(false);
  const [addons, setAddons] = useState<AddonItem[]>([]);
  const [preferences, setPreferences] = useState<Preferences>({ preferred_quality: "1080p", preferred_provider: "automatic" });
  const [addonName, setAddonName] = useState("");
  const [manifestUrl, setManifestUrl] = useState("");
  const [settingsLoading, setSettingsLoading] = useState(false);
  const [settingsError, setSettingsError] = useState("");
  const [settingsMessage, setSettingsMessage] = useState("");
  const [authError, setAuthError] = useState("");
  const [streamState, setStreamState] = useState<{ key: string; loading: boolean; streams: StreamOption[]; torrents: TorrentOption[]; error: string; saved: string }>({ key: "", loading: false, streams: [], torrents: [], error: "", saved: "" });
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

  useEffect(() => {
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    const updateShaderPreference = () => setRenderShader(!reducedMotion.matches && window.innerWidth > 900);
    updateShaderPreference();
    window.addEventListener("resize", updateShaderPreference, { passive: true });
    reducedMotion.addEventListener("change", updateShaderPreference);
    return () => {
      window.removeEventListener("resize", updateShaderPreference);
      reducedMotion.removeEventListener("change", updateShaderPreference);
    };
  }, []);

  useEffect(() => {
    if (authState !== "ready") return;
    let active = true;
    setResumeLoading(true);
    fetch("/api/library/resume")
      .then(async (response) => {
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || "Falha ao carregar Continuar assistindo.");
        if (active) setResumeItems(data.items || []);
      })
      .catch((cause) => {
        if (active) setResumeError(cause instanceof Error ? cause.message : "Falha ao carregar Continuar assistindo.");
      })
      .finally(() => { if (active) setResumeLoading(false); });
    return () => { active = false; };
  }, [authState]);

  useEffect(() => {
    if (authState !== "ready") return;
    let active = true;
    setHomeLoading(true);
    fetch("/api/home/catalog")
      .then(async (response) => {
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || "Não foi possível carregar as recomendações.");
        if (!active) return;
        setTmdbConfigured(Boolean(data.configured));
        setHomeWarning(data.warning || "");
        setHomeSections(data.sections || []);
      })
      .catch((cause) => {
        if (active) setHomeError(cause instanceof Error ? cause.message : "Falha ao carregar recomendacoes.");
      })
      .finally(() => { if (active) setHomeLoading(false); });
    return () => { active = false; };
  }, [authState]);

  useEffect(() => () => jobEvents.current?.close(), []);

  useEffect(() => {
    if (!details?.imdb_id) return;
    let active = true;
    const updateChecks = async () => {
      try {
        const response = await fetch(`/api/library/checks/${details.media_type}/${details.imdb_id}`);
        if (!response.ok || !active) return;
        const data = await response.json();
        setDetails((current) => {
          if (!current || current.imdb_id !== details.imdb_id) return current;
          const library = current.library_status || {};
          if (current.media_type === "movie") return { ...current, library_status: { ...library, link_check: data.movie || null } };
          return { ...current, library_status: { ...library, episodes: (library.episodes || []).map((item) => ({
            ...item, link_check: data.episodes?.[`${item.season_number}:${item.episode_number}`] || null,
          })) } };
        });
      } catch { /* o status mais recente continua visível até a próxima consulta */ }
    };
    const timer = window.setInterval(() => void updateChecks(), 30_000);
    return () => { active = false; window.clearInterval(timer); };
  }, [details?.imdb_id, details?.media_type]);

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
    setStreamState({ key, loading: true, streams: [], torrents: [], error: "", saved: "" });
    try {
      const response = await fetch(`/api/streams/series/${details?.imdb_id}/${episode.season_number}/${episode.episode_number}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Falha ao consultar os addons.");
      const addonErrors = (data.addon_errors || []).map((item: { provider: string; detail: string }) => `${item.provider}: ${item.detail}`).join(" · ");
      setStreamState({ key, loading: false, streams: data.streams, torrents: data.torrents || [], error: data.streams.length || data.torrents?.length ? "" : addonErrors || "Nenhum stream ou torrent foi retornado pelos addons.", saved: "" });
    } catch (cause) {
      setStreamState({ key, loading: false, streams: [], torrents: [], error: cause instanceof Error ? cause.message : "Falha ao consultar os addons.", saved: "" });
    }
  }

  async function loadMovieStreams() {
    setStreamState({ key: "movie", loading: true, streams: [], torrents: [], error: "", saved: "" });
    try {
      const response = await fetch(`/api/streams/movie/${details?.imdb_id}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Falha ao consultar os addons.");
      const addonErrors = (data.addon_errors || []).map((item: { provider: string; detail: string }) => `${item.provider}: ${item.detail}`).join(" · ");
      setStreamState({ key: "movie", loading: false, streams: data.streams, torrents: data.torrents || [], error: data.streams.length || data.torrents?.length ? "" : addonErrors || "Nenhum stream ou torrent foi retornado pelos addons.", saved: "" });
    } catch (cause) {
      setStreamState({ key: "movie", loading: false, streams: [], torrents: [], error: cause instanceof Error ? cause.message : "Falha ao consultar os addons.", saved: "" });
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

  async function downloadMovieTorrent(stream: TorrentOption) {
    setJobState(null);
    try {
      const response = await fetch(`/api/jobs/download/movie/${details?.imdb_id}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
        body: JSON.stringify({ info_hash: stream.info_hash, provider_id: stream.provider_id, file_idx: stream.file_idx }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Nao foi possivel iniciar o download.");
      setJobState({ id: data.job_id, kind: "torrent_download", status: "queued", total: 1, completed: 0, failed: 0, message: "Download torrent iniciado.", result: {} });
      watchJob(data.job_id);
    } catch (cause) {
      setStreamState((current) => ({ ...current, key: "movie", error: cause instanceof Error ? cause.message : "Falha ao iniciar o download." }));
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
    setView("library");
    setExpandedRow(null);
    setSearched(true);
    setError("");
    setCatalogSearchErrors([]);
    try {
      const params = new URLSearchParams({ query: value, media_type: filter });
      const response = await fetch(`/api/search?${params}`);
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "Falha na pesquisa.");
      setResults(payload.results);
      setCatalogSearchErrors(payload.addon_search_errors || []);
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

  const resultSections = (filter === "all"
    ? [
        { key: "movie", title: "Filmes", items: results.filter((item) => item.media_type === "movie") },
        { key: "series", title: "Séries", items: results.filter((item) => item.media_type === "series") },
      ]
    : [{ key: filter, title: filter === "movie" ? "Filmes" : "Séries", items: results }]
  ).filter((section) => section.items.length > 0);

  function renderResultCard(item: SearchResult) {
    const availability = item.availability === "available"
      ? `Stream · ${(item.available_providers || []).join(", ")}`
      : item.catalog_providers?.length ? `No catálogo · ${item.catalog_providers.join(", ")}`
        : item.availability === "unavailable" ? "Sem fonte nos addons" : "Disponibilidade não confirmada";
    return (
      <article
        className={`media-card search-${item.availability || "unknown"}`}
        key={item.id}
        role="button"
        tabIndex={0}
        aria-label={`Abrir detalhes de ${item.title}`}
        onClick={() => void openDetails(item)}
        onKeyDown={(event) => {
          if (event.target !== event.currentTarget) return;
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            void openDetails(item);
          }
        }}
      >
        <div className="poster-wrap">
          {item.poster_url ? <img src={item.poster_url} alt={`Pôster de ${item.title}`} loading="lazy" /> : <div className="poster-placeholder"><span>Sem capa disponível</span></div>}
          <span className="media-type">{item.media_type === "series" ? "SÉRIE" : "FILME"}</span>
        </div>
        <div className="card-copy">
          <div className="card-title-row"><h3>{item.title}</h3>{item.year && <span className="year">{item.year}</span>}</div>
          <span className={`availability-pill ${item.catalog_providers?.length ? "catalog-listed" : item.availability || "unknown"}`}>{availability}</span>
          <p>{item.overview || "Sinopse indisponível."}</p>
          <div className="card-footer"><span>IMDb <b>{item.imdb_id || "—"}</b></span><span className="details-button">Ver elenco e detalhes <span aria-hidden="true">↗</span></span></div>
        </div>
      </article>
    );
  }

  function renderResultSection(section: { key: string; title: string; items: SearchResult[]; eyebrow?: string }) {
    const expanded = expandedRow === section.key;
    const visibleItems = expanded ? section.items : section.items.slice(0, 12);
    return (
      <section className="media-row" key={section.key} aria-labelledby={`row-${section.key}`}>
        <div className="section-heading">
          <div><span className="eyebrow">{section.eyebrow || "PESQUISA"}</span><h2 id={`row-${section.key}`}>{section.title}</h2></div>
          <div className="results-actions"><span className="result-count">{section.items.length} {section.items.length === 1 ? "título" : "títulos"}</span>{section.items.length > 12 && <button className="view-all-button" type="button" aria-expanded={expanded} onClick={() => setExpandedRow(expanded ? null : section.key)}>{expanded ? "Recolher" : "Ver todos"}<span aria-hidden="true">{expanded ? "−" : "›"}</span></button>}</div>
        </div>
        <div className={`results-grid ${expanded ? "expanded" : ""}`} tabIndex={0} aria-label={`${section.title} encontrados`}>
          {visibleItems.map(renderResultCard)}
        </div>
      </section>
    );
  }

  return (
    <div className="app-layout">
      {renderShader && <div className="ambient-layer" aria-hidden="true"><Suspense fallback={null}><ShaderBackground /></Suspense></div>}
      <aside className="sidebar">
        <a className="brand" href="/" aria-label="Media Library Manager" title="Media Library Manager">
          <span className="brand-mark">M</span>
          <span className="brand-name">Media Library <strong>Manager</strong></span>
        </a>
        <nav className="sidebar-nav" aria-label="Navegacao principal">
          <button aria-label="Descobrir" title="Descobrir" className={`sidebar-link ${view === "library" ? "active" : ""}`} type="button" onClick={() => setView("library")}><svg aria-hidden="true" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="m16 16 4 4M8.5 13.5l2-5 5-2-2 5-5 2Z"/></svg><span className="nav-label">Descobrir</span></button>
          {isAdmin && <button aria-label={"Configurções"} title={"Configurações"} className={`sidebar-link ${view === "settings" ? "active" : ""}`} type="button" onClick={() => view === "settings" ? setView("library") : void openSettings()}><svg aria-hidden="true" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="m19.4 15 .1.1 1.2.9-1.2 2.1-1.4-.6a7.8 7.8 0 0 1-1.5.9l-.2 1.5h-2.4l-.2-1.5a7.8 7.8 0 0 1-1.6-.9l-1.3.6-1.2-2.1 1.2-.9a7 7 0 0 1 0-1.8l-1.2-.9 1.2-2.1 1.3.6a7.8 7.8 0 0 1 1.6-.9l.2-1.5h2.4l.2 1.5a7.8 7.8 0 0 1 1.5.9l1.4-.6 1.2 2.1-1.2.9a7 7 0 0 1 0 1.7Z"/></svg><span className="nav-label">Configura&#231;&#245;es</span></button>}
        </nav>
      </aside>
      <main className="page-shell">
      <header className="topbar">
        <form className="search-form topbar-search" onSubmit={handleSearch} role="search">
          <svg className="search-icon" aria-hidden="true" viewBox="0 0 24 24"><circle cx="10.8" cy="10.8" r="6.8"/><path d="m16 16 4.2 4.2"/></svg>
          <input aria-label={"Pesquisar filmes e séries"} placeholder={"Pesquisar filmes e séries..."} value={query} onChange={(event) => setQuery(event.target.value)} />
          <button type="submit" aria-label={loading ? "Pesquisando" : "Pesquisar"} disabled={loading || !query.trim()}>{loading ? "Buscando..." : "Pesquisar"}</button>
        </form>
        {currentUser && <div className="topbar-account"><span className="user-avatar" aria-hidden="true">{currentUser.slice(0, 1).toUpperCase()}</span><span className="topbar-username">{currentUser}</span><button className="logout-button" type="button" onClick={() => void handleLogout()}><svg aria-hidden="true" viewBox="0 0 24 24"><path d="M10 17l5-5-5-5M15 12H3"/><path d="M12 3h6a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-6"/></svg><span>Sair</span></button></div>}
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
                {addon.manifest && <div className="manifest-facts"><span>Resources: {(addon.manifest.resources || []).map((resource) => typeof resource === "string" ? resource : resource.name || "resource").join(", ") || "-"}</span><span>Types: {manifestTypes(addon.manifest).join(", ") || "-"}</span><span>ID prefixes: {manifestPrefixes(addon.manifest).join(", ") || "all"}</span><span>Catálogos com busca: {(addon.manifest.catalogs || []).filter((catalog) => catalog.extra?.some((extra) => extra.name === "search")).map((catalog) => `${catalog.name || catalog.id} (${catalog.type})`).join(", ") || "nenhum declarado"}</span></div>}
                {addon.manifest?.behaviorHints?.p2p && <p className="addon-capability-warning">Este addon declara streams P2P; esta aplicação aceita apenas URLs diretas HTTPS.</p>}
              </article>)}
              <form className="settings-form" onSubmit={handleAddonSubmit}><h3>Adicionar addon</h3><label>Nome<input value={addonName} onChange={(event) => setAddonName(event.target.value)} maxLength={120} required /></label><label>Manifest URL<input type="url" value={manifestUrl} onChange={(event) => setManifestUrl(event.target.value)} placeholder="https://addon.example/manifest.json" required /></label><button type="submit" disabled={!addonName.trim() || !manifestUrl.trim()}>Validar e cadastrar</button></form>
            </section>
            <section className="settings-card"><span className="eyebrow">REPRODUCAO</span><h2>Preferencias de stream</h2><form className="settings-form" onSubmit={savePreferences}><label>Qualidade preferida<select value={preferences.preferred_quality} onChange={(event) => setPreferences((current) => ({ ...current, preferred_quality: event.target.value }))}>{["2160p", "1440p", "1080p", "720p", "480p"].map((quality) => <option key={quality}>{quality}</option>)}</select></label><label>Provider preferido<select value={preferences.preferred_provider} onChange={(event) => setPreferences((current) => ({ ...current, preferred_provider: event.target.value }))}><option value="automatic">Automatico (dublado primeiro)</option>{addons.filter((addon) => addon.enabled).map((addon) => <option value={addon.id} key={addon.id}>{addon.name}</option>)}</select></label><p>Dublado sempre vem primeiro; provider e qualidade definem a ordem entre opcoes do mesmo idioma.</p><button type="submit">Salvar preferencias</button></form></section>
          </div>}
        </section>
      ) : <>
      <section className="hero" aria-labelledby="catalog-title">
        <div className="hero-copy">
          <span className="eyebrow"><span className="eyebrow-line" /> CAT&Aacute;LOGO PESSOAL</span>
          <h1 id="catalog-title">Descobrir filmes e s&eacute;ries</h1>
        </div>
        <div className="discover-controls">
          <div className="filter-row" role="group" aria-label="Tipo de conte&uacute;do">
            {FILTERS.map((option) => (
              <button className={`filter-chip ${filter === option.value ? "active" : ""}`} key={option.value} onClick={() => { setFilter(option.value); setExpandedRow(null); }} type="button">{option.label}</button>
            ))}
          </div>
          <div className="search-hint">Experimente <button type="button" onClick={() => setQuery("Mr. Robot")}>Mr. Robot</button> ou <button type="button" onClick={() => setQuery("The Office")}>The Office</button></div>
        </div>
      </section>

      <section className="resume-section" aria-labelledby="resume-title">
        <div className="section-heading">
          <div><span className="eyebrow">JELLYFIN</span><h2 id="resume-title">Continuar assistindo</h2></div>
          <div className="results-actions"><span className="result-count">{resumeItems.length ? `${resumeItems.length} itens` : "Sua atividade"}</span>{resumeItems.length > 6 && <button className="view-all-button" type="button" aria-expanded={expandedRow === "resume"} onClick={() => setExpandedRow(expandedRow === "resume" ? null : "resume")}>{expandedRow === "resume" ? "Recolher" : "Ver todos"}<span aria-hidden="true">{expandedRow === "resume" ? "\u2212" : "\u203a"}</span></button>}</div>
        </div>
        {resumeLoading && <div className="row-message" role="status">Carregando sua atividade do Jellyfin...</div>}
        {resumeError && <div className="message error-message">{resumeError}</div>}
        {!resumeLoading && !resumeError && resumeItems.length === 0 && <div className="row-message">Os t&iacute;tulos retom&aacute;veis da sua conta Jellyfin aparecer&atilde;o aqui.</div>}
        {resumeItems.length > 0 && (
          <div className={`resume-grid ${expandedRow === "resume" ? "expanded" : ""}`} aria-label="T&iacute;tulos para continuar assistindo">
            {(expandedRow === "resume" ? resumeItems : resumeItems.slice(0, 6)).map((item) => (
              <a className="resume-card" href={item.open_url} target="_blank" rel="noreferrer" key={item.id} aria-label={`Abrir ${item.title} no Jellyfin, ${Math.round(item.progress)} por cento assistido`}>
                <div className="resume-poster">
                  <img src={item.image_url} alt={`Poster de ${item.title}`} loading="lazy" />
                  <span className="resume-type">{item.media_type === "series" ? "S&Eacute;RIE" : "FILME"}</span>
                  <span className="resume-play" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M8 5.5v13l10-6.5-10-6.5Z" /></svg></span>
                  <span className="resume-progress" role="progressbar" aria-label={`Progresso: ${Math.round(item.progress)} por cento`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(item.progress)}><span style={{ width: `${item.progress}%` }} /></span>
                </div>
                <div className="resume-copy"><h3>{item.title}</h3><p>{item.subtitle}</p></div>
              </a>
            ))}
          </div>
        )}
      </section>

      <section className="results-section" aria-live="polite">
        {error && <div className="message error-message">{error}</div>}
        {catalogSearchErrors.length > 0 && <div className="message catalog-warning"><strong>Alguns cat&aacute;logos de addons n&atilde;o responderam.</strong><span>{catalogSearchErrors.map((item) => `${item.provider}: ${item.detail}`).join(" &middot; ")}</span></div>}
        {!error && searched && !loading && results.length === 0 && <div className="message">Nenhum resultado encontrado. Tente outro t&iacute;tulo.</div>}
        {results.length > 0 && <div className="search-results-heading"><span className="eyebrow">RESULTADOS DA BUSCA</span><h2>{results.length} {results.length === 1 ? "t&iacute;tulo encontrado" : "t&iacute;tulos encontrados"}</h2></div>}
        {searched ? resultSections.map(renderResultSection) : <>
          {homeLoading && <div className="row-message" role="status">Carregando recomenda&#231;&#245;es para voc&#234;...</div>}
          {homeError && <div className="message error-message">{homeError}</div>}
          {!homeLoading && !homeError && tmdbConfigured === false && <div className="message catalog-warning"><strong>Recomenda&#231;&#245;es, busca bil&#237;ngue e sinopses em portugu&#234;s precisam do TMDb.</strong><span>Configure TMDB_API_TOKEN como vari&#225;vel de ambiente no Coolify para ativar recomenda&#231;&#245;es personalizadas e metadata pt-BR.</span></div>}
          {homeWarning && <div className="message catalog-warning"><strong>O cat&#225;logo de recomenda&#231;&#245;es est&#225; temporariamente indispon&#237;vel.</strong><span>{homeWarning}</span></div>}
          {homeSections.map((section) => renderResultSection({ key: section.id, title: section.title, items: section.items, eyebrow: section.id === "for-you" ? "PELO SEU GOSTO" : section.id === "releases" ? "ESTREIAS PR&Oacute;XIMAS" : "POPULAR AGORA" }))}
          {!homeLoading && tmdbConfigured && homeSections.every((section) => section.items.length === 0) && <div className="catalog-empty"><span className="empty-mark" aria-hidden="true">M</span><p>Assista e marque filmes ou epis&#243;dios como vistos no Jellyfin para receber recomenda&#231;&#245;es personalizadas. Lan&#231;amentos e tend&#234;ncias aparecem aqui quando o cat&#225;logo responder.</p></div>}
        </>}
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
                  {details.media_type === "movie" && <div className="detail-block movie-add-block"><h3>Biblioteca</h3><p className={`library-state ${linkCheckLabel({ status: details.library_status?.status || "missing", link_check: details.library_status?.link_check }).state}`}>{linkCheckLabel({ status: details.library_status?.status || "missing", link_check: details.library_status?.link_check }).label}</p>{details.library_status?.link_check?.checked_at && <small>Verificado em {new Date(details.library_status.link_check.checked_at).toLocaleString()}</small>}{details.library_status?.path && <small>{details.library_status.path}</small>}<button className="episode-action" type="button" onClick={() => void loadMovieStreams()}>Consultar addons</button>{isAdmin && <label className="episode-action upload-action">Importar arquivo local<input type="file" accept=".mp4,.mkv,.webm,video/mp4,video/x-matroska,video/webm" onChange={(event) => { const file = event.currentTarget.files?.[0]; event.currentTarget.value = ""; if (file && details.imdb_id) void startImport(`/api/jobs/import/movie/${details.imdb_id}`, file); }} /></label>}{streamState.key === "movie" && <div className="stream-results">{streamState.loading && <p>Consultando addons...</p>}{streamState.error && <p className="error-message">{streamState.error}</p>}{streamState.saved && <p className="success-message">{streamState.saved}</p>}{streamState.streams.map((stream) => <div className="stream-option" key={`${stream.provider}-${stream.url}`}><div><strong>{stream.provider} · {streamLanguageLabel(stream.language)}</strong><span>{stream.quality || stream.name}</span><small>{stream.title}</small></div>{isAdmin && <button type="button" onClick={() => void addMovieStream(stream)}>Adicionar</button>}</div>)}{streamState.torrents.map((stream) => <div className="stream-option" key={`${stream.provider}-${stream.info_hash}`}><div><strong>{stream.provider} · Torrent</strong><span>{stream.quality || stream.name} · {streamLanguageLabel(stream.language)}</span><small>{stream.title}</small></div>{isAdmin && <button type="button" onClick={() => void downloadMovieTorrent(stream)}>Baixar temporariamente</button>}</div>)}{streamState.torrents.length > 0 && <small>O arquivo temporário será excluído depois que o Jellyfin marcar o filme como reproduzido.</small>}</div>}</div>}
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
                                    <div className="episode-copy"><strong>{episode.title}</strong>{(() => { const status = details.library_status?.episodes?.find((item) => item.season_number === episode.season_number && item.episode_number === episode.episode_number); const check = linkCheckLabel({ status: status?.status || "missing", link_check: status?.link_check }); return <small className={`library-state ${check.state}`} title={status?.link_check?.message || undefined}>{check.label}{status?.link_check?.checked_at ? ` · ${new Date(status.link_check.checked_at).toLocaleString()}` : ""}</small>; })()}<p>{episode.overview || "Sinopse indisponível."}</p><button className="episode-action" type="button" onClick={() => loadEpisodeStreams(episode)}>Consultar addons</button>{isAdmin && <label className="episode-action upload-action">Importar arquivo local<input type="file" accept=".mp4,.mkv,.webm,video/mp4,video/x-matroska,video/webm" onChange={(event) => { const file = event.currentTarget.files?.[0]; event.currentTarget.value = ""; if (file && details.imdb_id) void startImport(`/api/jobs/import/series/${details.imdb_id}/${episode.season_number}/${episode.episode_number}`, file); }} /></label>}</div>
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
    </div>
  );
}
