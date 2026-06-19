#!/bin/bash

if [ -n "$INIT_SCRIPT" -a -f "$INIT_SCRIPT" ]; then
	python3 $INIT_SCRIPT
fi

test -z "$PORT" && PORT=3000
test -z "$HOST" && HOST=0.0.0.0
test -z "$THREADS" && THREADS=128

if [ "x$DEBUG" = "xTrue" ]; then
	EXTRA_OPS="$EXTRA_OPS --log-level debug"
fi

#flask --app run.py run --with-threads --port $PORT --host $HOST $EXTRA_OPS
gunicorn "apps:create_app()" --bind $HOST:$PORT --worker-class gevent --access-logfile - -w 1 --proxy-allow-from "*" --access-logformat '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" "Origin: %({Origin}i)s"' --threads $THREADS $EXTRA_OPS
