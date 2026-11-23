/* ==========================================================================
   DermaMetric — interface interativa
   Canvas com zoom/pan/lupa, fluxo guiado em 4 etapas, comparador longitudinal
   e laudo. Dados sensíveis são servidos descriptografados pela API.
   ========================================================================== */
"use strict";

/* --------------------------------------------------------------------------
   Constantes de domínio
   -------------------------------------------------------------------------- */
// Sessões legadas mantidas para exportação/fallback quando não há paciente ativo.
const LEGACY_SESSION_IDS = ["T0", "T2", "T3", "T4"];
const SESSION_PLAN_OPTIONS = [3, 4, 5, 6];

const LESIONS = [
  { code: "L1", short: "L1", defaultLabel: "Lesão 1", ink: "#ea580c", stroke: "#fb923c", soft: "rgba(251,146,60,0.18)" },
  { code: "L2", short: "L2", defaultLabel: "Lesão 2", ink: "#db2777", stroke: "#f472b6", soft: "rgba(244,114,182,0.18)" },
  { code: "L3", short: "L3", defaultLabel: "Lesão 3", ink: "#0e7490", stroke: "#22d3ee", soft: "rgba(34,211,238,0.18)" },
  { code: "L4", short: "L4", defaultLabel: "Lesão 4", ink: "#4d7c0f", stroke: "#a3e635", soft: "rgba(163,230,53,0.18)" },
];

// Códigos antigos (E1_maior…) continuam legíveis para bancos já existentes.
const LEGACY_CODES = {
  E1_maior: "L1", E2_maior: "L2", E3_menor: "L3", E4_menor: "L4",
  E1: "L1", E2: "L2", E3: "L3", E4: "L4",
};

const LANDMARK_SUGGESTIONS = [
  "Umbigo", "Nevo lateral D", "Nevo lateral E", "Cicatriz",
  "Borda costal", "Crista ilíaca", "Linha média", "Referência lateral",
];

const GAIS_OPTIONS = [
  { value: -1, label: "Muito pior", short: "Pior" },
  { value: 0, label: "Sem mudança", short: "Igual" },
  { value: 1, label: "Melhor", short: "Melhor" },
  { value: 2, label: "Muito melhor", short: "M.melhor" },
  { value: 3, label: "Extremamente melhor", short: "Ótimo" },
];

const STEP_INFO = {
  1: {
    kicker: "Passo 1 de 4",
    title: "Upload do par fotográfico",
    hint: "Envie a foto com régua e a foto limpa da sessão. A data é lida do EXIF ou pode ser informada manualmente.",
    banner: "<b>Upload.</b> Selecione as imagens da sessão à direita para visualizá-las no palco clínico.",
  },
  2: {
    kicker: "Passo 2 de 4",
    title: "Calibração métrica (px/mm)",
    hint: "Clique em dois pontos conhecidos da régua. Clique sobre um ponto marcado para removê-lo individualmente.",
    banner: "<b>Calibração.</b> Clique no início e no fim de um trecho da régua (ex.: 50 mm). Clique num ponto para removê-lo.",
  },
  3: {
    kicker: "Passo 3 de 4",
    title: "Alinhamento anatômico e fotométrico",
    hint: "Nomeie os pontos anatômicos, desenhe a ROI de pele saudável e processe o alinhamento.",
    banner: "<b>Alinhamento.</b> Clique nos pontos anatômicos em ordem e nomeie cada um. Clique num ponto para removê-lo.",
  },
  4: {
    kicker: "Passo 4 de 4",
    title: "Morfometria das regiões-alvo",
    hint: "Selecione a lesão, clique nos vértices do polígono e feche a região para medir comprimento, largura, área e ΔE00.",
    banner: "<b>Medição.</b> Clique para adicionar vértices · duplo clique ou <b>Fechar polígono</b> para concluir.",
  },
};

/* --------------------------------------------------------------------------
   Estado da aplicação
   -------------------------------------------------------------------------- */
const state = {
  health: null,
  auth: { configured: false, authenticated: false, ready: false, token: null },
  recovery: { configured: false, keyId: null, createdAt: null },
  keyFile: null,
  keyFileDone: null,
  dmkeyKey: "",
  screen: "lock",
  patients: [],
  patientQuery: "",
  patientFilter: "all",
  patientSigil: true,
  profile: { professional_name: "", professional_registry: "", institution: "" },
  patient: null,
  unlockTarget: null,
  patientPin: {},
  personal: { patientId: null, data: null, password: "" },
  sessions: {},
  order: [],
  sessionId: null,
  step: 1,
  mode: "natural",
  knownMm: 50,
  calib: { p1: null, p2: null },
  landmarks: [],
  roi: null,
  roiDrawing: false,
  step3Preview: false,
  files: {},
  polygon: [],
  polygonActive: false,
  stria: "L1",
  importedRois: null,
  loupe: true,
  spaceDown: false,
  hover: null,
  hoverHit: null,
  landmarkPopover: null,
  compare: { sessionId: "T2", mode: "natural", anon: false, pos: 50 },
  backupSummary: null,
  profileTab: "profile",
  patientTab: "manual",
  caseImport: { file: null, summary: null },
  rails: { left: false, right: false },
  canvasFocus: false,
  busy: false,
  update: { info: null, status: null, timer: null, busy: false },
  deletePatient: { id: null, code: "", name: "" },
};

const view = { zoom: 1, ox: 0, oy: 0, autoFit: true };
const imageCache = new Map();
let currentImage = null;
let currentImageUrl = null;
let renderToken = 0;
let drag = null;
let press = null;
let lastPolygonClick = null;
let canvasSize = { w: 0, h: 0 };
let pinch = null;
let wasPinching = false;
const touchPoints = new Map();

const DESKTOP_LAUNCH_TOKEN = (() => {
  try {
    const params = new URLSearchParams(window.location.search);
    const fromQuery = (params.get("launch_token") || "").trim();
    if (fromQuery) {
      sessionStorage.setItem("dm_launch_token", fromQuery);
      // Remove o token da barra de endereço: ele fica só na sessão do WebView2.
      try {
        const url = new URL(window.location.href);
        url.searchParams.delete("launch_token");
        window.history.replaceState(
          window.history.state,
          "",
          `${url.pathname}${url.search}${url.hash}`,
        );
      } catch {
        // URL opaca/file:// ou replaceState bloqueado: mantém a navegação.
      }
      return fromQuery;
    }
    return (sessionStorage.getItem("dm_launch_token") || "").trim();
  } catch {
    return "";
  }
})();

function withLaunchToken(url) {
  if (!url || !DESKTOP_LAUNCH_TOKEN) return url;
  const str = String(url);
  if (str.startsWith("blob:") || str.startsWith("data:")) return str;
  if (!str.startsWith("/api/")) return str;
  if (str.includes("launch_token=")) return str;
  const sep = str.includes("?") ? "&" : "?";
  return `${str}${sep}launch_token=${encodeURIComponent(DESKTOP_LAUNCH_TOKEN)}`;
}

const $ = (id) => document.getElementById(id);

/* --------------------------------------------------------------------------
   Helpers multi-paciente / sessões dinâmicas
   -------------------------------------------------------------------------- */
function sessionCode(session) {
  if (!session) return "";
  return String(session.code || session.session_code || session.id || "");
}

function sessionOrdinal(code) {
  const digits = String(code || "").replace(/[^0-9]/g, "");
  return digits ? Number(digits) : 999;
}

function isBaseline(session) {
  return sessionCode(session) === "T0";
}

function baselineSession() {
  return state.order
    .map((id) => state.sessions[id])
    .find((session) => isBaseline(session)) || null;
}

function sortSessions(list) {
  return [...list].sort((a, b) => {
    const byNumber = sessionOrdinal(sessionCode(a)) - sessionOrdinal(sessionCode(b));
    return byNumber !== 0 ? byNumber : sessionCode(a).localeCompare(sessionCode(b));
  });
}

function sessionIsEmpty(session) {
  if (!session) return false;
  return !session.clean_image_url
    && !session.ruler_image_url
    && !session.aligned_image_url
    && !session.enhanced_image_url
    && !(session.measurements || []).length
    && !session.captured_at;
}

function patientTotal(patient) {
  if (!patient) return 0;
  return Number(
    patient.sessions_total
    ?? patient.session_count
    ?? (Array.isArray(patient.sessions) ? patient.sessions.length : 0),
  ) || 0;
}

function patientDone(patient) {
  if (!patient) return 0;
  if (Number.isFinite(Number(patient.sessions_done))) return Number(patient.sessions_done);
  return (patient.sessions || []).filter((session) =>
    (session.measurements || []).length || session.aligned_image_url,
  ).length;
}

function isPatientLocked(patient) {
  return Boolean(patient && (patient.status === "locked" || patient.locked || patient.has_pin));
}

/* --------------------------------------------------------------------------
   Sigilo nominal (LGPD): iniciais no diretório, nome só no prontuário
   -------------------------------------------------------------------------- */
const NAME_PARTICLES = new Set(["de", "da", "do", "das", "dos", "e", "di", "du", "del", "della", "van", "von"]);

function nameWords(fullName) {
  return String(fullName || "")
    .split(/\s+/)
    .filter((word) => word && !NAME_PARTICLES.has(word.toLowerCase()));
}

function maskedPatientName(fullName) {
  return nameWords(fullName).map((word) => `${word[0].toUpperCase()}.`).join(" ");
}

function patientInitials(fullName) {
  return nameWords(fullName).map((word) => word[0].toUpperCase()).join("").slice(0, 2);
}

function patientMaskedName(patient) {
  if (!patient) return "";
  return patient.masked_name || maskedPatientName(patient.name) || "";
}

function patientAvatarText(patient) {
  if (!patient) return "?";
  return patient.name_initials || patientInitials(patient.name) || String(patient.code || "?").slice(0, 2).toUpperCase();
}

/** Nome a exibir no diretório: oculto por padrão e sempre oculto com PIN. */
function patientDirectoryName(patient) {
  const locked = isPatientLocked(patient);
  const masked = patientMaskedName(patient) || "Paciente sem nome";
  if (locked || state.patientSigil) return masked;
  return patient.name || masked;
}

function sessionEndpoint(session, suffix = "") {
  return `/api/sessions/${encodeURIComponent(session.id)}${suffix}`;
}

/* --------------------------------------------------------------------------
   Utilitários
   -------------------------------------------------------------------------- */
function activeSession() {
  return state.sessions[state.sessionId] || null;
}

/**
 * Verdadeiro quando o palco mostra a imagem alinhada/realçada de uma sessão de
 * seguimento. Nesse caso a imagem já foi reprojetada para o espaço de
 * coordenadas do T0 pela homografia, então os pontos de referência desenhados
 * precisam ser os de T0 (e não os da foto original desta sessão).
 */
function isAlignedPreview() {
  const session = activeSession();
  return Boolean(
    state.step === 3
    && state.step3Preview
    && session
    && !isBaseline(session)
    && session.aligned_image_url,
  );
}

/**
 * Verdadeiro quando o palco exibe uma sessão de seguimento no espaço de
 * coordenadas alinhado ao T0 (prévia alinhada/realçada ou etapa de medição).
 */
function isAlignedSpace() {
  const session = activeSession();
  if (!session || isBaseline(session) || !session.aligned_image_url) return false;
  if (state.step === 4) return true;
  return isAlignedPreview();
}

/** ROI de pele saudável registrada em T0, no espaço alinhado. */
function referenceHealthyRoi() {
  const t0 = baselineSession();
  const roi = t0?.healthy_skin_roi;
  return Array.isArray(roi) && roi.length === 4 ? roi.map(Number) : null;
}

/** ROIs (polígonos) medidas em T0, normalizadas para L1..L4. */
function referenceRois() {
  const t0 = baselineSession();
  if (!t0 || !Array.isArray(t0.measurements)) return [];
  const seen = new Set();
  const rois = [];
  t0.measurements.forEach((measurement) => {
    if (!Array.isArray(measurement.polygon) || measurement.polygon.length < 3) return;
    const code = lesionCode(measurement.stria_code);
    if (seen.has(code)) return;
    seen.add(code);
    rois.push({
      code,
      points: measurement.polygon.map((point) => [Number(point[0]), Number(point[1])]),
    });
  });
  return rois;
}

/** ROIs de T0 ainda não medidas nesta sessão, desenhadas como guia fantasma. */
function ghostRois() {
  if (!isAlignedSpace()) return [];
  const session = activeSession();
  const measured = new Set((session?.measurements || []).map((m) => lesionCode(m.stria_code)));
  const activeCode = state.polygon.length ? lesionCode(state.stria) : null;
  return referenceRois().filter((roi) => !measured.has(roi.code) && roi.code !== activeCode);
}

function lesionMeta(code) {
  const normalized = LEGACY_CODES[code] || code;
  return LESIONS.find((item) => item.code === normalized) || LESIONS[0];
}

function lesionCode(code) {
  return lesionMeta(code).code;
}

function lesionLabel(session, code, fallback) {
  const normalized = lesionCode(code);
  const labels = session?.lesion_labels || {};
  const baseline = baselineSession()?.lesion_labels || {};
  const inherited = session && !isBaseline(session)
    ? (baseline[normalized] || baseline[code])
    : null;
  const saved = labels[normalized] || labels[code] || inherited;
  if (saved && String(saved).trim()) return String(saved).trim();
  if (fallback && String(fallback).trim()) return String(fallback).trim();
  return lesionMeta(code).defaultLabel;
}

function measurementFor(session, code) {
  if (!session || !Array.isArray(session.measurements)) return null;
  return session.measurements.find((m) => lesionCode(m.stria_code) === lesionCode(code)) || null;
}

function formatDate(value) {
  if (!value) return "";
  const match = String(value).match(/^(\d{4})-(\d{2})-(\d{2})/);
  return match ? `${match[3]}/${match[2]}/${match[1]}` : String(value);
}

function dateInputValue(value) {
  return value ? String(value).slice(0, 10) : "";
}

