PORT ?= 8855
SCALE ?= 1.0
MAX_FALSE ?= 0.1
DW = uv run datawatch

.PHONY: install test lint build eval gate run docker-build screenshot clean

install:
	uv sync --python 3.12

test:
	uv run pytest -q

lint:
	uvx ruff check .

build:  ## generate the 41-table warehouse into data/warehouse.duckdb
	$(DW) build --scale $(SCALE)

eval:  ## learned baselines vs static thresholds against the 8 planted incidents
	$(DW) eval --scale $(SCALE) --out docs/evaluation.json --markdown docs/evaluation.md

gate:  ## what CI runs: fail unless every planted incident is detected and false alerts stay under MAX_FALSE
	$(DW) eval --scale $(SCALE) --gate --max-false-per-table-week $(MAX_FALSE)

run:
	DATAWATCH_SCALE=$(SCALE) $(DW) serve --port $(PORT)

docker-build:
	docker build -t data-watch .

screenshot:  ## needs the server running on $(PORT) and Google Chrome
	uv run python scripts/screenshot.py --port $(PORT)

clean:
	rm -rf .pytest_cache .ruff_cache data
