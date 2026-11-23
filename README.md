<div align="center">
  <img src="frontend/favicon.svg" alt="DermaMetric Logo" width="96" height="96" />

  <h1>DermaMetric</h1>
  <p>
    <strong>Estação Clínica Local de Fotogrametria, Morfometria Cutânea e Laudos Longitudinais</strong>
  </p>

  <p>
    <a href="https://github.com/joaomaxdev/dermametric/releases/latest">
      <img src="https://img.shields.io/github/v/release/joaomaxdev/dermametric?style=for-the-badge&color=123131&labelColor=0c2323&label=Release" alt="Release" />
    </a>
    <a href="https://github.com/joaomaxdev/dermametric/actions/workflows/build-windows.yml">
      <img src="https://img.shields.io/github/actions/workflow/status/joaomaxdev/dermametric/build-windows.yml?style=for-the-badge&color=18c9a8&labelColor=0c2323&label=Windows%20Build" alt="Windows Build" />
    </a>
    <img src="https://img.shields.io/badge/Python-3.11%2B-123131?style=for-the-badge&logo=python&logoColor=57f5cc&labelColor=0c2323" alt="Python 3.11+" />
    <img src="https://img.shields.io/badge/LGPD-Local--First%20%26%20Cifrado-123131?style=for-the-badge&labelColor=0c2323" alt="LGPD Local-First" />
    <img src="https://img.shields.io/badge/Open%20Source-Colaborativo-18c9a8?style=for-the-badge&labelColor=0c2323" alt="Open Source" />
  </p>

  <p>
    <a href="#instalação-rápida-windows">Instalação Windows</a> •
    <a href="#recursos-clínicos-e-científicos">Recursos Clínicos</a> •
    <a href="#segurança-lgpd-e-criptografia">Segurança e LGPD</a> •
    <a href="#desenvolvimento-e-arquitetura">Desenvolvimento</a> •
    <a href="#apoie-o-projeto">Apoie o Projeto</a>
  </p>
</div>

---

## Visão Geral

O **DermaMetric** é uma plataforma **100% local (*local-first*)** de fotogrametria clínica e morfometria para acompanhamento objetivo e longitudinal de lesões, estrias e regiões cutâneas — aplicável a qualquer paciente, área anatômica ou protocolo dermatológico e estético.

Projetado para consultórios, clínicas e pesquisadores, o sistema combina **rigor metrológico** (calibração espacial por régua milimetrada, registro geométrico por homografia projetiva e análise colorimétrica CIELAB/CIEDE2000) com **privacidade absoluta**: nenhum prontuário, foto ou dado sensível trafega pela internet.

Roda como **aplicativo desktop nativo de duplo clique** (Windows/Linux) ou como serviço web local.

---

## Recursos Clínicos e Científicos

- **Protocolo Longitudinal Estruturado (`T0`, `T2`, `T3`, `T4`…):** acompanhamento seriado com par fotográfico por sessão:
  - **Foto com régua milimetrada:** define a escala física real (`px/mm`, faixa validada de `1,5` a `50 px/mm`);
  - **Foto limpa (sem régua):** utilizada para registro espacial, análise colorimétrica e segmentação poligonal de até **4 regiões-alvo (`L1` a `L4`)** com descrição clínica editável (mantendo retrocompatibilidade de API com `E1_maior`…`E4_menor`).
- **Registro Geométrico por Homografia Projetiva:** alinhamento espacial de sessões de seguimento (`Tn`) sobre o baseline (`T0`) a partir de **4 marcos anatômicos fixos** (*landmarks* nomeados), compensando distância, rotação e inclinação da câmera.
- **Normalização Fotométrica (CIELAB + CLAHE):** padronização de iluminação/exposição entre consultas com base em ROI de pele saudável.
- **Morfometria Sub-milimétrica e Colorimetria:**
  - **Área (`mm²`):** contagem de pixels da máscara convertida pela escala calibrada;
  - **Largura média e máxima (`mm`):** transformada de distância (eixo medial) com correção de quantização de meio pixel por borda;
  - **Comprimento (`mm`):** esqueleto morfológico (Zhang–Suen) prolongado até as extremidades da lesão;
  - **Contraste perceptual (`ΔE00`):** diferença colorimétrica CIEDE2000 entre o interior da região medida e um anel adjacente de pele saudável (~`3 mm`).
