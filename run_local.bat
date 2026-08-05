@echo off
setlocal
if not exist .venv (
  py -3.12 -m venv .venv
)
call .venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python manage.py migrate --run-syncdb
python manage.py bootstrap_admin
python manage.py seed_defaults
python manage.py runserver
