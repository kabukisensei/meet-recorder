PREFIX ?= $(HOME)/.local
BINDIR := $(PREFIX)/bin
CLI := meet-recorder

.PHONY: install uninstall check test compile

install:
	install -d $(BINDIR)
	install -m 755 $(CLI) $(BINDIR)/$(CLI)
	@echo "Installed $(CLI) to $(BINDIR)/$(CLI)"

uninstall:
	rm -f $(BINDIR)/$(CLI)

compile:
	python3 -m py_compile $(CLI) tests/test_pure.py
	bash -n meet-recorder-summarize

test:
	python3 -m unittest discover -s tests -v

check: compile test