function fmt(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
  return Number(value).toLocaleString("pt-BR", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function bytesLabel(bytes) {
  if (!bytes) return "";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let n = bytes;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return `${n.toFixed(n >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function toast(message, kind = "info", timeout = 4200) {
  const stack = $("toast-stack");
  if (!stack) return;
  const icons = {
    info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/></svg>',
    success: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>',
    error: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 8v5M12 16h.01"/><circle cx="12" cy="12" r="9"/></svg>',
  };
  const node = document.createElement("div");
  node.className = `toast is-${kind}`;
  node.innerHTML = `${icons[kind] || icons.info}<span>${esc(message)}</span>`;
  stack.appendChild(node);
  setTimeout(() => {
    node.classList.add("is-leaving");
    setTimeout(() => node.remove(), 200);
  }, timeout);
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (DESKTOP_LAUNCH_TOKEN) headers["X-DermaMetric-Launch"] = DESKTOP_LAUNCH_TOKEN;
  if (state.auth.token) headers.Authorization = `Bearer ${state.auth.token}`;
  const response = await fetch(path, { ...options, headers });
  let payload = null;
  const text = await response.text();
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = text; }
  }
  if (!response.ok) {
    const detail = payload && payload.detail;
    let message = typeof detail === "string" ? detail : null;
    if (Array.isArray(detail)) {
      message = detail.map((d) => d.msg || d.detail || JSON.stringify(d)).join(" · ");
    }
    throw new Error(message || `Falha na requisição (${response.status})`);
  }
  return payload;
}

function postJSON(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function patientHeaders(patientId) {
  const pin = state.patientPin[patientId];
  return pin ? { "X-Patient-Pin": pin } : {};
}
function patchJSON(path, body) {
  return api(path, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function putJSON(path, body) {
  return api(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function setBusy(flag, label) {
  state.busy = flag;
  document.body.classList.toggle("is-busy", flag);
  if (flag && label) toast(label, "info", 1800);
}

/* --------------------------------------------------------------------------
   Telas, autenticação e diretório de pacientes
   -------------------------------------------------------------------------- */
function setScreen(name) {
  state.screen = name;
  const lock = $("lock-screen");
  const directory = $("patient-directory");
  const shell = document.querySelector(".app-shell");
  const logout = $("auth-logout");
  const profileBtn = $("profile-open");
  const compareBtn = $("compare-open");
  if (lock) lock.hidden = name !== "lock";
  if (directory) directory.hidden = name !== "directory";
  if (shell) shell.hidden = name !== "workspace";
  if (logout) logout.hidden = name === "lock";
  if (profileBtn) profileBtn.hidden = name === "lock";
  if (compareBtn) compareBtn.hidden = name !== "workspace";
  document.body.classList.toggle("has-modal", name === "lock");
  // O foco no canvas é transitório: nunca fica preso ao sair do prontuário.
  if (name !== "workspace") exitCanvasFocus();
}

/* --------------------------------------------------------------------------
   Barras laterais recolhíveis e foco no canvas
   -------------------------------------------------------------------------- */
const RAIL_STORAGE_KEY = "dermametric.rails.v1";

function loadRailPreferences() {
  try {
    const raw = window.localStorage?.getItem(RAIL_STORAGE_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw);
    state.rails = {
      left: Boolean(parsed?.left),
      right: Boolean(parsed?.right),
    };
  } catch {
    state.rails = { left: false, right: false };
  }
}

function saveRailPreferences() {
  try {
    window.localStorage?.setItem(RAIL_STORAGE_KEY, JSON.stringify(state.rails));
  } catch {
    // localStorage indisponível (modo privado, cota): mantém apenas em memória.
  }
}

function isRailCollapsed(side) {
  return Boolean(state.rails[side] || state.canvasFocus);
}

function applyRailState() {
  const shell = document.querySelector(".app-shell");
  const collapsed = {
    left: isRailCollapsed("left"),
    right: isRailCollapsed("right"),
  };
  if (shell) {
    shell.classList.toggle("is-left-collapsed", collapsed.left);
    shell.classList.toggle("is-right-collapsed", collapsed.right);
  }
  ["left", "right"].forEach((side) => {
    const button = $(`rail-collapse-${side}`);
    if (!button) return;
    const area = side === "left" ? "esquerda" : "direita";
    const label = collapsed[side] ? "Expandir" : "Recolher";
    button.setAttribute("aria-expanded", String(!collapsed[side]));
    button.title = `${label} barra lateral ${area}`;
    button.setAttribute("aria-label", button.title);
    const text = button.querySelector(".rail-collapse-label");
    if (text) text.textContent = label;
  });
  const focus = $("canvas-focus");
  if (focus) {
    focus.classList.toggle("is-active", state.canvasFocus);
    focus.setAttribute("aria-pressed", String(state.canvasFocus));
    focus.title = state.canvasFocus
      ? "Sair do Foco no Canvas e restaurar as barras laterais"
      : "Foco no Canvas: recolhe as duas barras laterais";
  }
}

function setRailCollapsed(side, collapsed) {
  if (side !== "left" && side !== "right") return;
  state.rails[side] = Boolean(collapsed);
  saveRailPreferences();
  applyRailState();
}

function toggleRail(side) {
  if (state.canvasFocus) {
    // Ao ajustar uma lateral durante o foco, o foco termina respeitando o
    // estado visual atual e a barra clicada é invertida.
    state.canvasFocus = false;
    state.rails = { left: true, right: true };
    state.rails[side] = false;
    saveRailPreferences();
    applyRailState();
    return;
  }
  setRailCollapsed(side, !state.rails[side]);
}

function toggleCanvasFocus() {
  state.canvasFocus = !state.canvasFocus;
  applyRailState();
}

function exitCanvasFocus() {
  if (!state.canvasFocus) return;
  state.canvasFocus = false;
  applyRailState();
}

function resetWorkspaceRails() {
  state.canvasFocus = false;
  loadRailPreferences();
  applyRailState();
}

function showLockView(view) {
  setScreen("lock");
  const views = {
    setup: $("lock-setup"),
    login: $("lock-login"),
    recover: $("lock-recover"),
    keyfile: $("lock-keyfile"),
  };
  Object.entries(views).forEach(([name, node]) => {
    if (node) node.hidden = name !== view;
  });
  const panel = document.querySelector(".lock-panel");
  if (panel) panel.classList.toggle("is-wide", view === "recover" || view === "keyfile");
  const target = view === "setup"
    ? $("setup-password")
    : view === "login"
      ? $("login-password")
      : view === "recover"
        ? $("recover-key")
        : null;
  setTimeout(() => target?.focus(), 60);
}

function setLockError(id, message) {
  const box = $(id);
  if (!box) return;
  box.hidden = !message;
  box.textContent = message || "";
}

/* --------------------------------------------------------------------------
   Chave de recuperação e arquivo-chave (.dmkey)
   -------------------------------------------------------------------------- */
const DMK_REGEX = /^DMK(?:-[A-Z0-9]{4}){4}$/;
const DMK_ALPHABET = /^[A-Z0-9]{16}$/;

function formatTimestamp(value) {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return parsed.toLocaleString("pt-BR", {
    day: "2-digit", month: "2-digit", year: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

function normalizeDmk(raw) {
  const compact = String(raw || "").replace(/[^A-Za-z0-9]/g, "").toUpperCase();
  if (!compact.startsWith("DMK")) {
    throw new Error("Chave inválida: o código deve começar com DMK.");
  }
  const body = compact.slice(3);
  if (!DMK_ALPHABET.test(body)) {
    throw new Error("Chave inválida: use 16 caracteres (letras e números) após DMK.");
  }
  const groups = body.match(/.{4}/g) || [];
  return `DMK-${groups.join("-")}`;
}

function updateRecoveryState(payload) {
  if (!payload) return;
  const keyId = payload.key_id || payload.recovery_key_id || null;
  const createdAt = payload.created_at || null;
  state.recovery = {
    configured: true,
    keyId: keyId || state.recovery.keyId,
    createdAt: createdAt || state.recovery.createdAt,
  };
}

function keyFileFromPayload(payload) {
  const fallback = {
    app: "DermaMetric",
    type: "recovery_key",
    version: 1,
    key_id: payload?.key_id || state.recovery.keyId || "",
    created_at: payload?.created_at || new Date().toISOString(),
    institution: state.profile.institution || "",
    recovery_key: payload?.recovery_key || "",
  };
  const keyFile = payload?.key_file;
  if (!keyFile || typeof keyFile !== "object") return fallback;
  return {
    ...keyFile,
    institution: keyFile.institution || fallback.institution,
  };
}

function showKeyFileView(payload, options = {}) {
  const keyFile = keyFileFromPayload(payload);
  state.keyFile = keyFile;
  state.keyFileDone = typeof options.onDone === "function" ? options.onDone : null;
  const recoveryKey = keyFile.recovery_key || payload?.recovery_key || "";
  if ($("keyfile-title")) {
    $("keyfile-title").textContent = options.title || "Guarde sua chave de recuperação";
  }
  if ($("keyfile-lead")) {
    $("keyfile-lead").textContent = options.lead || (
      "Este arquivo é a única forma de recuperar os prontuários caso você "
      + "esqueça a senha mestra. Guarde-o fora deste computador, em local seguro."
    );
  }
  if ($("keyfile-code")) $("keyfile-code").textContent = recoveryKey || "—";
  if ($("keyfile-institution")) {
    $("keyfile-institution").textContent = keyFile.institution
      ? `Clínica: ${keyFile.institution}`
      : "Instituição não informada";
  }
  if ($("keyfile-date")) {
    $("keyfile-date").textContent = keyFile.created_at
      ? `Criada em ${formatTimestamp(keyFile.created_at)}`
      : "";
  }
  const checkbox = $("keyfile-confirm");
  if (checkbox) checkbox.checked = false;
  const done = $("keyfile-done");
  if (done) done.disabled = true;
  updateRecoveryState(payload || keyFile);
  showLockView("keyfile");
}

function finishKeyFileStep() {
  const next = state.keyFileDone;
  state.keyFileDone = null;
  if (typeof next === "function") {
    Promise.resolve(next()).catch((error) => toast(error.message, "error"));
  }
}

function downloadKeyFile() {
  const keyFile = state.keyFile;
  if (!keyFile?.recovery_key) {
    toast("Nenhum arquivo-chave disponível para download.", "error");
    return;
  }
  const payload = {
    app: keyFile.app || "DermaMetric",
    type: keyFile.type || "recovery_key",
    version: keyFile.version || 1,
    key_id: keyFile.key_id || "",
    created_at: keyFile.created_at || new Date().toISOString(),
    institution: keyFile.institution || "",
    recovery_key: keyFile.recovery_key,
  };
  const blob = new Blob([`${JSON.stringify(payload, null, 2)}\n`], {
    type: "application/json;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "dermametric-chave-recuperacao.dmkey";
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
  toast("Arquivo-chave .dmkey baixado.", "success");
}

async function copyRecoveryKey() {
  const code = ($("keyfile-code")?.textContent || "").trim();
  if (!code || code === "—") return;
  try {
    await navigator.clipboard.writeText(code);
    toast("Código copiado.", "success");
  } catch {
    const area = document.createElement("textarea");
    area.value = code;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    try {
      document.execCommand("copy");
      toast("Código copiado.", "success");
    } catch {
      toast("Não foi possível copiar automaticamente. Selecione o código.", "error");
    }
    area.remove();
  }
}

const DM_LOGO_SVG = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" width="46" height="46" role="img" aria-label="DermaMetric">
  <defs>
    <linearGradient id="dmPrintBg" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0" stop-color="#123131" />
      <stop offset="1" stop-color="#0c2323" />
    </linearGradient>
    <linearGradient id="dmPrintInk" x1="24%" y1="16%" x2="82%" y2="86%">
      <stop offset="0" stop-color="#18c9a8" />
      <stop offset="1" stop-color="#57f5cc" />
    </linearGradient>
    <mask id="dmPrintMask" maskUnits="userSpaceOnUse" x="0" y="0" width="128" height="128">
      <path d="M42 26 62 26C83.5 26 100 43 100 64C100 85 83.5 102 62 102L42 102C35 102 32 98.5 32 92L32 36C32 29.5 35 26 42 26Z" fill="#ffffff" />
      <g fill="none" stroke="#000000" stroke-width="4.5" stroke-linecap="round">
        <path d="M30 88C54 87 78 83 100 72" />
        <path d="M30 68C56 67 80 62 100 52" />
        <path d="M30 48C54 47 74 43 89 34" />
      </g>
    </mask>
  </defs>
  <rect x="1" y="1" width="126" height="126" rx="22%" fill="url(#dmPrintBg)" />
  <rect x="2.5" y="2.5" width="123" height="123" rx="26" fill="none" stroke="#6ee7d2" stroke-opacity="0.24" stroke-width="1.5" />
  <rect width="128" height="128" fill="url(#dmPrintInk)" mask="url(#dmPrintMask)" />
</svg>`;

function printRecoverySheet() {
  const keyFile = state.keyFile || {};
  const code = keyFile.recovery_key || ($("keyfile-code")?.textContent || "").trim();
  if (!code || code === "—") {
    toast("Nenhuma chave disponível para imprimir.", "error");
    return;
  }
  const institution = keyFile.institution || state.profile.institution || "Não informada";
  const created = keyFile.created_at ? formatTimestamp(keyFile.created_at) : "";
  const keyId = keyFile.key_id || state.recovery.keyId || "";
  const win = window.open("", "_blank", "width=760,height=920");
  if (!win) {
    toast("Permita janelas pop-up para imprimir a ficha de emergência.", "error");
    return;
  }
  const html = `<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8" />
<title>Ficha de emergência — DermaMetric</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: "Segoe UI", system-ui, sans-serif; color: #12222e; margin: 36px; line-height: 1.5; }
  .doc-head { display: flex; align-items: center; gap: 14px; margin-bottom: 12px; }
  .doc-head svg { width: 46px; height: 46px; flex: 0 0 auto; border-radius: 12px; }
  .doc-head .tag { margin: 0; }
  .tag { font-size: 11px; letter-spacing: .14em; text-transform: uppercase; color: #0f766e; font-weight: 700; }
  h1 { font-size: 24px; margin: 6px 0 2px; letter-spacing: -.02em; }
  .sub { color: #5b6b7b; font-size: 12px; margin-bottom: 24px; }
  .code { font-family: "Consolas", "Liberation Mono", monospace; font-size: 25px; letter-spacing: .1em;
          text-align: center; padding: 18px; border: 2px dashed #0f766e; border-radius: 10px;
          background: #f0fbf9; margin: 18px 0; word-break: break-all; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 22px; }
  td { padding: 6px 0; border-bottom: 1px solid #dfe7ee; }
  td:first-child { color: #5b6b7b; width: 38%; }
  h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .08em; color: #0f766e; margin: 22px 0 8px; }
  p { font-size: 12.5px; }
  .line { border-bottom: 1px solid #9fb0bd; height: 30px; }
  .note { margin-top: 26px; padding-top: 14px; border-top: 1px solid #dfe7ee; font-size: 11px; color: #5b6b7b; }
  @media print { body { margin: 18mm; } }
</style></head>
<body>
  <div class="doc-head">
    ${DM_LOGO_SVG}
    <div>
      <div class="tag">DermaMetric · documento confidencial</div>
      <h1>Ficha de emergência — Chave de Recuperação</h1>
    </div>
  </div>
  <div class="sub">Guarde este documento fora do computador da clínica. Ele permite redefinir a senha mestra sem perder os prontuários.</div>
  <div class="code">${esc(code)}</div>
  <table>
    <tr><td>Clínica / instituição</td><td>${esc(institution)}</td></tr>
    <tr><td>Identificador da chave</td><td>${esc(keyId || "—")}</td></tr>
    <tr><td>Gerada em</td><td>${esc(created || "—")}</td></tr>
  </table>
  <h2>Dica da senha mestra (opcional)</h2>
  <p>Anote apenas uma pista — nunca a senha completa.</p>
  <div class="line"></div><div class="line"></div>
  <h2>Como usar</h2>
  <p>Na tela de bloqueio do DermaMetric, clique em <strong>“Esqueci a senha”</strong>, selecione o arquivo
  <strong>dermametric-chave-recuperacao.dmkey</strong> (ou digite o código acima) e defina uma nova senha mestra.
  Depois, um novo arquivo-chave será gerado e este deixará de funcionar.</p>
  <div class="note">DermaMetric · estação clínica · dados de saúde protegidos (LGPD). Este documento não contém a senha mestra.</div>
</body></html>`;
  win.document.open();
  win.document.write(html);
  win.document.close();
  win.focus();
  setTimeout(() => {
    try { win.print(); } catch { /* usuário pode imprimir manualmente */ }
  }, 300);
}

function setDmkeyFeedback(message, kind) {
  const box = $("dmkey-feedback");
  if (!box) return;
  box.hidden = !message;
  box.textContent = message || "";
  box.classList.toggle("is-ok", kind === "ok");
  box.classList.toggle("is-error", kind === "error");
}

function validateDmkeyPayload(data) {
  if (!data || typeof data !== "object") {
    throw new Error("Arquivo inválido: conteúdo não é um JSON de arquivo-chave.");
  }
  if (data.app !== "DermaMetric" || data.type !== "recovery_key") {
    throw new Error("Arquivo inválido: não é um arquivo-chave do DermaMetric.");
  }
  if (Number(data.version) !== 1) {
    throw new Error("Versão de arquivo-chave não suportada. Atualize o DermaMetric.");
  }
  const key = normalizeDmk(data.recovery_key || "");
  if (data.key_id && state.recovery.keyId && data.key_id !== state.recovery.keyId) {
    throw new Error(
      "Este arquivo-chave pertence a outra clínica ou já foi substituído. "
      + "Use o .dmkey mais recente.",
    );
  }
  return { ...data, recovery_key: key };
}

async function handleDmkeyFile(file) {
  if (!file) return;
  try {
    if (file.size > 64 * 1024) {
      throw new Error("Arquivo muito grande para ser um arquivo-chave válido.");
    }
    let data;
    try {
      data = JSON.parse(await file.text());
    } catch {
      throw new Error("Não foi possível ler o arquivo. Selecione um .dmkey válido.");
    }
    const keyFile = validateDmkeyPayload(data);
    state.dmkeyKey = keyFile.recovery_key;
    if ($("recover-key")) $("recover-key").value = keyFile.recovery_key;
    setDmkeyFeedback(
      `Arquivo-chave válido carregado${keyFile.institution ? ` · ${keyFile.institution}` : ""}.`,
      "ok",
    );
    setLockError("recover-error", "");
  } catch (error) {
    state.dmkeyKey = "";
    if ($("recover-key")) $("recover-key").value = "";
    setDmkeyFeedback(error.message, "error");
  }
}

async function refreshRecoveryStatus() {
  try {
    const status = await api("/api/auth/status");
    const recovery = status?.recovery || {};
    state.recovery = {
      configured: Boolean(recovery.configured),
      keyId: recovery.key_id || null,
      createdAt: recovery.created_at || null,
    };
  } catch {
    /* mantém metadados em memória */
  }
  const meta = $("security-meta");
  if (!meta) return;
  if (!state.recovery.configured) {
    meta.textContent = "Recuperação ainda não configurada";
    return;
  }
  const parts = [];
  if (state.recovery.keyId) parts.push(`chave ${state.recovery.keyId}`);
  if (state.recovery.createdAt) parts.push(`criada em ${formatTimestamp(state.recovery.createdAt)}`);
  meta.textContent = parts.length ? `Chave ativa: ${parts.join(" · ")}` : "Chave ativa: recuperação configurada";
}

async function bootAuth() {
  try {
    const status = await api("/api/auth/status");
    state.auth.configured = Boolean(status?.configured);
    state.auth.token = status?.access_token || null;
    state.auth.authenticated = Boolean(
      state.auth.configured && status?.access_token,
    );
    const recovery = status?.recovery || {};
    state.recovery = {
      configured: Boolean(recovery.configured),
      keyId: recovery.key_id || null,
      createdAt: recovery.created_at || null,
    };
  } catch (error) {
    state.auth.configured = false;
    state.auth.authenticated = false;
    toast(`Não foi possível consultar o estado de acesso: ${error.message}`, "error", 6000);
  }
  state.auth.ready = true;
  if (!state.auth.configured) {
    showLockView("setup");
  } else if (!state.auth.authenticated) {
    showLockView("login");
  } else {
    await showDirectory();
  }
}

async function handleSetup() {
  const password = $("setup-password")?.value || "";
  const confirm = $("setup-confirm")?.value || "";
  setLockError("setup-error", "");
  if (password.length < 6) {
    setLockError("setup-error", "A senha precisa ter pelo menos 6 caracteres.");
    return;
  }
  if (password !== confirm) {
    setLockError("setup-error", "As senhas não conferem.");
    return;
  }
  setBusy(true);
  try {
    const response = await api(
      `/api/auth/setup?password=${encodeURIComponent(password)}`,
      { method: "POST" },
    );
    state.auth.configured = true;
    state.auth.token = response?.access_token || null;
    state.auth.authenticated = Boolean(response?.access_token ?? true);
    $("setup-password").value = "";
    $("setup-confirm").value = "";
    showKeyFileView(response, {
      title: "Guarde sua chave de recuperação",
      lead: "Este arquivo é a única forma de recuperar os prontuários caso "
        + "você esqueça a senha mestra. Baixe o .dmkey ou imprima a ficha e "
        + "guarde fora deste computador, em local seguro.",
      onDone: async () => {
        toast("Senha de acesso configurada.", "success");
        await showDirectory();
      },
    });
  } catch (error) {
    setLockError("setup-error", error.message);
  } finally {
    setBusy(false);
  }
}

async function handleLogin() {
  const password = $("login-password")?.value || "";
  setLockError("login-error", "");
  if (!password) {
    setLockError("login-error", "Informe a senha de acesso.");
    return;
  }
  setBusy(true);
  try {
    const response = await api(`/api/auth/login?password=${encodeURIComponent(password)}`, {
      method: "POST",
    });
    state.auth.token = response?.token || state.auth.token;
    state.auth.authenticated = true;
    $("login-password").value = "";
    const recovery = response?.recovery || {};
    state.recovery = {
      configured: Boolean(recovery.configured),
      keyId: recovery.key_id || null,
      createdAt: recovery.created_at || null,
    };
    if (response?.pending_recovery) {
      showKeyFileView(response.pending_recovery, {
        title: "Arquivo-chave da estação",
        lead: "Esta estação ainda não tinha um arquivo-chave de recuperação. "
          + "Um novo .dmkey foi gerado agora. Baixe e guarde em local seguro.",
        onDone: showDirectory,
      });
      return;
    }
    toast("Estação desbloqueada.", "success");
    await showDirectory();
  } catch (error) {
    setLockError("login-error", error.message);
    $("login-password")?.select();
  } finally {
    setBusy(false);
  }
}

async function handleRecover() {
  const typed = ($("recover-key")?.value || "").trim();
  const password = $("recover-password")?.value || "";
  const confirm = $("recover-confirm")?.value || "";
  setLockError("recover-error", "");
  let recoveryKey;
  try {
    recoveryKey = normalizeDmk(typed || state.dmkeyKey);
  } catch (error) {
    setLockError("recover-error", error.message);
    return;
  }
  if (password.length < 6) {
    setLockError("recover-error", "A nova senha precisa ter pelo menos 6 caracteres.");
    return;
  }
  if (password !== confirm) {
    setLockError("recover-error", "As senhas não conferem.");
    return;
  }
  setBusy(true, "Validando chave de recuperação…");
  try {
    const response = await postJSON("/api/auth/recover", {
      recovery_key: recoveryKey,
      new_password: password,
    });
    state.auth.configured = true;
    state.auth.token = response?.token || null;
    state.auth.authenticated = true;
    state.dmkeyKey = "";
    ["recover-key", "recover-password", "recover-confirm"].forEach((id) => {
      const input = $(id);
      if (input) input.value = "";
    });
    setDmkeyFeedback("", null);
    updateRecoveryState(response);
    showKeyFileView(response, {
      title: "Sua nova chave de recuperação",
      lead: "A senha mestra foi redefinida e a chave anterior deixou de funcionar. "
        + "Baixe o novo arquivo .dmkey e guarde antes de entrar.",
      onDone: async () => {
        toast("Acesso recuperado.", "success");
        await showDirectory();
      },
    });
  } catch (error) {
    setLockError("recover-error", error.message);
  } finally {
    setBusy(false);
  }
}

async function handleLogout() {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch (error) {
    toast(error.message, "error");
  }
  state.auth.authenticated = false;
  state.auth.token = null;
  state.patientPin = {};
  state.patient = null;
  state.patients = [];
  state.sessions = {};
  state.order = [];
  state.sessionId = null;
  state.unlockTarget = null;
  state.keyFile = null;
  state.keyFileDone = null;
  state.dmkeyKey = "";
  closePatientModal();
  closePinModal();
  closeLockPatientModal();
  showLockView("login");
  toast("Estação bloqueada.", "info", 2600);
}

async function showDirectory() {
  state.patient = null;
  state.sessions = {};
  state.order = [];
  state.sessionId = null;
  setScreen("directory");
  const logout = $("auth-logout");
  if (logout) logout.hidden = false;
  await loadProfile();
  await loadPatients();
}

async function loadProfile() {
  try {
    const data = await api("/api/profile");
    const profile = data?.profile || data || {};
    state.profile = {
      professional_name: String(profile.professional_name || ""),
      professional_registry: String(profile.professional_registry || ""),
      institution: String(profile.institution || ""),
    };
  } catch {
    state.profile = { professional_name: "", professional_registry: "", institution: "" };
  }
  renderProfileLabel();
}

function renderProfileLabel() {
  const label = $("profile-label");
  if (!label) return;
  const name = state.profile.professional_name.trim();
  const registry = state.profile.professional_registry.trim();
  if (name) {
    label.textContent = registry ? `${name} · ${registry}` : name;
  } else {
    label.textContent = "Configurar Profissional";
  }
}

async function loadPatients() {
  try {
    const data = await api("/api/patients");
    state.patients = Array.isArray(data?.patients) ? data.patients : [];
  } catch (error) {
    state.patients = [];
    toast(`Não foi possível carregar os pacientes: ${error.message}`, "error", 6000);
  }
  renderPatients();
}

function patientProgress(patient) {
  const total = patientTotal(patient);
  const done = patientDone(patient);
  if (!total) return 0;
  return Math.max(0, Math.min(100, Math.round((done / total) * 100)));
}

function formatPatientDate(value) {
  const parsed = value instanceof Date ? value : parsePatientDate(value);
  if (!parsed) return "—";
  const day = parsed.toLocaleDateString("pt-BR", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  });
  const time = parsed.toLocaleTimeString("pt-BR", {
    hour: "2-digit",
    minute: "2-digit",
  });
  return `${day} ${time}`;
}

function parsePatientDate(value) {
  if (!value) return null;
  const text = String(value).trim();
  if (!text) return null;
  let normalized = text;
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) {
    normalized = `${text}T00:00:00Z`;
  } else if (text.includes("T")) {
    if (!/(Z|[+-]\d{2}:?\d{2})$/.test(text)) normalized = `${text}Z`;
  } else {
    normalized = `${text.replace(" ", "T")}Z`;
  }
  const parsed = new Date(normalized);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

/** Atividade mais recente do paciente (paciente → criação → sessões). */
function patientUpdatedAt(patient) {
  if (!patient) return null;
  const candidates = [patient.updated_at, patient.created_at];
  (patient.sessions || []).forEach((session) => {
    candidates.push(session?.updated_at, session?.captured_at);
  });
  let newest = null;
  candidates.forEach((value) => {
    const parsed = parsePatientDate(value);
    if (parsed && (!newest || parsed > newest)) newest = parsed;
  });
  return newest;
}

function renderPatients() {
  const list = $("patient-list");
  const empty = $("patient-empty");
  const count = $("patient-count");
  if (!list) return;
  const query = normalizeHelpText(state.patientQuery.trim());
  const all = state.patients;
  const activeCount = all.filter((patient) => !isPatientLocked(patient)).length;
  const lockedCount = all.filter((patient) => isPatientLocked(patient)).length;

  if (count) {
    const total = all.length;
    count.textContent = `${total} paciente${total === 1 ? "" : "s"} · ${activeCount} em acompanhamento · ${lockedCount} protegido${lockedCount === 1 ? "" : "s"}`;
  }

  const summary = $("patient-summary");
  if (summary) {
    const cards = [
      { key: "all", label: "Total de Pacientes", value: all.length, tone: "neutral" },
      { key: "active", label: "Em Acompanhamento", value: activeCount, tone: "active" },
      { key: "locked", label: "Finalizados / Protegidos", value: lockedCount, tone: "locked" },
    ];
    summary.innerHTML = cards.map((card) => `
      <button type="button" class="summary-card is-${card.tone} ${state.patientFilter === card.key ? "is-selected" : ""}" data-filter="${card.key}" aria-pressed="${state.patientFilter === card.key}">
        <span class="summary-value">${card.value}</span>
        <span class="summary-label">${card.label}</span>
      </button>`).join("");
  }

  const counts = { all: all.length, active: activeCount, locked: lockedCount };
  Object.entries(counts).forEach(([key, value]) => {
    const node = $(`filter-count-${key}`);
    if (node) node.textContent = String(value);
  });
  document.querySelectorAll(".patient-filter").forEach((button) => {
    const active = button.dataset.filter === state.patientFilter;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", String(active));
  });

  const patients = all.filter((patient) => {
    const locked = isPatientLocked(patient);
    if (state.patientFilter === "active" && locked) return false;
    if (state.patientFilter === "locked" && !locked) return false;
    if (!query) return true;
    const revealed = !state.patientSigil && !locked;
    const haystack = normalizeHelpText([
      patient.code || "",
      patientMaskedName(patient),
      patientInitials(patient.name),
      revealed ? patient.name || "" : "",
    ].join(" "));
    return haystack.includes(query);
  });

  if (empty) {
    empty.hidden = patients.length > 0;
    const title = empty.querySelector("h3");
    const text = empty.querySelector("p");
    const filtering = Boolean(query) || state.patientFilter !== "all";
    if (title) title.textContent = filtering ? "Nenhum paciente encontrado" : "Nenhum paciente cadastrado";
    if (text) {
      text.innerHTML = filtering
        ? "Ajuste a busca ou o filtro selecionado para ver outros prontuários."
        : "Clique em <strong>+ Novo Paciente</strong> para criar o primeiro prontuário.";
    }
  }

  list.innerHTML = patients.map((patient) => {
    const locked = isPatientLocked(patient);
    const total = patientTotal(patient);
    const done = patientDone(patient);
    const progress = patientProgress(patient);
    const statusLabel = locked ? "Finalizado / Protegido" : "Em acompanhamento";
    const updated = formatPatientDate(patientUpdatedAt(patient) || patient.updated_at || patient.created_at);
    const updatedLabel = updated === "—" ? "" : `Atualizado em ${updated}`;
    const identity = patientDirectoryName(patient);
    const masked = locked || state.patientSigil;
    return `
      <div class="patient-card ${locked ? "is-locked" : ""}" data-patient-id="${esc(patient.id)}" role="button" tabindex="0" aria-label="Abrir prontuário ${esc(patient.code || "")}">
        <span class="patient-avatar" aria-hidden="true">${esc(patientAvatarText(patient))}</span>
        <span class="patient-main">
          <span class="patient-line patient-line-main">
            <strong class="patient-code-title">${esc(patient.code || "—")}</strong>
            ${locked ? '<span class="patient-lock" title="Protegido por PIN" aria-label="Protegido por PIN"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="10" width="16" height="11" rx="2.5" /><path d="M8 10V7a4 4 0 0 1 8 0v3" /></svg></span>' : ""}
            ${!locked && state.patientSigil ? '<span class="patient-sigil" title="Modo sigilo ativo" aria-label="Modo sigilo ativo">sigilo</span>' : ""}
            <button type="button" class="patient-personal" data-personal-id="${esc(patient.id)}" title="Ver / editar dados pessoais (senha mestra)" aria-label="Dados pessoais de ${esc(patient.code || "paciente")}">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" /><circle cx="12" cy="7" r="3.4" /><path d="M17.5 3.5 19 5l-2.6 2.6L15 6.1z" /></svg>
            </button>
            <button type="button" class="patient-personal patient-delete" data-delete-patient-id="${esc(patient.id)}" title="Excluir prontuário (senha mestra)" aria-label="Excluir prontuário ${esc(patient.code || "paciente")}">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18" /><path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" /><path d="M10 11v6M14 11v6" /></svg>
            </button>
          </span>
          <span class="patient-identity ${masked ? "is-masked" : ""}" title="${masked ? "Identidade protegida pelo modo sigilo" : esc(identity)}">${esc(identity)}</span>
          <span class="patient-meta">
            <span class="status-dot ${locked ? "is-locked" : ""}" aria-hidden="true"></span>
            ${esc(statusLabel)}
            ${updatedLabel ? `<span class="patient-sep">·</span>
            <span class="patient-updated">${esc(updatedLabel)}</span>` : ""}
          </span>
          <span class="patient-progress" aria-hidden="true">
            <span class="patient-progress-bar"><span style="width:${progress}%"></span></span>
            <span class="patient-progress-label">${done} de ${total} sess${total === 1 ? "ão" : "ões"}</span>
          </span>
        </span>
        <span class="patient-go" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6" /></svg>
        </span>
      </div>`;
  }).join("");
}

const EYE_ON_PATHS = '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" /><circle cx="12" cy="12" r="3" />';
const EYE_OFF_PATHS = '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94" /><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19" /><path d="M14.12 14.12a3 3 0 1 1-4.24-4.24" /><path d="m1 1 22 22" />';

function updatePrivacyToggle() {
  const button = $("privacy-toggle");
  if (!button) return;
  const sigil = state.patientSigil;
  button.classList.toggle("is-sigil", sigil);
  button.setAttribute("aria-pressed", String(sigil));
  button.title = sigil
    ? "Modo sigilo ativo: os nomes ficam ocultos"
    : "Nomes visíveis (prontuários com PIN continuam ocultos)";
  const label = $("privacy-toggle-label");
  if (label) label.textContent = sigil ? "Modo sigilo ativo" : "Exibir nomes";
  const svg = button.querySelector("svg");
  if (svg) svg.innerHTML = sigil ? EYE_OFF_PATHS : EYE_ON_PATHS;
}

function togglePatientSigil() {
  state.patientSigil = !state.patientSigil;
  updatePrivacyToggle();
  renderPatients();
}

function setPatientFilter(filter) {
  state.patientFilter = ["all", "active", "locked"].includes(filter) ? filter : "all";
  renderPatients();
}

async function openProfileModal() {
  const modal = $("profile-modal");
  if (!modal) return;
  hideRestoreBanner();
  setLockError("profile-error", "");
  setLockError("password-error", "");
  setLockError("recovery-error", "");
  setLockError("backup-export-error", "");
  setLockError("backup-restore-error", "");
  ["password-current", "password-new", "password-confirm", "recovery-password",
    "backup-export-password", "backup-restore-password"].forEach((id) => {
    const input = $(id);
    if (input) input.value = "";
  });
  const backupFile = $("backup-file");
  if (backupFile) backupFile.value = "";
  updateBackupFileStatus(null);
  const preview = $("backup-preview");
  if (preview) {
    preview.hidden = true;
    preview.innerHTML = "";
  }
  resetBackupProgress();
  state.backupSummary = null;
  switchProfileTab("profile");
  const result = $("recovery-result");
  if (result) result.hidden = true;
  if ($("profile-name")) $("profile-name").value = state.profile.professional_name || "";
  if ($("profile-registry")) $("profile-registry").value = state.profile.professional_registry || "";
  if ($("profile-institution")) $("profile-institution").value = state.profile.institution || "";
  modal.hidden = false;
  modal.setAttribute("aria-hidden", "false");
  document.body.classList.add("has-modal");
  await refreshRecoveryStatus();
  setTimeout(() => $("profile-name")?.focus(), 40);
}

function closeProfileModal() {
  const modal = $("profile-modal");
  if (!modal) return;
  modal.hidden = true;
  modal.setAttribute("aria-hidden", "true");
  if (state.screen !== "lock") document.body.classList.remove("has-modal");
}

async function submitProfile(event) {
  event?.preventDefault?.();
  const payload = {
    professional_name: ($("profile-name")?.value || "").trim(),
    professional_registry: ($("profile-registry")?.value || "").trim(),
    institution: ($("profile-institution")?.value || "").trim(),
  };
  setLockError("profile-error", "");
  setBusy(true, "Salvando perfil…");
  try {
    const data = await putJSON("/api/profile", payload);
    const profile = data?.profile || data || payload;
    state.profile = {
      professional_name: String(profile.professional_name || ""),
      professional_registry: String(profile.professional_registry || ""),
      institution: String(profile.institution || ""),
    };
    renderProfileLabel();
    closeProfileModal();
    toast(
      state.profile.professional_name
        ? "Perfil do profissional atualizado."
        : "Perfil limpo. O laudo voltará ao placeholder genérico.",
      "success",
    );
  } catch (error) {
    setLockError("profile-error", error.message);
  } finally {
    setBusy(false);
  }
}

async function submitChangePassword(event) {
  event?.preventDefault?.();
  const current = $("password-current")?.value || "";
  const next = $("password-new")?.value || "";
  const confirm = $("password-confirm")?.value || "";
  setLockError("password-error", "");
  if (!current) {
    setLockError("password-error", "Informe a senha mestra atual.");
    return;
  }
  if (next.length < 6) {
    setLockError("password-error", "A nova senha precisa ter pelo menos 6 caracteres.");
    return;
  }
  if (next !== confirm) {
    setLockError("password-error", "As senhas não conferem.");
    return;
  }
  setBusy(true, "Alterando senha mestra…");
  try {
    await postJSON("/api/auth/change-password", {
      current_password: current,
      new_password: next,
    });
    $("password-current").value = "";
    $("password-new").value = "";
    $("password-confirm").value = "";
    await refreshRecoveryStatus();
    toast("Senha mestra alterada.", "success");
  } catch (error) {
    setLockError("password-error", error.message);
  } finally {
    setBusy(false);
  }
}

async function regenerateRecoveryKeyFile() {
  const password = $("recovery-password")?.value || "";
  setLockError("recovery-error", "");
  if (!password) {
    setLockError("recovery-error", "Informe a senha mestra para gerar o arquivo-chave.");
    return;
  }
  setBusy(true, "Gerando novo arquivo-chave…");
  try {
    const data = await postJSON("/api/auth/regenerate-recovery-key", {
      master_password: password,
    });
    const keyFile = keyFileFromPayload(data);
    state.keyFile = keyFile;
    updateRecoveryState(data);
    downloadKeyFile();
    const result = $("recovery-result");
    const code = $("recovery-result-code");
    if (code) code.textContent = data.recovery_key || "";
    if (result) result.hidden = false;
    $("recovery-password").value = "";
    await refreshRecoveryStatus();
    toast("Novo arquivo-chave gerado. A chave anterior não funciona mais.", "success", 6000);
  } catch (error) {
    setLockError("recovery-error", error.message);
  } finally {
    setBusy(false);
  }
}

function openPatientModal() {
  const modal = $("patient-modal");
  if (!modal) return;
  setLockError("patient-form-error", "");
  $("patient-form")?.reset();
  const sessions = $("patient-sessions");
  if (sessions) sessions.value = "5";
  resetCaseImport();
  switchPatientTab("manual");
  modal.hidden = false;
  modal.setAttribute("aria-hidden", "false");
  document.body.classList.add("has-modal");
  setTimeout(() => $("patient-name")?.focus(), 40);
}

function closePatientModal() {
  const modal = $("patient-modal");
  if (!modal) return;
  modal.hidden = true;
  modal.setAttribute("aria-hidden", "true");
  resetCaseImport();
  if (state.screen !== "lock") document.body.classList.remove("has-modal");
}

/* --------------------------------------------------------------------------
   Abas do modal Novo Paciente: cadastro manual × importação de caso (.dmbak)
   -------------------------------------------------------------------------- */
const PATIENT_TABS = ["manual", "import"];

function switchPatientTab(tabName) {
  const name = PATIENT_TABS.includes(tabName) ? tabName : "manual";
  state.patientTab = name;
  document.querySelectorAll("[data-patient-tab]").forEach((button) => {
    const active = button.dataset.patientTab === name;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  });
  PATIENT_TABS.forEach((tab) => {
    const panel = tab === "manual" ? $("patient-form") : $("patient-panel-import");
    if (panel) panel.hidden = tab !== name;
  });
  const body = document.querySelector("#patient-modal .modal-body");
  if (body) body.scrollTop = 0;
}

function resetCaseImport() {
  state.caseImport = { file: null, summary: null };
  const input = $("case-import-file");
  if (input) input.value = "";
  const password = $("case-import-password");
  if (password) password.value = "";
  updateCaseImportFileStatus(null);
  setLockError("case-import-error", "");
  const preview = $("case-import-preview");
  if (preview) {
    preview.hidden = true;
    preview.innerHTML = "";
  }
  const conflict = $("case-import-conflict");
  if (conflict) conflict.hidden = true;
  const defaultStrategy = document.querySelector(
    'input[name="case-import-strategy"][value="new"]',
  );
  if (defaultStrategy) defaultStrategy.checked = true;
  setCaseImportProgress(0, "Preparando importação…");
  const progress = $("case-import-progress");
  if (progress) progress.hidden = true;
  const submit = $("case-import-submit");
  if (submit) {
    submit.hidden = true;
    submit.disabled = false;
  }
}

function updateCaseImportFileStatus(file) {
  const box = $("case-import-drop");
  const status = $("case-import-file-status");
  if (box) box.classList.toggle("has-file", Boolean(file));
  if (!status) return;
  if (file) {
    const sizeKb = Math.max(1, Math.round(file.size / 1024));
    status.textContent = `Arquivo carregado: ${file.name} (${sizeKb} KB)`;
  } else {
    status.textContent = "Nenhum arquivo selecionado. Clique acima para escolher ou arraste o .dmbak aqui.";
  }
}

function setCaseImportProgress(percent, label) {
  const value = Math.max(0, Math.min(100, Math.round(Number(percent) || 0)));
  const bar = $("case-import-bar");
  const text = $("case-import-percent");
  const status = $("case-import-status");
  if (bar) {
    bar.style.width = `${value}%`;
    bar.setAttribute("aria-valuenow", String(value));
  }
  if (text) text.textContent = `${value}%`;
  if (status && label) status.textContent = label;
}

function caseImportPreviewHtml(summary) {
  const professional = summary.professional || {};
  const counts = summary.counts || {};
  const code = summary.patient_code
    || (Array.isArray(summary.patient_codes) ? summary.patient_codes[0] : "")
    || "—";
  const created = formatTimestamp(summary.created_at) || summary.created_at || "—";
  const origin = [professional.name, professional.registry]
    .filter(Boolean)
    .join(" · ") || "Profissional não identificado";
  const institution = professional.institution
    ? `<span class="backup-origin-inst">${esc(professional.institution)}</span>`
    : "";
  return `
    <div class="backup-card-head">
      <span class="backup-mode">Caso individual</span>
      <span class="backup-date">${esc(created)}</span>
    </div>
    <div class="backup-origin">${esc(origin)}${institution}</div>
    <div class="backup-stats">
      <span><b>${esc(String(counts.sessions ?? 0))}</b> sessões</span>
      <span><b>${esc(String(counts.measurements ?? 0))}</b> medições</span>
      <span><b>${esc(String(counts.images ?? 0))}</b> imagens</span>
    </div>
    <div class="backup-codes"><code>${esc(code)}</code></div>
  `;
}

function postFormWithProgress(path, form, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path);
    if (DESKTOP_LAUNCH_TOKEN) xhr.setRequestHeader("X-DermaMetric-Launch", DESKTOP_LAUNCH_TOKEN);
    if (state.auth.token) xhr.setRequestHeader("Authorization", `Bearer ${state.auth.token}`);
    xhr.upload?.addEventListener("progress", (event) => {
      if (event.lengthComputable) {
        onProgress?.(Math.round((event.loaded / event.total) * 100));
      }
    });
    xhr.addEventListener("load", () => {
      let payload = null;
      try {
        payload = xhr.responseText ? JSON.parse(xhr.responseText) : null;
      } catch { payload = null; }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(payload);
        return;
      }
      const detail = payload?.detail;
      const message = typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail.map((item) => item?.msg || item?.detail || "").filter(Boolean).join(" · ")
          : `Falha na requisição (${xhr.status})`;
      reject(new Error(message));
    });
    xhr.addEventListener("error", () => reject(new Error("Falha de rede ao importar o caso.")));
    xhr.send(form);
  });
}

