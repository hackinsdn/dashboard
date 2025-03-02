from apps import db
from run import app
from apps.authentication.models import Users
from apps.home.models import Labs, LabCategories
from sqlalchemy import event
import sys
import uuid


def dbinit():
    try:
        cats = LabCategories.query.all()
        if len(cats) > 0:
            return
    except:
        pass
    db.create_all()
    default_pw = uuid.uuid4().hex[:8]
    admin = Users(
        username="admin",
        password=default_pw,
        email="admin@localhost.com",
        category="admin"
    )
    db.session.add(admin)
    print("#######################################")
    print(f"## Default admin password: {default_pw}")
    print("#######################################")

    idx = 1
    for cat, color in [('All', 'dark'), ('Introductory', 'light'), ('Intermediate', 'info'), ('Advanced', 'warning'), ('Offensive Security', 'danger'), ('Defensive Security', 'success'), ('Networking', 'primary')]:
        lab_cat = LabCategories(id=idx, category=cat, color_cls=color)
        db.session.add(lab_cat)
        idx += 1

    lab1 = Labs(
        category_id=2,
        title="Hello World",
        description="Hello World testing lab",
        manifest='apiVersion: apps/v1\r\nkind: Deployment\r\nmetadata:\r\n  name: helloworld-hackinsdn-${pod_hash}\r\n  labels:\r\n    app: helloworld-hackinsdn-${pod_hash}\r\nspec:\r\n  replicas: 1\r\n  selector:\r\n    matchLabels:\r\n      app: helloworld-hackinsdn-${pod_hash}\r\n  template:\r\n    metadata:\r\n      name: helloworld-hackinsdn-${pod_hash}\r\n      labels:\r\n        app: helloworld-hackinsdn-${pod_hash}\r\n    spec:\r\n      containers:\r\n      - name: helloworld-hackinsdn\r\n        image: hackinsdn/helloworld:latest\r\n        ports:\r\n        - containerPort: 80\r\n---\r\napiVersion: v1\r\nkind: Service\r\nmetadata:\r\n  name: helloworld-hackinsdn-${pod_hash}\r\n  labels:\r\n    app: helloworld-hackinsdn-${pod_hash}\r\nspec:\r\n  type: NodePort\r\n  ports:\r\n  - port: 80\r\n    targetPort: 80\r\n    name: http-helloworld-webserver\r\n  selector:\r\n    app: helloworld-hackinsdn-${pod_hash}',
        extended_desc=b'<h1>Hello World</h1><p><br></p><p>This lab is a hello world just to make sure everything is working.</p>',
    )
    lab1.set_lab_guide_md(
            '# Lab guide for Hello World\r\n\r\nThis is a lab guide for Hello World\r\n\r\n## 1. Make sure this lab is working\r\n\r\nTo make sure this lab is working, you should open the helloworld service as ilustrated below:\r\n\r\n![open-service](https://raw.githubusercontent.com/hackinsdn/labs/refs/heads/main/lab00-helloworld/images/helloworld-open-service.png)\r\n\r\nYou should see something like this:\r\n\r\n![service](https://raw.githubusercontent.com/hackinsdn/labs/refs/heads/main/lab00-helloworld/images/helloworld-service.png)\r\n\r\n> [!IMPORTANT]  \r\n> The hello world service shows a message indicating the status of the system. Which option below best describe the status indicated by the hello world service?\r\n>\r\n> <input type="radio" name="answer_helloworld_q1" id="id1" value="system-degradated" /> <label for="id1">System is working in a degradated status.</label><br>\r\n> <input type="radio" name="answer_helloworld_q1" id="id2" value="system-not-working" /> <label for="id2">System is not working.</label><br>\r\n> <input type="radio" name="answer_helloworld_q1" id="id3" value="system-working" /> <label for="id3">System is working correctly.</label><br>\r\n> <input type="radio" name="answer_helloworld_q1" id="id4" value="system-unknow" /> <label for="id4">It is not possible to know the system status.</label><br>\r\n\r\n## 2. Accessing the lab console\r\n\r\nSometimes you will need to run commands on some components of the Lab. Follow the indication below to run commands on the Lab container:\r\n\r\n![open-terminal](https://raw.githubusercontent.com/hackinsdn/labs/refs/heads/main/lab00-helloworld/images/helloworld-open-term.png)\r\n\r\nYou should see something like this:\r\n\r\n![terminal](https://raw.githubusercontent.com/hackinsdn/labs/refs/heads/main/lab00-helloworld/images/helloworld-terminal.png)\r\n\r\n> [!IMPORTANT]  \r\n> When you clicked on the link indicated above, which component was loaded from the Lab:\r\n>\r\n> <select name="answer_helloworld_q2">\r\n>  <option value="">--</option>\r\n>  <option>The Kubernetes cluster</option>\r\n>  <option>The terminal of the container</option>\r\n>  <option>Nothing was opened</option>\r\n>  <option>All options are correct</option>\r\n> </select>'
    )
    db.session.add(lab1)

    db.session.commit()

with app.app_context():
    dbinit()
