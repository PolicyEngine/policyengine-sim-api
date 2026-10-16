from unittest.mock import Mock, call

import pytest
from policyengine_fastapi.exit import exit as app_exit


@pytest.mark.asyncio
async def test_lifecycle_executes_callbacks__when_app_ends():
    app = Mock()
    callback = Mock()

    parent = Mock()
    parent.app = app
    parent.callback = callback

    app_exit()(callback)

    async with app_exit.lifespan():
        app()

    parent.assert_has_calls([call.app(), call.callback()])