function clearCaseImportPreview() {
  state.caseImport.summary = null;
  const preview = $("case-import-preview");
  if (preview) {
    preview.hidden = true;
    preview.innerHTML = "";
  }
  const conflict = $("case-import-conflict");
  if (conflict) conflict.hidden = true;
  const submit = $("case-import-submit");
  if (submit) submit.hidden = true;
  const progress = $("case-import-progress");
  if (progress) progress.hidden = true;
  setCaseImportProgress(0, "Preparando importação…");
}

async function validateCaseImport() {
  const file = state.caseImport.file || $("case-import-file")?.files?.[0];
  const password = $("case-import-password")?.value || "";
  setLockError("case-import-error", "");
  clearCaseImportPreview();
  if (!file) {
    setLockError("case-import-error", "Selecione o arquivo .dmbak do caso a importar.");
    return;
  }
  if (!password) {
    setLockError("case-import-error", "Informe a senha do arquivo (do caso ou mestra da estação).");
    return;
  }
  const form = new FormData();
  form.append("file", file);
  form.append("password", password);
  setBusy(true, "Validando o caso (.dmbak)…");
  try {
    const summary = await api("/api/backup/inspect", { method: "POST", body: form });
    if (summary.mode !== "patient" && summary.mode !== "selected") {
      setLockError(
        "case-import-error",
        "Este arquivo é um backup completo da estação. Para restaurá-lo, use Perfil → Backup e restauração.",
      );
      return;
    }
    state.caseImport.summary = summary;
    const preview = $("case-import-preview");
    if (preview) {
      preview.innerHTML = caseImportPreviewHtml(summary);
      preview.hidden = false;
    }
    const conflict = $("case-import-conflict");
    if (conflict) conflict.hidden = !summary.has_conflicts;
    if (summary.has_conflicts) {
      const defaultStrategy = document.querySelector(
        'input[name="case-import-strategy"][value="new"]',
      );
      if (defaultStrategy) defaultStrategy.checked = true;
    }
    const submit = $("case-import-submit");
    if (submit) submit.hidden = false;
    const code = summary.patient_code || "caso";
    toast(
      summary.has_conflicts
        ? `Caso ${code} válido. O código já existe nesta estação: escolha como importar.`
        : `Caso ${code} válido e pronto para importar.`,
      "success",
      5200,
    );
  } catch (error) {
    setLockError("case-import-error", error.message);
  } finally {
    setBusy(false);
  }
}

async function submitCaseImport(event) {
  event?.preventDefault?.();
  const file = state.caseImport.file || $("case-import-file")?.files?.[0];
  const password = $("case-import-password")?.value || "";
  const summary = state.caseImport.summary;
  setLockError("case-import-error", "");
  if (!file) {
    setLockError("case-import-error", "Selecione o arquivo .dmbak do caso a importar.");
    return;
  }
  if (!password) {
    setLockError("case-import-error", "Informe a senha do arquivo do caso.");
    return;
  }
  if (!summary) {
    setLockError("case-import-error", "Valide o caso antes de importar.");
    return;
  }
  const strategy = document.querySelector(
    'input[name="case-import-strategy"]:checked',
  )?.value || "new";
  const importAsNew = Boolean(summary.has_conflicts && strategy === "new");

  const form = new FormData();
  form.append("file", file);
  form.append("password", password);
  form.append("mode", "merge");
  form.append("import_as_new", importAsNew ? "true" : "false");

  const submit = $("case-import-submit");
  const progress = $("case-import-progress");
  if (progress) progress.hidden = false;
  if (submit) submit.disabled = true;
  setCaseImportProgress(5, "Enviando o arquivo do caso…");
  setBusy(true, "Importando caso (.dmbak)…");
  try {
    const result = await postFormWithProgress("/api/backup/restore", form, (percent) => {
      setCaseImportProgress(
        Math.min(70, 5 + Math.round(percent * 0.65)),
        `Enviando o arquivo do caso… ${percent}%`,
      );
    });
    setCaseImportProgress(85, "Gravando prontuário, sessões e imagens…");
    await loadPatients();
    setCaseImportProgress(100, "Prontuário disponível no diretório");
    const imported = Array.isArray(result?.imported_patients) ? result.imported_patients : [];
    const target = imported.find((item) => item.status === "created_copy")
      || imported.find((item) => item.status === "created")
      || imported[0]
      || null;
    const code = target?.patient_code || summary.patient_code || "";
    const copyLabel = target?.status === "created_copy"
      ? ` como novo prontuário${code ? ` ${code}` : ""}`
      : "";
    closePatientModal();
    toast(`Caso importado${copyLabel}.`, "success", 5200);
    if (target?.id) {
      await openPatient(target.id);
    }
  } catch (error) {
    setLockError("case-import-error", error.message);
    setCaseImportProgress(0, "Importação interrompida");
    if (submit) submit.disabled = false;
  } finally {
    setBusy(false);
  }
}

async function submitPatient(event) {
  event?.preventDefault?.();
  const name = ($("patient-name")?.value || "").trim();
  const notes = ($("patient-notes")?.value || "").trim();
  const sessions = Number($("patient-sessions")?.value || 4);
  setLockError("patient-form-error", "");
  if (!name) {
    setLockError("patient-form-error", "Informe o nome completo do paciente.");
    return;
  }
  if (!SESSION_PLAN_OPTIONS.includes(sessions)) {
    setLockError("patient-form-error", "Escolha entre 3 e 6 sessões.");
    return;
  }
  setBusy(true, "Criando prontuário…");
  try {
    // O código PAC-XXXXXX é gerado pelo backend (aleatório e único).
    const patient = await postJSON("/api/patients", { name, notes, sessions });
    closePatientModal();
    await loadPatients();
    toast(`Paciente ${patient.code} cadastrado com ${sessions} sessões.`, "success");
    await openPatient(patient.id, patient);
  } catch (error) {
    setLockError("patient-form-error", error.message);
  } finally {
    setBusy(false);
  }
}

function openPinModal(patient) {
  state.unlockTarget = patient;
  const modal = $("pin-modal");
  if (!modal) return;
  setLockError("pin-error", "");
  setLockError("pin-reset-error", "");
  const input = $("pin-input");
  if (input) input.value = "";
  ["pin-reset-password", "pin-reset-new"].forEach((id) => {
    const field = $(id);
    if (field) field.value = "";
  });
  const reset = $("pin-reset");
  if (reset) reset.hidden = true;
  const forgot = $("pin-forgot");
  if (forgot) forgot.hidden = false;
  modal.hidden = false;
  modal.setAttribute("aria-hidden", "false");
  document.body.classList.add("has-modal");
  setTimeout(() => input?.focus(), 40);
}

function togglePinReset(show) {
  const reset = $("pin-reset");
  const forgot = $("pin-forgot");
  if (!reset) return;
  const willShow = typeof show === "boolean" ? show : reset.hidden;
  reset.hidden = !willShow;
  if (forgot) forgot.hidden = willShow;
  if (willShow) setTimeout(() => $("pin-reset-password")?.focus(), 40);
}

async function resetPatientPin(remove) {
  const patient = state.unlockTarget;
  if (!patient) return;
  setLockError("pin-reset-error", "");
  const master = $("pin-reset-password")?.value || "";
  const newPin = ($("pin-reset-new")?.value || "").trim();
  if (!master) {
    setLockError("pin-reset-error", "Informe a senha mestra da estação.");
    return;
  }
  if (!remove && !/^\d{4,6}$/.test(newPin)) {
    setLockError("pin-reset-error", "O novo PIN deve ter de 4 a 6 dígitos numéricos.");
    return;
  }
  setBusy(true, remove ? "Removendo PIN…" : "Redefinindo PIN…");
  try {
    const data = await postJSON(
      `/api/patients/${encodeURIComponent(patient.id)}/reset-pin`,
      remove
        ? { master_password: master, remove_pin: true }
        : { master_password: master, new_pin: newPin },
    );
    const updated = data?.patient || patient;
    if (remove) {
      delete state.patientPin[patient.id];
      toast("PIN removido deste prontuário.", "success");
    } else {
      state.patientPin[patient.id] = newPin;
      toast("PIN redefinido.", "success");
    }
    closePinModal();
    await loadPatients();
    await openPatient(updated.id || patient.id, updated);
  } catch (error) {
    setLockError("pin-reset-error", error.message);
  } finally {
    setBusy(false);
  }
}

function closePinModal() {
  const modal = $("pin-modal");
  if (!modal) return;
  modal.hidden = true;
  modal.setAttribute("aria-hidden", "true");
  state.unlockTarget = null;
  if (state.screen !== "lock") document.body.classList.remove("has-modal");
}

async function confirmUnlock() {
  const patient = state.unlockTarget;
  const pin = ($("pin-input")?.value || "").trim();
  setLockError("pin-error", "");
  if (!patient) return;
  if (pin.length < 4) {
    setLockError("pin-error", "Informe o PIN de 4 a 6 dígitos.");
    return;
  }
  setBusy(true, "Validando PIN…");
  try {
    const response = await postJSON(
      `/api/patients/${encodeURIComponent(patient.id)}/unlock`,
      { pin },
    );
    const unlocked = response?.patient || patient;
    state.patientPin[patient.id] = pin;
    closePinModal();
    await openPatient(unlocked.id || patient.id, unlocked);
  } catch (error) {
    setLockError("pin-error", error.message);
    $("pin-input")?.select();
  } finally {
    setBusy(false);
  }
}

function openLockPatientModal() {
  if (!state.patient) return;
  const modal = $("lock-patient-modal");
  if (!modal) return;
  const input = $("lock-pin-input");
  if (input) input.value = "";
  modal.hidden = false;
  modal.setAttribute("aria-hidden", "false");
  document.body.classList.add("has-modal");
  setTimeout(() => input?.focus(), 40);
}

function closeLockPatientModal() {
  const modal = $("lock-patient-modal");
  if (!modal) return;
  modal.hidden = true;
  modal.setAttribute("aria-hidden", "true");
  if (state.screen !== "lock") document.body.classList.remove("has-modal");
}

/* --------------------------------------------------------------------------
   Dados pessoais (nome, nascimento, contato e notas) — senha mestra
   -------------------------------------------------------------------------- */
function findPatientById(patientId) {
  return (
    state.patients.find((item) => item.id === patientId)
    || (state.patient?.id === patientId ? state.patient : null)
  );
}

function setPersonalStep(unlocked) {
  const auth = $("personal-step-auth");
  const data = $("personal-step-data");
  if (auth) auth.hidden = unlocked;
  if (data) data.hidden = !unlocked;
}

function clearPersonalFields() {
  ["personal-name", "personal-birth", "personal-phone", "personal-notes"].forEach((id) => {
    const field = $(id);
    if (field) field.value = "";
  });
  const age = $("personal-age");
  if (age) age.textContent = "Idade: —";
  const meta = $("personal-meta");
  if (meta) meta.textContent = "";
}

function openPersonalDataModal(patientId) {
  const modal = $("personal-data-modal");
  if (!modal) return;
  const patient = findPatientById(patientId) || state.patient;
  if (!patient) {
    toast("Abra um prontuário antes de consultar os dados pessoais.", "error");
    return;
  }
  state.personal = { patientId: patient.id, data: null, password: "" };
  const code = $("personal-code");
  if (code) code.textContent = patient.code || "—";
  const password = $("personal-password");
  if (password) password.value = "";
  setLockError("personal-auth-error", "");
  setLockError("personal-save-error", "");
  clearPersonalFields();
  setPersonalStep(false);
  modal.hidden = false;
  modal.setAttribute("aria-hidden", "false");
  document.body.classList.add("has-modal");
  setTimeout(() => password?.focus(), 40);
}

function closePersonalDataModal() {
  const modal = $("personal-data-modal");
  if (!modal) return;
  modal.hidden = true;
  modal.setAttribute("aria-hidden", "true");
  state.personal = { patientId: null, data: null, password: "" };
  clearPersonalFields();
  const password = $("personal-password");
  if (password) password.value = "";
  setLockError("personal-auth-error", "");
  setLockError("personal-save-error", "");
  if (state.screen !== "lock") document.body.classList.remove("has-modal");
}

function updatePersonalAge() {
  const label = $("personal-age");
  if (!label) return;
  const birth = $("personal-birth")?.value || "";
  const born = parsePatientDate(birth);
  if (!born) {
    label.textContent = "Idade: —";
    return;
  }
  const today = new Date();
  let age = today.getFullYear() - born.getFullYear();
  if (
    today.getMonth() < born.getMonth()
    || (today.getMonth() === born.getMonth() && today.getDate() < born.getDate())
  ) {
    age -= 1;
  }
  label.textContent = age >= 0 ? `Idade: ${age} ano${age === 1 ? "" : "s"}` : "Idade: —";
}

function fillPersonalForm(data) {
  if (!data) return;
  const name = $("personal-name");
  if (name) name.value = data.name || "";
  const birth = $("personal-birth");
  if (birth) birth.value = data.birth_date || "";
  const phone = $("personal-phone");
  if (phone) phone.value = data.phone || data.contact || "";
  const notes = $("personal-notes");
  if (notes) notes.value = data.notes || "";
  updatePersonalAge();
  const meta = $("personal-meta");
  if (meta) {
    const pinLabel = data.has_pin ? "Protegido por PIN" : "Sem PIN";
    meta.textContent = [
      `Cadastrado em ${formatPatientDate(data.created_at)}`,
      `Atualizado em ${formatPatientDate(data.updated_at)}`,
      pinLabel,
    ].join(" · ");
  }
}

async function unlockPersonalData() {
  const patientId = state.personal.patientId;
  if (!patientId) return;
  const password = $("personal-password")?.value || "";
  setLockError("personal-auth-error", "");
  if (!password) {
    setLockError("personal-auth-error", "Informe a senha de acesso do(a) profissional.");
    return;
  }
  setBusy(true, "Validando senha mestra…");
  try {
    const response = await postJSON(
      `/api/patients/${encodeURIComponent(patientId)}/personal-data/unlock`,
      { master_password: password },
    );
    const data = response.patient || response.personal_data;
    state.personal.password = password;
    state.personal.data = data;
    fillPersonalForm(data);
    setPersonalStep(true);
    setTimeout(() => $("personal-name")?.focus(), 40);
  } catch (error) {
    setLockError("personal-auth-error", error.message || "Senha de acesso incorreta.");
  } finally {
    setBusy(false);
  }
}

function hidePersonalData() {
  state.personal.data = null;
  state.personal.password = "";
  const password = $("personal-password");
  if (password) password.value = "";
  clearPersonalFields();
  setLockError("personal-auth-error", "");
  setLockError("personal-save-error", "");
  setPersonalStep(false);
  setTimeout(() => $("personal-password")?.focus(), 40);
}

function applyPersonalDataToState(data) {
  if (!data?.id) return;
  const locked = Boolean(data.has_pin);
  const maskedName = data.masked_name || maskedPatientName(data.name);
  const initials = data.name_initials || patientInitials(data.name);

  const entry = state.patients.find((item) => item.id === data.id);
  if (entry) {
    entry.name = locked ? null : data.name;
    entry.masked_name = maskedName;
    entry.name_initials = initials;
    entry.updated_at = data.updated_at || entry.updated_at;
    entry.has_pin = locked;
    entry.locked = locked;
    entry.status = locked ? "locked" : "active";
    entry.notes = null;
    entry.birth_date = null;
    entry.phone = null;
  }

  if (state.patient?.id === data.id) {
    const unlockedPin = state.patientPin[data.id];
    state.patient.name = locked && !unlockedPin ? null : data.name;
    state.patient.masked_name = maskedName;
    state.patient.name_initials = initials;
    state.patient.updated_at = data.updated_at || state.patient.updated_at;
    state.patient.has_pin = locked;
    state.patient.locked = locked;
    state.patient.status = locked ? "locked" : "active";
  }
}

async function savePersonalData() {
  const patientId = state.personal.patientId;
  const password = state.personal.password;
  if (!patientId) return;
  if (!password) {
    setPersonalStep(false);
    return;
  }
  const name = ($("personal-name")?.value || "").trim();
  setLockError("personal-save-error", "");
  if (!name) {
    setLockError("personal-save-error", "Informe o nome completo do paciente.");
    return;
  }
  setBusy(true, "Salvando dados pessoais…");
  try {
    const response = await putJSON(
      `/api/patients/${encodeURIComponent(patientId)}/personal-data`,
      {
        master_password: password,
        name,
        birth_date: $("personal-birth")?.value || "",
        phone: ($("personal-phone")?.value || "").trim(),
        notes: ($("personal-notes")?.value || "").trim(),
      },
    );
    const data = response.patient || response.personal_data;
    state.personal.data = data;
    applyPersonalDataToState(data);
    fillPersonalForm(data);
    renderPatients();
    renderPatientHeader();
    toast("Dados pessoais atualizados.", "success");
  } catch (error) {
    setLockError("personal-save-error", error.message);
  } finally {
    setBusy(false);
  }
}

async function confirmFinishPatient() {
  const patient = state.patient;
  if (!patient) return;
  const pin = ($("lock-pin-input")?.value || "").trim();
  if (pin && !/^\d{4,6}$/.test(pin)) {
    toast("O PIN deve ter de 4 a 6 dígitos numéricos.", "error");
    return;
  }
  setBusy(true, "Finalizando atendimento…");
  try {
    await postJSON(`/api/patients/${encodeURIComponent(patient.id)}/lock`, { pin });
    closeLockPatientModal();
    toast(
      pin
        ? "Prontuário finalizado e protegido por PIN."
        : "Atendimento finalizado. Prontuário permanece em acompanhamento.",
      "success",
      4200,
    );
    await showDirectory();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setBusy(false);
  }
}

/* --------------------------------------------------------------------------
   Workspace por paciente
   -------------------------------------------------------------------------- */
function applyPatientPayload(payload) {
  if (!payload) return null;
  const patient = payload.patient && payload.patient.id ? payload.patient : payload;
  const sessions = Array.isArray(payload.sessions)
    ? payload.sessions
    : (Array.isArray(patient.sessions) ? patient.sessions : []);
  patient.sessions = sessions;
  state.patient = patient;
  state.sessions = {};
  sessions.forEach((session) => {
    if (session?.id) state.sessions[session.id] = session;
  });
  state.order = sortSessions(sessions).map((session) => session.id);
  return patient;
}

async function openPatient(patientId, preloaded) {
  resetWorkspaceRails();
  setBusy(true, "Abrindo prontuário…");
  try {
    const data = preloaded || await api(`/api/patients/${encodeURIComponent(patientId)}`, {
      headers: patientHeaders(patientId),
    });
    const patient = applyPatientPayload(data);
    if (!patient) throw new Error("Paciente não encontrado.");
    if (!state.order.length) {
      // Fallback para bancos antigos sem sessões vinculadas ao paciente.
      const legacy = await api("/api/sessions");
      applyPatientPayload({
        ...patient,
        sessions: legacy.sessions || [],
      });
    }
    if (!state.order.length) {
      throw new Error("Este paciente ainda não possui sessões.");
    }
    if (!state.sessions[state.sessionId]) state.sessionId = state.order[0];
    state.step = 1;
    state.mode = "natural";
    state.step3Preview = false;
    state.polygon = [];
    state.polygonActive = false;
    syncToolsFromSession();
    setScreen("workspace");
    renderPatientHeader();
    renderSidebar();
    renderSessionTabs();
    renderScales();
    renderStepNav();
    renderStepPanel();
    renderResults();
    renderCompare();
    updateHeader();
    view.autoFit = true;
    renderCanvas();
  } catch (error) {
    toast(error.message, "error", 6000);
  } finally {
    setBusy(false);
  }
}

function renderPatientHeader() {
  const badge = $("workspace-patient");
  if (!badge) return;
  const patient = state.patient;
  if (!patient) {
    badge.hidden = true;
    badge.textContent = "";
    return;
  }
  badge.hidden = false;
  const locked = isPatientLocked(patient);
  const displayName = locked
    ? patient.masked_name || maskedPatientName(patient) || "Paciente"
    : patient.name || "";
  badge.innerHTML = `
    <span class="wp-code">${esc(patient.code || "—")}</span>
    <span class="wp-name">${esc(displayName)}</span>
    ${locked ? '<span class="wp-lock">PIN</span>' : ""}
  `;
}

async function refreshSessions() {
  if (state.patient) {
    const data = await api(`/api/patients/${encodeURIComponent(state.patient.id)}`, {
      headers: patientHeaders(state.patient.id),
    });
    applyPatientPayload(data);
  } else {
    const data = await api("/api/sessions");
    applyPatientPayload({ sessions: data.sessions || [] });
    state.order = sortSessions(data.sessions || []).map((session) => session.id);
  }
  if (!state.sessionId || !state.sessions[state.sessionId]) {
    state.sessionId = state.order[0] || null;
  }
  syncToolsFromSession();
  renderPatientHeader();
  renderSidebar();
  renderSessionTabs();
  renderScales();
  renderStepNav();
  renderStepPanel();
  renderResults();
  renderCompare();
  view.autoFit = true;
  renderCanvas();
}

async function addSession() {
  if (!state.patient) {
    toast("Abra um paciente antes de adicionar sessões.", "error");
    return;
  }
  const before = new Set(state.order);
  setBusy(true, "Adicionando sessão…");
  try {
    const patient = await api(
      `/api/patients/${encodeURIComponent(state.patient.id)}/sessions`,
      { method: "POST" },
    );
    applyPatientPayload(patient);
    const added = state.order.find((id) => !before.has(id));
    if (added) {
      state.sessionId = added;
      state.step = 1;
      state.mode = "natural";
      syncToolsFromSession();
      renderSidebar();
      renderSessionTabs();
      renderScales();
      renderStepNav();
      renderStepPanel();
      renderResults();
      renderCompare();
      view.autoFit = true;
      renderCanvas();
      updateHeader();
    }
    toast(
      added
        ? `Sessão ${sessionCode(state.sessions[added])} adicionada.`
        : "Sessão adicionada.",
      "success",
    );
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setBusy(false);
  }
}

async function removeSession(sessionId) {
  const session = state.sessions[sessionId];
  if (!state.patient || !session) return;
  const code = sessionCode(session);
  if (code === "T0") {
    toast("A sessão T0 (baseline) não pode ser removida.", "error");
    return;
  }
  if (!sessionIsEmpty(session)) {
    toast(`A sessão ${code} possui dados e não pode ser removida.`, "error");
    return;
  }
  if (!window.confirm(`Remover a sessão vazia ${code}?`)) return;
  setBusy(true, "Removendo sessão…");
  try {
    const patient = await api(
      `/api/patients/${encodeURIComponent(state.patient.id)}/sessions/${encodeURIComponent(sessionId)}`,
      { method: "DELETE" },
    );
    applyPatientPayload(patient);
    if (!state.sessions[state.sessionId]) state.sessionId = state.order[0] || null;
    syncToolsFromSession();
    renderPatientHeader();
    renderSidebar();
    renderSessionTabs();
    renderScales();
    renderStepNav();
    renderStepPanel();
    renderResults();
    renderCompare();
    view.autoFit = true;
    renderCanvas();
    toast(`Sessão ${code} removida.`, "success", 2600);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setBusy(false);
  }
}

function renderSessionTabs() {
  const tabs = $("session-tabs");
  if (!tabs) return;
  const lastId = state.order[state.order.length - 1];
  tabs.innerHTML = state.order.map((id) => {
    const session = state.sessions[id];
    if (!session) return "";
    const code = sessionCode(session);
    const active = id === state.sessionId;
    const removable = code !== "T0" && id === lastId && sessionIsEmpty(session);
    return `
      <span class="session-tab-wrap ${active ? "is-active" : ""}">
        <button type="button" class="session-tab" data-session="${esc(id)}" aria-pressed="${active}">
          ${esc(code)}
        </button>
        ${removable ? `<button type="button" class="session-tab-remove" data-remove-session="${esc(id)}" title="Remover sessão vazia ${esc(code)}" aria-label="Remover sessão vazia ${esc(code)}">×</button>` : ""}
      </span>`;
  }).join("");
}

/* --------------------------------------------------------------------------
   Landmarks (retrocompatível com [x, y] antigo)
   -------------------------------------------------------------------------- */
function normalizeLandmark(point, index) {
  if (Array.isArray(point)) {
    return { label: `Ponto ${index + 1}`, x: Number(point[0]), y: Number(point[1]) };
  }
  return {
    label: String(point?.label || `Ponto ${index + 1}`),
    x: Number(point?.x),
    y: Number(point?.y),
  };
}

function t0LandmarkNames() {
  const t0 = baselineSession();
  return Array.isArray(t0?.landmarks) ? t0.landmarks : [];
}

function expectedLandmarkLabel(index) {
  const names = t0LandmarkNames();
  const point = names[index];
  return point && point.label ? point.label : `Ponto ${index + 1}`;
}

/* --------------------------------------------------------------------------
   Carregamento de imagens e geometria do canvas
   -------------------------------------------------------------------------- */
function loadImage(url) {
  const resolvedUrl = withLaunchToken(url);
  if (imageCache.has(resolvedUrl)) return imageCache.get(resolvedUrl);
  const promise = new Promise((resolve, reject) => {
    const img = new Image();
    img.decoding = "async";
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("Não foi possível carregar a imagem."));
    img.src = resolvedUrl;
  });
  imageCache.set(resolvedUrl, promise);
  promise.catch(() => imageCache.delete(resolvedUrl));
  return promise;
}

function getDisplayImage() {
  const session = activeSession();
  if (!session) return { url: null, kind: "none", note: "Sessão indisponível." };

  if (state.step === 1) {
    if (session.ruler_image_url) return { url: session.ruler_image_url, kind: "ruler" };
    if (session.clean_image_url) return { url: session.clean_image_url, kind: "clean" };
    return { url: null, kind: "none", note: "Envie a foto com régua e a foto limpa desta sessão para iniciar o protocolo." };
  }
  if (state.step === 2) {
    return {
      url: session.ruler_image_url || null,
      kind: "ruler",
      note: "Esta sessão ainda não possui foto com régua. Volte ao passo 1 para enviar.",
    };
  }
  if (state.step === 3) {
    if (state.step3Preview && session.aligned_image_url) {
      const enhanced = state.mode === "enhanced" && session.enhanced_image_url;
      return {
        url: enhanced ? session.enhanced_image_url : session.aligned_image_url,
        kind: enhanced ? "enhanced" : "aligned",
      };
    }
    return {
      url: session.clean_image_url || null,
      kind: "clean",
      note: "Esta sessão ainda não possui foto limpa. Volte ao passo 1 para enviar.",
    };
  }

  const enhanced = state.mode === "enhanced";
  let url = session.aligned_image_url || null;
  if (enhanced && session.enhanced_image_url) url = session.enhanced_image_url;
  return {
    url,
    kind: enhanced ? "enhanced" : "aligned",
    note: session.clean_image_url
      ? "Registre o alinhamento no passo 3 para liberar a medição sobre a imagem alinhada."
      : "Envie as imagens no passo 1 e registre o alinhamento no passo 3.",
  };
}

function fitView(img, w, h) {
  const pad = 22;
  const zoom = Math.min((w - pad * 2) / img.naturalWidth, (h - pad * 2) / img.naturalHeight);
  view.zoom = Math.max(0.02, zoom);
  view.ox = (w - img.naturalWidth * view.zoom) / 2;
  view.oy = (h - img.naturalHeight * view.zoom) / 2;
}

function toScreen(point) {
  return { x: view.ox + point[0] * view.zoom, y: view.oy + point[1] * view.zoom };
}

function toImage(sx, sy) {
  return [(sx - view.ox) / view.zoom, (sy - view.oy) / view.zoom];
}

function canvasPointFromEvent(event) {
  const rect = $("stage").getBoundingClientRect();
  return { x: event.clientX - rect.left, y: event.clientY - rect.top };
}

