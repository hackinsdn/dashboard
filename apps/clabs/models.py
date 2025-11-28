from datetime import datetime
from apps import db

class Clab(db.Model):
    __tablename__ = "clabs"

    id = db.Column(db.String(36), primary_key=True) 
    title = db.Column(db.String(255), nullable=False)

    description = db.Column(db.String(255))
    extended_desc = db.Column(db.LargeBinary)  
    clab_guide_md = db.Column(db.Text)
    clab_guide_html = db.Column(db.Text)
    yaml_manifest = db.Column(db.Text)
    namespace_default = db.Column(db.String(128))

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def __repr__(self):
        return f"<Clab {self.id} {self.title}>"

class ClabInstance(db.Model):
    __tablename__ = "clab_instances"

    # identidade / vínculo
    id = db.Column(db.String(24), primary_key=True)  # id curto, similar ao LabInstances
    token = db.Column(db.String(64), unique=True, index=True, nullable=False)

    owner_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True, nullable=False)
    clab_id = db.Column(db.String(36), db.ForeignKey("clabs.id"), index=True, nullable=False)

    # ciclo de vida
    is_deleted = db.Column(db.Boolean, default=False, nullable=False)
    expiration_ts = db.Column(db.DateTime, index=True)  # definido em lógica de criação
    finish_reason = db.Column(db.String(64))

    # recursos materializados e namespace efetivo
    _clab_resources = db.Column(db.Text)              # JSON/texto
    namespace_effective = db.Column(db.String(128))   # opcional

    # auditoria
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<ClabInstance {self.id} of {self.clab_id}>"
    

clab_groups = db.Table(
    "clab_groups",
    db.Column("clab_id", db.String(36), db.ForeignKey("clabs.id"), primary_key=True),
    db.Column("group_id", db.Integer, db.ForeignKey("groups.id"), primary_key=True),
)

clab_categories_association = db.Table(
    "clab_categories_association",
    db.Column("clab_id", db.String(36), db.ForeignKey("clabs.id"), primary_key=True),
    db.Column("category_id", db.Integer, db.ForeignKey("lab_categories.id"), primary_key=True),
)
