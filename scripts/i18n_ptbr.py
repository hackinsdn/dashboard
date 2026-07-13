#!/usr/bin/env python3
"""Master pt_BR translation table for the dashboard UI.

Single source of truth for Brazilian-Portuguese strings. Run after
`make i18n-update` to (re)apply translations, then `make i18n-compile`:

    python scripts/i18n_ptbr.py

It sets msgstr for every msgid in TRANSLATIONS, clears any stale "fuzzy"
flag, and prints any catalog msgid missing from the table so it can be
added. Keeping translations here (rather than hand-editing the .po) makes
the fuzzy-matching pitfall of `pybabel update` a non-issue.
"""
import os
import sys
from babel.messages.pofile import read_po, write_po

PO_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "apps", "translations", "pt_BR", "LC_MESSAGES", "messages.po",
)

TRANSLATIONS = {
    # --- forms / auth ---
    "Identifier": "Identificador",
    "Password": "Senha",
    "Username": "Usuário",
    "Email": "E-mail",
    "I agree to the terms": "Concordo com os termos",
    "You must agree to the terms.": "Você deve concordar com os termos.",
    "Group Name": "Nome do Grupo",
    "Description": "Descrição",
    "Organization": "Organização",
    "Expiration": "Expiração",
    "Accesstoken": "Token de Acesso",
    "Confirmation Token": "Token de Confirmação",
    "Passwords must match": "As senhas devem coincidir",
    "Password confirm": "Confirmação de senha",
    "Invalid character. Use letters, numbers, dot (.), underscore (_) or hyphen (-).":
        "Caractere inválido. Use letras, números, ponto (.), sublinhado (_) ou hífen (-).",
    "Invalid character. Use letters, numbers, dot (.), underscore (_) or hyphen (-)":
        "Caractere inválido. Use letras, números, ponto (.), sublinhado (_) ou hífen (-)",
    # --- navigation / sidebar chrome ---
    "Home": "Início",
    "Contact": "Contato",
    "Search": "Pesquisar",
    "Support cases": "Casos de suporte",
    "Case": "Caso",
    "New reply": "Nova resposta",
    "No support cases yet": "Nenhum caso de suporte ainda",
    "See All Messages": "Ver todas as mensagens",
    "No Notifications": "Sem notificações",
    "See All Notifications": "Ver todas as notificações",
    "Language": "Idioma",
    "Reload profile": "Recarregar perfil",
    "Sign-out": "Sair",
    "Dashboard": "Painel",
    "Groups": "Grupos",
    "Labs": "Laboratórios",
    "View Labs": "Ver Laboratórios",
    "Create Lab": "Criar Laboratório",
    "Create ContainerLab": "Criar ContainerLab",
    "Lab Answers": "Respostas dos Laboratórios",
    "Answer Sheet": "Gabarito",
    "Finished Labs": "Laboratórios Concluídos",
    "Running Labs": "Laboratórios em Execução",
    "MANAGEMENT": "GERENCIAMENTO",
    "Users": "Usuários",
    "Support": "Suporte",
    "Lab Categories": "Categorias de Laboratório",
    "New": "Novo",
    "Pods": "Pods",
    "Deployments": "Deployments",
    "Services": "Serviços",
    "MORE INFO": "MAIS INFORMAÇÕES",
    "Documentation": "Documentação",
    "Extra tools": "Ferramentas extras",
    # --- login / register ---
    "Login": "Entrar",
    "Federated authentication?": "Autenticação federada?",
    "Local Authentication": "Autenticação Local",
    "Email or Username": "E-mail ou Usuário",
    "Create an account": "Criar uma conta",
    "Sign In": "Entrar",
    "Forgot Password": "Esqueci a senha",
    "Register": "Registrar",
    "Add your credentials": "Insira suas credenciais",
    "I agree to the": "Concordo com os",
    "terms": "termos",
    "Have an account?": "Já tem uma conta?",
    # --- token / email confirmation / reset ---
    "Insert the token sent to your email (check your spam/junk folder!). The token will expire in %(minutes)s minutes.":
        "Insira o token enviado para o seu e-mail (verifique sua caixa de spam/lixo eletrônico!). O token expira em %(minutes)s minutos.",
    "Insert the token sent to your email (check your spam/junk folder!). The token will expire in %(minutes)s minutes":
        "Insira o token enviado para o seu e-mail (verifique sua caixa de spam/lixo eletrônico!). O token expira em %(minutes)s minutos",
    "Verify code": "Verificar código",
    "Resend code": "Reenviar código",
    "Confirm your e-mail": "Confirme seu e-mail",
    "Logout": "Sair",
    "Submit": "Enviar",
    "Token": "Token",
    "We need a valid e-mail address for your account before you can continue.":
        "Precisamos de um endereço de e-mail válido para sua conta antes de continuar.",
    "Continue": "Continuar",
    "Enter your email or username": "Digite seu e-mail ou nome de usuário",
    "Email or username": "E-mail ou nome de usuário",
    # --- error / status pages ---
    "Error": "Erro",
    "Oops!": "Ops!",
    "Error.": "Erro.",
    "Failed to run task.": "Falha ao executar a tarefa.",
    "Error 403": "Erro 403",
    "Access Forbidden": "Acesso proibido",
    "You don't have permission to access this resource. This might be due to insufficient privileges or security restrictions on this server.":
        "Você não tem permissão para acessar este recurso. Isso pode ocorrer por privilégios insuficientes ou restrições de segurança neste servidor.",
    "Go Back": "Voltar",
    "Error 404": "Erro 404",
    "Page Not Found": "Página não encontrada",
    "The page you are looking for might have been removed, had its name changed, or is temporarily unavailable. Please check the URL for any mistakes.":
        "A página que você procura pode ter sido removida, ter tido seu nome alterado ou estar temporariamente indisponível. Verifique se há erros na URL.",
    "Return to Home": "Voltar ao início",
    "Error 500": "Erro 500",
    "Internal Server Error": "Erro interno do servidor",
    "Our servers are currently experiencing technical difficulties. Our team has been notified and is working to resolve the issue as quickly as possible.":
        "Nossos servidores estão enfrentando dificuldades técnicas no momento. Nossa equipe foi notificada e está trabalhando para resolver o problema o mais rápido possível.",
    "Contact Support": "Contatar o suporte",
    # --- waiting approval ---
    "Unauthorized user": "Usuário não autorizado",
    "Success!": "Sucesso!",
    "Fail to save note!": "Falha ao salvar a nota!",
    "Waiting for approval": "Aguardando aprovação",
    "You must wait until your user is approved!": "Você deve aguardar até que seu usuário seja aprovado!",
    "Your note below was already sent to the administrators and can no longer be changed. Please contact an administrator if you need to update it.":
        "Sua nota abaixo já foi enviada aos administradores e não pode mais ser alterada. Entre em contato com um administrador se precisar atualizá-la.",
    "Note for the administrators": "Nota para os administradores",
    "wait!": "aguarde!",
    "To help the administrators identify you, please leave a note below with a reference (e.g., your institution, course, professor, or who referred you to HackInSDN). Note that once saved, the note can no longer be changed.":
        "Para ajudar os administradores a identificá-lo, deixe uma nota abaixo com uma referência (ex.: sua instituição, curso, professor ou quem o indicou ao HackInSDN). Observe que, uma vez salva, a nota não pode mais ser alterada.",
    "Ex: I'm a student of Prof. X at University Y": "Ex.: Sou aluno do Prof. X na Universidade Y",
    "Save note": "Salvar nota",
    # --- common actions / table chrome ---
    "Filter": "Filtrar",
    "Clear": "Limpar",
    "Filter by Group:": "Filtrar por Grupo:",
    "Filter by Category:": "Filtrar por Categoria:",
    "Filter by Status:": "Filtrar por Status:",
    "Filter by group:": "Filtrar por grupo:",
    "All Groups": "Todos os Grupos",
    "All Labs": "Todos os Laboratórios",
    "All labs": "Todos os laboratórios",
    "My own labs": "Meus próprios laboratórios",
    "Hide deleted": "Ocultar excluídos",
    "Show deleted": "Mostrar excluídos",
    "Completed": "Concluído",
    "Not Completed": "Não Concluído",
    "Deleted": "Excluído",
    "Collapse": "Recolher",
    "Remove": "Remover",
    "Resume": "Retomar",
    "Start": "Iniciar",
    "Fork": "Bifurcar",
    "Update": "Atualizar",
    "Delete": "Excluir",
    "Edit": "Editar",
    "View": "Visualizar",
    "Restore": "Restaurar",
    "Cancel": "Cancelar",
    "Actions": "Ações",
    "Success": "Sucesso",
    "Failure": "Falha",
    "Deleting": "Excluindo",
    "row(s)...": "linha(s)...",
    "No lab selected!": "Nenhum laboratório selecionado!",
    "User": "Usuário",
    "Lab title": "Título do laboratório",
    "Created At (UTC)": "Criado em (UTC)",
    "Finished At (UTC)": "Concluído em (UTC)",
    "Finish reason": "Motivo da conclusão",
    "No finished labs": "Nenhum laboratório concluído",
    # --- labs view modals ---
    "Confirm delete lab": "Confirmar exclusão do laboratório",
    "Are you sure you want to delete Lab": "Tem certeza de que deseja excluir o Laboratório",
    "Delete lab": "Excluir laboratório",
    "Confirm restore lab": "Confirmar restauração do laboratório",
    "Are you sure you want to restore this lab?": "Tem certeza de que deseja restaurar este laboratório?",
    "Restore lab": "Restaurar laboratório",
    # --- gallery ---
    "Gallery": "Galeria",
    # --- lab categories list/edit ---
    "List Lab Categories": "Listar Categorias de Laboratório",
    "Ops! A failure happened..": "Ops! Ocorreu uma falha..",
    "Create New Lab Category": "Criar Nova Categoria de Laboratório",
    "Category": "Categoria",
    "Color": "Cor",
    "Delete lab category": "Excluir categoria de laboratório",
    "Confirm delete Lab Category": "Confirmar exclusão da Categoria de Laboratório",
    "Are you sure you want to delete Lab Category": "Tem certeza de que deseja excluir a Categoria de Laboratório",
    "Delete Lab Category": "Excluir Categoria de Laboratório",
    "No lab category": "Nenhuma categoria de laboratório",
    "Edit Lab Category": "Editar Categoria de Laboratório",
    "Lab Category updated successfully!": "Categoria de laboratório atualizada com sucesso!",
    "Fail to update Lab Category!": "Falha ao atualizar a Categoria de Laboratório!",
    "New Lab Category": "Nova Categoria de Laboratório",
    "Lab Category": "Categoria de Laboratório",
    "Category Name": "Nome da Categoria",
}


def main():
    with open(PO_PATH, "rb") as f:
        catalog = read_po(f)
    catalog.language = "pt_BR"
    missing = []
    for msg in catalog:
        if not msg.id:
            continue
        if msg.id in TRANSLATIONS:
            msg.string = TRANSLATIONS[msg.id]
            if msg.fuzzy:
                msg.flags.discard("fuzzy")
        elif not msg.string or msg.fuzzy:
            missing.append(msg.id)
    with open(PO_PATH, "wb") as f:
        write_po(f, catalog, width=79)
    if missing:
        print("Untranslated msgids (add to scripts/i18n_ptbr.py):")
        for m in missing:
            print("  " + repr(m))
        sys.exit(1)
    print("All %d catalog strings translated." % len([m for m in catalog if m.id]))


if __name__ == "__main__":
    main()
