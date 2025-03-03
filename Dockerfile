FROM python:3.12-bookworm

RUN --mount=source=requirements.txt,target=/mnt/requirements.txt,type=bind \
    pip3 install -r /mnt/requirements.txt \
 && cd /tmp \
 && curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" \
 && install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl \
 && rm -rf /var/lib/apt/lists/* /tmp/*

COPY . /app
WORKDIR /app

EXPOSE 8080

ENTRYPOINT ["/app/docker-entrypoint.sh"]
