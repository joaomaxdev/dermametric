# Política de Segurança — DermaMetric

## Arquitetura de Privacidade e Proteção (LGPD)

O **DermaMetric** foi projetado sob o modelo *local-first* para processamento de imagens e dados clínicos sensíveis:

- **Processamento 100% local:** Nenhuma imagem clínica, dado de paciente, hash ou telemetria é enviado para servidores externos.
- **Criptografia em repouso:** Imagens brutas, alinhadas e realçadas são armazenadas cifradas em disco utilizando **Fernet (AES-128-CBC + HMAC-SHA256)** com derivação PBKDF2HMAC-SHA256 atrelada à senha mestra e ao arquivo-chave `.dmkey`.
- **Sigilo de prontuário:** Nomes completos de pacientes são mascarados por padrão nas listagens e podem ser protegidos individualmente por PIN numérico.
- **Isolamento da sessão desktop:** O servidor local escuta exclusivamente em loopback (`127.0.0.1`), restringe CORS à origem local e exige um token efêmero de sessão vinculado via cookie `HttpOnly; SameSite=Strict` à janela nativa do aplicativo (`pywebview`).

## Como Reportar uma Vulnerabilidade

Se você identificar uma falha de segurança, exposição indevida de dados ou problema na criptografia:

1. **Não abra uma Issue pública** descrevendo passos de exploração enquanto a correção não estiver publicada.
2. Envie um relato detalhado diretamente ao mantenedor pelo GitHub (`@joaomaxdev`) ou utilizando a aba **Security → Advisories → Report a vulnerability** deste repositório.
3. Inclua, sempre que possível, a versão do aplicativo (`v1.0.x`), o sistema operacional e os passos para reproduzir o comportamento.
