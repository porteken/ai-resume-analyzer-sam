"""Unit tests for runtime helpers in resume_analyzer.utils."""

from unittest.mock import MagicMock

import pytest


@pytest.mark.unit
class TestRuntimeUtils:
    def test_get_dynamodb_resource_uses_keepalive_and_standard_retries(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from resume_analyzer import utils

        mock_resource = MagicMock(name="dynamodb-resource")
        resource_factory = MagicMock(return_value=mock_resource)
        monkeypatch.setattr(utils.boto3, "resource", resource_factory)
        utils.reset_cached_clients()

        result = utils.get_dynamodb_resource()

        assert result is mock_resource
        resource_factory.assert_called_once()
        assert resource_factory.call_args.args == ("dynamodb",)
        config = resource_factory.call_args.kwargs["config"]
        assert config.tcp_keepalive is True
        assert config.retries == {"max_attempts": 3, "mode": "standard"}

    def test_get_s3_client_uses_keepalive_and_standard_retries(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from resume_analyzer import utils

        mock_client = MagicMock(name="s3-client")
        client_factory = MagicMock(return_value=mock_client)
        monkeypatch.setattr(utils.boto3, "client", client_factory)
        utils.reset_cached_clients()

        result = utils.get_s3_client()

        assert result is mock_client
        client_factory.assert_called_once()
        assert client_factory.call_args.args == ("s3",)
        config = client_factory.call_args.kwargs["config"]
        assert config.tcp_keepalive is True
        assert config.retries == {"max_attempts": 3, "mode": "standard"}
