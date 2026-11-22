# DermaMetric

Plataforma **local** de fotogrametria clínica e morfometria para acompanhamento
longitudinal de lesões e regiões cutâneas, aplicável a qualquer paciente,
região corporal ou protocolo dermatológico/estético.

Roda como **aplicativo desktop de duplo clique** (Windows/Linux) ou como
serviço web local. Protocolo com 4 momentos — **T0, T2, T3, T4** — cada um com:

- **foto com régua milimetrada** (calibração métrica);
- **foto limpa, sem régua** (alinhamento e medição);
- **4 regiões-alvo** por sessão (`L1` a `L4`), com descrição clínica editável.
  Os códigos legados `E1_maior`…`E4_menor` continuam aceitos pela API.

---

## Guia rápido da clínica

### 1. Instalação (Windows)

1. Baixe `DermaMetric-Setup.exe`.
2. Dê duplo clique e siga o assistente. **Não é necessário administrador** nem
   instalar Python.
3. O instalador cria atalhos na **Área de Trabalho** e no **Menu Iniciar**.

O programa roda 100% no computador da clínica, sem enviar dados para a
internet. Os dados ficam em:

- **Windows:** `%LOCALAPPDATA%\DermaMetric\data`
- **Linux:** `~/.local/share/dermametric/data`

### 2. Primeiro acesso

Na primeira execução, defina a **senha mestra** da estação. Em seguida:

1. Baixe o **arquivo-chave `.dmkey`** exibido na tela.
2. Guarde-o em local seguro (pendrive ou cofre de senhas), **fora** do
   computador da clínica.
3. Anote a senha mestra. Ela e o `.dmkey` são necessários para recuperar o
   acesso em caso de troca de máquina ou reinstalação.

> Sem a senha mestra **e** o `.dmkey`, os dados cifrados não podem ser
> recuperados. Trate o `.dmkey` como a chave física do cofre.

### 3. Backup completo (`.dmbak`)

1. Abra o **perfil** (ícone no topo da interface).
2. Na seção **“Backup e Restauração (.dmbak)”**, informe a senha mestra.
3. Clique em **Exportar Backup Completo (.dmbak)**.
4. Salve o arquivo em pendrive, HD externo ou pasta de rede segura.

O arquivo `.dmbak` é cifrado (AES + PBKDF2 + HMAC-SHA256) e contém prontuários,
imagens, medições e escalas clínicas.

**Exportar um caso isolado:** abra o prontuário do paciente e clique em
**“Exportar Caso (.dmbak)”**. O arquivo é nomeado com o código do paciente
(ex.: `PAC-AB12CD.dmbak`), útil para encaminhamento entre profissionais.

### 4. Restauração (`.dmbak`)

1. Perfil → seção de backup → **Selecionar arquivo .dmbak**.
2. Informe a senha mestra usada na exportação e clique em **Inspecionar
   arquivo**. Confira data, profissional, quantidade de pacientes e conflitos
   de código.
3. Escolha:
   - **Restaurar em modo Mesclar** — importa o que falta e preserva os dados
     locais (recomendado para o dia a dia);
   - **Substituir dados clínicos** — troca todos os prontuários pelos do
     backup (use com atenção).
4. Antes de qualquer gravação, o sistema cria automaticamente um snapshot
   `pre_restore_*.dmbak` em `data/backups/`.

### 5. Recuperação de acesso (`.dmkey`)

Se a senha mestra for esquecida ou o computador for trocado:

1. Na tela de bloqueio, escolha a opção de recuperação por arquivo-chave.
2. Selecione o `.dmkey` guardado (ou digite o código impresso na ficha).
3. Defina uma nova senha mestra e baixe/guarde o **novo** `.dmkey`.

O arquivo antigo deixa de funcionar após a troca.

---

## Desenvolvimento

### Requisitos

- Python 3.11+ (testado em 3.14)
- Windows 10/11 (build do instalador) ou Linux

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### Execução web (navegador)

```bash
.venv/bin/uvicorn backend.main:app --reload --port 8000
```

- Interface: <http://127.0.0.1:8000/>
- Documentação interativa: <http://127.0.0.1:8000/docs>
- Healthcheck: <http://127.0.0.1:8000/api/health>

### Execução desktop

