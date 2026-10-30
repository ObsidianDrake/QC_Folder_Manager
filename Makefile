PYTHON ?= python3
VENV ?= .venv
VENV_PYTHON := $(VENV)/bin/python
CONFIG ?= config.example.yml
DEPS_STAMP := $(VENV)/.qc-dependencies

.DEFAULT_GOAL := all
.PHONY: all help venv backend executable build test check-config smoke release clean

all: executable

help:
	@echo 'make / make build  Create venv, install dependencies, build binary'
	@echo 'make test          Prepare environment and run tests'
	@echo 'make check-config  Validate CONFIG (default: config.example.yml)'
	@echo 'make smoke         Build and smoke-test the packaged binary'
	@echo 'make release       RHEL 8.10 / Python 3.9.10 release validation'
	@echo 'make clean         Remove build outputs; keep venv, config and data'
	@echo 'Options: PYTHON=/path/to/python3  VENV=.venv  CONFIG=config.yml'

venv:
	@if [ ! -x "$(VENV_PYTHON)" ]; then \
		echo 'Creating Python virtual environment...'; \
		"$(PYTHON)" -c 'import sys; sys.exit("Python 3.9 or newer is required") if sys.version_info < (3, 9) else None' && \
		"$(PYTHON)" -m venv "$(VENV)"; \
	fi
	@"$(VENV_PYTHON)" -c 'import sys; sys.exit("Existing venv requires Python 3.9 or newer") if sys.version_info < (3, 9) else None'

$(DEPS_STAMP): requirements.txt requirements-build.txt requirements-dev.txt | venv
	@echo 'Installing pinned application, build and test dependencies...'
	"$(VENV_PYTHON)" -m pip install -r requirements-build.txt -r requirements-dev.txt
	@touch "$@"

backend: $(DEPS_STAMP)

executable: backend
	@echo 'Building QC Folder Manager (UI and SSH helper included)...'
	"$(VENV_PYTHON)" -m PyInstaller --clean --noconfirm qc-folder-manager.spec
	@echo 'Build complete: dist/qc-folder-manager'
	@echo 'Configuration is external; use --config /path/to/config.yml'

build: executable

test: backend
	"$(VENV_PYTHON)" -m pytest -q

check-config: backend
	"$(VENV_PYTHON)" -m qcmanager --config "$(CONFIG)" --check-config

smoke: executable
	"$(VENV_PYTHON)" tests/smoke_binary.py dist/qc-folder-manager

release: backend
	env PYTHON="$(abspath $(VENV_PYTHON))" bash scripts/build-rhel8.sh

clean:
	@echo 'Removing build/ and dist/ only...'
	rm -rf -- build dist
