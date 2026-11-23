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
    <img src="https://img.shields.io/badge/LGPD-100%25%20Local%20%26%20Cifrado-123131?style=for-the-badge&labelColor=0c2323" alt="LGPD Local-First" />
    <img src="https://img.shields.io/badge/Open%20Source-Gratuito-18c9a8?style=for-the-badge&labelColor=0c2323" alt="Open Source" />
  </p>

  <p>
    <a href="#instalação-rápida-windows">Instalação Windows</a> •
    <a href="#recursos-clínicos">Recursos Clínicos</a> •
    <a href="#guia-rápido-da-clínica">Guia da Clínica</a> •
    <a href="#segurança-e-privacidade-lgpd">Segurança &amp; LGPD</a> •
    <a href="#apoie-o-projeto">Apoie o Projeto</a>
  </p>
</div>

---

## Visão Geral

O **DermaMetric** é uma estação clínica **100% local (*offline-first*)** de fotogrametria e morfometria para acompanhamento objetivo e longitudinal de lesões, estrias e regiões cutâneas — aplicável a qualquer paciente, área anatômica ou protocolo dermatológico e estético.

Projetado para consultórios, clínicas e pesquisadores, o sistema combina **precisão metrológica** (calibração por régua milimetrada, alinhamento geométrico automático e análise colorimétrica clínica) com **privacidade total**: nenhum prontuário, foto ou dado do paciente é enviado para a nuvem.

Funciona como **aplicativo desktop nativo de duplo clique** no Windows (ou localmente no Linux), sem necessidade de internet, assinaturas ou servidores externos.

---

## Recursos Clínicos

- **Acompanhamento Longitudinal (`T0`, `T2`, `T3`, `T4`…):** protocolo organizado por consultas seriadas, com par fotográfico por sessão:
  - **Foto com régua milimetrada:** calibra automaticamente a escala real em milímetros;
  - **Foto limpa (sem régua):** utilizada para alinhamento, análise de cor e medição de até **4 regiões-alvo (`L1` a `L4`)** com descrição clínica editável.
- **Alinhamento Automático entre Consultas:** sobreposição espacial das fotos de retorno (`Tn`) em relação à consulta inicial (`T0`) por meio de **4 pontos anatômicos de referência**, compensando distância, rotação e inclinação da câmera.
- **Padronização de Iluminação:** normalização fotométrica automática com base na pele saudável adjacente, reduzindo variações de luz entre sessões.
- **Medições Objetivas Sub-milimétricas:**
  - **Área (`mm²`)** e **Comprimento (`mm`)** da região demarcada;
  - **Largura média e largura máxima (`mm`)** ao longo do eixo da lesão;
  - **Contraste perceptual (`ΔE00`):** diferença colorimétrica clínica (CIEDE2000) entre a lesão e a pele saudável ao redor.
- **Escalas Clínicas e Avaliação Cega:** registro integrado de **Dor (END `0–10`)**, **Satisfação (`1–5`)**, **GAIS (`−1` a `+3`)** e modo de comparação lado a lado sem identificação das datas para avaliação imparcial.
- **Laudo Clínico em PDF:** emissão imediata de relatório profissional pronto para imprimir ou anexar ao prontuário, contendo mapas anatômicos, recortes comparativos e tabela evolutiva.
- **Área de Trabalho Otimizada:** visualização ampla com lupa de precisão `4×`, atalhos de zoom e modo foco no canvas.

---

## Guia Rápido da Clínica

### Instalação Rápida (Windows)

