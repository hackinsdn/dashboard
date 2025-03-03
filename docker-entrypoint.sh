#!/bin/bash

if [ -n "$INIT_SCRIPT" -a -f "$INIT_SCRIPT" ]; then
	python3 $INIT_SCRIPT
fi

EXTRA_OPS=""

if [ "x$DEBUG" = "xTrue" ]; then
	EXTRA_OPS="$EXTRA_OPS --debug"
fi

flask --app run.py run --with-threads --port 8080 --host 0.0.0.0 $EXTRA_OPS