- **Escalas Clínicas e Avaliação Cega (*Blinded Assessment*):** registro integrado de **END** (Escala Numérica de Dor, `0–10`), **Satisfação Likert** (`1–5`), **GAIS** (`−1` a `+3`) e comparação lado a lado com ordem aleatória sem identificação temporal para reduzir viés do avaliador.
- **Laudo Clínico em PDF:** geração automática de relatório profissional com mapas anatômicos, recortes comparativos e tabela evolutiva de `L1`…`L4`.
- **Workspace Clínico de Alta Produtividade:** canvas interativo com lupa de precisão `4×`, atalhos de zoom, barras laterais retráteis e **Modo Foco no Canvas**.

---

## Guia Rápido da Clínica

### Instalação Rápida (Windows)

1. Acesse a página de [**Releases**](https://github.com/joaomaxdev/dermametric/releases/latest) e baixe o instalador **`DermaMetric-Setup.exe`**.
2. Dê duplo clique e siga o assistente. **Não exige privilégios de administrador** nem instalação prévia de Python.
3. O instalador cria automaticamente atalhos na **Área de Trabalho** e no **Menu Iniciar**.

Os dados clínicos permanecem exclusivamente no computador da clínica:

- **Windows:** `%LOCALAPPDATA%\DermaMetric\data`
- **Linux:** `~/.local/share/dermametric/data`

### Primeiro Acesso e Chave de Recuperação (`.dmkey`)

Na primeira execução, defina a **senha mestra** da estação clínica e:

1. Baixe o **arquivo-chave `.dmkey`** gerado na tela inicial.
2. Armazene-o em local seguro (pendrive dedicado ou cofre de senhas), **fora** do computador da clínica.
3. Guarde a senha mestra em local seguro.

> **Importante:** Sem a senha mestra **e** o arquivo `.dmkey`, os prontuários cifrados não podem ser recuperados em caso de troca de computador ou formatação. Trate o `.dmkey` como a chave física do cofre da clínica.

### Backup e Restauração (`.dmbak`)

- **Backup completo da clínica:** abra o **Perfil** no topo da interface → **Backup e Restauração (`.dmbak`)** → informe a senha mestra → **Exportar Backup Completo (`.dmbak`)**.
- **Exportar caso individual (encaminhamento):** dentro do prontuário do paciente, clique em **Exportar `.dmbak`** para gerar um pacote cifrado isolado (ex.: `PAC-AB12CD.dmbak`).
- **Restauração segura:** selecione o arquivo `.dmbak`, clique em **Inspecionar arquivo** para auditar metadados e conflitos e escolha entre **Mesclar** (preserva prontuários locais) ou **Substituir dados clínicos**. Antes de qualquer alteração, o sistema grava um snapshot automático `pre_restore_*.dmbak` em `data/backups/`.

### Recuperação de Acesso

Caso a senha mestra seja esquecida ou a estação seja migrada para outro computador:

1. Na tela de bloqueio, clique em **Recuperar acesso com arquivo-chave (`.dmkey`)**.
2. Selecione o `.dmkey` guardado (ou informe o código impresso na ficha de emergência).
3. Cadastre uma nova senha mestra e salve o **novo `.dmkey`** (o arquivo anterior é invalidado automaticamente).

---

## Segurança, LGPD e Criptografia

O DermaMetric aplica criptografia em repouso em todas as camadas sensíveis utilizando `cryptography.fernet.Fernet` e envelopes autenticados (`AES` + `PBKDF2` + `HMAC-SHA256`):

- **Banco SQLite protegido:** campos clínicos e pessoais (`label`, *landmarks*, ROI, escalas clínicas e polígonos) são persistidos com o prefixo `enc:v1:` e descriptografados apenas em memória durante a sessão autenticada.
- **Imagens cifradas em disco:** uploads e imagens processadas são gravados exclusivamente como `*.enc` e servidos sob demanda via `GET /api/files/...`.
- **Chave mestra protegida:** derivada via `DERMAMETRIC_MASTER_KEY` ou gerada uma única vez em `storage/.secret.key` com permissão restrita (`0600`).
- **Migração transparente:** bancos legados em texto puro são migrados automaticamente para o formato cifrado na inicialização.

---

## Apoie o Projeto

O **DermaMetric** é um projeto **open-source** desenvolvido de forma independente para democratizar o acesso a ferramentas de fotogrametria clínica de precisão, sem mensalidades e com total soberania sobre os dados dos pacientes.

Se este software ajuda na sua clínica, pesquisa acadêmica ou prática profissional, considere apoiar a manutenção contínua, novas funcionalidades e os builds oficiais:

<div align="center">

| Canal de Apoio | Chave / Identificador |
| :--- | :--- |
| **Pix (Brasil — Apoio Direto)** | `marques-joao-e00@jim.com` |

</div>

Você também pode colaborar gratuitamente:
- Deixando uma **Star ⭐** neste repositório para aumentar a visibilidade do projeto;
- Reportando bugs, sugerindo melhorias clínicas ou abrindo **Pull Requests**;
- Compartilhando o DermaMetric com colegas dermatologistas, esteticistas, fisioterapeutas dermatofuncionais e pesquisadores.

---

## Desenvolvimento e Arquitetura

### Requisitos

- **Python 3.11+** (testado em Python 3.14)
- **Windows 10/11** (para geração do instalador `.exe`) ou **Linux**

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### Execução em Modo Web

```bash
.venv/bin/uvicorn backend.main:app --reload --port 8000
```

- **Estação clínica:** <http://127.0.0.1:8000/>
- **Swagger / OpenAPI:** <http://127.0.0.1:8000/docs>
- **Healthcheck:** <http://127.0.0.1:8000/api/health>

### Execução em Modo Desktop

```bash
.venv/bin/python desktop_launcher.py
```

O launcher seleciona uma porta livre (`8765` por padrão), inicia o servidor FastAPI em thread *daemon*, aguarda `/api/health` e abre uma janela nativa via `pywebview` (com fallback automático para o navegador). Ao fechar a janela, o processo backend é encerrado de forma limpa.

Para validação automatizada em CI sem abertura de janela:

```bash
DERMAMETRIC_DATA_DIR=/tmp/dermametric-test \
  .venv/bin/python desktop_launcher.py --smoke-test
```

### Suíte de Testes Automatizados

```bash
.venv/bin/pytest -q
```

A suíte valida, com imagens sintéticas calibradas (elipse de `40 × 5 mm` a `10 px/mm`), erro inferior a **3%** em área, largura média, largura máxima, comprimento e `ΔE00`, além de todos os endpoints REST, persistência cifrada no SQLite, backup/restauração `.dmbak`, interface modal e *smoke test* do launcher desktop.

### Estrutura do Repositório

```text
dermametric/
├── backend/
│   ├── main.py          # API FastAPI, entrega de arquivos cifrados e inicialização
│   ├── paths.py         # Resolução centralizada de diretórios (dev × PyInstaller)
│   ├── database.py      # Schema SQLite, migrações e cifragem transparente de campos
│   ├── security.py      # Criptografia em repouso Fernet (LGPD)
│   ├── vision.py        # Calibração, homografia, CIELAB/CLAHE, morfometria e CIEDE2000
│   ├── report.py        # Gerador de laudo clínico em PDF
│   └── backup.py        # Empacotamento e auditoria do formato cifrado .dmbak
├── frontend/
│   ├── favicon.svg      # Identidade visual oficial DermaMetric
│   ├── index.html       # Estação clínica (4 etapas, escalas, comparador longitudinal)
│   ├── styles.css       # Design system responsivo e workspace compacto
│   └── app.js           # Canvas interativo, lupa 4×, medições e exportações
├── packaging/
│   ├── dermametric.spec # Configuração PyInstaller (--onedir --windowed)
│   ├── installer.iss    # Script Inno Setup 6 (DermaMetric-Setup.exe)
│   └── dermametric.ico  # Ícone Windows multi-resolução (16–256 px)
├── tests/               # Testes unitários, integração, backup, UI e desktop
├── desktop_launcher.py  # Ponto de entrada do aplicativo desktop (pywebview)
└── requirements.txt     # Dependências Python
```

### Caminhos de Execução (`backend/paths.py`)

| Contexto | `ASSETS_DIR` | `DATA_DIR` | `STORAGE_DIR` |
| :--- | :--- | :--- | :--- |
| **Desenvolvimento** | Raiz do repositório | `./data` | `./storage` |
| **Empacotado (PyInstaller)** | `sys._MEIPASS` | `%LOCALAPPDATA%\DermaMetric\data` (Win)<br>`~/.local/share/dermametric/data` (Linux) | `<DATA_DIR>/storage` |

Variáveis de ambiente suportadas para ambientes portáteis ou testes: `DERMAMETRIC_DATA_DIR`, `DERMAMETRIC_DB_PATH` e `DERMAMETRIC_STORAGE_DIR`.

---

## Build do Instalador Windows

### 1. Regenerar Ícone (`packaging/dermametric.ico`)

```bash
.venv/bin/python - <<'PY'
import subprocess, tempfile
from pathlib import Path
from PIL import Image

svg, out = Path("frontend/favicon.svg"), Path("packaging/dermametric.ico")
sizes = [16, 32, 48, 64, 128, 256]
with tempfile.TemporaryDirectory() as tmp:
    imgs = []
    for sz in sizes:
        png = Path(tmp) / f"{sz}.png"
        subprocess.run(["rsvg-convert", "-w", str(sz), "-h", str(sz), str(svg), "-o", str(png)], check=True)
        imgs.append(Image.open(png).convert("RGBA").copy())
    imgs[-1].save(out, format="ICO", sizes=[(sz, sz) for sz in sizes], append_images=imgs[:-1])
PY
```

### 2. Empacotar com PyInstaller

```powershell
pip install -r requirements.txt pyinstaller pywebview
python -m PyInstaller packaging/dermametric.spec --noconfirm --clean
```

Saída: `dist\DermaMetric\DermaMetric.exe`.

### 3. Compilar Instalador com Inno Setup 6

```powershell
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" `
  /DMyAppVersion=1.0.2 packaging\installer.iss
```

Saída: `dist\installer\DermaMetric-Setup.exe`.

### 4. CI/CD (GitHub Actions)

O workflow `.github/workflows/build-windows.yml` automatiza a compilação em `windows-latest`:
- **Execução manual (`workflow_dispatch`):** publica `DermaMetric-Setup.exe` como *artifact*;
- **Push de tag `v*` (ex.: `v1.0.2`):** compila o instalador e publica automaticamente na página de **Releases** do GitHub.

---

## Referência Rápida da API

| Método | Endpoint | Descrição |
| :--- | :--- | :--- |
| `POST` | `/api/sessions/{session_id}/upload` | Envia `ruler_image` e/ou `clean_image` (lê data EXIF automaticamente) |
| `POST` | `/api/sessions/{session_id}/calibrate` | Grava `px_per_mm` a partir de dois pontos da régua e `known_mm` |
| `POST` | `/api/sessions/{session_id}/register` | Alinha sessão contra `T0` via 4 marcos anatômicos e normaliza CIELAB |
| `POST` | `/api/sessions/{session_id}/measure` | Calcula morfometria (`mm`, `mm²`) e `ΔE00` para `L1`…`L4` |
| `POST` | `/api/sessions/{session_id}/clinical-scales` | Registra escalas END (`0–10`), Likert (`1–5`), GAIS (`−1..+3`) e notas |
| `PATCH` | `/api/sessions/{session_id}` | Atualiza rótulo, data de captura e descrições das regiões-alvo |
| `DELETE` | `/api/sessions/{session_id}/measurements/{stria_code}` | Remove uma medição específica (ou todas sem `stria_code`) |
| `GET` | `/api/files/{uploads\|processed}/{filename}` | Entrega imagens descriptografadas sob demanda para a sessão autenticada |

> **Ordem clínica obrigatória:** registre **T0** antes das sessões de seguimento (`T2`, `T3`, `T4`…), pois `T0` estabelece o sistema de coordenadas e a referência fotométrica do caso.

```bash
# Exemplo: calibrar escala (300 px = 30 mm)
curl -X POST http://127.0.0.1:8000/api/sessions/T0/calibrate \
  -H 'Content-Type: application/json' \
  -d '{"p1":[100,900],"p2":[400,900],"known_mm":30}'

# Exemplo: registrar medição poligonal em L1
curl -X POST http://127.0.0.1:8000/api/sessions/T0/measure \
  -H 'Content-Type: application/json' \
  -d '{"stria_code":"L1","polygon":[[400,480],[600,480],[600,520],[400,520]]}'
```

---

<div align="center">
  <p>Desenvolvido por <a href="https://github.com/joaomaxdev"><strong>joaomaxdev</strong></a></p>
</div>