```bash
.venv/bin/python desktop_launcher.py
```

O launcher escolhe uma porta livre (`8765` por padrão), sobe o FastAPI em
thread daemon, espera `/api/health` e abre uma janela nativa com `pywebview`
(com fallback para o navegador). Ao fechar a janela, o servidor é encerrado.

Para automação/CI, sem abrir janela:

```bash
DERMAMETRIC_DATA_DIR=/tmp/dermametric-test \
  .venv/bin/python desktop_launcher.py --smoke-test
```

### Testes

```bash
.venv/bin/pytest -q
```

A suíte valida, com imagens sintéticas calibradas (elipse de 40 × 5 mm a
10 px/mm), erro inferior a 3% em área, largura média, largura máxima,
comprimento e ΔE00, além de todos os endpoints, persistência no SQLite,
backup/restauração `.dmbak` e o smoke test do launcher desktop.

### Estrutura

```
backend/
  main.py        # API FastAPI, arquivos cifrados e inicialização do SQLite
  paths.py       # caminhos centralizados (dev x PyInstaller x dados do usuário)
  database.py    # schema, migrações, cifragem de campos e persistência
  security.py    # Fernet (LGPD): textos no SQLite e imagens em disco
  vision.py      # calibração, homografia, CIELAB/CLAHE, morfometria, CIEDE2000
  report.py      # laudo PDF (mapas, recortes, detalhamento L1..L4)
  backup.py      # formato proprietário .dmbak (AES/PBKDF2/HMAC)
desktop_launcher.py  # entrada do aplicativo desktop (pywebview)
frontend/
  index.html     # estação clínica (4 etapas, escalas, comparador)
  styles.css     # design system responsivo e layout clínico
  app.js         # canvas, medições, comparação e exportações
packaging/
  dermametric.spec   # PyInstaller (onedir + windowed)
  installer.iss      # Inno Setup (Setup.exe sem administrador)
  dermametric.ico    # ícone multi-resolução
tests/
  test_pipeline.py
  test_backup.py
  test_desktop.py
```

---

## Build do aplicativo (Windows)

### 1. Ícone

O ícone `packaging/dermametric.ico` (16–256 px) é gerado a partir de
`frontend/favicon.svg`. Para regenerar em Linux (requer `rsvg-convert` e
Pillow):

```bash
.venv/bin/python - <<'PY'
import subprocess, tempfile
from pathlib import Path
from PIL import Image
svg, out = Path("frontend/favicon.svg"), Path("packaging/dermametric.ico")
sizes = [16, 32, 48, 64, 128, 256]
with tempfile.TemporaryDirectory() as tmp:
    imgs = []
    for s in sizes:
        png = Path(tmp) / f"{s}.png"
        subprocess.run(["rsvg-convert", "-w", str(s), "-h", str(s), str(svg), "-o", str(png)], check=True)
        imgs.append(Image.open(png).convert("RGBA").copy())
    imgs[-1].save(out, format="ICO", sizes=[(s, s) for s in sizes], append_images=imgs[:-1])
PY
```

### 2. Executável (PyInstaller)

Em Windows, com as dependências instaladas:

```powershell
pip install -r requirements.txt pyinstaller pywebview
python -m PyInstaller packaging/dermametric.spec --noconfirm --clean
```

Resultado: `dist\DermaMetric\DermaMetric.exe` (modo `--onedir --windowed`,
sem console).

### 3. Instalador (Inno Setup)

