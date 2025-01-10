# HackInSDN Dashboard

Dashboard for HackInSDN

## Running

```
git clone https://github.com/hackinsdn/dashboard
cd dashboard
python3 -m venv venv
source venv/bin/activate
python3 -m pip install -r requirements.txt
deactivate
cp env-template .env
# ==> change .env file as needed
# ==> you may also want to change apps/config.py
./run-flask.sh
```
