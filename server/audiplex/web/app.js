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
  openDialog($("pick-playlist"));  // #3696
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
}

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
      el("button", { class: "icon", title: "Music video", "aria-label": "Music video", onclick: () => openMusicVideo(t) }, "🎬"),
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

// ---- history (#3696): browser Back steps through the app's own views ----------
//
// Every view change goes through withView(), which pushes one history entry and
// remembers the view's render function in memory (views are closures, so there
// is no URL routing; after a reload Back leaves the page as before). An open
// dialog owns one extra entry: Back closes it, and closing it any other way
// pops that entry. history.back() is async, so withView() waits for that pop
// before pushing, which keeps the stack from losing or doubling an entry.

const nav = { views: new Map(), seq: 0, cur: 0, pendingBack: null, resolveBack: null };

function navRecord(fn, replace) {
  const first = !nav.cur;
  const id = replace && !first ? nav.cur : ++nav.seq;
  nav.views.set(id, { fn, tab: state.tab });
  history[replace || first ? "replaceState" : "pushState"]({ v: id }, "");
  nav.cur = id;
}

async function withView(fn, opts = {}) {
  if (nav.pendingBack) await nav.pendingBack;
  if (!opts.fromHistory) navRecord(fn, opts.replace);
  state.view = fn;
  clearInterval(state.videoTimer);
  $("content").replaceChildren(el("p", { class: "muted" }, "Loading…"));
  await fn();
}

function openDialog(dlg) {
  dlg.showModal();
  history.pushState({ v: nav.cur, modal: true }, "");
  dlg.ownsHistory = true;
}

for (const dlg of document.querySelectorAll("dialog")) {
  dlg.addEventListener("close", () => {
    if (!dlg.ownsHistory) return;  // closed by Back: its entry is already gone
    dlg.ownsHistory = false;
    nav.pendingBack = new Promise((res) => { nav.resolveBack = res; });
    history.back();
  });
}