function pointInImage(point) {
  if (!currentImage || !point) return false;
  return point[0] >= 0 && point[1] >= 0
    && point[0] <= currentImage.naturalWidth
    && point[1] <= currentImage.naturalHeight;
}

/* --------------------------------------------------------------------------
   Hit-test de pontos existentes (remoção individual)
   -------------------------------------------------------------------------- */
const HIT_RADIUS = 12;

function screenOfLandmark(point) {
  return toScreen([point.x, point.y]);
}

function hitTest(screenPoint) {
  const near = (target) => Math.hypot(screenPoint.x - target.x, screenPoint.y - target.y) <= HIT_RADIUS;

  if (state.step === 4) {
    for (let index = state.polygon.length - 1; index >= 0; index -= 1) {
      if (near(toScreen(state.polygon[index]))) {
        return { type: "vertex", index, label: `vértice ${index + 1}` };
      }
    }
  }
  if (state.step === 3 && !isAlignedPreview()) {
    for (let index = state.landmarks.length - 1; index >= 0; index -= 1) {
      if (near(screenOfLandmark(state.landmarks[index]))) {
        return { type: "landmark", index, label: state.landmarks[index].label };
      }
    }
  }
  if (state.step === 2) {
    if (state.calib.p2 && near(toScreen(state.calib.p2))) {
      return { type: "calib", index: 2, label: "ponto 2" };
    }
    if (state.calib.p1 && near(toScreen(state.calib.p1))) {
      return { type: "calib", index: 1, label: "ponto 1" };
    }
  }
  return null;
}

function removePoint(hit) {
  if (!hit) return;
  hideLandmarkPopover();
  if (hit.type === "vertex") {
    state.polygon.splice(hit.index, 1);
    updatePolygonStatus();
    scheduleRedraw();
  } else if (hit.type === "landmark") {
    state.landmarks.splice(hit.index, 1);
    renderStepPanel();
    scheduleRedraw();
  } else if (hit.type === "calib") {
    if (hit.index === 1) {
      state.calib = { p1: state.calib.p2, p2: null };
    } else {
      state.calib = { ...state.calib, p2: null };
    }
    renderStepPanel();
    scheduleRedraw();
  }
  state.hoverHit = null;
  updateHoverHint({ x: -999, y: -999 });
  toast("Ponto removido. Os demais foram preservados.", "info", 2600);
}

/* --------------------------------------------------------------------------
   Desenho do palco
   -------------------------------------------------------------------------- */
function marker(ctx, x, y, color, label, radius = 7, hot = false) {
  ctx.save();
  if (hot) {
    ctx.beginPath();
    ctx.arc(x, y, radius + 7, 0, Math.PI * 2);
    ctx.strokeStyle = "#f87171";
    ctx.lineWidth = 2.5;
    ctx.stroke();
  }
  ctx.beginPath();
  ctx.arc(x, y, radius + 2.5, 0, Math.PI * 2);
  ctx.fillStyle = "rgba(6,12,17,0.72)";
  ctx.fill();
  ctx.beginPath();
  ctx.arc(x, y, radius, 0, Math.PI * 2);
  ctx.fillStyle = color;
  ctx.fill();
  ctx.lineWidth = 1.6;
  ctx.strokeStyle = "rgba(255,255,255,0.92)";
  ctx.stroke();
  if (label) {
    ctx.fillStyle = "#052e2b";
    ctx.font = "700 9px Inter, system-ui, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(label, x, y + 0.5);
  }
  ctx.restore();
}

/** Nome dinâmico do ponto, desenhado sob o marcador no canvas. */
function markerCaption(ctx, x, y, text) {
  const value = String(text || "").trim();
  if (!value) return;
  const label = value.length > 22 ? `${value.slice(0, 21)}…` : value;
  ctx.save();
  ctx.font = "700 9px Inter, system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  const width = ctx.measureText(label).width + 10;
  roundRect(ctx, x - width / 2, y, width, 15, 4);
  ctx.fillStyle = "rgba(6,12,17,0.82)";
  ctx.fill();
  ctx.strokeStyle = "rgba(251,191,36,0.45)";
  ctx.lineWidth = 1;
  ctx.stroke();
  ctx.fillStyle = "#fde68a";
  ctx.fillText(label, x, y + 8);
  ctx.restore();
}

function drawOverlays(ctx) {
  const session = activeSession();
  if (!session) return;
  const zoom = view.zoom;

  if (state.step === 2) {
    const { p1, p2 } = state.calib;
    if (p1) {
      const a = toScreen(p1);
      marker(ctx, a.x, a.y, "#5eead4", "1", 7, state.hoverHit?.type === "calib" && state.hoverHit.index === 1);
      if (p2) {
        const b = toScreen(p2);
        marker(ctx, b.x, b.y, "#5eead4", "2", 7, state.hoverHit?.type === "calib" && state.hoverHit.index === 2);
        ctx.save();
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.strokeStyle = "#5eead4";
        ctx.lineWidth = 2;
        ctx.setLineDash([]);
        ctx.stroke();
        const px = Math.hypot(p2[0] - p1[0], p2[1] - p1[1]);
        const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
        const text = `${state.knownMm.toFixed(1)} mm · ${px.toFixed(1)} px`;
        ctx.font = "600 11px 'JetBrains Mono', monospace";
        const width = ctx.measureText(text).width + 14;
        ctx.fillStyle = "rgba(6,12,17,0.86)";
        ctx.strokeStyle = "rgba(94,234,212,0.55)";
        ctx.lineWidth = 1;
        roundRect(ctx, mid.x - width / 2, mid.y - 26, width, 20, 6);
        ctx.fill();
        ctx.stroke();
        ctx.fillStyle = "#a7f3d0";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(text, mid.x, mid.y - 15.5);
        ctx.restore();
      }
    }
  }

  if (state.step === 3) {
    if (isAlignedPreview()) drawGhostRois(ctx);
    const useReference = isAlignedPreview();
    const points = useReference
      ? (baselineSession()?.landmarks || []).map((point, index) => normalizeLandmark(point, index))
      : state.landmarks;
    points.forEach((point, index) => {
      const p = toScreen([point.x, point.y]);
      const hot = !useReference && state.hoverHit?.type === "landmark" && state.hoverHit.index === index;
      marker(ctx, p.x, p.y, "#fbbf24", String(index + 1), 9, hot);
      markerCaption(ctx, p.x, p.y + 19, point.label || `Ponto ${index + 1}`);
    });

    const roi = useReference
      ? referenceHealthyRoi()
      : (drag && drag.type === "roi" ? drag.roi : state.roi);
    if (roi) {
      const a = toScreen([roi[0], roi[1]]);
      const w = roi[2] * zoom;
      const h = roi[3] * zoom;
      ctx.save();
      ctx.fillStyle = "rgba(45,212,191,0.16)";
      ctx.strokeStyle = "#2dd4bf";
      ctx.lineWidth = 1.8;
      ctx.setLineDash([7, 5]);
      ctx.fillRect(a.x, a.y, w, h);
      ctx.strokeRect(a.x, a.y, w, h);
      ctx.setLineDash([]);
      ctx.fillStyle = "#a7f3d0";
      ctx.font = "700 10px Inter, system-ui, sans-serif";
      ctx.fillText("PELE SAUDÁVEL", a.x + 6, a.y + 14);
      ctx.restore();
    }
  }

  if (state.step === 4) {
    (session.measurements || []).forEach((m) => {
      const meta = lesionMeta(m.stria_code);
      drawPolygon(ctx, m.polygon, meta.stroke, meta.soft, lesionCode(m.stria_code), false);
    });
    drawGhostRois(ctx);
    const meta = lesionMeta(state.stria);
    if (state.polygon.length) {
      drawPolygon(ctx, state.polygon, meta.stroke, meta.soft, meta.short, true);
      if (state.polygonActive && state.hover && pointInImage(state.hover)) {
        const last = toScreen(state.polygon[state.polygon.length - 1]);
        const p = toScreen(state.hover);
        ctx.save();
        ctx.beginPath();
        ctx.moveTo(last.x, last.y);
        ctx.lineTo(p.x, p.y);
        ctx.strokeStyle = meta.stroke;
        ctx.lineWidth = 1.2;
        ctx.setLineDash([5, 4]);
        ctx.globalAlpha = 0.8;
        ctx.stroke();
        ctx.restore();
      }
    }
  }
}

function drawPolygon(ctx, points, color, soft, label, live) {
  if (!points || points.length === 0) return;
  const screen = points.map(toScreen);
  ctx.save();
  ctx.beginPath();
  screen.forEach((p, i) => (i === 0 ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y)));
  if (points.length >= 3) ctx.closePath();
  if (points.length >= 3) {
    ctx.fillStyle = soft;
    ctx.fill();
  }
  ctx.strokeStyle = color;
  ctx.lineWidth = live ? 2 : 1.7;
  if (live && points.length < 3) ctx.setLineDash([6, 4]);
  ctx.stroke();
  ctx.setLineDash([]);
  screen.forEach((p, index) => {
    const hot = live && state.hoverHit?.type === "vertex" && state.hoverHit.index === index;
    ctx.beginPath();
    ctx.arc(p.x, p.y, hot ? 6 : (live ? 4 : 3), 0, Math.PI * 2);
    ctx.fillStyle = hot ? "#f87171" : "#0a1016";
    ctx.fill();
    ctx.lineWidth = 1.6;
    ctx.strokeStyle = color;
    ctx.stroke();
  });
  if (label && screen.length) {
    const centroid = screen.reduce((acc, p) => ({ x: acc.x + p.x / screen.length, y: acc.y + p.y / screen.length }), { x: 0, y: 0 });
    ctx.font = "800 10px Inter, system-ui, sans-serif";
    const width = ctx.measureText(label).width + 10;
    ctx.fillStyle = "rgba(6,12,17,0.82)";
    roundRect(ctx, centroid.x - width / 2, centroid.y - 9, width, 18, 5);
    ctx.fill();
    ctx.fillStyle = color;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(label, centroid.x, centroid.y);
  }
  ctx.restore();
}

/** Contorno tracejado sutil de uma ROI de T0, no espaço de coordenadas alinhado. */
function drawGhostPolygon(ctx, points, meta) {
  if (!points || points.length < 3) return;
  const screen = points.map(toScreen);
  ctx.save();
  ctx.globalAlpha = 0.45;
  ctx.beginPath();
  screen.forEach((p, i) => (i === 0 ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y)));
  ctx.closePath();
  ctx.strokeStyle = meta.stroke;
  ctx.lineWidth = 1.4;
  ctx.setLineDash([4, 4]);
  ctx.stroke();
  ctx.setLineDash([]);
  const centroid = screen.reduce(
    (acc, p) => ({ x: acc.x + p.x / screen.length, y: acc.y + p.y / screen.length }),
    { x: 0, y: 0 },
  );
  const text = `${meta.code} (T0)`;
  ctx.font = "700 9px Inter, system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  const width = ctx.measureText(text).width + 10;
  ctx.fillStyle = "rgba(6,12,17,0.72)";
  roundRect(ctx, centroid.x - width / 2, centroid.y - 8, width, 16, 4);
  ctx.fill();
  ctx.fillStyle = meta.stroke;
  ctx.fillText(text, centroid.x, centroid.y);
  ctx.restore();
}

function drawGhostRois(ctx) {
  ghostRois().forEach((roi) => {
    drawGhostPolygon(ctx, roi.points, lesionMeta(roi.code));
  });
}

function roundRect(ctx, x, y, w, h, r) {
  const radius = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + radius, y);
  ctx.arcTo(x + w, y, x + w, y + h, radius);
  ctx.arcTo(x + w, y + h, x, y + h, radius);
  ctx.arcTo(x, y + h, x, y, radius);
  ctx.arcTo(x, y, x + w, y, radius);
  ctx.closePath();
}

async function renderCanvas() {
  const token = ++renderToken;
  const wrap = $("canvas-wrap");
  const canvas = $("stage");
  if (!wrap || !canvas) return;
  const w = wrap.clientWidth;
  const h = wrap.clientHeight;
  if (!w || !h) return;

  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  canvas.style.width = `${w}px`;
  canvas.style.height = `${h}px`;
  canvasSize = { w, h };

  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const target = getDisplayImage();
  const urlChanged = target.url !== currentImageUrl;
  currentImageUrl = target.url || null;
  updateModeSwitch();

  if (!target.url) {
    currentImage = null;
    showEmptyState(target.note);
    el("loupe").hidden = true;
    el("canvas-readout").textContent = "x — · y —";
    return;
  }

  hideEmptyState();
  let img;
  try {
    img = await loadImage(target.url);
  } catch (error) {
    if (token !== renderToken) return;
    showEmptyState(`Não foi possível carregar a imagem. ${error.message}`);
    return;
  }
  if (token !== renderToken) return;

  currentImage = img;
  if (urlChanged) view.autoFit = true;
  if (view.autoFit) fitView(img, w, h);

  drawScene(ctx, img, w, h);
  updateLoupeVisibility();
  if (state.hover) drawLoupe(state.hover);
}

function drawScene(ctx, img, w, h) {
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = "high";
  ctx.save();
  ctx.shadowColor = "rgba(0,0,0,0.45)";
  ctx.shadowBlur = 18;
  ctx.fillStyle = "#05090d";
  ctx.fillRect(view.ox, view.oy, img.naturalWidth * view.zoom, img.naturalHeight * view.zoom);
  ctx.restore();
  ctx.drawImage(img, view.ox, view.oy, img.naturalWidth * view.zoom, img.naturalHeight * view.zoom);
  drawOverlays(ctx);
  updateZoomLabel();
  void w; void h;
}

function updateZoomLabel() {
  const label = $("zoom-level");
  if (label) label.textContent = `${Math.round(view.zoom * 100)}%`;
}

function showEmptyState(note) {
  const empty = $("empty-state");
  if (!empty) return;
  empty.hidden = false;
  empty.innerHTML = `
    <span class="empty-icon">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
        <rect width="18" height="18" x="3" y="3" rx="2" /><circle cx="9" cy="9" r="2" /><path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21" />
      </svg>
    </span>
    <h3>${esc(STEP_INFO[state.step].title)}</h3>
    <p>${esc(note || "Nenhuma imagem disponível para esta etapa.")}</p>
    <button class="btn btn-primary btn-sm" type="button" data-action="goto-upload">Ir para o upload</button>
  `;
}

function hideEmptyState() {
  const empty = $("empty-state");
  if (empty) empty.hidden = true;
}

function updateModeSwitch() {
  const box = $("mode-switch");
  if (!box) return;
  const session = activeSession();
  const show = state.step === 4 && Boolean(session && session.aligned_image_url);
  box.hidden = !show;
  const wrap = $("canvas-wrap");
  if (wrap) wrap.classList.toggle("has-mode-switch", show);
  if (!show) return;
  const enhancedReady = Boolean(session.enhanced_image_url);
  box.innerHTML = `
    <button type="button" data-mode="natural" class="${state.mode === "natural" ? "is-active" : ""}">Natural alinhada</button>
    <button type="button" data-mode="enhanced" class="${state.mode === "enhanced" ? "is-active" : ""}" ${enhancedReady ? "" : "disabled"}>Realce CIELAB</button>
  `;
}

function updateLoupeVisibility() {
  const loupe = $("loupe");
  if (!loupe) return;
  loupe.hidden = !(state.loupe && currentImage && state.hover);
}

function drawLoupe(point) {
  const canvas = $("loupe-canvas");
  if (!canvas || !currentImage) return;
  const size = canvas.clientWidth || 180;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(size * dpr);
  canvas.height = Math.round(size * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, size, size);

  const scale = Math.max(1.2, view.zoom * 4);
  const source = size / scale;
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = "high";
  try {
    ctx.drawImage(
      currentImage,
      point[0] - source / 2,
      point[1] - source / 2,
      source,
      source,
      0, 0, size, size,
    );
  } catch {
    /* região fora da imagem — mantém o fundo */
  }

  ctx.save();
  ctx.strokeStyle = "rgba(94,234,212,0.9)";
  ctx.lineWidth = 1;
  const c = size / 2;
  const arm = 10;
  ctx.beginPath();
  ctx.moveTo(c - arm, c);
  ctx.lineTo(c + arm, c);
  ctx.moveTo(c, c - arm);
  ctx.lineTo(c, c + arm);
  ctx.stroke();
  ctx.strokeStyle = "rgba(255,255,255,0.28)";
  ctx.strokeRect(0.5, 0.5, size - 1, size - 1);
  ctx.restore();

  const session = activeSession();
  const coords = $("loupe-coords");
  if (coords) {
    const mm = session && session.px_per_mm
      ? ` · ${fmt(point[0] / session.px_per_mm, 1)} × ${fmt(point[1] / session.px_per_mm, 1)} mm`
      : "";
    coords.textContent = `x ${Math.round(point[0])} · y ${Math.round(point[1])}${mm}`;
  }
}

/* --------------------------------------------------------------------------
   Redesenho rápido (sem recarregar a imagem)
   -------------------------------------------------------------------------- */
function el(id) {
  return document.getElementById(id);
}

let rafId = 0;

function scheduleRedraw() {
  if (rafId) return;
  rafId = requestAnimationFrame(() => {
    rafId = 0;
    redraw();
  });
}

function redraw() {
  const canvas = $("stage");
  if (!canvas || !currentImage) return;
  const dpr = window.devicePixelRatio || 1;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, canvasSize.w, canvasSize.h);
  drawScene(ctx, currentImage, canvasSize.w, canvasSize.h);
}

function zoomAt(factor, sx, sy) {
  const previous = view.zoom;
  const next = Math.min(24, Math.max(0.02, previous * factor));
  if (next === previous) return;
  view.ox = sx - ((sx - view.ox) * next) / previous;
  view.oy = sy - ((sy - view.oy) * next) / previous;
  view.zoom = next;
  view.autoFit = false;
  scheduleRedraw();
}

function resetView() {
  if (!currentImage) return;
  view.autoFit = true;
  fitView(currentImage, canvasSize.w, canvasSize.h);
  scheduleRedraw();
}

/* --------------------------------------------------------------------------
   Interações do canvas
   -------------------------------------------------------------------------- */
function updateHoverHint(screenPoint) {
  const canvas = $("stage");
  const tip = $("canvas-tip");
  if (!canvas) return;
  const hit = hitTest(screenPoint);
  state.hoverHit = hit;

  if (hit && tip) {
    const wrap = $("canvas-wrap");
    const canvasRect = canvas.getBoundingClientRect();
    const wrapRect = wrap ? wrap.getBoundingClientRect() : canvasRect;
    tip.hidden = false;
    tip.textContent = hit.type === "landmark" && hit.label
      ? `Clique para remover: ${hit.label}`
      : "Clique para remover este ponto";
    tip.style.left = `${canvasRect.left - wrapRect.left + screenPoint.x}px`;
    tip.style.top = `${canvasRect.top - wrapRect.top + screenPoint.y}px`;
    canvas.style.cursor = "pointer";
  } else if (tip) {
    tip.hidden = true;
    canvas.style.cursor = state.spaceDown ? "grab" : "crosshair";
  }
}

function onPointerDown(event) {
  const canvas = $("stage");
  if (!canvas) return;
  const pt = canvasPointFromEvent(event);

  if (event.pointerType === "touch") {
    touchPoints.set(event.pointerId, { x: pt.x, y: pt.y });
    if (touchPoints.size === 2) {
      const points = [...touchPoints.values()];
      pinch = {
        dist: Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y),
        zoom: view.zoom,
      };
      wasPinching = true;
      press = null;
      drag = null;
      return;
    }
  }

  if (event.button === 1 || (state.spaceDown && event.button === 0)) {
    event.preventDefault();
    press = { x: pt.x, y: pt.y, moved: false, button: event.button };
    drag = { type: "pan", sx: pt.x, sy: pt.y, ox: view.ox, oy: view.oy };
    $("canvas-wrap").classList.add("is-panning");
    canvas.setPointerCapture?.(event.pointerId);
    return;
  }

  if (event.button === 0 && state.step === 3 && state.roiDrawing && !isAlignedPreview()) {
    event.preventDefault();
    const imagePoint = toImage(pt.x, pt.y);
    press = { x: pt.x, y: pt.y, moved: false, button: 0 };
    drag = {
      type: "roi",
      start: imagePoint,
      roi: [imagePoint[0], imagePoint[1], 0, 0],
    };
    canvas.setPointerCapture?.(event.pointerId);
    return;
  }

  if (event.button === 0) {
    press = { x: pt.x, y: pt.y, moved: false, button: 0 };
  }
}

function onPointerMove(event) {
  const canvas = $("stage");
  if (!canvas) return;
  const pt = canvasPointFromEvent(event);
  const imagePoint = toImage(pt.x, pt.y);

  if (event.pointerType === "touch" && touchPoints.has(event.pointerId)) {
    touchPoints.set(event.pointerId, { x: pt.x, y: pt.y });
  }

  if (pinch && touchPoints.size >= 2) {
    const points = [...touchPoints.values()];
    const distance = Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y);
    if (pinch.dist > 4 && distance > 4) {
      const center = { x: (points[0].x + points[1].x) / 2, y: (points[0].y + points[1].y) / 2 };
      const targetZoom = Math.min(24, Math.max(0.02, pinch.zoom * (distance / pinch.dist)));
      zoomAt(targetZoom / view.zoom, center.x, center.y);
    }
    return;
  }

  if (pointInImage(imagePoint)) {
    state.hover = imagePoint;
    updateHoverHint(pt);
  } else {
    state.hover = null;
    state.hoverHit = null;
    const tip = $("canvas-tip");
    if (tip) tip.hidden = true;
    canvas.style.cursor = "crosshair";
  }

  if (press && Math.hypot(pt.x - press.x, pt.y - press.y) > 4) press.moved = true;

  if (drag && drag.type === "pan") {
    press.moved = true;
    view.ox = drag.ox + (pt.x - drag.sx);
    view.oy = drag.oy + (pt.y - drag.sy);
    view.autoFit = false;
    scheduleRedraw();
  } else if (drag && drag.type === "roi") {
    const start = drag.start;
    const x = Math.min(start[0], imagePoint[0]);
    const y = Math.min(start[1], imagePoint[1]);
    const w = Math.abs(imagePoint[0] - start[0]);
    const h = Math.abs(imagePoint[1] - start[1]);
    drag.roi = [x, y, w, h];
    scheduleRedraw();
  } else if (state.step === 4 && state.polygonActive) {
    scheduleRedraw();
  }

  const readout = $("canvas-readout");
  if (readout) {
    const session = activeSession();
    const mm = session && session.px_per_mm
      ? `  ·  ${fmt(imagePoint[0] / session.px_per_mm, 1)} × ${fmt(imagePoint[1] / session.px_per_mm, 1)} mm`
      : "";
    readout.textContent = `x ${Math.round(imagePoint[0])} · y ${Math.round(imagePoint[1])}${mm}`;
  }

  if (state.loupe && currentImage && state.hover) {
    updateLoupeVisibility();
    drawLoupe(state.hover);
  } else {
    updateLoupeVisibility();
  }
}

function onPointerUp(event) {
  const canvas = $("stage");
  const wrap = $("canvas-wrap");
  const pt = canvasPointFromEvent(event);

  const wasTouch = event.pointerType === "touch";
  if (wasTouch) touchPoints.delete(event.pointerId);
  if (pinch && touchPoints.size < 2) {
    pinch = null;
    if (wasPinching) {
      wasPinching = false;
      press = null;
      return;
    }
  }
  if (wasPinching) return;

  if (drag && drag.type === "pan") {
    drag = null;
    wrap.classList.remove("is-panning");
    canvas.releasePointerCapture?.(event.pointerId);
    press = null;
    return;
  }

  if (drag && drag.type === "roi") {
    const roi = drag.roi;
    drag = null;
    state.roiDrawing = false;
    canvas.releasePointerCapture?.(event.pointerId);
    if (roi && roi[2] >= 6 && roi[3] >= 6) {
      const bounds = currentImage ? [currentImage.naturalWidth, currentImage.naturalHeight] : null;
      let [x, y, w, h] = roi;
      if (bounds) {
        x = Math.max(0, Math.min(bounds[0] - 1, x));
        y = Math.max(0, Math.min(bounds[1] - 1, y));
        w = Math.min(w, bounds[0] - x);
        h = Math.min(h, bounds[1] - y);
      }
      state.roi = [Math.round(x), Math.round(y), Math.max(1, Math.round(w)), Math.max(1, Math.round(h))];
      toast("ROI de pele saudável definida.", "success", 2600);
    } else {
      toast("Arraste uma área maior para definir a ROI.", "error");
    }
    renderStepPanel();
    scheduleRedraw();
    press = null;
    return;
  }

  if (press && !press.moved && event.button === 0) {
    handleCanvasClick(toImage(pt.x, pt.y));
  }
  press = null;
}

function onDoubleClick() {
  if (state.step === 4 && state.polygonActive && state.polygon.length >= 3) {
    closePolygon();
  }
}

function handleCanvasClick(point) {
  const session = activeSession();
  if (!session) return;

  if (!pointInImage(point)) return;

  // Duplo clique no palco encerra o polígono sem criar/remover vértices.
  if (state.step === 4 && state.polygonActive && state.polygon.length >= 3 && lastPolygonClick) {
    const elapsed = performance.now() - lastPolygonClick.t;
    const distance = Math.hypot(point[0] - lastPolygonClick.x, point[1] - lastPolygonClick.y);
    if (elapsed < 380 && distance < 8) {
      lastPolygonClick = null;
      closePolygon();
      return;
    }
  }

  const hit = hitTest(toScreen(point));
  if (hit) {
    removePoint(hit);
    return;
  }

  if (state.step === 1) {
    toast("Envie as imagens no painel do inspetor para começar.", "info", 2600);
    return;
  }

  if (state.step === 2) {
    if (!session.ruler_image_url) {
      toast("Envie a foto com régua milimetrada no passo 1.", "error");
      return;
    }
    if (!state.calib.p1 || state.calib.p2) {
      state.calib = { p1: [point[0], point[1]], p2: null };
    } else {
      state.calib.p2 = [point[0], point[1]];
    }
    renderStepPanel();
    scheduleRedraw();
    return;
  }

  if (state.step === 3) {
    if (!session.clean_image_url) {
      toast("Envie a foto limpa no passo 1.", "error");
      return;
    }
    if (state.step3Preview) {
      toast("Selecione “Original” para voltar a marcar pontos anatômicos.", "info", 3200);
      return;
    }
    if (state.roiDrawing) return;
    if (state.landmarks.length >= 4) {
      toast("Os 4 pontos anatômicos já estão definidos. Clique sobre um ponto para removê-lo.", "info");
      return;
    }
    const index = state.landmarks.length;
    const label = isBaseline(session)
      ? `Ponto ${index + 1}`
      : expectedLandmarkLabel(index);
    state.landmarks.push({ label, x: point[0], y: point[1] });
    state.hoverHit = null;
    renderStepPanel();
    scheduleRedraw();
    if (isBaseline(session)) {
      const screen = toScreen(point);
      openLandmarkPopover(index, screen.x, screen.y);
    } else {
      toast(`Ponto ${index + 1}: ${label}`, "info", 2400);
    }
    return;
  }

  if (state.step === 4) {
    if (!session.aligned_image_url) {
      toast("Registre o alinhamento no passo 3 antes de medir.", "error");
      return;
    }
    if (state.polygon.length >= 200) {
      toast("Limite de vértices atingido.", "error");
      return;
    }
    state.polygon.push([point[0], point[1]]);
    state.polygonActive = true;
    lastPolygonClick = { x: point[0], y: point[1], t: performance.now() };
    updatePolygonStatus();
    scheduleRedraw();
  }
}

function onWheel(event) {
  if (!currentImage) return;
  event.preventDefault();
  const pt = canvasPointFromEvent(event);
  const factor = Math.exp(-event.deltaY * 0.0015);
  zoomAt(factor, pt.x, pt.y);
}

function isTypingTarget(target) {
  if (!target) return false;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
}

function onKeyDown(event) {
  if (isTypingTarget(event.target)) return;
  if (event.code === "Space") {
    event.preventDefault();
    if (!state.spaceDown) {
      state.spaceDown = true;
      $("canvas-wrap").classList.add("is-pan-ready");
    }
  }
  if (event.key === "Escape") {
    state.polygon = [];
    state.polygonActive = false;
    lastPolygonClick = null;
    updatePolygonStatus();
    scheduleRedraw();
  }
}

function onKeyUp(event) {
  if (event.code === "Space") {
    state.spaceDown = false;
    const wrap = $("canvas-wrap");
    if (wrap) {
      wrap.classList.remove("is-pan-ready");
      wrap.classList.remove("is-panning");
    }
    if (drag && drag.type === "pan") drag = null;
  }
}

/* --------------------------------------------------------------------------
   Popover de nomeação de ponto (T0)
   -------------------------------------------------------------------------- */
