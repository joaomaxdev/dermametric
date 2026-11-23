# Changelog

Todas as mudanças relevantes do **DermaMetric** estão documentadas neste arquivo.

## [v1.0.3] — 2026-11-22

### Segurança
- Restrição de CORS exclusivamente para origens locais em loopback (`127.0.0.1` e `localhost`).
- Adição de token efêmero de sessão (`desktop_launch_guard`) com cookie `HttpOnly; SameSite=Strict`, bloqueando acessos não autorizados a partir de navegadores externos na mesma máquina (`403 Forbidden`).

### Documentação
- Reestruturação do `README.md` com foco clínico, instalação Windows via executável e suporte open-source via Pix (`JOAO MAX DEV`).
- Adição de `LICENSE` (MIT), `SECURITY.md`, `CHANGELOG.md`, `CONTRIBUTING.md` e templates de Issues.

## [v1.0.2] — 2026-11-22

### Melhorias
- Separação do backup individual de paciente/caso (`.dmbak`) do backup completo da estação clínica.
- Compactação do workspace clínico para melhor aproveitamento de tela em monitores desktop.

## [v1.0.1] — 2026-11-22

### Correções
- Ajustes no empacotamento Windows (`PyInstaller` + `Inno Setup`) e inicialização sem console (`--windowed`).

## [v1.0.0] — 2026-11-22

### Lançamento Inicial
- Plataforma desktop local-first de fotogrametria e morfometria cutânea (`T0` a `Tn`) com calibração métrica em milímetros e lupa de precisão `4×`.
- Análise colorimétrica CIELAB (`L*a*b*`, `ΔE*76` / `ΔE*00`, índice de eritema) e escalas clínicas longitudinais.
- Criptografia em repouso (Fernet), senha mestra da estação, chave de recuperação `.dmkey`, PIN por prontuário e exportação de laudos clínicos em PDF (com modo sigiloso).
