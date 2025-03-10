#!/bin/bash

if [ -n "$INIT_SCRIPT" -a -f "$INIT_SCRIPT" ]; then
	python3 $INIT_SCRIPT
fi

test -z "$PORT" && PORT=3000
test -z "$HOST" && HOST=0.0.0.0

if [ "x$DEBUG" = "xTrue" ]; then
	EXTRA_OPS="$EXTRA_OPS --debug"
fi

flask --app run.py run --with-threads --port $PORT --host $HOST $EXTRA_OPS