Instale o [Inno Setup 6](https://jrsoftware.org/isinfo.php) e rode:

```powershell
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" `
  /DMyAppVersion=1.0.0 packaging\installer.iss
```

Resultado: `dist\installer\DermaMetric-Setup.exe`, com atalhos na Área de
Trabalho e Menu Iniciar e `PrivilegesRequired=lowest` (sem administrador).

### 4. GitHub Actions

O workflow `.github/workflows/build-windows.yml` compila o instalador em
`windows-latest` e:

- publica `DermaMetric-Setup.exe` como **artifact** em execuções manuais
  (`workflow_dispatch`);
- em tags `v*` (ex.: `v1.0.0`), publica também como **release** do GitHub.

---

## API e fluxo clínico

1. `POST /api/sessions/{session_id}/upload` — envia `ruler_image` e/ou
   `clean_image` (multipart), `label` e, opcionalmente, `captured_at`. A data
   é lida do EXIF da foto quando disponível.
2. `POST /api/sessions/{session_id}/calibrate` — informa dois pontos da régua
   e a distância real (`known_mm`); grava `px_per_mm` (faixa 1,5–50 px/mm).
3. `POST /api/sessions/{session_id}/register` — informa os 4 pontos
   anatômicos nomeados (`[{label, x, y}]`) e a ROI de pele saudável; alinha
   contra T0, normaliza a exposição em CIELAB e gera a versão realçada.
4. `POST /api/sessions/{session_id}/measure` — informa `stria_code` (`L1`…`L4`)
   e o polígono traçado; calcula e grava a morfometria + ΔE00.
5. `POST /api/sessions/{session_id}/clinical-scales` — grava END (0–10),
   Likert (1–5), GAIS (−1 a +3) e observações.
6. `PATCH /api/sessions/{session_id}` — atualiza rótulo, data de captura e
   descrições das regiões-alvo.
7. `DELETE /api/sessions/{session_id}/measurements/{stria_code}` — exclui uma
   medição; `DELETE /api/sessions/{session_id}/measurements` limpa a sessão.
8. `GET /api/files/{uploads|processed}/{filename}` — entrega imagens
   descriptografadas sob demanda.

> **Ordem obrigatória**: registre **T0** antes de T2/T3/T4 (T0 é a referência
> geométrica e fotométrica). Calibre a régua antes de medir.

### Exemplos

```bash
# Calibração: 300 px correspondem a 30 mm
curl -X POST http://127.0.0.1:8000/api/sessions/T0/calibrate \
  -H 'Content-Type: application/json' \
  -d '{"p1":[100,900],"p2":[400,900],"known_mm":30}'

# Medição de uma região-alvo
curl -X POST http://127.0.0.1:8000/api/sessions/T0/measure \
  -H 'Content-Type: application/json' \
  -d '{"stria_code":"L1","polygon":[[400,480],[600,480],[600,520],[400,520]]}'

# Exclusão individual de uma medição
curl -X DELETE http://127.0.0.1:8000/api/sessions/T0/measurements/L1
```

## Modelo de medição

- **Área**: contagem de pixels da máscara / `px_per_mm²`.
- **Larguras**: transformada de distância (eixo medial) com correção de
  quantização de meio pixel por borda.
- **Comprimento**: comprimento do esqueleto (Zhang–Suen) prolongado até as
  pontas afiadas da máscara.
- **Contraste**: CIEDE2000 entre o interior da região e um anel de pele
  saudável adjacente de ~3 mm.

## Armazenamento, LGPD e criptografia

Todos os dados ficam locais. A plataforma aplica criptografia em repouso com
`cryptography.fernet.Fernet`:

- campos sensíveis do SQLite (`label`, landmarks, ROI, escalas e polígonos)
  são gravados com o prefixo `enc:v1:` e descriptografados apenas na resposta
  da API;
- imagens enviadas e processadas são gravadas como `*.enc` e servidas
  exclusivamente por `GET /api/files/...`;
- a chave mestra vem de `DERMAMETRIC_MASTER_KEY` ou é gerada uma única vez em
  `storage/.secret.key` com permissão `0600`;
- bancos antigos em texto puro são migrados automaticamente na inicialização.

### Caminhos (dev x empacotado)

`backend/paths.py` centraliza a resolução:

| Contexto | `ASSETS_DIR` | `DATA_DIR` | `STORAGE_DIR` |
|---|---|---|---|
| Desenvolvimento | raiz do repositório | `./data` | `./storage` |
| PyInstaller | `sys._MEIPASS` | `%LOCALAPPDATA%\DermaMetric\data` (Win) / `~/.local/share/dermametric/data` (Linux) | `<DATA_DIR>/storage` |

Overrides: `DERMAMETRIC_DATA_DIR`, `DERMAMETRIC_DB_PATH` e
`DERMAMETRIC_STORAGE_DIR` (úteis para testes e instalações portáteis).

O `.gitignore` exclui imagens, banco, chave, snapshots e artefatos de teste; o
banco guarda apenas caminhos relativos ao diretório de storage, mantendo-o
portátil.
