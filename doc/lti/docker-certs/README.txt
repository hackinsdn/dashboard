# you should add to this folder the certificates that will be
# loaded into the apache server:
#
#	server.crt
# 	server.key
#
# Those files can be created with:
#    FQDN=test1.mydomain.tld
#    openssl req -x509 -newkey rsa:4096 -keyout server.key -out server.crt -sha256 -days 3650 -nodes -subj "/CN=$FQDN" -addext "subjectAltName=DNS:$FQDN,DNS:localhost,IP:127.0.0.1"
#
# The original creation process applied by the docker container is:
#  cat /opt/bitnami/scripts/apache/setup.sh
#    ...
#    info "Generating sample certificates"
#    SSL_KEY_FILE="${APACHE_CONF_DIR}/bitnami/certs/server.key"
#    SSL_CERT_FILE="${APACHE_CONF_DIR}/bitnami/certs/server.crt"
#    SSL_CSR_FILE="${APACHE_CONF_DIR}/bitnami/certs/server.csr"
#    SSL_SUBJ="/CN=example.com"
#    SSL_EXT="subjectAltName=DNS:example.com,DNS:www.example.com,IP:127.0.0.1"
#    rm -f "$SSL_KEY_FILE" "$SSL_CERT_FILE"
#    openssl genrsa -out "$SSL_KEY_FILE" 4096
#    # OpenSSL version 1.0.x does not use the same parameters as OpenSSL >= 1.1.x
#    if [[ "$(openssl version | grep -oE "[0-9]+\.[0-9]+")" == "1.0" ]]; then
#        openssl req -new -sha256 -out "$SSL_CSR_FILE" -key "$SSL_KEY_FILE" -nodes -subj "$SSL_SUBJ"
#    else
#        openssl req -new -sha256 -out "$SSL_CSR_FILE" -key "$SSL_KEY_FILE" -nodes -subj "$SSL_SUBJ" -addext "$SSL_EXT"
#    fi
#    openssl x509 -req -sha256 -in "$SSL_CSR_FILE" -signkey "$SSL_KEY_FILE" -out "$SSL_CERT_FILE" -days 1825 -extfile <(echo -n "$SSL_EXT")
