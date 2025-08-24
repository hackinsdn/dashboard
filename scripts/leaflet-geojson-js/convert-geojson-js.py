#!/usr/bin/env python3

import json, sys

with open(sys.argv[1]) as s:
    geojson = s.read()
with open(sys.argv[2], "w") as d:
    d.write("var statesData = " + geojson)
