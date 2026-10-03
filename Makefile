.PHONY: build check test release clean

VERSION ?=

build:
	python3 build.py

check: build
	python3 build.py --check

test: build
	python3 -m unittest discover -s tests -p 'test_*.py' -v
	bash tests/test_shell.sh

release:
	@test -n "$(VERSION)" || (echo 'usage: make release VERSION=x.y.z' >&2; exit 1)
	@./scripts/release.sh "$(VERSION)"

clean:
	rm -rf src/claude_copilot_shim/__pycache__ tests/__pycache__
