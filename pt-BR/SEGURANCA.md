# Política de segurança

[English](../SECURITY.md) · [Documentação em português](README.md)

> Tradução para português brasileiro. Em caso de divergência, consulte a versão original em inglês.

## Escopo

Oppy é um monitor pessoal e auto-hospedado de oportunidades. A branch atual de desenvolvimento é `main`; não há período de suporte, cronograma de correções, SLA de resposta de segurança ou programa de recompensas prometidos.

Problemas relevantes incluem exposição de credenciais, tratamento inseguro de HTML ou URLs externos, requisições de rede não intencionais, falhas de integridade do banco ou backups e exposição de informações sensíveis por logs, fixtures ou capturas de diagnóstico. As restrições de acesso das fontes devem ser respeitadas.

## Relatos

Use a [página de avisos de segurança](https://github.com/kayo-willian/oppy/security/advisories) e **Report a vulnerability** se o relato privado de vulnerabilidades estiver disponível. Caso contrário, abra uma issue pública solicitando um meio de contato privado, sem incluir detalhes de exploração, tokens, identificadores privados ou evidências brutas. Não presuma que o relato privado esteja habilitado.

Inclua o commit afetado, ambiente, limite esperado, comportamento observado, impacto, reprodução mínima segura e correção sugerida, se conhecida. Diferencie problemas reproduzidos de hipóteses. Utilize fixtures locais e endpoints simulados; não teste sistemas sem autorização.

Não compartilhe arquivos `.env` reais, tokens do Telegram, IDs de conversas, bancos, backups, HTML bruto de diagnóstico ou logs sem sanitização. Se um token do Telegram for exposto, revogue-o pelo BotFather e substitua-o localmente.

Dê uma oportunidade razoável de investigação antes de publicar detalhes exploráveis. Bugs comuns não sensíveis devem ser relatados em Issues. Consulte [CONTRIBUICAO.md](CONTRIBUICAO.md) para testes seguros.