1. Acesse a página de [**Releases**](https://github.com/joaomaxdev/dermametric/releases/latest) e baixe o instalador **`DermaMetric-Setup.exe`**.
2. Dê duplo clique e siga o assistente. **Não exige senha de administrador** nem instalação de programas adicionais.
3. O instalador cria atalhos na **Área de Trabalho** e no **Menu Iniciar**.

Todos os dados permanecem salvos apenas no computador da clínica:

- **Windows:** `%LOCALAPPDATA%\DermaMetric\data`
- **Linux:** `~/.local/share/dermametric/data`

### Primeiro Acesso e Chave de Recuperação (`.dmkey`)

Ao abrir o programa pela primeira vez, crie a **senha mestra** da estação clínica e:

1. Baixe o **arquivo-chave `.dmkey`** exibido na tela.
2. Guarde-o em local seguro (pendrive exclusivo ou cofre de senhas), **fora** do computador da clínica.
3. Anote e proteja sua senha mestra.

> **Atenção:** Sem a senha mestra **e** o arquivo `.dmkey`, os prontuários cifrados não podem ser recuperados em caso de troca de máquina ou formatação.

### Backup e Encaminhamento de Casos (`.dmbak`)

- **Backup completo da clínica:** acesse o **Perfil** no topo da tela → **Backup e Restauração (`.dmbak`)** → digite a senha mestra → **Exportar Backup Completo (`.dmbak`)**.
- **Exportar um caso individual:** dentro do prontuário do paciente, clique em **Exportar `.dmbak`** para gerar um arquivo cifrado isolado (ex.: `PAC-AB12CD.dmbak`), ideal para encaminhamento entre profissionais.
- **Restauração segura:** selecione o arquivo `.dmbak`, clique em **Inspecionar arquivo** para conferir data e quantidade de pacientes e escolha entre **Mesclar** (mantém os registros atuais) ou **Substituir dados clínicos**. Antes de gravar, o sistema salva automaticamente uma cópia de segurança preventiva.

### Recuperação de Acesso

Se a senha mestra for esquecida ou o sistema for instalado em um novo computador:

1. Na tela de bloqueio, clique em **Recuperar acesso com arquivo-chave (`.dmkey`)**.
2. Selecione o `.dmkey` salvo anteriormente (ou informe o código de emergência).
3. Defina uma nova senha mestra e salve o **novo `.dmkey`**.

---

## Segurança e Privacidade (LGPD)

O DermaMetric foi projetado desde a base para atender às boas práticas de proteção de dados sensíveis de saúde (**LGPD**):

- **Operação 100% offline:** nenhum dado clínico ou imagem sai do computador local.
- **Criptografia em repouso:** prontuários, anotações, medições e fotografias são armazenados de forma cifrada em disco, acessíveis somente após desbloqueio com a senha mestra.
- **Backups protegidos:** todos os arquivos `.dmbak` são criptografados com a senha mestra da clínica.

---

## Apoie o Projeto

O **DermaMetric** é um software **open-source** gratuito, desenvolvido de forma independente para oferecer fotogrametria clínica de precisão sem mensalidades.

Se o projeto agrega valor à sua clínica, pesquisa ou prática profissional, considere apoiar o desenvolvimento contínuo com qualquer valor via **Pix**:

<div align="center">
  <img src="docs/pix-qrcode.png" alt="QR Code Pix - Apoie o DermaMetric" width="210" height="210" />
  <p><strong>Chave Pix (E-mail):</strong> <code>marques-joao-e00@jim.com</code></p>
  <p><sub>Escaneie o QR Code no aplicativo do seu banco (valor livre) ou copie a chave acima.</sub></p>
</div>

**Pix Copia e Cola (valor livre):**

```text
00020126460014br.gov.bcb.pix0124marques-joao-e00@jim.com5204000053039865802BR5912JOAO MAX DEV6009ITAPIPOCA62150511DERMAMETRIC6304E1A4
```

Você também pode apoiar gratuitamente:
- Deixando uma **Star ⭐** neste repositório;
- Compartilhando o DermaMetric com colegas da área clínica e acadêmica;
- Abrindo **Issues** com sugestões ou contribuindo via **Pull Requests**.

---

## Executar a Partir do Código-Fonte (Desenvolvedores)

Para rodar o ambiente de desenvolvimento ou contribuir com o código:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Iniciar interface web local (http://127.0.0.1:8000)
.venv/bin/uvicorn backend.main:app --reload --port 8000

# Ou iniciar a janela desktop nativa
.venv/bin/python desktop_launcher.py

# Executar suíte de testes
.venv/bin/pytest -q
```

---

<div align="center">
  <p>Desenvolvido por <a href="https://github.com/joaomaxdev"><strong>joaomaxdev</strong></a></p>
</div>
