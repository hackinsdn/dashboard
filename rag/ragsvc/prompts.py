# -*- encoding: utf-8 -*-
"""Per-locale prompts.

Only the *instructions* are localized here. The user-facing refusal and error
texts are rendered by the dashboard through Flask-Babel -- the model is never
trusted to produce a correct pt-BR error message, and adding a locale to the UI
must not require redeploying this container.

The model signals "not in the context" with a sentinel rather than prose,
because prose has to be pattern-matched in every language and the sentinel does
not.
"""

NO_ANSWER = "NO_ANSWER"

_LANGUAGE_NAMES = {
    "en": "English",
    "pt_BR": "Brazilian Portuguese (português do Brasil)",
}

_SYSTEM = {
    "en": """You are the HackInSDN documentation assistant.

Rules, in order of importance:
1. Answer ONLY using the numbered context blocks below. Never use outside knowledge.
2. If the context blocks do not contain the answer, reply with exactly {sentinel} and nothing else.
3. Write the answer in {language}, even when the context is in another language.
4. Cite the blocks you used inline, like [1] or [2]. Every factual sentence needs a citation.
5. Be concise: at most one short paragraph or a few bullet points. Do not invent commands,
   URLs, menu names or option names that are not in the context.""",
    "pt_BR": """Você é o assistente de documentação do HackInSDN.

Regras, em ordem de importância:
1. Responda SOMENTE com base nos blocos de contexto numerados abaixo. Nunca use conhecimento externo.
2. Se os blocos de contexto não contiverem a resposta, responda exatamente {sentinel} e nada mais.
3. Escreva a resposta em {language}, mesmo que o contexto esteja em outro idioma.
4. Cite os blocos utilizados no texto, como [1] ou [2]. Toda afirmação factual precisa de citação.
5. Seja conciso: no máximo um parágrafo curto ou alguns itens. Não invente comandos, URLs,
   nomes de menus ou de opções que não estejam no contexto.""",
}

_QUESTION_LABEL = {"en": "Question", "pt_BR": "Pergunta"}
_CONTEXT_LABEL = {"en": "Context", "pt_BR": "Contexto"}


def normalize_locale(locale):
    """Map anything the dashboard sends onto a supported prompt locale."""
    if not locale:
        return "en"
    locale = locale.replace("-", "_")
    if locale in _SYSTEM:
        return locale
    base = locale.split("_")[0].lower()
    for known in _SYSTEM:
        if known.split("_")[0].lower() == base:
            return known
    return "en"


def system_prompt(locale):
    locale = normalize_locale(locale)
    return _SYSTEM[locale].format(
        sentinel=NO_ANSWER, language=_LANGUAGE_NAMES.get(locale, "English")
    )


def format_context(hits, locale="en"):
    """Render retrieved chunks as numbered blocks the model can cite."""
    locale = normalize_locale(locale)
    blocks = []
    for i, hit in enumerate(hits, start=1):
        header = hit.title_path or hit.title or hit.doc_id
        blocks.append(f"[{i}] {header}\n{hit.text}")
    return f"{_CONTEXT_LABEL[locale]}:\n\n" + "\n\n".join(blocks)


def build_messages(question, hits, locale="en", history=None):
    """Chat messages for the generation backend."""
    locale = normalize_locale(locale)
    messages = [{"role": "system", "content": system_prompt(locale)}]
    for turn in history or []:
        role = turn.get("role")
        content = (turn.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append(
        {
            "role": "user",
            "content": f"{format_context(hits, locale)}\n\n{_QUESTION_LABEL[locale]}: {question}",
        }
    )
    return messages
