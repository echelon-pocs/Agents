# PRD-001: Triagem Matinal de Email

- **Status:** DRAFT
- **Criado:** 2026-05-07
- **Owner:** Maestro
- **PO:** Pedro
- **Origem:** conversa em chat 2026-05-07

---

## 1. Problem Statement

A caixa de entrada principal recebe ~50 emails/dia, dos quais a esmagadora maioria é ruído (newsletters, notificações automáticas, marketing). O custo cognitivo de triar manualmente todas as manhãs é desproporcional ao valor extraído — emails verdadeiramente importantes ficam misturados com FYI e ruído, e o tempo de resposta sofre.

O Jarbas deve agir como assistente de triagem: ler a caixa de entrada, classificar por relevância, produzir um resumo matinal acionável e (numa fase posterior) propor rascunhos de resposta para aprovação manual. Tudo localmente, sem que o conteúdo dos emails saia da LAN.

## 2. Goals & Non-Goals

### Goals
- Reduzir o tempo de triagem matinal de ~20min para <5min.
- Garantir que nenhum email **Importante** passa despercebido.
- Manter 100% do processamento de conteúdo dentro da rede local.
- Entregar o resumo às 07:30 via Telegram, antes do início do dia.
- Permitir, em fase 2, gerar rascunhos de resposta com aprovação one-shot.

### Non-Goals (v1)
- **Sem auto-envio** de respostas. O humano aprova sempre.
- **Sem suporte multi-conta.** Apenas a conta Gmail principal.
- **Sem aprendizagem online** em v1 (sem fine-tuning, sem feedback loop).
- **Sem triagem em tempo real.** É um batch matinal, não uma stream contínua.
- **Sem ações para além de classificar e resumir** (não arquiva, não apaga, não move).
- **Sem integração com calendário, tarefas, ou CRM** nesta versão.

## 3. User Stories

- **US-1 — Resumo matinal:** Como utilizador, quero receber às 07:30 um resumo Telegram com os emails da última noite agrupados por importância, para decidir o que ler com pressa antes do trabalho.
- **US-2 — Confiança na classificação:** Como utilizador, quero que **Importante** seja conservador (poucos falsos negativos), para confiar que nada crítico está enterrado em FYI/Ruído.
- **US-3 — Drill-down:** Como utilizador, quero abrir o resumo e ver o assunto + remetente + 1-2 linhas de cada email Importante, para decidir se respondo já ou mais tarde.
- **US-4 — Rascunhos de resposta (Fase 2):** Como utilizador, quero pedir ao Jarbas um rascunho de resposta para um email específico, rever, editar se preciso, e aprovar com um toque para enviar.
- **US-5 — Privacidade:** Como dono da casa, quero garantir que o conteúdo de qualquer email nunca é enviado a um serviço externo de inferência.

## 4. Functional Requirements (phased)

### Fase 1 — Fetch + Classificação + Resumo

**FR-1.1** O sistema obtém emails novos da conta Gmail principal via IMAP (com OAuth2 ou app password) numa cadência configurável (default: a cada 15min entre 22:00-07:30 e batch único às 07:25).

**FR-1.2** Os emails são armazenados localmente em formato Maildir num volume cifrado (decisão de storage será ADR à parte).

**FR-1.3** Para cada email novo, é executada uma classificação local em três tiers:
- **Importante:** requer atenção ou resposta humana num prazo curto (pessoal, trabalho urgente, financeiro, segurança, família).
- **FYI:** informativo, vale a pena ler mas sem ação imediata (newsletters de qualidade, atualizações de serviços que uso).
- **Ruído:** marketing, notificações automáticas redundantes, spam não filtrado.

**FR-1.4** A classificação usa um LLM local (Ollama) com prompt estruturado e few-shot examples. Sem fine-tuning em v1.

**FR-1.5** Camada determinística (rules) precede o LLM:
- Allowlist de remetentes → força Importante.
- Denylist / padrões conhecidos (no-reply de marketing, unsubscribe headers comuns) → força Ruído.
- O LLM só decide o que as regras não decidem.

