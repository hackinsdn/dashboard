#!/bin/bash

source venv/bin/activate

source .env

export FLASK_APP=run.py
if [ ! -f key.pem ]; then
	openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem -sha256 -days 3650 -nodes -subj "/C=XX/ST=StateName/L=CityName/O=CompanyName/OU=CompanySectionName/CN=aw-sdx-meican.amlight.net"
fi
flask run --port 5000 --host 127.0.0.1
#flask run --port 443 --host 0.0.0.0 --cert cert.pem --key key.pem
