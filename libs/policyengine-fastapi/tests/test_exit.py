from unittest.mock import Mock, call

import pytest
from policyengine_fastapi.exit import exit


@pytest.mark.asyncio
async def test_lifecycle_executes_callbacks__when_app_ends():
    app = Mock()
    callback = Mock()

    parent = Mock()
    parent.app = app
    parent.callback = callback

    exit()(callback)

    async with exit.lifespan():
        app()

    parent.assert_has_calls([call.app(), call.callback()])