function openLandmarkPopover(index, sx, sy) {
  const box = $("landmark-popover");
  if (!box) return;
  const point = state.landmarks[index];
  if (!point) return;
  state.landmarkPopover = { index };
  box.hidden = false;
  box.innerHTML = `
    <div class="lp-head"><span>Nomear ponto ${index + 1}</span><span>${Math.round(point.x)} · ${Math.round(point.y)} px</span></div>
    <input id="lp-input" type="text" maxlength="80" value="${esc(point.label)}" placeholder="ex.: Umbigo" list="landmark-suggestions" />
    <div class="lp-suggestions">
      ${LANDMARK_SUGGESTIONS.map((name) => `<button type="button" data-suggestion="${esc(name)}">${esc(name)}</button>`).join("")}
    </div>
    <div class="lp-actions">
      <button type="button" class="btn btn-primary btn-sm" data-action="save-landmark-label">Salvar nome</button>
      <button type="button" class="btn btn-danger-ghost btn-sm" data-action="remove-landmark" data-index="${index}">Remover</button>
    </div>
  `;
  const wrap = $("canvas-wrap");
  const width = wrap ? wrap.clientWidth : 0;
  const height = wrap ? wrap.clientHeight : 0;
  const popWidth = Math.min(270, Math.max(180, width - 16));
  const left = Math.max(8, Math.min(width - popWidth - 8, sx + 16));
  const top = Math.max(8, Math.min(height - 168, sy - 12));
  box.style.left = `${left}px`;
  box.style.top = `${top}px`;
  const input = $("lp-input");
  input?.focus();
  input?.select();
  input?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      applyLandmarkLabel();
    }
  });
  box.querySelectorAll("[data-suggestion]").forEach((button) => {
    button.addEventListener("click", () => {
      const field = $("lp-input");
      if (field) { field.value = button.dataset.suggestion; field.focus(); }
    });
  });
}

function hideLandmarkPopover() {
  const box = $("landmark-popover");
  if (box) box.hidden = true;
  state.landmarkPopover = null;
}

function applyLandmarkLabel() {
  const holder = state.landmarkPopover;
  const field = $("lp-input");
  if (!holder || !field) { hideLandmarkPopover(); return; }
  const point = state.landmarks[holder.index];
  if (point) point.label = field.value.trim() || `Ponto ${holder.index + 1}`;
  hideLandmarkPopover();
  renderStepPanel();
  scheduleRedraw();
}

/* --------------------------------------------------------------------------
   Sessões: sidebar e escalas
   -------------------------------------------------------------------------- */
function syncToolsFromSession() {
  const session = activeSession();
  if (!session) return;
  const points = session.ruler_points;
  if (points && points.p1 && points.p2) {
    state.calib = { p1: points.p1, p2: points.p2 };
    if (points.known_mm) state.knownMm = Number(points.known_mm);
  } else {
    state.calib = { p1: null, p2: null };
  }
  state.landmarks = Array.isArray(session.landmarks)
    ? session.landmarks.map((point, index) => normalizeLandmark(point, index))
    : [];
  state.roi = Array.isArray(session.healthy_skin_roi) ? [...session.healthy_skin_roi] : null;
  state.polygon = [];
  state.polygonActive = false;
  lastPolygonClick = null;
  state.hoverHit = null;
  state.landmarkPopover = null;
  state.roiDrawing = false;
  state.step3Preview = false;
  state.pendingGais = undefined;
  if (!session.enhanced_image_url && state.mode === "enhanced") state.mode = "natural";
}

function progressOf(session) {
  const has = (value) => value !== null && value !== undefined && value !== "";
  return [
    {
      label: "Upload",
      state: has(session.ruler_image_url) && has(session.clean_image_url)
        ? "done"
        : (has(session.ruler_image_url) || has(session.clean_image_url) ? "partial" : "todo"),
    },
    { label: "Calibrada", state: has(session.px_per_mm) ? "done" : "todo" },
    { label: "Alinhada", state: has(session.aligned_image_url) ? "done" : "todo" },
    { label: "Medida", state: (session.measurements || []).length ? "done" : "todo" },
  ];
}

function renderSidebar() {
  const list = $("session-list");
  if (!list) return;
  list.innerHTML = state.order.map((id) => {
    const session = state.sessions[id];
    if (!session) return "";
    const steps = progressOf(session);
    const date = formatDate(session.captured_at);
    const code = sessionCode(session);
    return `
      <button type="button" class="session-card ${id === state.sessionId ? "is-active" : ""}" data-session="${esc(id)}" aria-pressed="${id === state.sessionId}">
        <span class="session-top">
          <span class="session-id-wrap">
            <span class="session-id">${esc(code)}</span>
            <span class="session-badge">${isBaseline(session) ? "baseline" : "seguimento"}</span>
          </span>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>
        </span>
        <span class="session-label">${esc(session.label || "Sem rótulo")}</span>
        <span class="session-date">${date ? `Captura · ${date}` : "Data de captura não informada"}</span>
        <span class="progress-row">
          ${steps.map((s) => `<span class="progress-step ${s.state === "done" ? "is-done" : s.state === "partial" ? "is-partial" : ""}"><span class="dot"></span>${s.label}</span>`).join("")}
        </span>
      </button>
    `;
  }).join("");
}

function selectSession(id) {
  if (!state.sessions[id] || id === state.sessionId) return;
  state.sessionId = id;
  state.step = 1;
  state.mode = "natural";
  syncToolsFromSession();
  renderSidebar();
  renderSessionTabs();
  renderPatientHeader();
  renderScales();
  renderStepNav();
  renderStepPanel();
  renderResults();
  renderCompare();
  view.autoFit = true;
  renderCanvas();
  updateHeader();
}

function renderScales() {
  const box = $("scales-form");
  const badge = $("scales-session");
  const session = activeSession();
  if (!box || !session) return;
  if (badge) badge.textContent = sessionCode(session);
  const scales = session.clinical_scales || { end_pain: 0, likert_satisfaction: 3, gais_score: 0, notes: "" };
  const gais = Number(scales.gais_score) || 0;

  box.innerHTML = `
    <div class="scale-block">
      <div class="scale-label"><span>Dor percebida (END) ${helpBadge("help-glossary-end", "END: escala numérica de dor, de 0 (sem dor) a 10 (pior dor imaginável).")}</span><strong id="end-value">${Number(scales.end_pain) || 0}<small>/10</small></strong></div>
      <input id="end-input" type="range" min="0" max="10" step="1" value="${Number(scales.end_pain) || 0}" aria-label="Dor END 0 a 10" />
      <div class="scale-ticks"><span>0 · sem dor</span><span>10 · máxima</span></div>
    </div>
    <div class="scale-block">
      <div class="scale-label"><span>Satisfação (Likert) ${helpBadge("help-glossary-likert", "Likert: satisfação percebida pela paciente, de 1 (muito insatisfeita) a 5 (muito satisfeita).")}</span><strong id="likert-value">${Number(scales.likert_satisfaction) || 3}<small>/5</small></strong></div>
      <input id="likert-input" type="range" min="1" max="5" step="1" value="${Number(scales.likert_satisfaction) || 3}" aria-label="Satisfação Likert 1 a 5" />
      <div class="scale-ticks"><span>1 · insatisfeita</span><span>5 · muito satisfeita</span></div>
    </div>
    <div class="scale-block">
      <div class="scale-label"><span>GAIS ${helpBadge("help-glossary-gais", "GAIS: melhora estética global percebida, de −1 (piora) a +3 (melhora ótima).")}</span><strong id="gais-value">${gais > 0 ? "+" : ""}${gais}<small>/ ±3</small></strong></div>
      <div class="gais-scale" role="group" aria-label="GAIS de -1 a 3">
        ${GAIS_OPTIONS.map((option) => `
          <button type="button" data-gais="${option.value}" class="${option.value === gais ? "is-active" : ""}" aria-pressed="${option.value === gais}">
            <strong>${option.value > 0 ? "+" : ""}${option.value}</strong><span>${option.short}</span>
          </button>`).join("")}
      </div>
      <div class="scale-help">−1 = muito pior · 0 = sem mudança · +3 = extremamente melhor</div>
    </div>
    <div class="field">
      <span>Notas clínicas</span>
      <textarea id="notes-input" class="notes" rows="3" maxlength="4000" placeholder="Observações da consulta, intercorrências, adesão ao protocolo…">${esc(scales.notes || "")}</textarea>
    </div>
    <button type="button" class="btn btn-primary btn-block btn-sm" data-action="save-scales">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2Z"/><path d="M17 21v-8H7v8M7 3v5h8"/></svg>
      Salvar escalas de ${esc(sessionCode(session))}
    </button>
  `;

  const end = $("end-input");
  const likert = $("likert-input");
  if (end) {
    end.addEventListener("input", () => {
      $("end-value").innerHTML = `${end.value}<small>/10</small>`;
    });
  }
  if (likert) {
    likert.addEventListener("input", () => {
      $("likert-value").innerHTML = `${likert.value}<small>/5</small>`;
    });
  }
  box.querySelectorAll("[data-gais]").forEach((button) => {
    button.addEventListener("click", () => {
      state.pendingGais = Number(button.dataset.gais);
      box.querySelectorAll("[data-gais]").forEach((b) => b.classList.toggle("is-active", b === button));
      const value = Number(button.dataset.gais);
      $("gais-value").innerHTML = `${value > 0 ? "+" : ""}${value}<small>/ ±3</small>`;
    });
  });
}

async function saveScales() {
  const session = activeSession();
  if (!session) return;
  const payload = {
    end_pain: Number($("end-input")?.value ?? 0),
    likert_satisfaction: Number($("likert-input")?.value ?? 3),
    gais_score: Number(state.pendingGais ?? session.clinical_scales?.gais_score ?? 0),
    notes: $("notes-input")?.value ?? "",
  };
  try {
    const response = await postJSON(sessionEndpoint(session, "/clinical-scales"), payload);
    state.pendingGais = undefined;
    state.sessions[session.id] = response.session;
    renderSidebar();
    renderScales();
    toast(`Escalas de ${sessionCode(session)} salvas.`, "success");
  } catch (error) {
    toast(error.message, "error");
  }
}

/* --------------------------------------------------------------------------
   Cabeçalho, etapas e navegação
   -------------------------------------------------------------------------- */
function updateHeader() {
  const info = STEP_INFO[state.step];
  const kicker = $("step-kicker");
  const title = $("step-title");
  const hint = $("step-hint");
  const banner = $("tool-banner");
  if (kicker) kicker.textContent = info.kicker;
  if (title) title.textContent = info.title;
  if (hint) hint.textContent = info.hint;
  if (banner) banner.innerHTML = info.banner;
}

function setStep(step) {
  if (step === state.step) return;
  state.step = step;
  state.hover = null;
  state.hoverHit = null;
  hideLandmarkPopover();
  if (step !== 3) state.step3Preview = false;
  updateHeader();
  renderStepNav();
  renderStepPanel();
  renderResults();
  view.autoFit = true;
  renderCanvas();
}

function stepCompletion() {
  const session = activeSession();
  if (!session) return [false, false, false, false];
  return [
    Boolean(session.ruler_image_url && session.clean_image_url),
    Boolean(session.px_per_mm),
    Boolean(session.aligned_image_url),
    (session.measurements || []).length > 0,
  ];
}

function renderStepNav() {
  const nav = $("steps-nav");
  if (!nav) return;
  const done = stepCompletion();
  const names = ["Upload", "Calibrar", "Alinhar", "Medir"];
  nav.innerHTML = names.map((name, index) => {
    const step = index + 1;
    const classes = [
      "step-tab",
      state.step === step ? "is-active" : "",
      done[index] ? "is-done" : "",
    ].filter(Boolean).join(" ");
    return `
      <button type="button" class="${classes}" data-step="${step}" aria-current="${state.step === step ? "step" : "false"}">
        <span class="step-num">${done[index] ? "✓" : step}</span>
        <span class="step-name">${name}</span>
      </button>`;
  }).join("");
}

function stepPanelShell(title, subtitle, body) {
  return `
    <h2>${title}</h2>
    <p class="panel-sub">${subtitle}</p>
    <div class="step-body">${body}</div>
  `;
}

function renderStepPanel() {
  const panel = $("step-panel");
  if (!panel) return;
  const session = activeSession();
  if (!session) { panel.innerHTML = ""; return; }

  if (state.step === 1) panel.innerHTML = step1HTML(session);
  else if (state.step === 2) panel.innerHTML = step2HTML(session);
  else if (state.step === 3) panel.innerHTML = step3HTML(session);
  else panel.innerHTML = step4HTML(session);

  bindStepPanel();
}

/* --------------------------------------------------------------------------
   Passo 1 — Upload
   -------------------------------------------------------------------------- */
function thumbHTML(url, kind, title) {
  const resolvedUrl = url ? withLaunchToken(url) : "";
  const src = resolvedUrl ? `<img src="${esc(resolvedUrl)}" alt="${esc(title)}" loading="lazy" />` : `
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <path d="M21 15v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-3" /><path d="m17 8-5-5-5 5" /><path d="M12 3v12" />
    </svg>`;
  return `<span class="upload-thumb" data-thumb="${kind}">${src}</span>`;
}

function step1HTML(session) {
  const body = `
    <div class="upload-grid">
      <label class="upload-card" data-drop="ruler">
        <input id="ruler-file" type="file" accept="image/*" />
        ${thumbHTML(session.ruler_image_url, "ruler", "Foto com régua")}
        <span class="upload-meta">
          <strong>Foto com régua</strong>
          <span data-file-name="ruler">${session.ruler_image_url ? "Enviada · clique para trocar" : "PNG, JPG, WEBP ou TIFF"}</span>
        </span>
      </label>
      <label class="upload-card" data-drop="clean">
        <input id="clean-file" type="file" accept="image/*" />
        ${thumbHTML(session.clean_image_url, "clean", "Foto limpa")}
        <span class="upload-meta">
          <strong>Foto limpa</strong>
          <span data-file-name="clean">${session.clean_image_url ? "Enviada · clique para trocar" : "Sem régua, mesma sessão"}</span>
        </span>
      </label>
    </div>

    <div class="field">
      <span>Data da captura</span>
      <input id="captured-at" type="date" value="${dateInputValue(session.captured_at)}" />
      <small>Lida automaticamente do EXIF da foto. Se não houver EXIF, informe manualmente.</small>
    </div>

    <div class="field">
      <span>Rótulo da sessão</span>
      <input id="label-input" type="text" maxlength="120" value="${esc(session.label || "")}" placeholder="ex.: D28 pós-procedimento" />
      <small>Este rótulo aparece nos relatórios e na exportação CSV.</small>
    </div>

    <div class="form-actions">
      <button type="button" class="btn btn-primary btn-block" data-action="upload-images">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><path d="m17 8-5-5-5 5" /><path d="M12 3v12" /></svg>
        Enviar para ${esc(sessionCode(session))}
      </button>
      <button type="button" class="btn btn-ghost btn-block" data-action="save-meta">Salvar data e rótulo</button>
    </div>
    <p class="form-hint" id="upload-hint">O envio preserva as fotos anteriores até que você escolha novos arquivos.</p>
  `;
  return stepPanelShell("Par fotográfico da sessão", "As duas imagens devem ser do mesmo dia e enquadramento.", body);
}

function setUploadFile(kind, file) {
  if (!file) return;
  if (!file.type.startsWith("image/") && !/\.(jpe?g|png|webp|bmp|tiff?)$/i.test(file.name)) {
    toast("Selecione um arquivo de imagem válido.", "error");
    return;
  }
  state.files[kind] = file;
  const thumb = document.querySelector(`[data-thumb="${kind}"]`);
  if (thumb) {
    const url = URL.createObjectURL(file);
    thumb.innerHTML = `<img src="${url}" alt="Prévia ${kind}" />`;
  }
  const name = document.querySelector(`[data-file-name="${kind}"]`);
  if (name) name.textContent = `${file.name} · ${bytesLabel(file.size)}`;
}

async function doUpload() {
  const session = activeSession();
  if (!session) return;
  const ruler = state.files.ruler;
  const clean = state.files.clean;
  if (!ruler && !clean) {
    toast("Selecione ao menos uma imagem para enviar.", "error");
    return;
  }
  const form = new FormData();
  if (ruler) form.append("ruler_image", ruler);
  if (clean) form.append("clean_image", clean);
  const label = $("label-input")?.value?.trim();
  if (label) form.append("label", label);
  const capturedAt = $("captured-at")?.value;
  if (capturedAt) form.append("captured_at", capturedAt);

  setBusy(true, "Enviando imagens…");
  try {
    const response = await api(sessionEndpoint(session, "/upload"), { method: "POST", body: form });
    state.files = {};
    state.sessions[session.id] = response.session;
    toast(`Imagens de ${sessionCode(session)} enviadas${response.captured_at ? ` · ${formatDate(response.captured_at)}` : ""}.`, "success");
    await refreshSessions();
    setStep(2);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setBusy(false);
  }
}

async function saveMetadata() {
  const session = activeSession();
  if (!session) return;
  const label = $("label-input")?.value?.trim() || "";
  const capturedAt = $("captured-at")?.value || null;
  try {
    const response = await patchJSON(sessionEndpoint(session), {
      label: label || null,
      captured_at: capturedAt,
    });
    state.sessions[session.id] = response.session;
    renderSidebar();
    toast(`Data e rótulo de ${sessionCode(session)} atualizados.`, "success");
  } catch (error) {
    toast(error.message, "error");
  }
}

/* --------------------------------------------------------------------------
   Passo 2 — Calibração
   -------------------------------------------------------------------------- */
function scaleOfPoints(p1, p2) {
  if (!p1 || !p2) return null;
  const px = Math.hypot(p2[0] - p1[0], p2[1] - p1[1]);
  const known = Number(state.knownMm);
  if (!Number.isFinite(known) || known <= 0) return { px, pxPerMm: null, plausible: false };
  const pxPerMm = px / known;
  return { px, pxPerMm, plausible: pxPerMm >= 1.5 && pxPerMm <= 50 };
}

function step2HTML(session) {
  const { p1, p2 } = state.calib;
  const scale = scaleOfPoints(p1, p2);
  const px = scale?.px ?? null;
  const warning = scale && scale.pxPerMm !== null && !scale.plausible
    ? `<div class="result-banner" style="border-color:#f3b6b1;background:#fff5f4"><div><div class="rb-label" style="color:#b42318">Escala implausível</div><div class="rb-value" style="color:#b42318">${fmt(scale.pxPerMm, 2)} <small>px/mm</small></div></div><span class="pill">verifique</span></div>`
    : "";
  const body = `
    <ol class="instructions">
      <li>Clique no <b>ponto inicial</b> da régua visível na foto.</li>
      <li>Clique no <b>ponto final</b> do mesmo trecho.</li>
      <li>Informe a distância real entre os dois pontos.</li>
    </ol>

    <div class="coord-grid">
      <div class="coord-card"><span>PONTO 1</span><code>${p1 ? `x ${p1[0].toFixed(1)} · y ${p1[1].toFixed(1)}` : "— aguardando"}</code></div>
      <div class="coord-card"><span>PONTO 2</span><code>${p2 ? `x ${p2[0].toFixed(1)} · y ${p2[1].toFixed(1)}` : "— aguardando"}</code></div>
    </div>
    <p class="form-hint">Clique diretamente sobre um ponto marcado para removê-lo sem apagar o outro.</p>

    <div class="field">
      <span>Distância real entre os pontos</span>
      <input id="known-mm" type="number" min="1" max="500" step="0.5" value="${state.knownMm}" />
      <small>Use um trecho longo da régua (ex.: 50 mm) para reduzir o erro de leitura.</small>
    </div>

    ${px ? `<div class="result-banner"><div><div class="rb-label">Distância em pixels</div><div class="rb-value">${fmt(px, 1)} <small>px</small></div></div><div><div class="rb-label">Escala ${helpBadge("help-glossary-fotogrametria", "Fotogrametria clínica: px/mm converte o polígono em medidas reais.")}</div><div class="rb-value">${scale.pxPerMm === null ? "—" : fmt(scale.pxPerMm, 2)} <small>px/mm</small></div></div></div>` : ""}
    ${warning}

    ${session.px_per_mm ? `<div class="result-banner"><div><div class="rb-label">Calibração registrada</div><div class="rb-value">${fmt(session.px_per_mm, 3)} <small>px/mm</small></div></div><span class="pill">ok</span></div>` : ""}

    <div class="form-actions">
      <button type="button" class="btn btn-primary" data-action="calibrate" ${p1 && p2 && scale?.plausible ? "" : "disabled"}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m14.5 12.5-8 8a2.12 2.12 0 0 1-3-3l8-8" /><path d="m16 16 6-6" /><path d="m8 8 6-6" /><path d="m9 7 8 8" /><path d="m21 11-8-8" /></svg>
        Calibrar escala
      </button>
      <button type="button" class="btn btn-ghost" data-action="reset-calib" ${p1 ? "" : "disabled"}>Limpar pontos</button>
    </div>
    <p class="form-hint">${p1 && p2 ? (scale?.plausible ? "Pronto para calcular px/mm." : "Ajuste a distância real: a escala está fora da faixa plausível (1,5–50 px/mm).") : `Clique no palco para definir ${p1 ? "o segundo" : "o primeiro"} ponto.`}</p>
  `;
  return stepPanelShell("Calibração métrica", "A régua define quantos pixels equivalem a 1 mm na foto.", body);
}

async function doCalibrate() {
  const session = activeSession();
  const { p1, p2 } = state.calib;
  if (!session || !p1 || !p2) {
    toast("Clique nos dois pontos da régua antes de calibrar.", "error");
    return;
  }
  const known = Number(state.knownMm);
  if (!Number.isFinite(known) || known <= 0) {
    toast("Informe uma distância real positiva em milímetros.", "error");
    return;
  }
  const px = Math.hypot(p2[0] - p1[0], p2[1] - p1[1]);
  const pxPerMm = px / known;
  if (pxPerMm < 1.5 || pxPerMm > 50) {
    toast(`Escala implausível: ${fmt(pxPerMm, 2)} px/mm. Confira a régua e a distância real (1,5–50 px/mm).`, "error", 6000);
    return;
  }
  setBusy(true, "Calculando escala…");
  try {
    const response = await postJSON(sessionEndpoint(session, "/calibrate"), {
      p1: [p1[0], p1[1]],
      p2: [p2[0], p2[1]],
      known_mm: known,
    });
    state.sessions[session.id] = response.session;
    toast(`Escala calibrada: ${fmt(response.px_per_mm, 3)} px/mm.`, "success");
    await refreshSessions();
    setStep(3);
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setBusy(false);
  }
}

/* --------------------------------------------------------------------------
   Passo 3 — Alinhamento
   -------------------------------------------------------------------------- */
function step3HTML(session) {
  const landmarksReady = state.landmarks.length === 4;
  const roiReady = Array.isArray(state.roi) && state.roi[2] > 0 && state.roi[3] > 0;
  const alignedPreview = isAlignedPreview();
  const shownRoi = alignedPreview ? referenceHealthyRoi() : state.roi;
  const shownRoiReady = Array.isArray(shownRoi) && shownRoi[2] > 0 && shownRoi[3] > 0;
  const reference = !isBaseline(session);
  const processed = Boolean(session.aligned_image_url);
  const isT0 = isBaseline(session);

  const landmarkRows = [0, 1, 2, 3].map((index) => {
    const point = state.landmarks[index];
    const expected = expectedLandmarkLabel(index);
    if (point) {
      return `<div class="landmark-row is-set">
        <span class="marker-num">${index + 1}</span>
        <input type="text" maxlength="80" data-landmark-label="${index}" value="${esc(point.label)}" list="landmark-suggestions" aria-label="Nome do ponto ${index + 1}" ${isT0 ? "" : "readonly"} />
        <button type="button" class="point-remove" data-action="remove-landmark" data-index="${index}" title="Remover ponto ${index + 1}" aria-label="Remover ponto ${index + 1}">×</button>
      </div>`;
    }
    return `<div class="landmark-row">
      <span class="marker-num">${index + 1}</span>
      <span class="hint">${reference ? `Aguardando · ${esc(expected)}` : "Clique no canvas para marcar"}</span>
    </div>`;
  }).join("");

  const body = `
    <datalist id="landmark-suggestions">
      ${LANDMARK_SUGGESTIONS.map((name) => `<option value="${esc(name)}"></option>`).join("")}
    </datalist>

    <div class="section-title"><span>Pontos anatômicos ${helpBadge("help-glossary-landmarks", "Marcos anatômicos: pontos fixos marcados na mesma ordem em todas as sessões.")}</span><span>${state.landmarks.length}/4</span></div>
    <div class="landmark-list">${landmarkRows}</div>
    <p class="form-hint">${isT0 ? "Clique no canvas e nomeie cada ponto. Os nomes serão reutilizados nas sessões seguintes." : "Marque os pontos na ordem dos nomes definidos em T0."} Clique num ponto marcado para removê-lo.</p>

    <div class="section-title"><span>Pele saudável (ROI) ${helpBadge("help-glossary-roi", "ROI de pele saudável: retângulo de pele íntegra usado como referência de cor.")}</span><span>${shownRoiReady ? (alignedPreview ? "T0" : "definida") : "pendente"}</span></div>
    <div class="roi-summary ${shownRoiReady ? "is-set" : ""}">
      <span>${shownRoiReady ? `x ${shownRoi[0]} · y ${shownRoi[1]} · ${shownRoi[2]}×${shownRoi[3]} px` : "Área de pele íntegra, sem lesões"}</span>
      <span class="form-actions">
        <button type="button" class="btn btn-soft btn-sm ${state.roiDrawing ? "is-active" : ""}" data-action="draw-roi" ${alignedPreview ? "disabled" : ""}>${state.roiDrawing ? "Arraste na imagem…" : "Desenhar ROI"}</button>
        ${roiReady ? `<button type="button" class="btn btn-ghost btn-sm" data-action="clear-roi" ${alignedPreview ? "disabled" : ""}>Limpar</button>` : ""}
      </span>
    </div>
    ${alignedPreview ? '<p class="form-hint">Prévia no espaço de T0: exibindo pontos e ROI de T0. Volte a “Original” para editar os marcadores desta sessão.</p>' : ""}

    ${reference ? `
      <div class="section-title"><span>Referência T0</span><span>${esc(sessionCode(session))} → T0</span></div>
      <div class="ref-panel">
        <div class="ref-head"><span>Imagem e pontos de referência</span><span>4 pontos</span></div>
        <div class="ref-canvas-wrap"><canvas id="ref-canvas" aria-label="Imagem de referência T0"></canvas></div>
        <div class="ref-note">Os pontos marcados em ${esc(sessionCode(session))} serão projetados sobre esta geometria de T0.</div>
      </div>` : `
      <div class="roi-summary"><span><b>T0 é a referência do estudo.</b> Os pontos definidos aqui serão reutilizados nas demais sessões.</span></div>`}

    ${processed ? `
      <div class="result-banner"><div><div class="rb-label">Alinhamento processado ${helpBadge("help-glossary-homografia", "Homografia: matriz 3×3 que projeta a sessão sobre a geometria de T0.")}</div><div class="rb-value">${session.enhanced_image_url ? `CIELAB + CLAHE ${helpBadge("help-glossary-clahe", "CLAHE: realce local de contraste aplicado apenas ao canal L*.")}` : "Homografia"}</div></div><span class="pill">ok</span></div>
      <div class="segmented grow" role="group" aria-label="Prévia do processamento">
        <button type="button" class="${!state.step3Preview ? "is-active" : ""}" data-action="preview" data-preview="original">Original</button>
        <button type="button" class="${state.step3Preview && state.mode === "natural" ? "is-active" : ""}" data-action="preview" data-preview="natural">Alinhada natural</button>
        <button type="button" class="${state.step3Preview && state.mode === "enhanced" ? "is-active" : ""}" data-action="preview" data-preview="enhanced" ${session.enhanced_image_url ? "" : "disabled"}>Realce CIELAB</button>
      </div>
      <button type="button" class="btn btn-ghost btn-block" data-action="goto-step4">Ir para a medição</button>
    ` : ""}

    <div class="form-actions">
      <button type="button" class="btn btn-primary btn-block" data-action="register" ${landmarksReady && roiReady ? "" : "disabled"}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 7V5a2 2 0 0 1 2-2h2" /><path d="M17 3h2a2 2 0 0 1 2 2v2" /><path d="M21 17v2a2 2 0 0 1-2 2h-2" /><path d="M7 21H5a2 2 0 0 1-2-2v-2" /><circle cx="12" cy="12" r="2.5" /></svg>
        ${processed ? "Reprocessar alinhamento" : "Processar alinhamento"}
      </button>
      <button type="button" class="btn btn-ghost" data-action="clear-landmarks" ${state.landmarks.length ? "" : "disabled"}>Limpar pontos</button>
    </div>
    <p class="form-hint">${landmarksReady && roiReady ? "Tudo pronto para registrar." : "Marque os 4 pontos na ordem listada e defina a ROI."}</p>
  `;
  return stepPanelShell("Alinhamento e fotometria", "Homografia para T0 e normalização CIELAB com CLAHE.", body);
}