**FR-1.6** Às 07:30, é gerado um resumo agregado e enviado via Telegram:
- Cabeçalho: contagem por tier.
- Lista detalhada de **Importantes** (assunto, remetente, snippet de 1-2 linhas).
- Lista compacta de **FYI** (assunto + remetente).
- **Ruído** apenas como contagem.

**FR-1.7** A entrega Telegram inclui um link/comando para drill-down (e.g., `/email 3` mostra o corpo completo do 3º email Importante).

**FR-1.8** Falhas de fetch ou classificação não silenciam o resumo — o Jarbas envia um aviso explícito ("3 emails não foram classificados, ver logs").

### Fase 2 — Rascunhos de Resposta

**FR-2.1** A partir do resumo, o utilizador pode invocar `/draft <id>` para pedir um rascunho de resposta a um email Importante.

**FR-2.2** O LLM local gera um rascunho com base no histórico do thread (se disponível em Maildir) e estilo configurado pelo utilizador.

**FR-2.3** O rascunho é apresentado no Telegram com três opções: **Aprovar e enviar**, **Editar**, **Descartar**.

**FR-2.4** "Aprovar e enviar" envia via SMTP autenticado da conta principal. Nada é enviado sem este passo explícito.

**FR-2.5** Edições são feitas em-chat (texto livre que substitui o rascunho) ou via comando que abre o rascunho num editor temporário acessível por interface.

### Fase 3 — Aprendizagem

**FR-3.1** Captura de sinal: quando o utilizador (a) corrige a classificação de um email, ou (b) edita substancialmente um rascunho, o sistema regista o par (input, correção).

**FR-3.2** Refinamento do prompt few-shot baseado em correções acumuladas (curadoria semi-automática, sem fine-tuning ainda).

**FR-3.3** Avaliação periódica da accuracy de classificação contra correções históricas. Se cair abaixo de threshold, alerta.

## 5. Technical Architecture (high-level)

```
┌──────────────┐    IMAP/OAuth2   ┌────────────────┐
│  Gmail (ext) │ ───────────────▶ │  Janus (fetch) │
└──────────────┘                  └───────┬────────┘
                                          │ Maildir (cifrado)
                                          ▼
                                  ┌────────────────┐
                                  │   Mercurius    │
                                  │  (rules + LLM) │
                                  └───────┬────────┘
                                          │ classified
                                          ▼
                                  ┌────────────────┐    ┌──────────┐
                                  │   Pollux       │◀───┤  Ollama  │
                                  │ (LLM serving)  │    └──────────┘
                                  └───────┬────────┘
                                          │ summary
                                          ▼
                                  ┌────────────────┐
                                  │    Hermes      │ ───▶ Telegram
                                  │  (delivery)    │
                                  └────────────────┘
```

**Agent ownership:**
- **Janus** — fetch IMAP, OAuth2/app password, persistência Maildir, SMTP send (Fase 2).
- **Mercurius** — pipeline de classificação, rules engine, prompts, orquestração geral, geração de rascunhos (Fase 2).
- **Pollux** — escolha do modelo Ollama, benchmark de qualidade vs latência, optimização de prompts.
- **Hermes** — formatação e entrega Telegram, comandos interativos (`/email`, `/draft`).
- **Castor** — volume cifrado, backup do Maildir, scheduling (cron/systemd timer).
- **Vesta** — auditoria de fluxo, garantia de no-egress de conteúdo, gestão de credenciais.
- **Argos** — testes e2e, fixtures de emails, validação de classificação.

**Fluxo nominal (07:25-07:30):**
1. Cron dispara batch final de fetch (Janus).
2. Mercurius lê novos emails do Maildir, aplica rules, depois LLM para os não-decididos.
3. Mercurius escreve classificações como metadata anexa ao Maildir (ficheiros `.jarbas.json` paralelos).
4. Hermes lê classificações, formata resumo, envia via Telegram Bot API.
5. Estado persistido para drill-down posterior.

## 6. Technical Constraints

