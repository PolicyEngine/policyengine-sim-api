update:
	@for pyproject in libs/*/pyproject.toml; do \
		(cd "$$(dirname $$pyproject)" && uv lock --upgrade); \
	done