async function drawReferencePanel() {
  const canvas = $("ref-canvas");
  const t0 = baselineSession();
  if (!canvas || !t0) return;
  const url = t0.aligned_image_url || t0.clean_image_url;
  const wrap = canvas.parentElement;
  if (!url || !wrap) return;
  const w = wrap.clientWidth;
  const h = wrap.clientHeight;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  let img;
  try { img = await loadImage(url); } catch { return; }
  if (!$("ref-canvas")) return;
  const zoom = Math.min(w / img.naturalWidth, h / img.naturalHeight);
  const ox = (w - img.naturalWidth * zoom) / 2;
  const oy = (h - img.naturalHeight * zoom) / 2;
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(img, ox, oy, img.naturalWidth * zoom, img.naturalHeight * zoom);

  const points = t0.landmarks;
  if (Array.isArray(points) && points.length) {
    points.forEach((raw, index) => {
      const point = normalizeLandmark(raw, index);
      const x = ox + point.x * zoom;
      const y = oy + point.y * zoom;
      ctx.beginPath();
      ctx.arc(x, y, 9, 0, Math.PI * 2);
      ctx.fillStyle = "rgba(251,191,36,0.9)";
      ctx.fill();
      ctx.lineWidth = 2;
      ctx.strokeStyle = "rgba(10,16,22,0.85)";
      ctx.stroke();
      ctx.fillStyle = "#0a1016";
      ctx.font = "800 10px Inter, system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(String(index + 1), x, y + 0.5);
      if (point.label) {
        const label = point.label.length > 18 ? `${point.label.slice(0, 17)}…` : point.label;
        ctx.font = "700 9px Inter, system-ui, sans-serif";
        const width = ctx.measureText(label).width + 8;
        ctx.fillStyle = "rgba(6,12,17,0.82)";
        roundRect(ctx, x - width / 2, y + 12, width, 14, 4);
        ctx.fill();
        ctx.fillStyle = "#fde68a";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(label, x, y + 19.5);
      }
    });
  }

  const roi = t0.healthy_skin_roi;
  if (Array.isArray(roi)) {
    ctx.save();
    ctx.strokeStyle = "rgba(45,212,191,0.9)";
    ctx.setLineDash([6, 4]);
    ctx.lineWidth = 1.5;
    ctx.strokeRect(ox + roi[0] * zoom, oy + roi[1] * zoom, roi[2] * zoom, roi[3] * zoom);
    ctx.restore();
  }
}

async function doRegister() {
  const session = activeSession();
  if (!session) return;
  if (state.landmarks.length !== 4) {
    toast("Marque os 4 pontos anatômicos antes de processar.", "error");
    return;
  }
  if (!state.roi) {
    toast("Desenhe a ROI de pele saudável.", "error");
    return;
  }
  if (!isBaseline(session)) {
    state.landmarks = state.landmarks.map((point, index) => ({
      label: point.label || expectedLandmarkLabel(index),
      x: point.x,
      y: point.y,
    }));
  }
  setBusy(true, "Alinhando e normalizando…");
  try {
    const response = await postJSON(sessionEndpoint(session, "/register"), {
      landmarks: state.landmarks.map((p) => ({ label: p.label, x: p.x, y: p.y })),
      healthy_skin_roi: state.roi.map((v) => Math.round(v)),
    });
    state.sessions[session.id] = response.session;
    state.step3Preview = true;
    state.mode = "natural";
    toast(`Alinhamento de ${sessionCode(session)} concluído.`, "success");
    await refreshSessions();
    state.step3Preview = true;
    renderStepPanel();
    renderCanvas();
  } catch (error) {
    toast(error.message, "error");
  } finally {
    setBusy(false);
  }
}

/* --------------------------------------------------------------------------
   Passo 4 — Medição
   -------------------------------------------------------------------------- */
function step4HTML(session) {
  const meta = lesionMeta(state.stria);
  const current = measurementFor(session, state.stria);
  const count = state.polygon.length;
  const ready = Boolean(session.aligned_image_url);

  const lesionInputs = LESIONS.map((item) => `
    <div class="lesion-label-row">
      <span class="code"><span class="stria-dot" style="--stria-color:${item.ink}"></span>${item.code}</span>
      <input type="text" maxlength="120" data-lesion-input="${item.code}" value="${esc(lesionLabel(session, item.code))}" placeholder="Descrição da lesão/região" aria-label="Descrição de ${item.code}" />
    </div>`).join("");

  const body = `
    <div class="section-title"><span>Região-alvo em análise</span><span>${esc(lesionLabel(session, state.stria))}</span></div>
    <div class="stria-grid">
      ${LESIONS.map((item) => `
        <button type="button" class="stria-card ${item.code === lesionCode(state.stria) ? "is-active" : ""}" data-stria="${item.code}" style="--stria-color:${item.ink}; --stria-soft:${item.soft.replace("0.18", "0.12")}">
          <span class="stria-card-top"><span class="stria-dot"></span><strong>${esc(item.code)}</strong></span>
          <span>${esc(lesionLabel(session, item.code))} · ${measurementFor(session, item.code) ? "medida" : "não medida"}</span>
        </button>`).join("")}
    </div>

    <div class="section-title"><span>Descrição das regiões</span><span>editável</span></div>
    <div class="lesion-labels">${lesionInputs}</div>

    <div class="section-title"><span>Polígono ${helpBadge("help-glossary-morfometria", "Morfometria: área por Gauss/Shoelace convertida para mm² pela escala.")}</span><span id="poly-count">${count} vértice${count === 1 ? "" : "s"}</span></div>
    <div class="form-actions">
      <button type="button" class="btn btn-primary btn-sm" data-action="measure-stria" ${count >= 3 && state.polygonActive ? "" : "disabled"}>Medir região</button>
      <button type="button" class="btn btn-ghost btn-sm" data-action="undo-vertex" ${count ? "" : "disabled"}>Desfazer</button>
      <button type="button" class="btn btn-danger-ghost btn-sm" data-action="clear-polygon" ${count ? "" : "disabled"}>Limpar</button>
      ${isAlignedSpace() && referenceRois().some(r => r.code === lesionCode(state.stria)) ? `
        <button type="button" class="btn btn-soft btn-sm" data-action="import-rois" title="Importar o contorno de ${lesionCode(state.stria)} definido na sessão T0">
          Importar ROIs do T0
        </button>` : ""}
    </div>
    <p class="form-hint">${ready ? "Clique nos vértices acompanhando o contorno · clique num vértice para removê-lo · duplo clique fecha e mede." : "O polígono usa a imagem alinhada do passo 3."}</p>

    ${current ? `
      <div class="section-title"><span>Resultado de ${esc(lesionLabel(session, current.stria_code, current.stria_label))}</span><span>${new Date(current.created_at).toLocaleString("pt-BR")}</span></div>
      <div class="metric-grid">
        <div class="metric-card"><span>Comprimento</span><strong>${fmt(current.length_mm, 1)} <small>mm</small></strong></div>
        <div class="metric-card"><span>Largura média</span><strong>${fmt(current.mean_width_mm, 1)} <small>mm</small></strong></div>
        <div class="metric-card"><span>Largura máxima</span><strong>${fmt(current.max_width_mm, 1)} <small>mm</small></strong></div>
        <div class="metric-card is-accent"><span>Área</span><strong>${fmt(current.area_mm2, 1)} <small>mm²</small></strong></div>
        <div class="metric-card is-accent"><span>ΔE00 vs. pele sadia ${helpBadge("help-glossary-deltae", "ΔE00 (CIEDE2000): diferença de cor entre a estria e a pele saudável; menor é melhor.")}</span><strong>${fmt(current.delta_e_00, 2)}</strong></div>
        <div class="metric-card"><span>Vértices salvos</span><strong>${current.polygon.length}</strong></div>
      </div>
      <p class="form-hint">Medição substitui o registro anterior desta região nesta sessão.</p>
    ` : `<div class="results-empty">Nenhuma medição de ${esc(meta.short)} nesta sessão.</div>`}
  `;
  return stepPanelShell("Morfometria da região-alvo", "Delimite a lesão sobre a imagem alinhada.", body);
}

function updatePolygonStatus() {
  const count = state.polygon.length;
  const label = $("poly-count");
  if (label) label.textContent = `${count} vértice${count === 1 ? "" : "s"}`;
  const panel = $("step-panel");
  if (!panel || state.step !== 4) return;
  const measure = panel.querySelector('[data-action="measure-stria"]');
  const undo = panel.querySelector('[data-action="undo-vertex"]');
  const clear = panel.querySelector('[data-action="clear-polygon"]');
  if (measure) measure.disabled = count < 3 || !state.polygonActive;
  if (undo) undo.disabled = count === 0;
  if (clear) clear.disabled = count === 0;
}

function closePolygon() {
  if (state.polygon.length < 3) {
    toast("Adicione pelo menos 3 vértices para fechar o polígono.", "error");
    return;
  }
  state.polygonActive = false;
  state.hover = null;
  state.hoverHit = null;
  lastPolygonClick = null;
  const tip = $("canvas-tip");
  if (tip) tip.hidden = true;
  updatePolygonStatus();
  scheduleRedraw();
  measureStria();
}

async function measureStria() {
  const session = activeSession();
  if (!session) return;
  if (!session.aligned_image_url) {
    toast("A sessão precisa estar alinhada para medir.", "error");
    return;
  }
  if (!session.px_per_mm) {
    toast("Calibre a régua antes de medir.", "error");
    return;
  }
  if (state.polygon.length < 3) {
    toast("O polígono precisa de pelo menos 3 vértices.", "error");
    return;
  }
  const snapshot = state.polygon.map((p) => [p[0], p[1]]);
  setBusy(true, "Calculando morfometria…");
  try {
    const response = await postJSON(sessionEndpoint(session, "/measure"), {
      stria_code: lesionCode(state.stria),
      stria_label: lesionLabel(session, state.stria),
      polygon: snapshot,
    });
    state.polygon = [];
    state.polygonActive = false;
    lastPolygonClick = null;
    state.hover = null;
    state.hoverHit = null;
    toast(
      `${lesionCode(response.stria_code)} · comprimento ${fmt(response.length_mm, 1)} mm · área ${fmt(response.area_mm2, 1)} mm²`,
      "success",
      5200,
    );
    await refreshSessions();
    scheduleRedraw();
  } catch (error) {
    if (state.polygon.length >= 3) state.polygonActive = true;
    updatePolygonStatus();
    scheduleRedraw();
    toast(error.message, "error");
  } finally {
    setBusy(false);
  }
}

/* --------------------------------------------------------------------------
   Tabela consolidada
   -------------------------------------------------------------------------- */
function renderResults() {
  const box = $("results-table");
  const badge = $("results-count");
  const session = activeSession();
  if (!box) return;
  const measurements = session?.measurements || [];
  if (badge) badge.textContent = String(measurements.length);

  if (!measurements.length) {
    box.innerHTML = `<div class="results-empty">Nenhuma região medida nesta sessão.<br />Conclua alinhamento e medição no passo 4.</div>`;
    return;
  }

  box.innerHTML = `
    <div class="results-toolbar">
      <button type="button" class="btn btn-danger-ghost btn-sm" data-action="clear-measurements">🗑 Limpar todas</button>
    </div>
    <table class="data-table">
      <thead>
        <tr><th>Região</th><th>Comp.</th><th>Larg. méd.</th><th>Área</th><th>ΔE00</th><th></th></tr>
      </thead>
      <tbody>
        ${measurements.map((m) => {
          const meta = lesionMeta(m.stria_code);
          return `<tr>
            <td class="code"><span class="stria-name"><span class="stria-dot" style="background:${meta.ink}"></span>${esc(lesionLabel(session, m.stria_code, m.stria_label))}</span></td>
            <td>${fmt(m.length_mm, 1)} mm</td>
            <td>${fmt(m.mean_width_mm, 1)} mm</td>
            <td>${fmt(m.area_mm2, 1)} mm²</td>
            <td>${fmt(m.delta_e_00, 2)}</td>
            <td><button type="button" class="row-delete" data-action="delete-measurement" data-code="${esc(m.stria_code)}" title="Excluir medição" aria-label="Excluir medição de ${esc(lesionLabel(session, m.stria_code, m.stria_label))}">🗑</button></td>
          </tr>`;
        }).join("")}
      </tbody>
    </table>`;
}

async function deleteMeasurement(code) {
  const session = activeSession();
  if (!session) return;
  try {
    await api(sessionEndpoint(session, `/measurements/${encodeURIComponent(code)}`), { method: "DELETE" });
    toast(`Medição de ${lesionLabel(session, code)} excluída.`, "success");
    await refreshSessions();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function clearMeasurements() {
  const session = activeSession();
  if (!session) return;
  if (!(session.measurements || []).length) {
    toast("Não há medições para limpar nesta sessão.", "info");
    return;
  }
  try {
    const response = await api(sessionEndpoint(session, "/measurements"), { method: "DELETE" });
    toast(`${response.deleted} medição(ões) de ${sessionCode(session)} removida(s).`, "success");
    await refreshSessions();
  } catch (error) {
    toast(error.message, "error");
  }
}

/* --------------------------------------------------------------------------
   Delegação de ações do inspetor
   -------------------------------------------------------------------------- */
function bindStepPanel() {
  if (state.step === 3) drawReferencePanel();
  const known = $("known-mm");
  if (known) {
    known.addEventListener("input", () => {
      state.knownMm = Number(known.value) || 0;
    });
    known.addEventListener("change", () => renderStepPanel());
  }
  const ruler = $("ruler-file");
  if (ruler) ruler.addEventListener("change", () => setUploadFile("ruler", ruler.files[0]));
  const clean = $("clean-file");
  if (clean) clean.addEventListener("change", () => setUploadFile("clean", clean.files[0]));

  document.querySelectorAll("[data-landmark-label]").forEach((input) => {
    input.addEventListener("input", () => {
      const index = Number(input.dataset.landmarkLabel);
      if (state.landmarks[index]) state.landmarks[index].label = input.value;
    });
    input.addEventListener("change", () => scheduleRedraw());
  });

  document.querySelectorAll("[data-lesion-input]").forEach((input) => {
    input.addEventListener("change", () => saveLesionLabels());
  });
}

async function saveLesionLabels() {
  const session = activeSession();
  if (!session) return;
  const labels = {};
  document.querySelectorAll("[data-lesion-input]").forEach((input) => {
    const value = input.value.trim();
    if (value) labels[input.dataset.lesionInput] = value;
  });
  try {
    const response = await patchJSON(sessionEndpoint(session), { lesion_labels: labels });
    state.sessions[session.id] = response.session;
    renderSidebar();
    renderStepPanel();
    renderResults();
    renderCompare();
    toast("Descrições das regiões atualizadas.", "success", 2600);
  } catch (error) {
    toast(error.message, "error");
  }
}

function handleAction(action, target) {
  switch (action) {
    case "goto-upload": setStep(1); break;
    case "upload-images": doUpload(); break;
    case "save-meta": saveMetadata(); break;
    case "calibrate": doCalibrate(); break;
    case "reset-calib":
      state.calib = { p1: null, p2: null };
      renderStepPanel();
      scheduleRedraw();
      break;
    case "draw-roi":
      if (isAlignedPreview()) {
        toast("Selecione “Original” para editar a ROI desta sessão.", "info", 3200);
        break;
      }
      state.roiDrawing = true;
      toast("Arraste sobre a pele íntegra de referência.", "info", 3200);
      renderStepPanel();
      break;
    case "clear-roi":
      if (isAlignedPreview()) {
        toast("Selecione “Original” para editar a ROI desta sessão.", "info", 3200);
        break;
      }
      state.roi = null;
      state.roiDrawing = false;
      renderStepPanel();
      scheduleRedraw();
      break;
    case "clear-landmarks":
      state.landmarks = [];
      hideLandmarkPopover();
      renderStepPanel();
      scheduleRedraw();
      break;
    case "remove-landmark": {
      const index = Number(target.dataset.index);
      if (Number.isInteger(index) && state.landmarks[index]) {
        state.landmarks.splice(index, 1);
        hideLandmarkPopover();
        renderStepPanel();
        scheduleRedraw();
      }
      break;
    }
    case "save-landmark-label": applyLandmarkLabel(); break;
    case "register": doRegister(); break;
    case "preview": {
      const preview = target.dataset.preview;
      state.step3Preview = preview !== "original";
      state.mode = preview === "enhanced" ? "enhanced" : "natural";
      if (state.step3Preview) state.roiDrawing = false;
      renderStepPanel();
      renderCanvas();
      break;
    }
    case "goto-step4": setStep(4); break;
    case "measure-stria": measureStria(); break;
    case "close-polygon": closePolygon(); break;
    case "undo-vertex":
      state.polygon.pop();
      if (!state.polygon.length) state.polygonActive = false;
      updatePolygonStatus();
      scheduleRedraw();
      break;
    case "import-rois": {
      const session = activeSession();
      if (!session) return;
      const target = lesionCode(state.stria);
      const roi = referenceRois().find((item) => item.code === target);
      if (!roi) {
        toast(`T0 não possui ROI medida para ${target}.`, "error");
        return;
      }
      if (!session.aligned_image_url) {
        toast("Conclua o alinhamento desta sessão antes de importar as ROIs.", "error");
        return;
      }
      // Deep copy: a edição local não altera os polígonos de T0.
      state.polygon = roi.points.map((point) => [Number(point[0]), Number(point[1])]);
      state.polygonActive = true;
      state.hover = null;
      state.hoverHit = null;
      lastPolygonClick = null;
      // A imagem do passo 4 já está no espaço alinhado ao T0.
      updatePolygonStatus();
      renderStepPanel();
      scheduleRedraw();
      toast(`ROI de ${target} importada de T0 · ajuste os vértices ou clique em “Medir região”.`, "success", 6000);
      break;
    }
    case "clear-polygon":
      state.polygon = [];
      state.polygonActive = false;
      lastPolygonClick = null;
      updatePolygonStatus();
      scheduleRedraw();
      break;
    case "delete-measurement": deleteMeasurement(target.dataset.code); break;
    case "clear-measurements": clearMeasurements(); break;
    case "save-scales": saveScales(); break;
    default: break;
  }
}

function onDocumentClick(event) {
  if (!event.target.closest("#landmark-popover") && !event.target.closest("#stage")) hideLandmarkPopover();
  const removeTarget = event.target.closest("[data-remove-session]");
  if (removeTarget) {
    removeSession(removeTarget.dataset.removeSession);
    return;
  }
  const deleteTarget = event.target.closest("[data-delete-patient-id]");
  if (deleteTarget) {
    event.preventDefault();
    event.stopPropagation();
    openDeletePatientModal(deleteTarget.dataset.deletePatientId);
    return;
  }
  const personalTarget = event.target.closest("[data-personal-id]");
  if (personalTarget) {
    event.preventDefault();
    event.stopPropagation();
    openPersonalDataModal(personalTarget.dataset.personalId);
    return;
  }
  const patientTarget = event.target.closest("[data-patient-id]");
  if (patientTarget) {
    const patient = state.patients.find((item) => item.id === patientTarget.dataset.patientId);
    if (!patient) return;
    if (isPatientLocked(patient)) {
      openPinModal(patient);
    } else {
      openPatient(patient.id, patient);
    }
    return;
  }
  const actionTarget = event.target.closest("[data-action]");
  if (actionTarget) {
    handleAction(actionTarget.dataset.action, actionTarget);
    return;
  }
  const sessionTarget = event.target.closest("[data-session]");
  if (sessionTarget) {
    selectSession(sessionTarget.dataset.session);
    return;
  }
  const stepTarget = event.target.closest("[data-step]");
  if (stepTarget) {
    setStep(Number(stepTarget.dataset.step));
    return;
  }
  const striaTarget = event.target.closest("[data-stria]");
  if (striaTarget) {
    state.stria = lesionCode(striaTarget.dataset.stria);
    state.polygon = [];
    state.polygonActive = false;
    lastPolygonClick = null;
    renderStepPanel();
    scheduleRedraw();
    return;
  }
  const modeTarget = event.target.closest("#mode-switch [data-mode]");
  if (modeTarget && !modeTarget.disabled) {
    state.mode = modeTarget.dataset.mode;
    updateModeSwitch();
    renderCanvas();
    renderStepPanel();
  }
}

/* --------------------------------------------------------------------------
   Comparativo longitudinal & laudo
   -------------------------------------------------------------------------- */
function followUpIds() {
  return state.order.filter((id) => !isBaseline(state.sessions[id]));
}

function compareCandidates() {
  return followUpIds().filter((id) => state.sessions[id]?.aligned_image_url);
}

function compareImageUrls(sessionId) {
  const t0 = baselineSession();
  const other = state.sessions[sessionId];
  if (!t0 || !other) return { base: null, top: null };
  const enhanced = state.compare.mode === "enhanced";
  const base = enhanced
    ? (t0.enhanced_image_url || t0.aligned_image_url)
    : (t0.aligned_image_url || t0.clean_image_url);
  const top = enhanced
    ? (other.enhanced_image_url || other.aligned_image_url)
    : (other.aligned_image_url || other.clean_image_url);
  return { base, top };
}

function renderCompare() {
  const candidatesBox = $("compare-sessions");
  if (!candidatesBox) return;
  const candidates = compareCandidates();
  if (!candidates.includes(state.compare.sessionId)) {
    state.compare.sessionId = candidates[0] || followUpIds()[0] || null;
  }

  candidatesBox.innerHTML = followUpIds().map((id) => {
    const session = state.sessions[id];
    const ready = Boolean(session?.aligned_image_url);
    return `<button type="button" data-compare-session="${esc(id)}" class="${state.compare.sessionId === id ? "is-active" : ""}" ${ready ? "" : "disabled"}>${esc(sessionCode(session))}</button>`;
  }).join("");

  updateBA();
  renderLongitudinal();
  renderGaisBlind();
}

function updateBA() {
  const stage = $("ba-stage");
  const base = $("ba-base");
  const top = $("ba-top");
  const empty = $("ba-empty");
  const divider = $("ba-divider");
  const left = $("ba-label-left");
  const right = $("ba-label-right");
  if (!stage || !base || !top || !empty || !divider) return;

  const urls = compareImageUrls(state.compare.sessionId);
  const ready = Boolean(urls.base && urls.top);
  empty.hidden = ready;
  stage.style.cursor = ready ? "ew-resize" : "not-allowed";
  if (!ready) {
    base.removeAttribute("src");
    top.removeAttribute("src");
    empty.innerHTML = baselineSession()?.aligned_image_url
      ? `A sessão ${esc(sessionCode(state.sessions[state.compare.sessionId]))} ainda não foi alinhada.<br />Conclua o passo 3 para comparar.`
      : "A sessão T0 precisa estar alinhada para iniciar a comparação.";
    divider.style.opacity = "0";
    return;
  }

  divider.style.opacity = "1";
  const baseSrc = withLaunchToken(urls.base);
  const topSrc = withLaunchToken(urls.top);
  if (base.getAttribute("src") !== baseSrc) base.src = baseSrc;
  if (top.getAttribute("src") !== topSrc) top.src = topSrc;

  const anon = state.compare.anon;
  const t0 = baselineSession();
  const other = state.sessions[state.compare.sessionId];
  if (left) left.textContent = anon ? "Antes" : `T0 · ${formatDate(t0?.captured_at) || "baseline"}`;
  if (right) right.textContent = anon ? "Depois" : `${sessionCode(other)} · ${formatDate(other?.captured_at) || "seguimento"}`;
  applyComparePosition();
}

function applyComparePosition() {
  const pos = Math.max(2, Math.min(98, state.compare.pos));
  state.compare.pos = pos;
  const top = $("ba-top");
  const divider = $("ba-divider");
  if (top) top.style.clipPath = `inset(0 0 0 ${pos}%)`;
  if (divider) divider.style.left = `${pos}%`;
}

function setComparePosition(clientX) {
  const stage = $("ba-stage");
  if (!stage) return;
  const rect = stage.getBoundingClientRect();
  if (!rect.width) return;
  let pct = ((clientX - rect.left) / rect.width) * 100;
  pct = Math.max(2, Math.min(98, pct));
  state.compare.pos = pct;
  applyComparePosition();
}

async function setGais(sessionId, value) {
  const session = state.sessions[sessionId];
  if (!session) return;
  const current = session.clinical_scales || { end_pain: 0, likert_satisfaction: 3, notes: "" };
  try {
    const response = await postJSON(sessionEndpoint(session, "/clinical-scales"), {
      end_pain: Number(current.end_pain) || 0,
      likert_satisfaction: Number(current.likert_satisfaction) || 3,
      gais_score: value,
      notes: current.notes || "",
    });
    state.sessions[sessionId] = response.session;
    renderGaisBlind();
    renderScales();
    renderSidebar();
    toast(`GAIS de ${sessionCode(session)}: ${value > 0 ? "+" : ""}${value}.`, "success", 2600);
  } catch (error) {
    toast(error.message, "error");
  }
}

function renderGaisBlind() {
  const box = $("gais-options");
  if (!box) return;
  const session = state.sessions[state.compare.sessionId];
  const score = Number(session?.clinical_scales?.gais_score ?? 0);
  box.innerHTML = GAIS_OPTIONS.map((option) => `
    <button type="button" data-compare-gais="${option.value}" class="${option.value === score ? "is-active" : ""}" title="${esc(option.label)}" aria-pressed="${option.value === score}">
      ${option.value > 0 ? "+" : ""}${option.value}
    </button>`).join("");
}

function metricCell(value, reference, digits, invert) {
  if (value === null || value === undefined) return `<td class="empty">—</td>`;
  const shown = fmt(value, digits);
  if (!reference || reference === value) {
    return `<td class="matrix-val">${shown}</td>`;
  }
  if (!reference || reference <= 0) {
    return `<td class="matrix-val">${shown}</td>`;
  }
  const reduction = ((reference - value) / reference) * 100;
  const good = invert ? reduction > 0 : reduction < 0;
  const arrow = reduction >= 0 ? "↓" : "↑";
  const cls = Math.abs(reduction) < 0.05 ? "" : good ? "good" : "bad";
  return `<td class="matrix-val">${shown}<span class="pct ${cls}">${arrow} ${fmt(Math.abs(reduction), 0)}%</span></td>`;
}

function renderLongitudinal() {
  const box = $("longitudinal-table");
  if (!box) return;
  const sessions = state.order.length ? state.order : LEGACY_SESSION_IDS;
  const anyMeasurement = sessions.some((id) => (state.sessions[id]?.measurements || []).length);
  if (!anyMeasurement) {
    box.innerHTML = `<div class="results-empty">Ainda não há medições registradas.<br />Meça as regiões-alvo para gerar a matriz longitudinal.</div>`;
    return;
  }

  const metrics = [
    { key: "mean_width_mm", label: "Largura média", unit: "mm", digits: 1 },
    { key: "area_mm2", label: "Área", unit: "mm²", digits: 1 },
    { key: "delta_e_00", label: "ΔE00", unit: "", digits: 2 },
  ];

  const t0 = baselineSession();
  const rows = LESIONS.map((meta) => {
    const t0Measurement = measurementFor(t0, meta.code);
    const perSession = sessions.map((id) => measurementFor(state.sessions[id], meta.code));
    const metricRows = metrics.map((metric) => {
      const cells = perSession.map((m, index) => {
        if (!m) return `<td class="empty">—</td>`;
        const value = Number(m[metric.key]);
        const isT0 = sessionCode(state.sessions[sessions[index]]) === "T0";
        return isT0
          ? `<td class="matrix-val">${fmt(value, metric.digits)}</td>`
          : metricCell(value, t0Measurement ? Number(t0Measurement[metric.key]) : null, metric.digits, true);
      }).join("");
      return `<tr><td class="metric-name">${metric.label}${metric.unit ? ` <span class="muted">(${metric.unit})</span>` : ""}</td>${cells}</tr>`;
    }).join("");
    return `
      <tr class="group-start"><td colspan="${sessions.length + 1}" class="stria-row"><span class="stria-name"><span class="stria-dot" style="background:${meta.ink}"></span>${esc(lesionLabel(t0, meta.code))}</span></td></tr>
      ${metricRows}`;
  }).join("");

  const headers = sessions.map((id) => `<th>${esc(sessionCode(state.sessions[id]) || id)}</th>`).join("");
  box.innerHTML = `
    <div class="matrix-wrap">
      <table class="matrix">
        <thead>
          <tr><th>Métrica</th>${headers}</tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <div class="matrix-legend">
      <span><span class="pct good">↓</span> redução vs. T0</span>
      <span><span class="pct bad">↑</span> aumento vs. T0</span>
      <span>ΔE00: menor = mais próximo da pele saudável ${helpBadge("help-glossary-deltae", "ΔE00 abaixo de 1,0 é imperceptível; acima de 3,5 é contraste evidente.")}</span>
    </div>`;
}

function csvNumber(value, digits = 2) {
  if (value === null || value === undefined) return "";
  return Number(value).toFixed(digits).replace(".", ",");
}

function exportCSV() {
  const lines = [];
  lines.push([
    "sessao", "data_captura", "rotulo", "regiao", "descricao_regiao", "comprimento_mm", "largura_media_mm",
    "largura_maxima_mm", "area_mm2", "delta_e_00", "vertices", "medido_em",
    "end_dor", "likert_satisfacao", "gais",
  ].join(";"));
  const exportOrder = state.order.length ? state.order : LEGACY_SESSION_IDS;
  exportOrder.forEach((id) => {
    const session = state.sessions[id];
    if (!session) return;
    (session.measurements || []).forEach((m) => {
      const scales = session.clinical_scales || {};
      lines.push([
        sessionCode(session) || id,
        formatDate(session.captured_at),
        `"${(session.label || "").replace(/"/g, '""')}"`,
        lesionCode(m.stria_code),
        `"${lesionLabel(session, m.stria_code, m.stria_label).replace(/"/g, '""')}"`,
        csvNumber(m.length_mm),
        csvNumber(m.mean_width_mm),
        csvNumber(m.max_width_mm),
        csvNumber(m.area_mm2),
        csvNumber(m.delta_e_00),
        m.polygon?.length ?? "",
        `"${m.created_at || ""}"`,
        scales.end_pain ?? "",
        scales.likert_satisfaction ?? "",
        scales.gais_score ?? "",
      ].join(";"));
    });
  });

  if (lines.length <= 1) {
    toast("Sem medições para exportar.", "error");
    return;
  }

  const blob = new Blob([`\uFEFF${lines.join("\r\n")}`], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  const stamp = new Date().toISOString().slice(0, 10);
  link.href = url;
  link.download = `dermametric_longitudinal_${stamp}.csv`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
  toast("Relatório longitudinal exportado.", "success");
}

async function exportPatientPDF() {
  const patient = state.patient;
  if (!patient) {
    toast("Abra um prontuário antes de exportar o laudo.", "error");
    return;
  }
  const headers = {};
  if (state.auth.token) headers.Authorization = `Bearer ${state.auth.token}`;
  const pin = state.patientPin[patient.id];
  if (pin) headers["X-Patient-Pin"] = pin;
  // O laudo é sigiloso por padrão. O nome completo só é enviado quando o
  // profissional marca a opção E o prontuário está efetivamente desbloqueado.
  const unlocked = !patient.has_pin || Boolean(pin);
  const wantsName = Boolean($("pdf-include-name")?.checked);
  if (wantsName && !unlocked) {
    toast("Desbloqueie o prontuário com o PIN antes de incluir o nome no laudo.", "error", 6000);
    return;
  }
  const queryString = wantsName ? "?include_name=true" : "";

  setBusy(true, "Gerando laudo em PDF…");
  try {
    const response = await fetch(
      `/api/patients/${encodeURIComponent(patient.id)}/report.pdf${queryString}`,
      { headers },
    );
    if (!response.ok) {
      let detail = `Falha ao gerar o laudo (${response.status})`;
      try {
        const payload = await response.json();
        if (payload && typeof payload.detail === "string") detail = payload.detail;
      } catch { /* resposta sem JSON */ }
      throw new Error(detail);
    }
    const blob = await response.blob();
    const disposition = response.headers.get("content-disposition") || "";
    const match = /filename="?([^";]+)"?/i.exec(disposition);
    const stamp = new Date().toISOString().slice(0, 10);
    const filename = match
      ? match[1]
      : `DermaMetric_${patient.code || "paciente"}_${stamp}.pdf`;

    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 6000);
    toast(`Laudo de ${patient.code || "paciente"} exportado em PDF.`, "success", 4600);
  } catch (error) {
    toast(error.message, "error", 6000);
  } finally {
    setBusy(false);
  }
}

