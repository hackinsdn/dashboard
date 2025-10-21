from datetime import datetime
from apps import db

class Clab(db.Model):
    __tablename__ = "clabs"
    __table_args__ = (
        db.Index(
            "ix_clabs_namespace_title_unique",
            "namespace_default",
            "title",
            unique=True,
        ),
    )

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

    groups = db.relationship(
        "Groups",
        secondary="clab_groups",
        lazy="selectin",
        backref=db.backref("clabs", lazy="selectin"),
    )

    categories = db.relationship(
        "LabCategories",
        secondary="clab_categories_association",
        lazy="selectin",
        backref=db.backref("clabs", lazy="selectin"),
    )

    instances = db.relationship(
        "ClabInstance",
        back_populates="clab",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self):
        return f"<Clab {self.id} {self.title}>"

class ClabInstance(db.Model):
    __tablename__ = "clab_instances"

    id = db.Column(db.String(24), primary_key=True)  
    token = db.Column(db.String(64), unique=True, index=True, nullable=False)

    owner_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True, nullable=False)
    clab_id = db.Column(db.String(36), db.ForeignKey("clabs.id"), index=True, nullable=False)

    is_deleted = db.Column(db.Boolean, default=False, nullable=False)
    expiration_ts = db.Column(db.DateTime, index=True)  
    finish_reason = db.Column(db.String(64))

    _clab_resources = db.Column(db.Text)             
    namespace_effective = db.Column(db.String(128)) 

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    clab = db.relationship(
        "Clab",
        back_populates="instances",
        lazy="selectin",
    )

    owner = db.relationship(
        "Users",
        lazy="selectin",
    )

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
