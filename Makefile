PYTHON ?= python3

.PHONY: test build check-config
test:
	$(PYTHON) -m pytest -q

build:
	$(PYTHON) -m PyInstaller --clean --noconfirm qc-folder-manager.spec

check-config:
	$(PYTHON) -m qcmanager --config config.example.yml --check-config
