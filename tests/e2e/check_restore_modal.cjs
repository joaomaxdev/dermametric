/**
 * Verificação end-to-end (Chromium headless) do fluxo REAL de restauração.
 *
 * Exercita exatamente o caminho do usuário, sem mock de rede:
 *   1. garante a estação (setup/login) e cria um paciente de teste;
 *   2. exporta um `.dmbak` real via `POST /api/backup/export`;
 *   3. abre o modal de perfil, seleciona o arquivo real, digita a senha e
 *      clica em "Inspecionar arquivo" (`POST /api/backup/inspect`);
 *   4. clica em "Substituir dados clínicos" (modo `replace`) e confirma;
 *   5. valida o cartão, o cabeçalho, a barra em 100%, o console de logs, o
 *      botão "Concluir e Ver Pacientes" e o banner fail-safe externo;
 *   6. clica em "Concluir e Ver Pacientes" e confere o diretório reidratado.
 *
 * Uso (a partir da raiz do projeto, com o servidor já no ar):
 *   node tests/e2e/check_restore_modal.cjs --url http://127.0.0.1:8000 --password SENHA
 *
 * Em servidor novo (sem senha configurada) a senha padrão é criada
 * automaticamente. Para servidor já configurado, informe `--password` (ou
 * `E2E_MASTER_PASSWORD`). O teste é destrutivo no sentido de que restaura um
 * backup dele mesmo; o backend cria um snapshot `pre_restore_*.dmbak` antes.
 */

const fs = require("fs");
const os = require("os");
const path = require("path");

function loadPlaywright() {
  const candidates = [
    "playwright",
    path.join(process.env.HOME || "", ".npm-global/lib/node_modules/playwright"),
  ];
  const { execSync } = require("child_process");
  try {
    const npxRoot = execSync("npm root -g", { encoding: "utf8" }).trim();
    candidates.push(path.join(npxRoot, "playwright"));
  } catch {
    /* ignora */
  }
  try {
    const cache = execSync(
      "ls -d " + path.join(process.env.HOME || "", ".npm/_npx/*/node_modules/playwright") + " 2>/dev/null",
      { encoding: "utf8", shell: "/bin/bash" },
    ).trim().split("\n");
    candidates.push(...cache);
  } catch {
    /* ignora */
  }
  for (const candidate of candidates) {
    if (!candidate) continue;
    try {
      return require(candidate);
    } catch {
      /* tenta o próximo */
    }
  }
  throw new Error("Pacote 'playwright' não encontrado. Rode 'npx playwright --version'.");
}

function parseArgs(argv) {
  const args = {
    url: "http://127.0.0.1:8000",
    password: process.env.E2E_MASTER_PASSWORD || "e2e-senha-mestra-1",
  };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--url") args.url = argv[i + 1];
    if (argv[i] === "--password") args.password = argv[i + 1];
  }
  return args;
}

async function stationToken(baseUrl, password) {
  const status = await fetch(`${baseUrl}/api/auth/status`).then((r) => r.json());
  if (!status.configured) {
    const created = await fetch(
      `${baseUrl}/api/auth/setup?password=${encodeURIComponent(password)}`,
      { method: "POST" },
    ).then((r) => r.json());
    return { token: created.access_token, created: true };
  }
  if (status.access_token) return { token: status.access_token, created: false };
  const login = await fetch(`${baseUrl}/api/auth/login?password=${encodeURIComponent(password)}`, {
    method: "POST",
  });
  if (!login.ok) {
    throw new Error(
      "Estação já configurada e senha mestra desconhecida. "
      + "Informe --password/E2E_MASTER_PASSWORD ou use um servidor novo.",
    );
  }
  const body = await login.json();
  return { token: body.token, created: false };
}

async function createFixturePatient(baseUrl, token) {
  const response = await fetch(`${baseUrl}/api/patients`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({
      name: "Paciente E2E Restauração",
      sessions: 3,
      notes: "Criado automaticamente pelo teste e2e do modal de restauração.",
    }),
  });
  if (!response.ok) {
    throw new Error(`Falha ao criar paciente e2e (${response.status}): ${await response.text()}`);
  }
  const patient = await response.json();
  return patient.code || patient.patient_code || patient.id;
}

async function exportRealBackup(baseUrl, token, password) {
  const response = await fetch(`${baseUrl}/api/backup/export`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({ password }),
  });
  if (!response.ok) {
    throw new Error(`Falha ao exportar .dmbak e2e (${response.status}): ${await response.text()}`);
  }
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "dermametric-e2e-"));
  const file = path.join(directory, "e2e-real.dmbak");
  fs.writeFileSync(file, Buffer.from(await response.arrayBuffer()));
  return file;
}

