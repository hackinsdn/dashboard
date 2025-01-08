from apps import db
from flask import current_app as app
from apps.authentication.models import Users
from apps.home.models import Labs, LabCategories
from sqlalchemy import event
import sys

## Engine events track
#@event.listens_for(db.engine, 'handle_error')
#def receive_handle_error(e):
#    app.logger.error('%s' % (e.original_exception))
#    sys.exc_clear()
#    sys.exit(1)


db.create_all()
#admin = Users(
#    username="admin",
#    password="hackinsdn",
#    email="admin@localhost.com",
#    category="admin"
#)
#db.session.add(admin)
#
#idx = 1
#for cat, color in [('All', 'dark'), ('Introductory', 'light'), ('Intermediate', 'info'), ('Advanced', 'warning'), ('Offensive Security', 'danger'), ('Defensive Security', 'success'), ('Networking', 'primary')]:
#    lab_cat = LabCategories(id=idx, category=cat, color_cls=color)
#    db.session.add(lab_cat)
#    idx += 1
#
#lab1 = Labs(
#    id="774b5484-4aea-4ee9-a645-907261bd9c7b",
#    title="Detecção e contenção de ataques de varredura de portas",
#    description="""
#    Neste laboratório o aluno terá contato com ferramentas para simulação de
#    ataques de varredura de portas (scan), como estes ataques podem ser
#    detectados em uma ferramenta de monitoramento de segurança (IDS) e como
#    pode-se conter/bloquear tais ataques.
#    """,
#    category_id=3,
#)
#db.session.add(lab1)
db.session.commit()