- **Privacidade:** o conteúdo dos emails **nunca** sai da LAN. Apenas o tráfego IMAP/SMTP cifrado com o Gmail é permitido. Ollama, classificação, geração de rascunhos — tudo on-prem.
- **Latência:** o resumo das 07:30 deve estar entregue antes das 07:32 em condições nominais. Classificação por email <2s em hardware local.
- **Volume:** dimensionado para ~50 emails/dia. Deve degradar graciosamente até ~200/dia.
- **Modelo:** LLM local via Ollama. Pollux escolhe o modelo (provável candidato: 8-14B class). Sem dependência de APIs externas.
- **Resiliência:** falha de IMAP, Ollama down, ou Telegram inacessível devem produzir um relatório de erro e não silenciar o utilizador.
- **Credenciais:** OAuth2 tokens / app passwords / Telegram bot token armazenados via secret store local (gerido por Vesta — ADR à parte).
- **Backup:** Maildir cifrado incluído na rotina de backup do NAS (Castor).

## 7. Success Metrics

**Fase 1:**
- **M1:** Resumo entregue antes das 07:32 em ≥95% dos dias durante 30 dias consecutivos.
- **M2:** Zero falsos negativos críticos (nenhum email **Importante** real classificado como Ruído) durante 14 dias de avaliação manual.
- **M3:** Tempo de triagem matinal auto-reportado pelo PO desce de ~20min para <5min.
- **M4:** Zero egress de conteúdo de email detectado em auditoria de rede (Vesta).

**Fase 2:**
- **M5:** ≥60% dos rascunhos aprovados sem edição substancial (edição <20% do texto).
- **M6:** Zero envios de email não autorizados pelo humano.

**Fase 3:**
- **M7:** Accuracy de classificação ≥90% medida contra correções acumuladas.
- **M8:** Redução mensurável da taxa de correção ao longo do tempo.

## 8. Open Questions

**OQ-1 — Backend de armazenamento:** Maildir vs Dovecot vs Nextcloud Mail. Será resolvido em ADR-001 (a propor).

**OQ-2 — Autenticação Gmail:** OAuth2 (mais limpo, mais setup) vs App Password (mais simples, depende de 2FA + setting que a Google pode descontinuar). Recomendação inicial: App Password para v1, migrar para OAuth2 se necessário.

**OQ-3 — Modelo LLM concreto:** Pollux deve fazer benchmark de pelo menos 3 candidatos (e.g., Llama 3.1 8B, Qwen 2.5 14B, Mistral) em fixtures de classificação reais antes de decidir. ADR à parte.

**OQ-4 — Granularidade do tier "Importante":** dividir em "Urgente" vs "Importante" desde já, ou manter um só tier e usar ordenação interna? Recomendação: um só tier em v1, reavaliar em Fase 3.

**OQ-5 — Histórico para rascunhos (Fase 2):** quanto contexto de threads anteriores incluir no prompt? Trade-off entre qualidade e janela de contexto. A definir em Fase 2.

**OQ-6 — Estilo de resposta:** o utilizador quer um único estilo configurado, ou estilos por contacto/categoria? Decisão para Fase 2.

**OQ-7 — Tratamento de anexos:** ignorar em v1 ou extrair texto (PDF/DOCX) para classificação? Recomendação: ignorar em v1, mencionar presença no resumo.

**OQ-8 — Internacionalização:** prompts em PT, EN, ou ambos? Recomendação: prompts internos em EN, output ao utilizador em PT.

## 9. Phase Plan

| Fase | Scope | Agentes principais | Deliverable |
|------|-------|--------------------|-------------|
| 1 | Fetch + classificação + resumo Telegram | Janus, Mercurius, Pollux, Hermes, Castor, Vesta, Argos | Resumo diário 07:30 funcional |
| 2 | Rascunhos de resposta + envio aprovado | Mercurius, Janus, Hermes, Vesta, Argos | Workflow `/draft` end-to-end |
| 3 | Captura de correções + refinamento de prompts | Mercurius, Pollux, Argos | Loop de melhoria mensurável |

## 10. Acceptance Log

- 2026-05-07 — PRD criado em DRAFT, a aguardar confirmação do PO.

---