/* --------------------------------------------------------------------------
   Backup e restauração proprietários (.dmbak)
   -------------------------------------------------------------------------- */
function backupResponseFilename(response, fallback) {
  const disposition = response.headers.get("content-disposition") || "";
  const match = /filename="?([^";]+)"?/i.exec(disposition);
  return match ? match[1] : fallback;
}

function triggerBlobDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 6000);
}

async function requestBackupExport(password, patientId = null, archivePassword = "") {
  const headers = { "Content-Type": "application/json" };
  if (DESKTOP_LAUNCH_TOKEN) headers["X-DermaMetric-Launch"] = DESKTOP_LAUNCH_TOKEN;
  if (state.auth.token) headers.Authorization = `Bearer ${state.auth.token}`;
  const body = { password };
  if (patientId) body.patient_id = patientId;
  const dedicated = String(archivePassword || "").trim();
  if (patientId && dedicated) body.archive_password = dedicated;
  const response = await fetch("/api/backup/export", {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = `Falha ao exportar o backup (${response.status})`;
    try {
      const payload = await response.json();
      if (payload && typeof payload.detail === "string") detail = payload.detail;
    } catch { /* resposta binária/erro sem JSON */ }
    throw new Error(detail);
  }
  const blob = await response.blob();
  const fallback = patientId
    ? `DermaMetric-Caso-${state.patient?.code || "Paciente"}-${new Date().toISOString().slice(0, 10).replace(/-/g, "")}.dmbak`
    : `DermaMetric-Backup-Completo-${new Date().toISOString().slice(0, 16).replace(/[-:T]/g, "").slice(0, 13)}.dmbak`;
  const filename = backupResponseFilename(response, fallback);
  triggerBlobDownload(blob, filename);
  return filename;
}

/* --------------------------------------------------------------------------
   Abas do modal de perfil (Dados · Segurança · Backup)
   -------------------------------------------------------------------------- */
const PROFILE_TABS = ["profile", "security", "backup"];

function switchProfileTab(tabName) {
  const name = PROFILE_TABS.includes(tabName) ? tabName : "profile";
  state.profileTab = name;
  document.querySelectorAll("[data-profile-tab]").forEach((button) => {
    const active = button.dataset.profileTab === name;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  });
  PROFILE_TABS.forEach((tab) => {
    const panel = $(`profile-panel-${tab}`);
    if (panel) panel.hidden = tab !== name;
  });
  const body = document.querySelector("#profile-modal .modal-body");
  if (body) body.scrollTop = 0;
}

async function exportFullBackup() {
  const password = $("backup-export-password")?.value || "";
  setLockError("backup-export-error", "");
  if (!password) {
    setLockError("backup-export-error", "Confirme a senha mestra para exportar o backup.");
    return;
  }
  setBusy(true, "Cifrando backup completo (.dmbak)…");
  try {
    const filename = await requestBackupExport(password, null);
    $("backup-export-password").value = "";
    toast(`Backup completo exportado: ${filename}`, "success", 5200);
  } catch (error) {
    setLockError("backup-export-error", error.message);
  } finally {
    setBusy(false);
  }
}

function resetExportCaseExtras() {
  const checkbox = $("export-case-custom-password");
  if (checkbox) checkbox.checked = false;
  const field = $("export-case-file-password-field");
  if (field) field.hidden = true;
  const password = $("export-case-file-password");
  if (password) {
    password.value = "";
    password.type = "password";
  }
  const toggle = $("export-case-file-password-toggle");
  if (toggle) {
    toggle.setAttribute("aria-pressed", "false");
    toggle.setAttribute("aria-label", "Mostrar senha");
    toggle.title = "Mostrar senha";
  }
}

function syncExportCasePasswordField() {
  const checkbox = $("export-case-custom-password");
  const field = $("export-case-file-password-field");
  const enabled = Boolean(checkbox?.checked);
  if (field) field.hidden = !enabled;
  if (enabled) setTimeout(() => $("export-case-file-password")?.focus(), 30);
}

function toggleExportCasePasswordVisibility() {
  const input = $("export-case-file-password");
  const toggle = $("export-case-file-password-toggle");
  if (!input) return;
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  if (toggle) {
    toggle.setAttribute("aria-pressed", String(show));
    toggle.setAttribute("aria-label", show ? "Ocultar senha" : "Mostrar senha");
    toggle.title = show ? "Ocultar senha" : "Mostrar senha";
  }
}

function closeExportCaseModal() {
  const modal = $("export-case-modal");
  if (!modal) return;
  modal.hidden = true;
  modal.setAttribute("aria-hidden", "true");
  const input = $("export-case-password");
  if (input) input.value = "";
  resetExportCaseExtras();
  setLockError("export-case-error", "");
  if (state.screen !== "lock") document.body.classList.remove("has-modal");
}

function exportPatientCaseBackup() {
  const patient = state.patient;
  if (!patient) {
    toast("Abra um prontuário antes de exportar o caso.", "error");
    return;
  }
  const modal = $("export-case-modal");
  if (!modal) return;
  setLockError("export-case-error", "");
  const input = $("export-case-password");
  if (input) input.value = "";
  resetExportCaseExtras();
  const title = $("export-case-title");
  if (title) title.textContent = `Exportar Caso ${patient.code || ""} (.dmbak)`;
  const desc = $("export-case-desc");
  if (desc) {
    desc.textContent = `Informe a senha mestra da estação para cifrar o prontuário ${patient.code || ""} com todas as sessões e imagens.`;
  }
  modal.hidden = false;
  modal.removeAttribute("hidden");
  modal.setAttribute("aria-hidden", "false");
  document.body.classList.add("has-modal");
  setTimeout(() => input?.focus(), 40);
}

async function submitExportCaseBackup(event) {
  event?.preventDefault?.();
  const patient = state.patient;
  if (!patient) {
    closeExportCaseModal();
    return;
  }
  const password = $("export-case-password")?.value || "";
  const useCustom = Boolean($("export-case-custom-password")?.checked);
  const archivePassword = useCustom ? ($("export-case-file-password")?.value || "") : "";
  setLockError("export-case-error", "");
  if (!password) {
    setLockError("export-case-error", "Informe a senha mestra para exportar o caso.");
    return;
  }
  if (useCustom) {
    if (!archivePassword) {
      setLockError("export-case-error", "Defina a senha específica do arquivo do caso.");
      return;
    }
    if (archivePassword.length < 6) {
      setLockError("export-case-error", "A senha do arquivo deve ter ao menos 6 caracteres.");
      return;
    }
  }
  setBusy(true, "Gerando backup do caso (.dmbak)…");
  try {
    const filename = await requestBackupExport(password, patient.id, archivePassword);
    closeExportCaseModal();
    const note = archivePassword
      ? " Protegido pela senha específica do arquivo."
      : "";
    toast(`Caso ${patient.code || ""} exportado: ${filename}.${note}`.trim(), "success", 5200);
  } catch (error) {
    setLockError("export-case-error", error.message);
  } finally {
    setBusy(false);
  }
}

function backupPreviewHtml(summary) {
  const professional = summary.professional || {};
  const counts = summary.counts || {};
  const codes = Array.isArray(summary.patient_codes) ? summary.patient_codes : [];
  const conflicts = Array.isArray(summary.conflicts) ? summary.conflicts : [];
  const modeLabel = summary.mode === "selected"
    ? `Seleção de análises (${summary.selected_count ?? codes.length})`
    : summary.mode === "patient"
      ? "Caso individual"
      : "Completo";
  const created = formatTimestamp(summary.created_at) || summary.created_at || "—";
  const origin = [professional.name, professional.registry]
    .filter(Boolean)
    .join(" · ") || "Profissional não identificado";
  const institution = professional.institution
    ? `<span class="backup-origin-inst">${esc(professional.institution)}</span>`
    : "";
  return `
    <div class="backup-card-head">
      <span class="backup-mode">${esc(modeLabel)}</span>
      <span class="backup-date">${esc(created)}</span>
    </div>
    <div class="backup-origin">${esc(origin)}${institution}</div>
    <div class="backup-stats">
      <span><b>${esc(String(counts.patients ?? codes.length))}</b> pacientes</span>
      <span><b>${esc(String(counts.sessions ?? 0))}</b> sessões</span>
      <span><b>${esc(String(counts.measurements ?? 0))}</b> medições</span>
      <span><b>${esc(String(counts.images ?? 0))}</b> imagens</span>
    </div>
    <div class="backup-codes">${codes.map((code) => `<code>${esc(code)}</code>`).join("") || "<span>Sem códigos</span>"}</div>
    ${conflicts.length
      ? `<p class="backup-conflict">Atenção: ${conflicts.length} código(s) já existem nesta estação (${esc(conflicts.join(", "))}). No modo Mesclar, os dados locais são preservados.</p>`
      : '<p class="backup-clean">Nenhum conflito de código detectado nesta estação.</p>'}
    <div class="backup-preview-actions">
      <button type="button" class="btn btn-soft btn-sm" data-backup-restore="merge">Restaurar em modo Mesclar</button>
      <button type="button" class="btn btn-danger-ghost btn-sm" data-backup-restore="replace">Substituir dados clínicos</button>
    </div>
  `;
}

const BACKUP_STEPS = [
  { percent: 10, label: "1/5 Lendo cabeçalho DMBAK..." },
  { percent: 30, label: "2/5 Validando senha e integridade HMAC..." },
  { percent: 50, label: "3/5 Criando snapshot de segurança..." },
  { percent: 75, label: "4/5 Recifrando imagens e gravando registros..." },
  { percent: 90, label: "5/5 Sincronizando prontuários na tela..." },
];

function setBackupProgress(percent, label) {
  const value = Math.max(0, Math.min(100, Math.round(Number(percent) || 0)));
  const bar = $("backup-progress-bar");
  const percentNode = $("backup-progress-percent");
  const stepNode = $("backup-progress-step");
  if (bar) {
    bar.style.width = `${value}%`;
    bar.setAttribute("aria-valuenow", String(value));
  }
  if (percentNode) percentNode.textContent = `${value}%`;
  if (stepNode && label) stepNode.textContent = label;
}

function backupLogLine(message, level = "info") {
  const log = $("backup-log");
  if (!log) return;
  const time = new Date().toLocaleTimeString("pt-BR", { hour12: false });
  const line = document.createElement("div");
  line.className = `backup-log-line is-${level}`;
  const stamp = document.createElement("span");
  stamp.className = "backup-log-time";
  stamp.textContent = time;
  const text = document.createElement("span");
  text.className = "backup-log-msg";
  text.textContent = String(message);
  line.append(stamp, text);
  log.appendChild(line);
  log.scrollTop = log.scrollHeight;
}

function resetBackupProgress() {
  const panel = $("backup-progress");
  if (panel) panel.hidden = true;
  const log = $("backup-log");
  if (log) log.innerHTML = "";
  const finish = $("backup-finish");
  if (finish) finish.hidden = true;
  setBackupProgress(0, "Preparando restauração…");
}

function startBackupStageTicker() {
  let index = 0;
  const first = BACKUP_STEPS[index];
  setBackupProgress(first.percent, first.label);
  backupLogLine(first.label, "step");
  const timer = window.setInterval(() => {
    // Mantém em 4/5 até a resposta do servidor; 5/5 é aplicado na sincronização.
    if (index >= BACKUP_STEPS.length - 2) return;
    index += 1;
    const step = BACKUP_STEPS[index];
    setBackupProgress(step.percent, step.label);
    backupLogLine(step.label, "step");
  }, 850);
  return timer;
}

function resetClinicalUiAfterRestore() {
  renderToken += 1;
  currentImage = null;
  currentImageUrl = null;
  state.patient = null;
  state.sessions = {};
  state.order = [];
  state.sessionId = null;
  state.patientPin = {};
  state.unlockTarget = null;
  state.step = 1;
  state.polygon = [];
  state.polygonActive = false;
  closePatientModal();
  closePinModal();
  closeLockPatientModal();
}

/**
 * Banner de falha segura, fora do modal.
 *
 * Mesmo que a camada do cartão do modal não repinte no Brave/Wayland, o
 * usuário continua vendo o resumo da restauração e consegue fechar a caixa.
 */
function showRestoreBanner(title, detail, kind = "success") {
  const banner = $("restore-banner");
  if (!banner) return;
  const titleNode = $("restore-banner-title");
  const detailNode = $("restore-banner-detail");
  if (titleNode) titleNode.textContent = title;
  if (detailNode) detailNode.textContent = detail;
  banner.classList.toggle("is-error", kind === "error");
  banner.hidden = false;
}

function hideRestoreBanner() {
  const banner = $("restore-banner");
  if (banner) banner.hidden = true;
}

/**
 * Fechar do banner também encerra o modal de perfil: se o cartão estiver
 * branco/travado, este é o caminho garantido de saída.
 */
function dismissRestoreBanner() {
  hideRestoreBanner();
  const modal = $("profile-modal");
  if (modal && !modal.hidden) closeProfileModal();
}

/**
 * Garante que o cartão do modal de perfil continue visível com o relatório.
 *
 * Em Brave/Chromium sobre Wayland o cartão já apareceu completamente branco
 * depois de uma restauração pesada: a camada composta era descartada e nada
 * do conteúdo interno repintava. Aqui reexibimos o modal, forçamos um
 * reflow real do cartão (display:none -> leitura de layout -> display) e
 * rolados até o painel de progresso. O banner externo cobre o fail-safe.
 */
function revealBackupReport(scroll = true) {
  const modal = $("profile-modal");
  if (!modal) return;
  modal.hidden = false;
  modal.removeAttribute("hidden");
  modal.setAttribute("aria-hidden", "false");
  document.body.classList.add("has-modal");

  // O relatório vive na aba de backup: ative-a antes de revelar progresso/logs.
  switchProfileTab("backup");

  const panel = $("backup-progress");
  if (panel) {
    panel.hidden = false;
    panel.removeAttribute("hidden");
  }

  const head = modal.querySelector(".modal-head");
  const body = modal.querySelector(".modal-body");
  if (head) head.style.display = "flex";
  if (body) body.style.display = "block";

  if (panel && scroll && body) {
    body.scrollTop = 0;
  }
}

async function syncDirectoryAfterRestore() {
  resetClinicalUiAfterRestore();
  setScreen("directory");
  const directory = $("patient-directory");
  if (directory) directory.hidden = false;
  const shell = document.querySelector(".app-shell");
  if (shell) shell.hidden = true;
  await loadProfile();
  await loadPatients();
  renderPatients();
  // O modal de perfil permanece aberto com o relatório até "Concluir".
  revealBackupReport();
}

async function finishBackupRestore() {
  hideRestoreBanner();
  closeProfileModal();
  setScreen("directory");
  const directory = $("patient-directory");
  if (directory) directory.hidden = false;
  const shell = document.querySelector(".app-shell");
  if (shell) shell.hidden = true;
  try {
    await loadProfile();
    await loadPatients();
    renderPatients();
    window.scrollTo({ top: 0, behavior: "smooth" });
  } catch (error) {
    toast(`Não foi possível recarregar os pacientes: ${error.message}`, "error", 6000);
  }
}

function updateBackupFileStatus(file) {
  const box = $("backup-file-drop");
  const status = $("backup-file-status");
  if (box) box.classList.toggle("has-file", Boolean(file));
  if (status) {
    if (file) {
      const sizeKb = Math.max(1, Math.round(file.size / 1024));
      status.textContent = `Arquivo carregado: ${file.name} (${sizeKb} KB)`;
    } else {
      status.textContent = "Nenhum arquivo selecionado. Clique acima para escolher ou arraste o .dmbak aqui.";
    }
  }
}

async function inspectBackupFile() {
  const file = state.selectedBackupFile || $("backup-file")?.files?.[0];
  const password = $("backup-restore-password")?.value || "";
  setLockError("backup-restore-error", "");
  const preview = $("backup-preview");
  if (preview) {
    preview.hidden = true;
    preview.innerHTML = "";
  }
  resetBackupProgress();
  state.backupSummary = null;
  if (!file) {
    setLockError("backup-restore-error", "Selecione um arquivo .dmbak para inspecionar.");
    return;
  }
  if (!password) {
    setLockError("backup-restore-error", "Informe a senha definida na exportação (mestra ou do pacote).");
    return;
  }
  const form = new FormData();
  form.append("file", file);
  form.append("password", password);
  setBusy(true, "Validando assinatura e integridade do backup…");
  try {
    const summary = await api("/api/backup/inspect", { method: "POST", body: form });
    state.backupSummary = summary;
    if (preview) {
      preview.innerHTML = backupPreviewHtml(summary);
      preview.hidden = false;
    }
    toast("Backup válido: assinatura, senha e checksums conferidos.", "success", 4600);
  } catch (error) {
    setLockError("backup-restore-error", error.message);
  } finally {
    setBusy(false);
  }
}

async function restoreBackupFile(mode) {
  const file = state.selectedBackupFile || $("backup-file")?.files?.[0];
  const password = $("backup-restore-password")?.value || "";
  setLockError("backup-restore-error", "");
  if (!file || !password) {
    setLockError("backup-restore-error", "Selecione o arquivo e informe a senha mestra.");
    return;
  }
  if (!state.backupSummary) {
    setLockError("backup-restore-error", "Inspecione o arquivo antes de restaurar.");
    return;
  }
  const verb = mode === "replace"
    ? "SUBSTITUIR todos os dados clínicos atuais pelos do backup"
    : "MESCLAR o backup com os dados atuais (sem apagar prontuários locais)";
  if (!window.confirm(`Confirma ${verb}?\n\nUm snapshot automático pre_restore_*.dmbak será criado antes da restauração.`)) {
    return;
  }

  const form = new FormData();
  form.append("file", file);
  form.append("password", password);
  form.append("mode", mode);

  resetBackupProgress();
  const panel = $("backup-progress");
  if (panel) panel.hidden = false;
  const finishButton = $("backup-finish");
  if (finishButton) finishButton.hidden = true;
  setBusy(true);
  backupLogLine(`Arquivo selecionado: ${file.name} (${Math.max(1, Math.round(file.size / 1024))} KB)`, "info");
  backupLogLine(`Modo escolhido: ${mode === "replace" ? "Substituir" : "Mesclar"}`, "info");
  backupLogLine("Validação prévia concluída na inspeção do arquivo.", "success");
  const ticker = startBackupStageTicker();
  /** Preenchido assim que o backend confirma a restauração (antes do sync da tela). */
  let restoreConfirmed = null;

  try {
    const result = await api("/api/backup/restore", { method: "POST", body: form });
    window.clearInterval(ticker);
    restoreConfirmed = result;
    setBackupProgress(80, BACKUP_STEPS[3].label);
    (result.logs || []).forEach((message) => {
      const text = String(message);
      const level = /erro|falha|corromp|inválid|incorret/i.test(text) ? "error" : "success";
      backupLogLine(text, level);
    });
    (result.imported_patients || []).forEach((patient) => {
      const status = patient.status === "created"
        ? "criado"
        : patient.status === "merged"
          ? "mesclado com dados locais"
          : "ignorado";
      backupLogLine(
        `Paciente ${patient.patient_code} ${status} · `
        + `${patient.sessions_count} sessão(ões) · ${patient.images_count ?? 0} imagem(ns)`,
        patient.status === "skipped" ? "info" : "success",
      );
    });
    setBackupProgress(90, BACKUP_STEPS[4].label);
    backupLogLine("Recarregando GET /api/patients e re-renderizando o diretório…", "step");
    await syncDirectoryAfterRestore();
    const totalPatients = result.imported_patients?.length ?? 0;
    const totalSessions = result.total_sessions ?? result.sessions ?? 0;
    const totalMeasurements = result.total_measurements ?? result.measurements ?? 0;
    const totalImages = result.total_images ?? result.images ?? 0;
    setBackupProgress(100, "5/5 Prontuários sincronizados");
    backupLogLine(
      `Restauração concluída: ${totalPatients} paciente(s), ${totalSessions} sessão(ões), `
      + `${totalMeasurements} medição(ões), ${totalImages} imagem(ns).`,
      "success",
    );
    if (result.backup_path) {
      backupLogLine(`Snapshot preventivo: ${result.backup_path}`, "info");
    }
    if (finishButton) finishButton.hidden = false;
    revealBackupReport();
    showRestoreBanner(
      `Backup restaurado: ${totalPatients} paciente(s), ${totalSessions} sessão(ões)`,
      `Modo ${result.mode === "replace" ? "Substituir" : "Mesclar"} · `
        + `${totalMeasurements} medição(ões) · ${totalImages} imagem(ns).`,
      "success",
    );
  } catch (error) {
    window.clearInterval(ticker);
    const message = error.message || "Falha desconhecida na restauração.";
    if (restoreConfirmed) {
      // O backend já gravou o backup; só a reidratação da tela falhou.
      const patients = restoreConfirmed.imported_patients?.length ?? 0;
      const sessions = restoreConfirmed.total_sessions ?? restoreConfirmed.sessions ?? 0;
      setBackupProgress(100, "Restauração concluída; falha ao sincronizar a tela");
      backupLogLine(`Backup já gravado no disco. Falha ao atualizar a tela: ${message}`, "error");
      setLockError("backup-restore-error", `Backup gravado, mas a tela não sincronizou: ${message}`);
      revealBackupReport();
      showRestoreBanner(
        `Backup restaurado: ${patients} paciente(s), ${sessions} sessão(ões)`,
        `A tela não sincronizou: ${message}`,
        "error",
      );
    } else {
      setBackupProgress(100, "Falha na restauração");
      backupLogLine(message, "error");
      setLockError("backup-restore-error", message);
      // Mesmo com erro o cartão precisa permanecer legível com o log da falha.
      revealBackupReport();
      showRestoreBanner("Falha na restauração", message, "error");
    }
  } finally {
    window.clearInterval(ticker);
    setBusy(false);
  }
}

function openCompare() {
  renderCompare();
  const modal = $("compare-modal");
  if (!modal) return;
  modal.hidden = false;
  modal.setAttribute("aria-hidden", "false");
  document.body.classList.add("has-modal");
  setTimeout(() => $("pdf-report")?.focus(), 40);
}

function closeCompare() {
  const modal = $("compare-modal");
  if (!modal) return;
  modal.hidden = true;
  modal.setAttribute("aria-hidden", "true");
  document.body.classList.remove("has-modal");
}

/* --------------------------------------------------------------------------
   Saúde da API e inicialização
   -------------------------------------------------------------------------- */
async function checkHealth() {
  const badge = $("health-badge");
  const text = $("health-text");
  try {
    const health = await api("/api/health");
    state.health = health;
    setAppVersion(health?.version);
    if (badge) {
      badge.classList.remove("is-pending", "is-offline");
      badge.classList.add("is-online");
    }
    if (text) text.textContent = "Sistema online · dados cifrados";
  } catch {
    if (badge) {
      badge.classList.remove("is-pending", "is-online");
      badge.classList.add("is-offline");
    }
    if (text) text.textContent = "Sistema offline";
  }
}

/* --------------------------------------------------------------------------
   Atualizações do DermaMetric (GitHub Releases)
   -------------------------------------------------------------------------- */
const UPDATE_STATUS_LABELS = {
  idle: "Pronto para verificar atualizações.",
  checking: "Verificando atualizações…",
  downloading: "Baixando atualização…",
  installing: "Instalando atualização…",
  restarting: "Reiniciando o DermaMetric…",
  error: "Falha na atualização.",
};

function normalizeVersionLabel(value) {
  const clean = String(value || "").trim().replace(/^v/i, "");
  return clean ? `v${clean}` : "—";
}

function setAppVersion(version) {
  const clean = String(version || "").trim().replace(/^v/i, "");
  const label = $("app-version");
  if (label) label.textContent = clean ? `v${clean}` : "v—";
  const current = $("update-current-version");
  if (current && clean) current.textContent = `v${clean}`;
}

function openUpdateModal() {
  const modal = $("update-modal");
  if (!modal) return;
  modal.hidden = false;
  modal.setAttribute("aria-hidden", "false");
  document.body.classList.add("has-modal");
  const shell = modal.querySelector(".update-modal") || modal.querySelector(".modal-window");
  setTimeout(() => shell?.focus({ preventScroll: true }), 40);
  checkForUpdates();
}

function closeUpdateModal() {
  const modal = $("update-modal");
  if (!modal || modal.hidden) return;
  modal.hidden = true;
  modal.setAttribute("aria-hidden", "true");
  stopUpdatePolling();
  const otherOpen = [
    "profile-modal", "patient-modal", "pin-modal", "lock-patient-modal",
    "compare-modal", "personal-data-modal", "delete-patient-modal", "help-modal",
  ].some((id) => {
    const node = $(id);
    return node && !node.hidden;
  });
  if (state.screen !== "lock" && !otherOpen) document.body.classList.remove("has-modal");
}

function showUpdateLink(force = false) {
  const link = $("update-link");
  if (!link) return;
  const info = state.update.info;
  if (info?.html_url) link.href = info.html_url;
  link.hidden = !force && !(info?.update_available && !info?.can_auto_update);
}

async function checkForUpdates() {
  if (state.update.busy) return;
  state.update.busy = true;
  const button = $("update-check");
  if (button) button.disabled = true;
  setLockError("update-error", "");
  const summary = $("update-summary");
  if (summary) {
    summary.hidden = false;
    summary.textContent = "Consultando o GitHub Releases…";
  }
  try {
    const info = await api("/api/system/update/check");
    state.update.info = info || null;
    renderUpdateInfo(info || {});
  } catch (error) {
    if (summary) {
      summary.hidden = false;
      summary.textContent = "Não foi possível verificar atualizações agora.";
    }
    setLockError("update-error", error.message || "Falha ao consultar atualizações.");
    showUpdateLink(true);
  } finally {
    state.update.busy = false;
    if (button) button.disabled = false;
  }
}

function renderUpdateInfo(info) {
  const current = normalizeVersionLabel(info.current_version || state.health?.version);
  const latest = info.latest_version ? normalizeVersionLabel(info.latest_version) : "—";
  const currentNode = $("update-current-version");
  const latestNode = $("update-latest-version");
  if (currentNode) currentNode.textContent = current;
  if (latestNode) latestNode.textContent = latest;

  const summary = $("update-summary");
  if (summary) {
    summary.hidden = false;
    if (info.update_available) {
      summary.textContent = `Nova versão ${latest} disponível. Instalada: ${current}.`;
    } else if (latest !== "—") {
      summary.textContent = `Você está na versão mais recente (${current}).`;
    } else {
      summary.textContent = "Nenhuma release publicada foi encontrada.";
    }
  }

  const notesWrap = $("update-notes-wrap");
  const notes = $("update-notes");
  const notesVersion = $("update-notes-version");
  const releaseNotes = String(info.release_notes || "").trim();
  if (notesWrap) notesWrap.hidden = !releaseNotes;
  if (notes) notes.textContent = releaseNotes;
  if (notesVersion) notesVersion.textContent = releaseNotes ? latest : "";

  const apply = $("update-apply");
  const canAuto = Boolean(info.update_available && info.can_auto_update);
  if (apply) {
    apply.hidden = !canAuto;
    apply.disabled = false;
    apply.textContent = "Atualizar agora";
  }
  setLockError("update-error", "");
  showUpdateLink();
  if (!canAuto) {
    const progress = $("update-progress");
    if (progress) progress.hidden = true;
    const statusMessage = $("update-status-message");
    if (statusMessage) statusMessage.hidden = true;
  }
}

function renderUpdateStatus(status) {
  if (!status || typeof status !== "object") return;
  state.update.status = status;
  const kind = String(status.status || "idle");
  const progress = Math.max(0, Math.min(100, Math.round(Number(status.progress) || 0)));
  const running = ["downloading", "installing", "restarting"].includes(kind);
  const downloaded = Number(status.downloaded_bytes) || 0;
  const total = Number(status.total_bytes) || 0;

  const wrap = $("update-progress");
  if (wrap) wrap.hidden = !(running || progress > 0 || kind === "error");
  const bar = $("update-progress-bar");
  if (bar) bar.style.width = `${progress}%`;
  const track = $("update-progress-track");
  if (track) track.setAttribute("aria-valuenow", String(progress));
  const label = $("update-progress-label");
  if (label) label.textContent = `${progress}%`;
  const bytes = $("update-progress-bytes");
  if (bytes) {
    bytes.textContent = total > 0
      ? `${bytesLabel(downloaded)} de ${bytesLabel(total)}`
      : downloaded > 0 ? bytesLabel(downloaded) : "";
  }

  const message = String(status.message || UPDATE_STATUS_LABELS[kind] || "").trim();
  const messageNode = $("update-status-message");
  if (messageNode) {
    messageNode.hidden = !message;
    messageNode.textContent = message;
  }

  if (kind === "error") {
    setLockError("update-error", status.error || status.message || "Falha ao aplicar a atualização.");
    showUpdateLink(true);
  } else if (running) {
    setLockError("update-error", "");
  }

  const apply = $("update-apply");
  if (apply) {
    if (running) apply.hidden = true;
    apply.disabled = running;
  }
}

function stopUpdatePolling() {
  if (state.update.timer) {
    clearTimeout(state.update.timer);
    state.update.timer = null;
  }
}

function scheduleUpdatePoll() {
  stopUpdatePolling();
  state.update.timer = setTimeout(pollUpdateStatus, 1200);
}

async function pollUpdateStatus() {
  state.update.timer = null;
  try {
    const status = await api("/api/system/update/status");
    renderUpdateStatus(status);
    if (["downloading", "installing"].includes(String(status?.status || ""))) {
      scheduleUpdatePoll();
    }
  } catch (error) {
    setLockError("update-error", `Não foi possível acompanhar a atualização: ${error.message}`);
  }
}

async function applyUpdate() {
  const button = $("update-apply");
  if (button) button.disabled = true;
  setLockError("update-error", "");
  try {
    const status = await postJSON("/api/system/update/apply", {});
    renderUpdateStatus(status);
    scheduleUpdatePoll();
  } catch (error) {
    setLockError("update-error", error.message || "Não foi possível iniciar a atualização.");
    showUpdateLink(true);
    if (button) button.disabled = false;
  }
}

/* --------------------------------------------------------------------------
   Exclusão de prontuário (protegida pela senha mestra)
   -------------------------------------------------------------------------- */
function openDeletePatientModal(patientId) {
  const modal = $("delete-patient-modal");
  if (!modal) return;
  const patient = state.patients.find((item) => item.id === patientId)
    || (state.patient?.id === patientId ? state.patient : null);
  if (!patient) {
    toast("Prontuário não encontrado para exclusão.", "error");
    return;
  }
  state.deletePatient = {
    id: patient.id,
    code: patient.code || "—",
    name: patientDirectoryName(patient) || "Paciente",
  };
  const code = $("delete-patient-code");
  const name = $("delete-patient-name");
  if (code) code.textContent = state.deletePatient.code;
  if (name) name.textContent = state.deletePatient.name;
  const password = $("delete-patient-password");
  if (password) password.value = "";
  setLockError("delete-patient-error", "");
  modal.hidden = false;
  modal.setAttribute("aria-hidden", "false");
  document.body.classList.add("has-modal");
  setTimeout(() => password?.focus(), 40);
}

function closeDeletePatientModal() {
  const modal = $("delete-patient-modal");
  if (!modal) return;
  modal.hidden = true;
  modal.setAttribute("aria-hidden", "true");
  state.deletePatient = { id: null, code: "", name: "" };
  const password = $("delete-patient-password");
  if (password) password.value = "";
  setLockError("delete-patient-error", "");
  const otherOpen = [
    "profile-modal", "patient-modal", "pin-modal", "lock-patient-modal",
    "compare-modal", "personal-data-modal", "delete-patient-modal", "help-modal", "update-modal",
  ].some((id) => {
    const node = $(id);
    return node && !node.hidden;
  });
  if (state.screen !== "lock" && !otherOpen) document.body.classList.remove("has-modal");
}

async function confirmDeletePatient() {
  const patientId = state.deletePatient.id;
  if (!patientId) return;
  const password = $("delete-patient-password")?.value || "";
  if (!password) {
    setLockError("delete-patient-error", "Informe a senha mestra para excluir este prontuário.");
    $("delete-patient-password")?.focus();
    return;
  }
  const button = $("delete-patient-confirm");
  if (button) button.disabled = true;
  setLockError("delete-patient-error", "");
  setBusy(true, "Excluindo prontuário…");
  const code = state.deletePatient.code;
  try {
    const result = await postJSON(
      `/api/patients/${encodeURIComponent(patientId)}/delete`,
      { password, master_password: password },
    );
    const removed = Number(result?.removed_files) || 0;
    const wasActive = state.patient?.id === patientId;
    delete state.patientPin[patientId];
    closeDeletePatientModal();
    if (wasActive) {
      await showDirectory();
    } else {
      await loadPatients();
    }
    toast(
      `Prontuário ${result?.code || code} excluído definitivamente · ${removed} imagem(ns) removida(s).`,
      "success",
      5600,
    );
  } catch (error) {
    setLockError("delete-patient-error", error.message || "Não foi possível excluir o prontuário.");
  } finally {
    if (button) button.disabled = false;
    setBusy(false);
  }
}

/* --------------------------------------------------------------------------
   Guia de Uso & Metodologia (ajuda contextual offline)
   -------------------------------------------------------------------------- */
function helpBadge(target, tooltip) {
  const label = String(tooltip || "Abrir explicação metodológica");
  const safe = esc(label).replace(/"/g, "&quot;");
  return `<button type="button" class="help-badge" data-help="${esc(target)}" title="${safe}" aria-label="Ajuda: ${safe}">?</button>`;
}

function normalizeHelpText(value) {
  return String(value || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase();
}

function visibleHelpEntries(panel) {
  return panel ? panel.querySelectorAll("[data-help-entry]:not([hidden])").length : 0;
}

function updateHelpCounts() {
  const guide = $("help-guide");
  const glossary = $("help-glossary");
  if ($("help-count-guide")) $("help-count-guide").textContent = String(visibleHelpEntries(guide));
  if ($("help-count-glossary")) {
    $("help-count-glossary").textContent = String(visibleHelpEntries(glossary));
  }
}

function filterHelp() {
  const query = normalizeHelpText($("help-search")?.value || "").trim();
  document.querySelectorAll("[data-help-entry]").forEach((entry) => {
    if (!query) {
      entry.hidden = false;
      return;
    }
    const haystack = normalizeHelpText(`${entry.dataset.helpSearch || ""} ${entry.textContent}`);
    entry.hidden = !haystack.includes(query);
  });
  document.querySelectorAll("[data-help-group]").forEach((group) => {
    group.hidden = !group.querySelector("[data-help-entry]:not([hidden])");
  });
  ["guide", "glossary"].forEach((name) => {
    const panel = $(`help-${name}`);
    const empty = $(`help-empty-${name}`);
    if (!panel) return;
    const count = visibleHelpEntries(panel);
    if (empty) empty.hidden = !(query && count === 0);
  });
  updateHelpCounts();
}

function setHelpTab(tab) {
  const name = tab === "glossary" ? "glossary" : "guide";
  document.querySelectorAll("[data-help-tab]").forEach((button) => {
    const active = button.dataset.helpTab === name;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  });
  const guide = $("help-guide");
  const glossary = $("help-glossary");
  if (guide) guide.hidden = name !== "guide";
  if (glossary) glossary.hidden = name !== "glossary";
  const body = $("help-body");
  if (body) body.scrollTop = 0;
  filterHelp();
}

function openHelpModal(options = {}) {
  const modal = $("help-modal");
  if (!modal) return;
  const targetId = typeof options.target === "string" ? options.target : null;
  const tab = targetId
    ? (targetId.startsWith("help-glossary") ? "glossary" : "guide")
    : (options.tab === "glossary" ? "glossary" : "guide");
  const search = $("help-search");
  if (search) search.value = "";
  document.querySelectorAll(".help-entry.is-target").forEach((node) => {
    node.classList.remove("is-target");
  });
  modal.hidden = false;
  modal.setAttribute("aria-hidden", "false");
  document.body.classList.add("has-modal");
  setHelpTab(tab);
  const target = targetId ? document.getElementById(targetId) : null;
  requestAnimationFrame(() => {
    if (target) {
      target.classList.add("is-target");
      target.scrollIntoView({ block: "start", behavior: "auto" });
      target.focus({ preventScroll: true });
      setTimeout(() => target.classList.remove("is-target"), 2400);
      return;
    }
    const body = $("help-body");
    if (body) body.scrollTop = 0;
    const shell = modal.querySelector(".help-modal") || modal.querySelector(".modal-window");
    shell?.focus({ preventScroll: true });
  });
}

function closeHelpModal() {
  const modal = $("help-modal");
  if (!modal || modal.hidden) return;
  modal.hidden = true;
  modal.setAttribute("aria-hidden", "true");
  document.querySelectorAll(".help-entry.is-target").forEach((node) => {
    node.classList.remove("is-target");
  });
  const otherOpen = ["profile-modal", "patient-modal", "pin-modal", "lock-patient-modal", "compare-modal", "personal-data-modal"]
    .some((id) => {
      const node = $(id);
      return node && !node.hidden;
    });
  if (state.screen !== "lock" && !otherOpen) document.body.classList.remove("has-modal");
}

function onHelpKeyDown(event) {
  const modal = $("help-modal");
  if (!modal || modal.hidden) return;
  if (event.key !== "Escape") return;
  const search = $("help-search");
  if (search && search.value) {
    event.preventDefault();
    event.stopPropagation();
    search.value = "";
    filterHelp();
    search.focus();
    return;
  }
  event.preventDefault();
  event.stopPropagation();
  closeHelpModal();
}

function onHelpBadgeClick(event) {
  const badge = event.target.closest("[data-help]");
  if (!badge) return;
  event.preventDefault();
  event.stopPropagation();
  openHelpModal({ target: badge.dataset.help });
}

function bindEvents() {
  const canvas = $("stage");
  canvas.addEventListener("pointerdown", onPointerDown);
  canvas.addEventListener("pointermove", onPointerMove);
  canvas.addEventListener("pointerup", onPointerUp);
  canvas.addEventListener("pointercancel", onPointerUp);
  canvas.addEventListener("pointerleave", () => {
    if (!drag) {
      state.hover = null;
      state.hoverHit = null;
      updateLoupeVisibility();
      const tip = $("canvas-tip");
      if (tip) tip.hidden = true;
    }
  });
  canvas.addEventListener("dblclick", onDoubleClick);
  canvas.addEventListener("wheel", onWheel, { passive: false });
  canvas.addEventListener("contextmenu", (event) => event.preventDefault());

  window.addEventListener("keydown", onKeyDown);
  window.addEventListener("keyup", onKeyUp);
  window.addEventListener("keydown", onHelpKeyDown, true);
  document.addEventListener("click", onHelpBadgeClick, true);

  $("help-open")?.addEventListener("click", () => openHelpModal());
  document.querySelectorAll("[data-close-help]").forEach((node) => {
    node.addEventListener("click", closeHelpModal);
  });
  $("update-open")?.addEventListener("click", openUpdateModal);
  $("update-check")?.addEventListener("click", checkForUpdates);
  $("update-apply")?.addEventListener("click", applyUpdate);
  document.querySelectorAll("[data-close-update]").forEach((node) => {
    node.addEventListener("click", closeUpdateModal);
  });
  $("help-search")?.addEventListener("input", filterHelp);
  document.querySelectorAll("[data-help-tab]").forEach((button) => {
    button.addEventListener("click", () => setHelpTab(button.dataset.helpTab));
  });

  $("zoom-in")?.addEventListener("click", () => zoomAt(1.25, canvasSize.w / 2, canvasSize.h / 2));
  $("zoom-out")?.addEventListener("click", () => zoomAt(0.8, canvasSize.w / 2, canvasSize.h / 2));
  $("zoom-fit")?.addEventListener("click", resetView);
  $("rail-collapse-left")?.addEventListener("click", () => toggleRail("left"));
  $("rail-collapse-right")?.addEventListener("click", () => toggleRail("right"));
  $("canvas-focus")?.addEventListener("click", toggleCanvasFocus);
  $("loupe-toggle")?.addEventListener("click", (event) => {
    state.loupe = !state.loupe;
    event.currentTarget.classList.toggle("is-active", state.loupe);
    event.currentTarget.setAttribute("aria-pressed", String(state.loupe));
    if (!state.loupe) $("loupe").hidden = true;
    else if (currentImage && state.hover) {
      updateLoupeVisibility();
      drawLoupe(state.hover);
    }
  });

  $("compare-open")?.addEventListener("click", openCompare);
  $("compare-modal")?.addEventListener("click", (event) => {
    if (event.target.closest("[data-close-compare]")) closeCompare();
  });
  $("csv-export")?.addEventListener("click", exportCSV);
  $("pdf-report")?.addEventListener("click", exportPatientPDF);
  $("compare-anon")?.addEventListener("change", (event) => {
    state.compare.anon = event.target.checked;
    $("compare-modal")?.classList.toggle("is-anon", state.compare.anon);
    updateBA();
  });
  $("compare-mode")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-mode]");
    if (!button) return;
    state.compare.mode = button.dataset.mode;
    $("compare-mode").querySelectorAll("button").forEach((b) => b.classList.toggle("is-active", b === button));
    updateBA();
  });
  $("compare-sessions")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-compare-session]");
    if (!button || button.disabled) return;
    state.compare.sessionId = button.dataset.compareSession;
    state.compare.pos = 50;
    renderCompare();
  });
  $("gais-options")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-compare-gais]");
    if (!button) return;
    setGais(state.compare.sessionId, Number(button.dataset.compareGais));
  });

  const stage = $("ba-stage");
  let baDragging = false;
  stage?.addEventListener("pointerdown", (event) => {
    if (!compareImageUrls(state.compare.sessionId).top) return;
    baDragging = true;
    stage.setPointerCapture?.(event.pointerId);
    setComparePosition(event.clientX);
  });
  stage?.addEventListener("pointermove", (event) => {
    if (baDragging) setComparePosition(event.clientX);
  });
  stage?.addEventListener("pointerup", (event) => {
    baDragging = false;
    stage.releasePointerCapture?.(event.pointerId);
  });
  stage?.addEventListener("pointercancel", () => { baDragging = false; });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("compare-modal")?.hidden) closeCompare();
  });

  document.addEventListener("click", onDocumentClick);
  document.addEventListener("click", (event) => {
    if (!window.matchMedia("(max-width: 980px)").matches) return;
    const head = event.target.closest(".sidebar .panel-head, .results-panel .panel-head");
    if (head) head.parentElement.classList.toggle("is-collapsed");
  });

  let resizeFrame = 0;
  const ro = new ResizeObserver(() => {
    if (resizeFrame) cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(() => {
      resizeFrame = 0;
      renderCanvas();
      if (state.step === 3) drawReferencePanel();
    });
  });
  const wrap = $("canvas-wrap");
  if (wrap) ro.observe(wrap);
}

