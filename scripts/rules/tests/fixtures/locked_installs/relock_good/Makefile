update:
	@for pyproject in libs/*/pyproject.toml projects/*/pyproject.toml; do \
		(cd "$$(dirname $$pyproject)" && uv lock --upgrade); \
	done
