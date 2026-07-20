# Translation (i18n) workflow — see doc/i18n.md
#
# Typical cycle after adding/changing translatable strings:
#   make i18n-extract   # rebuild the .pot template from the source
#   make i18n-update    # merge new strings into every language .po
#   # ... edit apps/translations/<lang>/LC_MESSAGES/messages.po ...
#   make i18n-compile   # build the .mo catalogs the app loads at runtime
#
# To add a new language (e.g. Spanish):
#   make i18n-init LANG=es

PYBABEL ?= pybabel
TRANSLATIONS_DIR = apps/translations
POT = $(TRANSLATIONS_DIR)/messages.pot

.PHONY: i18n-extract i18n-update i18n-ptbr i18n-compile i18n-init

i18n-extract:
	$(PYBABEL) extract -F babel.cfg -k _l -o $(POT) .

i18n-update: i18n-extract
	# --no-fuzzy-matching: never auto-fill a new string from a similar existing
	# translation (those guesses are usually wrong); leave new strings empty.
	$(PYBABEL) update --no-fuzzy-matching -i $(POT) -d $(TRANSLATIONS_DIR)

# Apply the pt_BR master translation table (scripts/i18n_ptbr.py). Exits
# non-zero and lists any catalog string missing from the table.
i18n-ptbr:
	python scripts/i18n_ptbr.py

i18n-compile:
	$(PYBABEL) compile -d $(TRANSLATIONS_DIR)

i18n-init:
	@test -n "$(LANG)" || (echo "Usage: make i18n-init LANG=<code>" && exit 1)
	$(PYBABEL) init -i $(POT) -d $(TRANSLATIONS_DIR) -l $(LANG)
