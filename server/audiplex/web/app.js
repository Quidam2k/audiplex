// Audiplex browser UI (#2806). Vanilla JS, no build step, no external code.
//
// SECURITY: library metadata (titles, names, paths) is untrusted. Every piece
// of it reaches the DOM through el()/textContent, never through HTML parsing.
// The owner's token lives in localStorage, and "Sign out" clears it.
"use strict";

const TOKEN_KEY = "audiplex.token";
const $ = (id) => document.getElementById(id);

const state = {
  tab: "library",
  view: null,          // function that re-renders the current view
  crumbs: [],          // [{label, go}]
  favorites: new Set(), // "type:key"
  ratings: new Map(),  // track id -> stars
  playlists: [],
};

// ---- DOM helpers ----------------------------------------------------------

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "class") node.className = v;
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove("show"), 2500);
}

function fmtDuration(seconds) {
  const s = Math.max(0, Math.round(seconds || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`
           : `${m}:${String(sec).padStart(2, "0")}`;
}

// ---- API ------------------------------------------------------------------

function token() {
  try { return localStorage.getItem(TOKEN_KEY); } catch { return null; }
}

async function api(method, path, body) {
  const headers = { Authorization: `Bearer ${token()}` };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const r = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  if (r.status === 401) {
    signOut("Your sign-in expired. Please sign in again.");
    throw new Error("unauthorized");
  }
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch { /* not JSON */ }
    throw new Error(`${r.status}: ${detail}`);
  }
  return r.status === 204 ? null : r.json();
}

const get = (p) => api("GET", p);
const q = encodeURIComponent;

async function guard(fn, okMsg) {
  try {
    const out = await fn();
    if (okMsg) toast(okMsg);
    return out;
  } catch (e) {
    if (e.message !== "unauthorized") toast(`Failed: ${e.message}`);
    return undefined;
  }
}

// ---- sign in / out ---------------------------------------------------------

function showLogin(msg) {
  $("app-view").hidden = true;
  $("device-box").hidden = true;
  $("signout").hidden = true;
  $("login-view").hidden = false;
  $("login-error").textContent = msg || "";
  $("username").focus();
}

function signOut(msg) {
  try { localStorage.removeItem(TOKEN_KEY); } catch { /* storage blocked */ }
  showLogin(msg);
}

$("login-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  $("login-error").textContent = "";
  const r = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: $("username").value.trim(), password: $("password").value }),
  }).catch(() => null);
  if (!r || !r.ok) {
    $("login-error").textContent = r && r.status === 401 ? "Wrong username or password." : "Can't reach the server.";
    return;
  }
  const { token: t } = await r.json();
  try { localStorage.setItem(TOKEN_KEY, t); } catch { /* storage blocked: this tab only */ }
  $("password").value = "";
  start();
});

$("signout").addEventListener("click", () => signOut("Signed out."));

// ---- devices (the page is a remote; it never plays audio itself) -----------

async function loadDevices() {
  const data = await guard(() => get("/api/playback/devices"));
  if (!data) return;
  const sel = $("device");
  const active = data.active_device_id;
  sel.replaceChildren(
    el("option", { value: "", disabled: true, selected: !active }, "Choose a device"),
    ...data.devices.map((d) =>
      el("option", { value: d.id, selected: d.id === active },
        `${d.name || d.id}${d.connected ? "" : " (offline)"}`)),
  );
  $("device-box").hidden = false;
}

$("device").addEventListener("change", async (ev) => {
  const id = ev.target.value;
  await guard(() => api("POST", `/api/playback/devices/${q(id)}/activate`),
    `Playback moved to ${ev.target.selectedOptions[0].textContent}`);
  loadDevices();
});

async function sendPlay(type, trackIds) {
  if (!trackIds.length) return toast("Nothing to play.");
  const label = type === "play_now" ? "Playing" : "Queued";
  await guard(() => api("POST", "/api/playback/command", { type, payload: { track_ids: trackIds } }),
    `${label} ${trackIds.length} track${trackIds.length === 1 ? "" : "s"}`);
}

// ---- favorites, ratings, playlists caches ----------------------------------

async function loadCaches() {
  const [favs, ratings, playlists] = await Promise.all([
    guard(() => get("/api/music/favorites")),
    guard(() => get("/api/music/ratings")),
    guard(() => get("/api/music/playlists")),
  ]);
  state.favorites = new Set((favs || []).map((f) => `${f.entity_type}:${f.entity_key}`));
  state.ratings = new Map((ratings || []).map((r) => [r.track_id, r.rating]));
  state.playlists = playlists || [];
}

function favButton(type, key) {
  const id = `${type}:${key}`;
  const btn = el("button", { class: "icon fav", "aria-label": "Favorite", title: "Favorite" });
  const paint = () => {
    const on = state.favorites.has(id);
    btn.textContent = on ? "★" : "☆";
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  };
  btn.addEventListener("click", async () => {
    const on = state.favorites.has(id);
    const ok = await guard(() => on
      ? api("DELETE", `/api/music/favorites/${q(type)}/${q(key)}`)
      : api("POST", "/api/music/favorites", { entity_type: type, entity_key: String(key) }));
    if (ok === undefined) return;
    if (on) state.favorites.delete(id); else state.favorites.add(id);
    paint();
  });
  paint();
  return btn;
}

function ratingSelect(trackId) {
  const sel = el("select", { class: "rating", "aria-label": "Rating" },
    el("option", { value: "0" }, "–"),
    [1, 2, 3, 4, 5].map((n) => el("option", { value: n }, `${n}★`)));
  sel.value = String(state.ratings.get(trackId) || 0);
  sel.addEventListener("change", async () => {
    const n = Number(sel.value);
    const ok = await guard(() => n
      ? api("PUT", `/api/music/tracks/${trackId}/rating`, { rating: n })
      : api("DELETE", `/api/music/tracks/${trackId}/rating`));
    if (ok === undefined) { sel.value = String(state.ratings.get(trackId) || 0); return; }
    if (n) state.ratings.set(trackId, n); else state.ratings.delete(trackId);
  });
  return sel;
}

// Add-to-playlist dialog
let pickIds = [];
async function pickPlaylist(trackIds) {
  pickIds = trackIds;
  const list = $("pick-list");
  list.replaceChildren(...state.playlists.map((p) =>
    el("button", {
      type: "button", class: "pick",
      onclick: async () => {
        $("pick-playlist").close();
        const ok = await guard(() => api("POST", `/api/music/playlists/${p.id}/tracks`, { track_ids: pickIds }),
          `Added to ${p.name}`);
        if (ok) await refreshPlaylists();
      },
    }, `${p.name} (${p.track_count})`)));
  if (!state.playlists.length) list.append(el("p", { class: "muted" }, "No playlists yet."));
  $("pick-new").value = "";
  $("pick-playlist").showModal();
}

$("pick-create").addEventListener("click", async (ev) => {
  ev.preventDefault();
  const name = $("pick-new").value.trim();
  if (!name) return toast("Give the playlist a name.");
  $("pick-playlist").close();
  const ok = await guard(() => api("POST", "/api/music/playlists", { name, track_ids: pickIds }),
    `Created ${name}`);
  if (ok) await refreshPlaylists();
});

async function refreshPlaylists() {
  state.playlists = (await guard(() => get("/api/music/playlists"))) || state.playlists;
}

// ---- rendering --------------------------------------------------------------

function setCrumbs(crumbs) {
  state.crumbs = crumbs;
  $("crumbs").replaceChildren(...crumbs.map((c, i) =>
    i === crumbs.length - 1
      ? el("span", { class: "crumb current" }, c.label)
      : el("button", { class: "crumb link", onclick: c.go }, c.label)));
}

function render(nodes) {
  $("content").replaceChildren(...[nodes].flat());
  applyFilter();
}

function applyFilter() {
  const f = $("filter").value.trim().toLowerCase();
  for (const row of $("content").querySelectorAll("[data-filter]")) {
    row.hidden = Boolean(f) && !row.dataset.filter.includes(f);
  }
}
$("filter").addEventListener("input", applyFilter);

function actionBar(trackIds, extra) {
  return el("div", { class: "actions" },
    el("button", { onclick: () => sendPlay("play_now", trackIds) }, "Play all"),
    el("button", { onclick: () => sendPlay("queue", trackIds) }, "Queue all"),
    el("button", { onclick: () => pickPlaylist(trackIds) }, "Add to playlist"),
    extra || []);
}

function trackRow(t, extra) {
  return el("li", { class: "track", "data-filter": `${t.title} ${t.artist_name || ""}`.toLowerCase(), "data-track-id": t.id },
    el("div", { class: "meta" },
      el("span", { class: "title" }, t.title),
      el("span", { class: "sub" }, `${t.artist_name || ""} · ${fmtDuration(t.duration_seconds)}`)),
    el("div", { class: "row-actions" },
      favButton("track", t.id),
      ratingSelect(t.id),
      el("button", { class: "icon", title: "Play", "aria-label": "Play", onclick: () => sendPlay("play_now", [t.id]) }, "▶"),
      el("button", { class: "icon", title: "Queue", "aria-label": "Queue", onclick: () => sendPlay("queue", [t.id]) }, "+"),
      el("button", { class: "icon", title: "Add to playlist", "aria-label": "Add to playlist", onclick: () => pickPlaylist([t.id]) }, "≡"),
      extra || []));
}

function trackList(tracks, extraFor) {
  if (!tracks.length) return el("p", { class: "muted" }, "No tracks.");
  return el("ol", { class: "tracks" }, tracks.map((t, i) => trackRow(t, extraFor && extraFor(t, i))));
}

function linkRow(label, sub, go, fav) {
  return el("li", { class: "link-row", "data-filter": `${label} ${sub || ""}`.toLowerCase() },
    el("button", { class: "row-link", onclick: go },
      el("span", { class: "title" }, label), sub ? el("span", { class: "sub" }, sub) : null),
    fav || null);
}

async function withView(fn) {
  state.view = fn;
  $("content").replaceChildren(el("p", { class: "muted" }, "Loading…"));
  await fn();
}

// Library: artists -> artist -> album
async function showArtists() {
  const artists = await guard(() => get("/api/music/artists"));
  if (!artists) return;
  setCrumbs([{ label: "Artists" }]);
  render(el("ul", { class: "list" }, artists.map((a) =>
    linkRow(a.name, null, () => withView(() => showArtist(a.id)), favButton("artist", a.id)))));
}

async function showArtist(id) {
  const a = await guard(() => get(`/api/music/artists/${id}`));
  if (!a) return;
  setCrumbs([{ label: "Artists", go: () => withView(showArtists) }, { label: a.name }]);
  const tracks = (await guard(() => get(`/api/music/artists/${id}/tracks`))) || [];
  render([
    actionBar(tracks.map((t) => t.id)),
    el("ul", { class: "list" }, a.albums.map((al) =>
      linkRow(al.title, [al.year, `${al.track_count} tracks`].filter(Boolean).join(" · "),
        () => withView(() => showAlbum(al.id, [{ label: "Artists", go: () => withView(showArtists) },
          { label: a.name, go: () => withView(() => showArtist(id)) }])),
        favButton("album", al.id)))),
  ]);
}

async function showAlbum(id, trail) {
  const al = await guard(() => get(`/api/music/albums/${id}`));
  if (!al) return;
  setCrumbs([...trail, { label: al.title }]);
  render([
    el("p", { class: "sub" }, [al.artist_name, al.year, fmtDuration(al.duration_seconds)].filter(Boolean).join(" · ")),
    actionBar(al.tracks.map((t) => t.id), favButton("album", al.id)),
    trackList(al.tracks),
  ]);
}

// Folders
async function showFolder(path) {
  const data = await guard(() => get(path ? `/api/music/folders?path=${q(path)}` : "/api/music/folders"));
  if (!data) return;
  const trail = [{ label: "Folders", go: () => withView(() => showFolder(null)) }];
  if (path) {
    if (data.parent) trail.push({ label: "…", go: () => withView(() => showFolder(data.parent)) });
    trail.push({ label: path.split(/[\\/]/).filter(Boolean).pop() || path });
  }
  setCrumbs(trail);
  const here = trail.slice(0, -1).concat({ label: trail[trail.length - 1].label, go: () => withView(() => showFolder(path)) });
  const loadAll = async () => (await guard(() => get(`/api/music/folders/tracks?path=${q(path)}`))) || [];
  render([
    path ? el("div", { class: "actions" },
      el("button", { onclick: async () => sendPlay("play_now", shuffle((await loadAll()).map((t) => t.id))) }, "Shuffle folder"),
      el("button", { onclick: async () => sendPlay("queue", (await loadAll()).map((t) => t.id)) }, "Queue folder"),
      el("button", { onclick: async () => pickPlaylist((await loadAll()).map((t) => t.id)) }, "Add folder to playlist"))
      : null,
    el("ul", { class: "list" },
      data.folders.map((f) => linkRow(f.name, `${f.album_count} albums · ${f.track_count} tracks`,
        () => withView(() => showFolder(f.path)))),
      data.albums.map((al) => linkRow(al.title, al.artist_name,
        () => withView(() => showAlbum(al.id, here)), favButton("album", al.id)))),
  ]);
}

function shuffle(ids) {
  const a = ids.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

// Playlists
async function showPlaylists() {
  await refreshPlaylists();
  setCrumbs([{ label: "Playlists" }]);
  const name = el("input", { placeholder: "New playlist name", "aria-label": "New playlist name", id: "new-playlist" });
  render([
    el("form", {
      class: "actions",
      onsubmit: async (ev) => {
        ev.preventDefault();
        const n = name.value.trim();
        if (!n) return;
        const p = await guard(() => api("POST", "/api/music/playlists", { name: n, track_ids: [] }), `Created ${n}`);
        if (p) withView(() => showPlaylist(p.id));
      },
    }, name, el("button", { type: "submit" }, "Create")),
    el("ul", { class: "list" }, state.playlists.map((p) =>
      linkRow(p.name, `${p.track_count} tracks`, () => withView(() => showPlaylist(p.id))))),
  ]);
}

async function showPlaylist(id) {
  const p = await guard(() => get(`/api/music/playlists/${id}`));
  if (!p) return;
  setCrumbs([{ label: "Playlists", go: () => withView(showPlaylists) }, { label: p.name }]);
  const ids = p.tracks.map((t) => t.id);
  const save = async (trackIds, msg) => {
    const ok = await guard(() => api("PUT", `/api/music/playlists/${id}`, { track_ids: trackIds }), msg);
    if (ok) withView(() => showPlaylist(id));
  };
  const move = (i, d) => {
    const next = ids.slice();
    [next[i], next[i + d]] = [next[i + d], next[i]];
    save(next);
  };
  render([
    actionBar(ids, [
      el("button", {
        onclick: async () => {
          const n = prompt("Rename playlist", p.name);
          if (n && n.trim()) {
            const ok = await guard(() => api("PUT", `/api/music/playlists/${id}`, { name: n.trim() }), "Renamed");
            if (ok) withView(() => showPlaylist(id));
          }
        },
      }, "Rename"),
      el("button", {
        class: "danger",
        onclick: async () => {
          if (!confirm(`Delete the playlist "${p.name}"? The songs stay in your library.`)) return;
          const ok = await guard(() => api("DELETE", `/api/music/playlists/${id}`), "Playlist deleted");
          if (ok) withView(showPlaylists);
        },
      }, "Delete"),
    ]),
    trackList(p.tracks, (t, i) => [
      el("button", { class: "icon", title: "Move up", "aria-label": "Move up", disabled: i === 0, onclick: () => move(i, -1) }, "↑"),
      el("button", { class: "icon", title: "Move down", "aria-label": "Move down", disabled: i === ids.length - 1, onclick: () => move(i, 1) }, "↓"),
      el("button", {
        class: "icon", title: "Remove from playlist", "aria-label": "Remove from playlist",
        onclick: () => save(ids.filter((_, j) => j !== i), "Removed"),
      }, "✕"),
    ]),
  ]);
}

// Favorites
async function showFavorites() {
  const favs = (await guard(() => get("/api/music/favorites"))) || [];
  state.favorites = new Set(favs.map((f) => `${f.entity_type}:${f.entity_key}`));
  setCrumbs([{ label: "Favorites" }]);
  const byType = (t) => favs.filter((f) => f.entity_type === t).map((f) => f.entity_key);
  const tracks = (await Promise.all(byType("track").map((k) => guard(() => get(`/api/music/tracks/${q(k)}`))))).filter(Boolean);
  const albums = (await Promise.all(byType("album").map((k) => guard(() => get(`/api/music/albums/${q(k)}`))))).filter(Boolean);
  const artistIds = new Set(byType("artist").map(Number));
  const artists = artistIds.size ? ((await guard(() => get("/api/music/artists"))) || []).filter((a) => artistIds.has(a.id)) : [];
  const favTrail = [{ label: "Favorites", go: () => withView(showFavorites) }];
  render([
    el("h2", {}, "Songs"),
    tracks.length ? actionBar(tracks.map((t) => t.id)) : null,
    trackList(tracks),
    el("h2", {}, "Albums"),
    albums.length ? el("ul", { class: "list" }, albums.map((al) =>
      linkRow(al.title, al.artist_name, () => withView(() => showAlbum(al.id, favTrail)), favButton("album", al.id))))
      : el("p", { class: "muted" }, "None yet."),
    el("h2", {}, "Artists"),
    artists.length ? el("ul", { class: "list" }, artists.map((a) =>
      linkRow(a.name, null, () => withView(() => showArtist(a.id)), favButton("artist", a.id))))
      : el("p", { class: "muted" }, "None yet."),
  ]);
}

// ---- tabs + boot -------------------------------------------------------------

const TABS = {
  library: showArtists,
  folders: () => showFolder(null),
  playlists: showPlaylists,
  favorites: showFavorites,
};

function selectTab(tab) {
  state.tab = tab;
  for (const b of document.querySelectorAll(".tab")) {
    b.setAttribute("aria-selected", b.dataset.tab === tab ? "true" : "false");
  }
  $("filter").value = "";
  withView(TABS[tab]);
}

for (const b of document.querySelectorAll(".tab")) {
  b.addEventListener("click", () => selectTab(b.dataset.tab));
}

async function start() {
  if (!token()) return showLogin();
  $("login-view").hidden = true;
  $("app-view").hidden = false;
  $("signout").hidden = false;
  await loadCaches();
  if (!token()) return;  // a 401 during load already sent us to sign-in
  loadDevices();
  if (!start.timer) start.timer = setInterval(() => { if (token() && !document.hidden) loadDevices(); }, 15000);
  selectTab(state.tab);
}

start();