window.addEventListener("popstate", (ev) => {
  if (nav.resolveBack) {  // the pop we asked for when a dialog closed
    nav.resolveBack();
    nav.resolveBack = nav.pendingBack = null;
    return;
  }
  const open = document.querySelector("dialog[open]");
  if (open) {
    open.ownsHistory = false;
    open.close();
  }
  const id = ev.state && ev.state.v;
  if (ev.state && ev.state.modal) return;  // Forward onto a closed dialog's entry
  const entry = nav.views.get(id);
  if (!entry || id === nav.cur) return;
  nav.cur = id;
  setTabUI(entry.tab);
  withView(entry.fn, { fromHistory: true });
});

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
    if (ok) withView(() => showPlaylist(id), { replace: true });  // #3696 same view
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
            if (ok) withView(() => showPlaylist(id), { replace: true });  // #3696 same view
          }
        },
      }, "Rename"),
      el("button", {
        class: "danger",
        onclick: async () => {
          if (!confirm(`Delete the playlist "${p.name}"? The songs stay in your library.`)) return;
          const ok = await guard(() => api("DELETE", `/api/music/playlists/${id}`), "Playlist deleted");
          if (ok) withView(showPlaylists, { replace: true });  // #3696 deleted: no way back
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

// ---- music video (#6172) ------------------------------------------------------

const MV = { track: null, est: null, folder: "", selected: [], urls: [], gen: 0, badges: new Map() };
const ACTIVE_JOB = ["queued", "analyzing", "rendering", "stitching"];

function lsGet(k) { try { return localStorage.getItem(k) || ""; } catch { return ""; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch { /* storage blocked */ } }

function mvResetThumbs() {
  MV.gen++;
  for (const u of MV.urls) URL.revokeObjectURL(u);
  MV.urls = [];
}

function mvPaint() {
  const n = MV.est ? MV.est.n_images : 0, k = MV.selected.length;
  for (const [name, { btn, badge }] of MV.badges) {
    const i = MV.selected.indexOf(name);
    btn.classList.toggle("selected", i >= 0);
    btn.setAttribute("aria-pressed", i >= 0 ? "true" : "false");
    badge.hidden = i < 0;
    badge.textContent = String(i + 1);
  }
  $("mv-fill").max = Math.max(n, 1);
  $("mv-fill").value = k;
  $("mv-count").textContent = `${k} / ${n} images`;
  $("mv-go").disabled = !n || k !== n;
}

async function mvLoadEstimate() {
  const est = await guard(() => get(`/api/music-video/estimate/${MV.track.id}?quality=${q($("mv-quality").value)}`));
  if (!est) return null;
  MV.est = est;
  $("mv-need").textContent = `You need ${est.n_images} images (one ~${est.clip_seconds} s clip each). ` +
    `Estimated render: ~${Math.ceil(est.est_render_seconds / 60)} min.`;
  MV.selected = MV.selected.slice(0, est.n_images);
  mvPaint();
  return est;
}

// <img> can't send the auth header: fetch -> blob -> object URL, ~6 at a time.
async function mvLoadThumbs(folder, items, gen) {
  let next = 0;
  const worker = async () => {
    while (next < items.length && gen === MV.gen) {
      const { name, img } = items[next++];
      try {
        const r = await fetch(`/api/music-video/thumb?folder=${q(folder)}&name=${q(name)}`,
          { headers: { Authorization: `Bearer ${token()}` } });
        if (!r.ok) continue;
        const url = URL.createObjectURL(await r.blob());
        if (gen !== MV.gen) { URL.revokeObjectURL(url); return; }
        MV.urls.push(url);
        img.src = url;
      } catch { /* leave the thumb blank */ }
    }
  };
  await Promise.all(Array.from({ length: 6 }, worker));
}

async function mvOpenFolder(path) {
  path = path.trim();
  if (!path) return toast("Enter a folder path.");
  if (MV.selected.length && path !== MV.folder && !confirm("Changing folder clears your selected images. Continue?")) return;
  const data = await guard(() => get(`/api/music-video/images?folder=${q(path)}`));
  if (!data) return;
  mvResetThumbs();
  const gen = MV.gen;
  MV.folder = data.folder;
  MV.selected = [];
  MV.badges = new Map();
  $("mv-folder").value = data.folder;
  $("mv-up").hidden = !data.parent;
  $("mv-up").onclick = () => mvOpenFolder(data.parent);
  const sep = data.folder.includes("\\") ? "\\" : "/";
  $("mv-subs").replaceChildren(...data.subfolders.map((s) =>
    el("button", { type: "button", onclick: () => mvOpenFolder(data.folder.replace(/[\\/]+$/, "") + sep + s) }, `📁 ${s}`)));
  const items = data.images.map((name) => {
    const img = el("img", { alt: name });
    const badge = el("span", { class: "badge", hidden: true });
    const btn = el("button", {
      type: "button", class: "thumb", title: name, "aria-pressed": "false",
      onclick: () => {
        const i = MV.selected.indexOf(name);
        if (i >= 0) MV.selected.splice(i, 1);
        else if (MV.selected.length >= MV.est.n_images) return toast("That's enough images — deselect one to swap");
        else MV.selected.push(name);
        mvPaint();
      },
    }, img, badge);
    MV.badges.set(name, { btn, badge });
    return { name, img, btn };
  });
  $("mv-grid").replaceChildren(...(items.length ? items.map((i) => i.btn) : [el("p", { class: "muted" }, "No images in this folder.")]));
  mvPaint();
  mvLoadThumbs(data.folder, items, gen);
}

async function openMusicVideo(track) {
  Object.assign(MV, { track, est: null, folder: "", selected: [], badges: new Map() });
  mvResetThumbs();
  $("mv-title").textContent = `Music video: ${track.title} (${fmtDuration(track.duration_seconds)})`;
  $("mv-quality").value = "draft";
  $("mv-need").textContent = "";
  $("mv-grid").replaceChildren();
  $("mv-subs").replaceChildren();
  $("mv-up").hidden = true;
  mvPaint();
  openDialog($("music-video"));  // #3696
  const est = await mvLoadEstimate();
  if (!est) return;
  $("mv-direction").value = lsGet("audiplex.mv.direction") || est.last_direction || "";
  const folder = lsGet("audiplex.mv.folder") || est.last_folder || "";
  $("mv-folder").value = folder;
  if (folder) mvOpenFolder(folder);
}

$("mv-quality").addEventListener("change", mvLoadEstimate);
$("mv-open").addEventListener("click", () => mvOpenFolder($("mv-folder").value));
$("mv-folder").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") { ev.preventDefault(); mvOpenFolder($("mv-folder").value); }
});
$("mv-cancel").addEventListener("click", () => $("music-video").close());
$("music-video").addEventListener("close", mvResetThumbs);
$("mv-go").addEventListener("click", async () => {
  const direction = $("mv-direction").value.trim();
  const job = await guard(() => api("POST", "/api/music-video/jobs", {
    track_id: MV.track.id, quality: $("mv-quality").value, folder: MV.folder,
    images: MV.selected.slice(), direction,
  }));
  if (!job) return;
  lsSet("audiplex.mv.folder", MV.folder);
  lsSet("audiplex.mv.direction", direction);
  $("music-video").close();
  toast("Music video queued");
  selectTab("videos");
});

// Videos tab: list jobs; auto-refresh every 10 s while any job is active.
async function renderVideosView() {
  clearInterval(state.videoTimer);
  const jobs = await guard(() => get("/api/music-video/jobs"));
  if (!jobs || state.view !== renderVideosView) return;
  setCrumbs([{ label: "Videos" }]);
  jobs.sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)) || b.id - a.id);
  const act = (path, msg) => async () => {
    if (await guard(() => api("POST", path), msg) !== undefined) renderVideosView();
  };
  render(jobs.length ? el("div", {}, jobs.map((j) => {
    const slot = el("div", {});
    return el("div", { class: "job", "data-filter": `${j.title} ${j.status}`.toLowerCase() },
      el("div", { class: "head" },
        el("span", { class: "title" }, j.title),
        el("span", { class: "sub" }, `${j.quality} · ${j.status}${j.detail ? ` · ${j.detail}` : ""}`)),
      el("progress", { max: Math.max(j.clips_total, 1), value: j.clips_done }),
      el("div", { class: "actions" },
        ACTIVE_JOB.includes(j.status) ? el("button", { class: "danger", onclick: act(`/api/music-video/jobs/${j.id}/cancel`, "Cancelled") }, "Cancel") : null,
        ["failed", "cancelled"].includes(j.status) ? el("button", { onclick: act(`/api/music-video/jobs/${j.id}/retry`, "Retrying") }, "Retry") : null,
        j.has_video ? el("button", {
          onclick: async (ev) => {
            const v = await guard(() => get(`/api/music-video/jobs/${j.id}/video-url`));
            if (!v) return;
            ev.target.hidden = true;
            slot.replaceChildren(
              el("video", { controls: true, preload: "metadata", src: v.url }),
              el("a", { href: v.url, download: "" }, "Download"));
          },
        }, "Play") : null),
      slot);
  })) : el("p", { class: "muted" }, "No music videos yet. Use 🎬 on a song."));
  if (jobs.some((j) => ACTIVE_JOB.includes(j.status))) {
    state.videoTimer = setInterval(() => {
      // Don't rebuild the list under a video that is on screen.
      if (state.view === renderVideosView && !document.hidden && !$("content").querySelector("video")) renderVideosView();
    }, 10000);
  }
}

