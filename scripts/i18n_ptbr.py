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
    "View Labs": "Ver Labs",
    "Create Lab": "Criar Lab",
    "Create ContainerLab": "Criar ContainerLab",
    "Lab Answers": "Respostas dos Labs",
    "Answer Sheet": "Gabarito",
    "Finished Labs": "Labs Concluídos",
    "Running Labs": "Labs em Execução",
    "MANAGEMENT": "GERENCIAMENTO",
    "Users": "Usuários",
    "Support": "Suporte",
    "Lab Categories": "Categorias de Labs",
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
    # --- groups list/edit ---
    "List Groups": "Listar Grupos",
    "Create New Group": "Criar Novo Grupo",
    "Join group": "Entrar no grupo",
    "Join/Auto-Enrol": "Entrar/Auto-inscrição",
    "Edit/View": "Editar/Ver",
    "Delete group": "Excluir grupo",
    "Confirm delete group": "Confirmar exclusão do grupo",
    "Are you sure you want to delete this group?": "Tem certeza de que deseja excluir este grupo?",
    "Please provide the access token to join the group": "Forneça o token de acesso para entrar no grupo",
    "Access Token": "Token de Acesso",
    "Join the group": "Entrar no grupo",
    "No group": "Nenhum grupo",
    "Edit Group": "Editar Grupo",
    "Group updated successfully!": "Grupo atualizado com sucesso!",
    "Fail to update group!": "Falha ao atualizar o grupo!",
    "Group": "Grupo",
    "General Information": "Informações Gerais",
    "Access token is a method of allowing <i>self enrolment/auto join</i> to a group. Users will be asked to supply the access token to be authorized as a member of the group.":
        "O token de acesso é um método que permite a <i>auto-inscrição/entrada automática</i> em um grupo. Os usuários deverão fornecer o token de acesso para serem autorizados como membros do grupo.",
    "Generate Random Token": "Gerar Token Aleatório",
    "Expiration Date": "Data de Expiração",
    "The date after which this group and its resources will no longer be available. Can be used, for instance, to setup a due date for running a lab (for an exam, contest, CTF, etc). Default: never expires.":
        "A data após a qual este grupo e seus recursos deixarão de estar disponíveis. Pode ser usada, por exemplo, para definir um prazo para a execução de um laboratório (para uma prova, competição, CTF, etc). Padrão: nunca expira.",
    "Pre-Approved Users": "Usuários Pré-Aprovados",
    "Please provide a list of users (e-mail addresses, one per line) to be automatically approved when they first login.":
        "Forneça uma lista de usuários (endereços de e-mail, um por linha) a serem aprovados automaticamente no primeiro login.",
    "Members": "Membros",
    "Select the users (left) which will be <i>members</i> of the group (right). Group members cannot change any attribute of the group (only used for Labs access control).":
        "Selecione os usuários (à esquerda) que serão <i>membros</i> do grupo (à direita). Os membros do grupo não podem alterar nenhum atributo do grupo (usado apenas para controle de acesso aos Laboratórios).",
    "Assistants": "Assistentes",
    "Select the users (left) which will be <i>assistants</i> of the group (right). Group assistants are only allowed to modify the list of members and access group resources (labs and lab instances).":
        "Selecione os usuários (à esquerda) que serão <i>assistentes</i> do grupo (à direita). Os assistentes do grupo só podem modificar a lista de membros e acessar os recursos do grupo (laboratórios e instâncias de laboratório).",
    "Owners": "Proprietários",
    "Select the users (left) which will be <i>owners</i> of the group (right). Group owner are allowed to modify any attribute of the group, as well as remove it.":
        "Selecione os usuários (à esquerda) que serão <i>proprietários</i> do grupo (à direita). Os proprietários do grupo podem modificar qualquer atributo do grupo, bem como removê-lo.",
    # --- support cluster ---
    "Support Chats": "Chats de Suporte",
    "All support threads": "Todas as conversas de suporte",
    "Open support threads": "Conversas de suporte abertas",
    "Show open only": "Mostrar apenas abertas",
    "Show all threads": "Mostrar todas as conversas",
    "Unread": "Não lidas",
    "Last activity": "Última atividade",
    "No open conversations or unread messages": "Nenhuma conversa aberta ou mensagem não lida",
    "My Support": "Meu Suporte",
    "My Support Cases": "Meus Casos de Suporte",
    "Your conversations with support": "Suas conversas com o suporte",
    "new reply": "nova resposta",
    "You have no support cases yet": "Você ainda não tem casos de suporte",
    "Open": "Aberto",
    "Finished": "Finalizado",
    "Support Chat": "Chat de Suporte",
    "Thread": "Conversa",
    "Finish conversation": "Encerrar conversa",
    "Started from:": "Iniciado em:",
    "IP:": "IP:",
    "Browser:": "Navegador:",
    "Type your reply ...": "Digite sua resposta ...",
    "Send": "Enviar",
    "Finish this conversation?": "Encerrar esta conversa?",
    "Failed to send reply": "Falha ao enviar a resposta",
    "User": "Usuário",
    "Assistant": "Assistente",
    "Support Case": "Caso de Suporte",
    "My Support Case": "Meu Caso de Suporte",
    "Conversation": "Conversa",
    "You": "Você",
    "Use the chat button at the bottom-right of any page to continue this conversation.":
        "Use o botão de chat no canto inferior direito de qualquer página para continuar esta conversa.",
    "This conversation is finished. Start a new one from the chat button at the bottom-right.":
        "Esta conversa foi finalizada. Inicie uma nova pelo botão de chat no canto inferior direito.",
    # --- k8s lists ---
    "Kubernetes Pods": "Pods do Kubernetes",
    "Kubernetes Deployments": "Deployments do Kubernetes",
    "Kubernetes Services": "Serviços do Kubernetes",
    "Select all Pods": "Selecionar todos os Pods",
    "Delete selected Pods": "Excluir Pods selecionados",
    "Select all Deployments": "Selecionar todos os Deployments",
    "Delete selected Deployments": "Excluir Deployments selecionados",
    "Select all Services": "Selecionar todos os Serviços",
    "Delete selected Services": "Excluir Serviços selecionados",
    "Select all users": "Selecionar todos os usuários",
    "Delete selected users": "Excluir usuários selecionados",
    "Approve selected users": "Aprovar usuários selecionados",
    "Name": "Nome",
    "Ready": "Pronto",
    "Status": "Status",
    "Age": "Idade",
    "IP": "IP",
    "Node": "Nó",
    "Containers": "Contêineres",
    "Type": "Tipo",
    "Ports": "Portas",
    "Show pod yaml": "Mostrar YAML do pod",
    "Show deployment yaml": "Mostrar YAML do deployment",
    "Show service yaml": "Mostrar YAML do serviço",
    "Pod information in YAML format:": "Informações do Pod em formato YAML:",
    "Deployment information in YAML format:": "Informações do Deployment em formato YAML:",
    "Service information in YAML format:": "Informações do Serviço em formato YAML:",
    "Confirm User removal": "Confirmar remoção de Usuário",
    "Are you sure you want to remove the selected Users?": "Tem certeza de que deseja remover os Usuários selecionados?",
    "Delete Users": "Excluir Usuários",
    "Confirm Approve Users": "Confirmar Aprovação de Usuários",
    "Are you sure you want to approve the selected Users? Approved users have access to run Labs.":
        "Tem certeza de que deseja aprovar os Usuários selecionados? Usuários aprovados têm acesso para executar Laboratórios.",
    "Approve Users": "Aprovar Usuários",
    "Show Pod YAML": "Mostrar YAML do Pod",
    "Show Deployment YAML": "Mostrar YAML do Deployment",
    "Show Service YAML": "Mostrar YAML do Serviço",
    "Close": "Fechar",
    "No Pod running": "Nenhum Pod em execução",
    "No Deployment running": "Nenhum Deployment em execução",
    "No Service running": "Nenhum Serviço em execução",
    "Approving": "Aprovando",
    "No Pod selected!": "Nenhum Pod selecionado!",
    "No Deployment selected!": "Nenhum Deployment selecionado!",
    "No Service selected!": "Nenhum Serviço selecionado!",
    "No user selected!": "Nenhum usuário selecionado!",
    # --- dashboard / index ---
    "HackInSDN Dashboard": "Painel HackInSDN",
    "Registered Labs": "Labs Registrados",
    "Likes": "Curtidas",
    "Available CPUs": "CPUs Disponíveis",
    "Available Memory": "Memória Disponível",
    "Storage capacity": "Capacidade de Armazenamento",
    "Nodes": "Nós",
    "Lab Usage Report": "Relatório de Uso de Labs",
    "Completed Labs in the past 6 months": "Labs Concluídos nos últimos 6 meses",
    "User evaluation": "Avaliação do usuário",
    "Completed Labs": "Labs Concluídos",
    "Answered questions": "Perguntas respondidas",
    "Solved Challenges": "Desafios resolvidos",
    "Send Inquiries": "Enviar Solicitações",
    "LAST MONTH LAB COMPLETION": "CONCLUSÃO DE LABS NO ÚLTIMO MÊS",
    "LAST MONTH ANSWRED QUESTIONS": "PERGUNTAS RESPONDIDAS NO ÚLTIMO MÊS",
    "LAST MONTH SOLVED CHALLENGES": "DESAFIOS RESOLVIDOS NO ÚLTIMO MÊS",
    "Users Feedback": "Feedback dos Usuários",
    "View Feedback": "Ver Feedback",
    "Add Feedback": "Adicionar Feedback",
    "See All": "Ver Tudo",
    "Please rate your overall experience on our website!": "Por favor, avalie sua experiência geral em nosso site!",
    "Enjoying the platform?": "Está gostando da plataforma?",
    "Rating:": "Avaliação:",
    "Please leave a comment (Optional):": "Deixe um comentário (Opcional):",
    "Leave your comment...": "Deixe seu comentário...",
    "Recently Added Labs": "Labs Adicionados Recentemente",
    "No short description provided. Click to see more details": "Nenhuma descrição breve fornecida. Clique para ver mais detalhes",
    "View All Labs": "Ver Todos os Labs",
    "Lab categories and usage": "Categorias e uso de Labs",
    "Lab Instances": "Instâncias de Lab",
    "Please select a rating before submitting.": "Selecione uma avaliação antes de enviar.",
    "Rating submitted successfully!": "Avaliação enviada com sucesso!",
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
