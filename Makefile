PY ?= python

.PHONY: install dry collect backfill run bot dashboard test demo

install:
	$(PY) -m pip install -r requirements.txt

dry:            ## проверка конфига (шаг 0)
	$(PY) -m pipeline.run_collect --dry-run

collect:        ## один проход: сбор -> нормализация -> аналитика -> алерты
	$(PY) -m pipeline.run_collect

backfill:       ## догрузить историю funding и свечей за 7 дней
	$(PY) -m pipeline.run_collect --backfill-days 7

run:            ## весь сервис: планировщик + Telegram-бот
	$(PY) -m pipeline.scheduler

dashboard:
	$(PY) -m streamlit run dashboard/app.py

test:
	$(PY) -m pytest -q

demo:           ## синтетические данные в data/demo.duckdb, чтобы посмотреть бота и дашборд без ожидания
	$(PY) -m scripts.seed_demo

autostart:      ## macOS: сервис сбора стартует при входе и перезапускается при падении
	bash scripts/install_launchd.sh

autostart-off:
	bash scripts/install_launchd.sh --uninstall
