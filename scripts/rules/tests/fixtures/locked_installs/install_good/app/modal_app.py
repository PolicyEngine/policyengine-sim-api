import modal

image = modal.Image.debian_slim().uv_sync(uv_project_dir=".", frozen=True)