function bindPatientEvents() {
  $("setup-save")?.addEventListener("click", handleSetup);
  $("setup-confirm")?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); handleSetup(); }
  });
  $("login-ok")?.addEventListener("click", handleLogin);
  $("login-password")?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); handleLogin(); }
  });
  $("forgot-password")?.addEventListener("click", () => {
    setLockError("recover-error", "");
    setLockError("login-error", "");
    setDmkeyFeedback("", null);
    state.dmkeyKey = "";
    ["recover-key", "recover-password", "recover-confirm"].forEach((id) => {
      const input = $(id);
      if (input) input.value = "";
    });
    showLockView("recover");
  });
  $("recover-cancel")?.addEventListener("click", () => {
    setLockError("recover-error", "");
    showLockView("login");
  });
  $("recover-submit")?.addEventListener("click", handleRecover);
  ["recover-password", "recover-confirm"].forEach((id) => {
    $(id)?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); handleRecover(); }
    });
  });
  $("dmkey-file")?.addEventListener("change", (event) => {
    handleDmkeyFile(event.target.files?.[0]);
  });
  const dropzone = $("dmkey-drop");
  if (dropzone) {
    ["dragenter", "dragover"].forEach((type) => {
      dropzone.addEventListener(type, (event) => {
        event.preventDefault();
        dropzone.classList.add("is-dragging");
      });
    });
    ["dragleave", "dragend", "drop"].forEach((type) => {
      dropzone.addEventListener(type, () => dropzone.classList.remove("is-dragging"));
    });
    dropzone.addEventListener("drop", (event) => {
      event.preventDefault();
      handleDmkeyFile(event.dataTransfer?.files?.[0]);
    });
  }
  $("keyfile-download")?.addEventListener("click", downloadKeyFile);
  $("keyfile-copy")?.addEventListener("click", copyRecoveryKey);
  $("keyfile-print")?.addEventListener("click", printRecoverySheet);
  $("keyfile-confirm")?.addEventListener("change", (event) => {
    const done = $("keyfile-done");
    if (done) done.disabled = !event.target.checked;
  });
  $("keyfile-done")?.addEventListener("click", finishKeyFileStep);
  $("auth-logout")?.addEventListener("click", handleLogout);

  $("profile-open")?.addEventListener("click", openProfileModal);
  $("profile-form")?.addEventListener("submit", submitProfile);
  $("password-form")?.addEventListener("submit", submitChangePassword);
  $("regenerate-key")?.addEventListener("click", regenerateRecoveryKeyFile);
  $("backup-export-full")?.addEventListener("click", exportFullBackup);
  document.querySelectorAll("[data-profile-tab]").forEach((button) => {
    button.addEventListener("click", () => switchProfileTab(button.dataset.profileTab));
  });
  document.querySelector(".profile-tabs")?.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const order = ["profile", "security", "backup"];
    const current = order.indexOf(state.profileTab);
    let next = current;
    if (event.key === "ArrowRight") next = (current + 1) % order.length;
    if (event.key === "ArrowLeft") next = (current - 1 + order.length) % order.length;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = order.length - 1;
    event.preventDefault();
    switchProfileTab(order[next]);
    document.querySelector(`[data-profile-tab="${order[next]}"]`)?.focus();
  });
  $("backup-inspect")?.addEventListener("click", inspectBackupFile);
  $("backup-finish")?.addEventListener("click", finishBackupRestore);
  $("restore-banner-close")?.addEventListener("click", dismissRestoreBanner);
  $("backup-preview")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-backup-restore]");
    if (!button) return;
    restoreBackupFile(button.dataset.backupRestore);
  });
  $("backup-file")?.addEventListener("change", (event) => {
    const file = event.target?.files?.[0] || null;
    state.selectedBackupFile = file;
    state.backupSummary = null;
    updateBackupFileStatus(file);
    const preview = $("backup-preview");
    if (preview) {
      preview.hidden = true;
      preview.innerHTML = "";
    }
    resetBackupProgress();
    setLockError("backup-restore-error", "");
  });
  const backupDrop = $("backup-file-drop");
  if (backupDrop) {
    backupDrop.addEventListener("dragover", (event) => {
      event.preventDefault();
      backupDrop.classList.add("is-dragover");
    });
    backupDrop.addEventListener("dragleave", () => {
      backupDrop.classList.remove("is-dragover");
    });
    backupDrop.addEventListener("drop", (event) => {
      event.preventDefault();
      backupDrop.classList.remove("is-dragover");
      const file = event.dataTransfer?.files?.[0] || null;
      if (!file) return;
      state.selectedBackupFile = file;
      state.backupSummary = null;
      updateBackupFileStatus(file);
      const input = $("backup-file");
      if (input) {
        try {
          const dt = new DataTransfer();
          dt.items.add(file);
          input.files = dt.files;
        } catch {}
      }
      resetBackupProgress();
      setLockError("backup-restore-error", "");
    });
  }
  $("patient-export-backup")?.addEventListener("click", exportPatientCaseBackup);
  $("export-case-form")?.addEventListener("submit", submitExportCaseBackup);
  $("export-case-custom-password")?.addEventListener("change", syncExportCasePasswordField);
  $("export-case-file-password-toggle")?.addEventListener("click", toggleExportCasePasswordVisibility);
  document.querySelectorAll("[data-close-export-case]").forEach((node) => {
    node.addEventListener("click", closeExportCaseModal);
  });
  document.querySelectorAll("[data-close-profile]").forEach((node) => {
    node.addEventListener("click", closeProfileModal);
  });

  $("patient-new")?.addEventListener("click", openPatientModal);
  $("patient-form")?.addEventListener("submit", submitPatient);
  document.querySelectorAll("[data-close-patient-modal]").forEach((node) => {
    node.addEventListener("click", closePatientModal);
  });
  document.querySelectorAll("[data-patient-tab]").forEach((button) => {
    button.addEventListener("click", () => switchPatientTab(button.dataset.patientTab));
    button.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      const index = PATIENT_TABS.indexOf(button.dataset.patientTab);
      const delta = event.key === "ArrowRight" ? 1 : -1;
      const next = (index + delta + PATIENT_TABS.length) % PATIENT_TABS.length;
      switchPatientTab(PATIENT_TABS[next]);
      document.querySelector(`[data-patient-tab="${PATIENT_TABS[next]}"]`)?.focus();
    });
  });
  $("case-import-file")?.addEventListener("change", (event) => {
    const file = event.target?.files?.[0] || null;
    state.caseImport.file = file;
    updateCaseImportFileStatus(file);
    clearCaseImportPreview();
    setLockError("case-import-error", "");
  });
  const caseDrop = $("case-import-drop");
  if (caseDrop) {
    caseDrop.addEventListener("dragover", (event) => {
      event.preventDefault();
      caseDrop.classList.add("is-dragover");
    });
    caseDrop.addEventListener("dragleave", () => {
      caseDrop.classList.remove("is-dragover");
    });
    caseDrop.addEventListener("drop", (event) => {
      event.preventDefault();
      caseDrop.classList.remove("is-dragover");
      const file = event.dataTransfer?.files?.[0] || null;
      if (!file) return;
      state.caseImport.file = file;
      updateCaseImportFileStatus(file);
      const input = $("case-import-file");
      if (input) {
        try {
          const dt = new DataTransfer();
          dt.items.add(file);
          input.files = dt.files;
        } catch {}
      }
      clearCaseImportPreview();
      setLockError("case-import-error", "");
    });
  }
  $("case-import-validate")?.addEventListener("click", validateCaseImport);
  $("case-import-submit")?.addEventListener("click", submitCaseImport);
  $("patient-search")?.addEventListener("input", (event) => {
    state.patientQuery = event.target.value || "";
    renderPatients();
  });
  $("privacy-toggle")?.addEventListener("click", togglePatientSigil);
  $("patient-filters")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-filter]");
    if (!button) return;
    setPatientFilter(button.dataset.filter);
  });
  $("patient-summary")?.addEventListener("click", (event) => {
    const card = event.target.closest("[data-filter]");
    if (!card) return;
    setPatientFilter(card.dataset.filter);
  });

  $("pin-ok")?.addEventListener("click", confirmUnlock);
  $("pin-input")?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); confirmUnlock(); }
  });
  $("pin-forgot")?.addEventListener("click", () => togglePinReset());
  $("pin-reset-ok")?.addEventListener("click", () => resetPatientPin(false));
  $("pin-remove")?.addEventListener("click", () => resetPatientPin(true));
  $("pin-reset-password")?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); resetPatientPin(false); }
  });
  document.querySelectorAll("[data-close-pin]").forEach((node) => {
    node.addEventListener("click", closePinModal);
  });

  $("lock-patient-ok")?.addEventListener("click", confirmFinishPatient);
  $("lock-pin-input")?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); confirmFinishPatient(); }
  });
  document.querySelectorAll("[data-close-lock-patient]").forEach((node) => {
    node.addEventListener("click", closeLockPatientModal);
  });

  $("patients-back")?.addEventListener("click", () => showDirectory());
  $("finish-patient")?.addEventListener("click", openLockPatientModal);
  $("add-session-btn")?.addEventListener("click", addSession);

  // Dados pessoais (senha mestra)
  $("personal-open")?.addEventListener("click", () => {
    if (!state.patient) {
      toast("Abra um prontuário antes de consultar os dados pessoais.", "error");
      return;
    }
    openPersonalDataModal(state.patient.id);
  });
  $("personal-unlock")?.addEventListener("click", unlockPersonalData);
  $("personal-lock")?.addEventListener("click", hidePersonalData);
  $("personal-save")?.addEventListener("click", savePersonalData);
  $("personal-password")?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); unlockPersonalData(); }
  });
  $("personal-birth")?.addEventListener("input", updatePersonalAge);
  document.querySelectorAll("[data-close-personal]").forEach((node) => {
    node.addEventListener("click", closePersonalDataModal);
  });

  // Exclusão de prontuário (senha mestra)
  $("delete-patient-confirm")?.addEventListener("click", confirmDeletePatient);
  $("delete-patient-password")?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); confirmDeletePatient(); }
  });
  document.querySelectorAll("[data-close-delete-patient]").forEach((node) => {
    node.addEventListener("click", closeDeletePatientModal);
  });

  // Acessibilidade do card (div role=button): Enter/Espaço abrem o prontuário.
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    if (event.target.closest("button, a, input, textarea, select")) return;
    const card = event.target.closest("[data-patient-id]");
    if (!card) return;
    event.preventDefault();
    card.click();
  });

  // Esc fecha o modal de dados pessoais (inclusive com foco em um campo).
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const modal = $("personal-data-modal");
    if (!modal || modal.hidden) return;
    event.preventDefault();
    closePersonalDataModal();
  }, true);
}

async function init() {
  state.files = {};
  state.step3Preview = false;
  loadRailPreferences();
  applyRailState();
  bindPatientEvents();
  bindEvents();
  updatePrivacyToggle();
  updateHeader();
  checkHealth();
  await bootAuth();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