async function main() {
  const { url, password } = parseArgs(process.argv.slice(2));
  const { token } = await stationToken(url, password);
  const patientCode = await createFixturePatient(url, token);
  const backupPath = await exportRealBackup(url, token, password);

  const { chromium } = loadPlaywright();
  const executablePath = process.env.PLAYWRIGHT_CHROMIUM
    || ["/usr/bin/chromium", "/usr/bin/google-chrome-stable"].find((candidate) => {
      try { return fs.existsSync(candidate); } catch { return false; }
    });

  const browser = await chromium.launch(executablePath ? { executablePath } : {});
  const page = await browser.newPage({ viewport: { width: 1366, height: 768 } });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("dialog", (dialog) => dialog.accept());

  try {
    await page.goto(url, { waitUntil: "networkidle" });
    await page.waitForFunction(() => typeof state !== "undefined" && state.screen === "directory", null, { timeout: 20000 });

    // Fluxo real: abrir o modal pelo botão do topo.
    await page.click("#profile-open");
    await page.waitForSelector("#profile-modal .modal-window", { state: "visible" });

    // O backup vive na aba "Backup da Estação"; o modal abre em "Dados".
    await page.click('[data-profile-tab="backup"]');
    await page.waitForSelector("#profile-panel-backup:not([hidden])");

    // Upload real do arquivo .dmbak exportado pelo backend.
    await page.setInputFiles("#backup-file", backupPath);
    await page.fill("#backup-restore-password", password);
    await page.click("#backup-inspect");
    await page.waitForSelector("#backup-preview:not([hidden])", { timeout: 30000 });
    await page.waitForFunction(
      (code) => document.getElementById("backup-preview")?.textContent.includes(code),
      patientCode,
      { timeout: 20000 },
    );

    // Substituir dados clínicos (modo replace) e confirmar o diálogo nativo.
    await page.click('[data-backup-restore="replace"]');
    await page.waitForFunction(
      () => document.getElementById("backup-progress-percent")?.textContent === "100%",
      null,
      { timeout: 60000 },
    );
    await page.waitForSelector("#backup-finish:not([hidden])", { timeout: 30000 });
    await page.waitForTimeout(500);

    const report = await page.evaluate(() => {
      const rect = (selector) => document.querySelector(selector)?.getBoundingClientRect();
      const modal = document.getElementById("profile-modal");
      const card = modal.querySelector(".modal-window");
      const head = modal.querySelector(".modal-head");
      const closeButton = modal.querySelector("[data-close-profile].icon-btn");
      const progress = document.getElementById("backup-progress");
      const bar = document.getElementById("backup-progress-bar");
      const log = document.getElementById("backup-log");
      const finish = document.getElementById("backup-finish");
      const banner = document.getElementById("restore-banner");
      const body = modal.querySelector(".modal-body");
      const visible = (element) => {
        if (!element) return false;
        const box = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return box.height > 0 && box.width > 0
          && style.display !== "none" && style.visibility !== "hidden"
          && Number(style.opacity) > 0;
      };
      return {
        modalHidden: modal.hidden,
        bodyClass: document.body.className,
        screen: typeof state === "undefined" ? null : state.screen,
        directoryVisible: !document.getElementById("patient-directory").hidden,
        shellHidden: document.querySelector(".app-shell").hidden,
        cardVisible: visible(card),
        cardBox: rect("#profile-modal .modal-window")?.toJSON() ?? null,
        cardTransform: getComputedStyle(card).transform,
        cardIsolation: getComputedStyle(card).isolation,
        headVisible: visible(head),
        headText: head?.textContent?.replace(/\s+/g, " ").trim() || "",
        closeVisible: visible(closeButton),
        progressVisible: visible(progress),
        percent: document.getElementById("backup-progress-percent")?.textContent,
        barWidth: bar ? getComputedStyle(bar).width : null,
        logLines: log ? log.children.length : 0,
        logText: log?.textContent || "",
        finishVisible: visible(finish),
        finishHidden: finish?.hidden,
        finishText: finish?.textContent?.trim() || "",
        bodyScrollTop: body?.scrollTop,
        progressInView: progress
          ? progress.getBoundingClientRect().top < window.innerHeight
            && progress.getBoundingClientRect().bottom > 0
          : false,
        bannerVisible: visible(banner),
        bannerText: banner?.textContent?.replace(/\s+/g, " ").trim() || "",
        tabsVisible: visible(modal.querySelector(".profile-tabs")),
        activeTab: modal.querySelector("[data-profile-tab].is-active")?.dataset.profileTab || null,
        backupPanelVisible: visible(document.getElementById("profile-panel-backup")),
        profilePanelHidden: document.getElementById("profile-panel-profile").hidden,
      };
    });

    await page.screenshot({ path: process.env.E2E_SCREENSHOT || "/tmp/dermametric-e2e-restore.png" });

    // Fecha o relatório pelo botão final e confere o diretório reidratado.
    await page.click("#backup-finish");
    await page.waitForFunction(() => document.getElementById("profile-modal").hidden === true,
      null, { timeout: 20000 });
    await page.waitForTimeout(400);
    const afterFinish = await page.evaluate(() => ({
      modalHidden: document.getElementById("profile-modal").hidden,
      bannerHidden: document.getElementById("restore-banner").hidden,
      screen: typeof state === "undefined" ? null : state.screen,
      directoryVisible: !document.getElementById("patient-directory").hidden,
      shellHidden: document.querySelector(".app-shell").hidden,
      patients: typeof state === "undefined" ? 0 : (state.patients?.length ?? 0),
      cards: document.querySelectorAll("#patient-list .patient-card").length,
      hasPatientCode: (document.getElementById("patient-list")?.textContent || "").length > 0,
    }));
    await page.screenshot({ path: "/tmp/dermametric-e2e-restore-finished.png" });

    const failures = [];
    const check = (condition, message) => { if (!condition) failures.push(message); };
    check(errors.length === 0, `Erros JS na página: ${errors.join(" | ")}`);
    check(report.modalHidden === false, "modal de perfil ficou oculto");
    check(report.bodyClass.includes("has-modal"), "body perdeu a classe has-modal");
    check(report.cardVisible, "cartão do modal não está visível");
    check(report.cardTransform === "none", `cartão manteve transform composto (${report.cardTransform})`);
    check(report.cardIsolation === "auto", `cartão manteve isolation (${report.cardIsolation})`);
    check(report.headVisible, "cabeçalho do modal não está visível");
    check(report.headText.includes("Perfil do Profissional"), "cabeçalho perdeu o título");
    check(report.closeVisible, "botão de fechar do modal não está visível");
    check(report.tabsVisible, "abas do perfil não estão visíveis");
    check(report.activeTab === "backup", `aba ativa deveria ser backup (${report.activeTab})`);
    check(report.backupPanelVisible, "painel da aba de backup não está visível");
    check(report.profilePanelHidden, "painel de dados do profissional deveria estar oculto");
    check(report.progressVisible, "painel de progresso não está visível");
    check(report.percent === "100%", `progresso não chegou a 100% (${report.percent})`);
    check(report.barWidth !== "0px", "barra de progresso está vazia");
    check(report.logLines >= 5, `console de logs tem poucas linhas (${report.logLines})`);
    check(report.logText.includes(patientCode), `log não cita o paciente real (${patientCode})`);
    check(report.finishVisible, "botão 'Concluir e Ver Pacientes' não está visível");
    check(report.finishText.includes("Concluir"), "botão final perdeu o texto");
    check(report.progressInView, "relatório ficou fora da área visível do cartão");
    check(report.bannerVisible, "banner fail-safe não apareceu");
    check(report.bannerText.includes("Backup restaurado"), "banner sem resumo da restauração");
    check(report.bannerText.includes("paciente"), "banner sem contagem de pacientes");
    check(afterFinish.modalHidden, "modal não fechou após 'Concluir'");
    check(afterFinish.bannerHidden, "banner fail-safe não sumiu após 'Concluir'");
    check(afterFinish.screen === "directory", "tela ativa não voltou ao diretório");
    check(afterFinish.directoryVisible, "diretório de pacientes não está visível");
    check(afterFinish.shellHidden, "workspace antigo continuou visível");
    check(afterFinish.hasPatientCode, "diretório não foi reidratado");

    console.log(JSON.stringify({ patientCode, report, afterFinish }, null, 2));
    if (failures.length) {
      console.error("\nFALHAS:");
      failures.forEach((failure) => console.error(` - ${failure}`));
      process.exitCode = 1;
      return;
    }
    console.log("\nOK: modal, progresso, logs e botão final visíveis após a restauração real.");
  } finally {
    await browser.close();
    try { fs.rmSync(path.dirname(backupPath), { recursive: true, force: true }); } catch { /* ignora */ }
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