// ---- tabs + boot -------------------------------------------------------------

const TABS = {
  library: showArtists,
  folders: () => showFolder(null),
  playlists: showPlaylists,
  favorites: showFavorites,
  videos: renderVideosView,
};

function setTabUI(tab) {
  state.tab = tab;
  for (const b of document.querySelectorAll(".tab")) {
    b.setAttribute("aria-selected", b.dataset.tab === tab ? "true" : "false");
  }
}

function selectTab(tab) {
  setTabUI(tab);
  $("filter").value = "";
  withView(TABS[tab]);
}

// ---- search (#3696): the box searches the whole library ---------------------

let searchTimer = null;
let searchSeq = 0;

$("filter").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(runSearch, 300);
});

function runSearch() {
  const text = $("filter").value.trim();
  const inSearch = Boolean(state.view && state.view.isSearch);
  if (text.length < 2) {
    if (inSearch && !text) history.back();  // cleared: back to where he was
    return;
  }
  const fn = () => showSearch(text);
  fn.isSearch = true;
  // one history entry per search: later keystrokes refine it in place
  withView(fn, { replace: inSearch });
}

async function showSearch(text) {
  if ($("filter").value.trim() !== text) $("filter").value = text;
  const seq = ++searchSeq;
  const r = await guard(() => get(`/api/music/search?q=${q(text)}&limit=50`));
  if (!r || seq !== searchSeq) return;  // a newer search superseded this one
  const trail = [{ label: `Search: ${text}`, go: () => withView(() => showSearch(text)) }];
  setCrumbs([{ label: `Search: ${text}` }]);
  const section = (title, rows) => (rows.length ? [el("h3", {}, title), el("ul", { class: "list" }, rows)] : []);
  const out = [
    ...section("Artists", r.artists.map((a) =>
      linkRow(a.name, null, () => withView(() => showArtist(a.id)), favButton("artist", a.id)))),
    ...section("Albums", r.albums.map((al) =>
      linkRow(al.title, al.artist_name, () => withView(() => showAlbum(al.id, trail)), favButton("album", al.id)))),
    ...(r.tracks.length ? [el("h3", {}, "Songs"), trackList(r.tracks)] : []),
  ];
  render(out.length ? out : el("p", { class: "muted" }, `Nothing in the library matches "${text}".`));
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
