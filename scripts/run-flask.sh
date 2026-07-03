#!/bin/bash

source venv/bin/activate

source .env

export FLASK_DEBUG=$DEBUG
export FLASK_APP=run.py

FQDN="${BASE_URL:-https://localhost}"
if [ ! -f key.pem ]; then
	if [[ "$FQDN" =~ ^[a-zA-Z]+://([^/:]+) ]]; then
		FQDN="${BASH_REMATCH[1]}"
	fi
	openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem -sha256 -days 3650 -nodes -subj "/C=XX/ST=StateName/L=CityName/O=CompanyName/OU=CompanySectionName/CN=$FQDN" -addext "subjectAltName=DNS:$FQDN,IP:127.0.0.1"
	# Local Dev/Test: copy the resulting cert.pem to your computer and mark it as trusted (macOS Keychain → the cert → Always Trust)
fi
flask run --with-threads --port 5000 --host 127.0.0.1
#flask run --port 443 --host 0.0.0.0 --cert cert.pem --key key.pem
