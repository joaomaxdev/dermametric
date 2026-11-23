# Como Contribuir com o DermaMetric

Contribuições de profissionais de saúde, pesquisadores e desenvolvedores são muito bem-vindas.

## Sugestões Clínicas e Relato de Problemas

- Utilize os formulários na aba **Issues** para reportar falhas ou sugerir novas métricas, escalas clínicas e melhorias de fluxo.
- Nunca anexe imagens reais de pacientes ou prontuários identificáveis em Issues públicas.

## Desenvolvimento Local

```bash
git clone <url-do-repositorio>
cd dermametric
python -m venv .venv
source .venv/bin/activate  # No Windows: .venv\Scripts\activate
pip install -r requirements.txt
pytest -q
```

Para executar a interface desktop localmente:

```bash
python desktop_launcher.py
```

## Padrão de Pull Requests

1. Crie uma branch a partir de `main` (`feat/nome-da-melhoria` ou `fix/descricao-do-ajuste`).
2. Garanta que toda a suíte de testes passe (`pytest -q`).
3. Descreva claramente o impacto clínico ou técnico no Pull Request.
