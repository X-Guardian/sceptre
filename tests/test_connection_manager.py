# -*- coding: utf-8 -*-

from typing import Union
from unittest.mock import Mock, patch, sentinel, create_autospec

import pytest
from boto3.session import Session
from botocore.exceptions import ClientError
from moto import mock_s3

from sceptre.connection_manager import (
    ConnectionManager,
    _retry_boto_call,
    STACK_DEFAULT,
)
from sceptre.exceptions import RetryLimitExceededError, InvalidAWSCredentialsError


class TestConnectionManager(object):
    def setup_method(self, test_method):
        self.stack_name = None
        self.profile = None
        self.iam_role = None
        self.iam_role_session_duration = 3600
        self.region = "eu-west-1"

        self.environment_variables = {
            "AWS_ACCESS_KEY_ID": "sceptre_test_key_id",
            "AWS_SECRET_ACCESS_KEY": "sceptre_test_access_key",
        }
        self.session_class = create_autospec(Session)
        self.mock_session: Union[Mock, Session] = self.session_class.return_value

        ConnectionManager._boto_sessions = {}
        ConnectionManager._clients = {}
        ConnectionManager._stack_keys = {}

        self.connection_manager = ConnectionManager(
            region=self.region,
            stack_name=self.stack_name,
            profile=self.profile,
            iam_role=self.iam_role,
            session_class=self.session_class,
            get_envs_func=lambda: self.environment_variables,
        )

    def test_connection_manager_initialised_with_no_optional_parameters(self):
        connection_manager = ConnectionManager(region=sentinel.region)

        assert connection_manager.stack_name is None
        assert connection_manager.profile is None
        assert connection_manager.region == sentinel.region
        assert connection_manager._boto_sessions == {}
        assert connection_manager._clients == {}
        assert connection_manager._stack_keys == {}

    def test_connection_manager_initialised_with_all_parameters(self):
        connection_manager = ConnectionManager(
            region=self.region,
            stack_name="stack",
            profile="profile",
            iam_role="iam_role",
            iam_role_session_duration=21600,
        )

        assert connection_manager.stack_name == "stack"
        assert connection_manager.profile == "profile"
        assert connection_manager.iam_role == "iam_role"
        assert connection_manager.iam_role_session_duration == 21600
        assert connection_manager.region == self.region
        assert connection_manager._boto_sessions == {}
        assert connection_manager._clients == {}
        assert connection_manager._stack_keys == {
            "stack": (self.region, "profile", "iam_role")
        }

    def test_repr(self):
        self.connection_manager.stack_name = "stack"
        self.connection_manager.profile = "profile"
        self.connection_manager.region = "region"
        self.connection_manager.iam_role = "iam_role"
        response = self.connection_manager.__repr__()
        assert (
            response == "sceptre.connection_manager.ConnectionManager("
            "region='region', profile='profile', stack_name='stack', "
            "iam_role='iam_role', iam_role_session_duration='None')"
        )

    def test_repr_with_iam_role_session_duration(self):
        self.connection_manager.stack_name = "stack"
        self.connection_manager.profile = "profile"
        self.connection_manager.region = "region"
        self.connection_manager.iam_role = "iam_role"
        self.connection_manager.iam_role_session_duration = 21600
        response = self.connection_manager.__repr__()
        assert (
            response == "sceptre.connection_manager.ConnectionManager("
            "region='region', profile='profile', stack_name='stack', "
            "iam_role='iam_role', iam_role_session_duration='21600')"
        )

    def test_boto_session_with_cache(self):
        self.connection_manager._boto_sessions["test"] = sentinel.boto_session

        boto_session = self.connection_manager._boto_sessions["test"]
        assert boto_session == sentinel.boto_session

    def test__get_session__no_args__no_defaults__makes_boto_session_with_defaults(self):
        self.connection_manager.profile = None
        self.connection_manager.iam_role = None

        boto_session = self.connection_manager.get_session()

        self.session_class.assert_called_once_with(
            profile_name=None,
            region_name=self.region,
            aws_access_key_id=self.environment_variables["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=self.environment_variables["AWS_SECRET_ACCESS_KEY"],
            aws_session_token=None,
        )
        assert boto_session == self.mock_session

    def test_get_session__no_args__connection_manager_has_profile__uses_profile(self):
        self.connection_manager.profile = "fancy"
        self.connection_manager.iam_role = None

        boto_session = self.connection_manager.get_session()

        self.session_class.assert_called_once_with(
            profile_name="fancy",
            region_name=self.region,
            aws_access_key_id=self.environment_variables["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=self.environment_variables["AWS_SECRET_ACCESS_KEY"],
            aws_session_token=None,
        )
        assert boto_session == self.mock_session

    def test_get_session___profile_specified__makes_boto_session_with_passed_profile(
        self,
    ):
        self.connection_manager.profile = None

        boto_session = self.connection_manager.get_session(profile="fancy")

        self.session_class.assert_called_once_with(
            profile_name="fancy",
            region_name=self.region,
            aws_access_key_id=self.environment_variables["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=self.environment_variables["AWS_SECRET_ACCESS_KEY"],
            aws_session_token=None,
        )
        assert boto_session == self.mock_session

    def test_get_session__none_for_profile_passed__connection_manager_has_default_profile__uses_no_profile(
        self,
    ):
        self.connection_manager.profile = "default profile"

        boto_session = self.connection_manager.get_session(profile=None)

        self.session_class.assert_called_once_with(
            profile_name=None,
            region_name=self.region,
            aws_access_key_id=self.environment_variables["AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=self.environment_variables["AWS_SECRET_ACCESS_KEY"],
            aws_session_token=None,
        )
        assert boto_session == self.mock_session

    def test_get_session__no_iam_role_passed__no_iam_role_on_connection_manager__does_not_assume_role(
        self,
    ):
        self.connection_manager.iam_role = None

        self.connection_manager.get_session()
        self.mock_session.client.assert_not_called()

    def test_get_session__none_passed_for_iam_role__iam_role_on_connection_manager__does_not_assume_role(
        self,
    ):
        self.connection_manager.iam_role = "arn:aws:iam::123456:role/my-path/other-role"
        self.connection_manager.get_session(iam_role=None)

        self.mock_session.client.assert_not_called()

    @pytest.mark.parametrize(
        "connection_manager,arg",
        [
            pytest.param(
                "arn:aws:iam::123456:role/my-path/my-role",
                STACK_DEFAULT,
                id="role on connection manager",
            ),
            pytest.param(
                "arn:aws:iam::123456:role/my-path/other-role",
                "arn:aws:iam::123456:role/my-path/my-role",
                id="overrides connection manager",
            ),
        ],
    )
    def test_get_session__iam_role__assumes_that_role(self, connection_manager, arg):
        self.connection_manager.iam_role = connection_manager

        kwargs = {}
        if arg != STACK_DEFAULT:
            kwargs["iam_role"] = arg

        self.connection_manager.get_session(**kwargs)

        self.mock_session.client.assert_called_once_with("sts")
        expected_role = arg if arg != STACK_DEFAULT else connection_manager
        self.mock_session.client.return_value.assume_role.assert_called_once_with(
            RoleArn=expected_role, RoleSessionName="my-role-session"
        )

        credentials = self.mock_session.client.return_value.assume_role()["Credentials"]

        self.session_class.assert_any_call(
            region_name=self.region,
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
        )

    def test_get_session__iam_role_and_session_duration_on_connection_manager__uses_session_duration(
        self,
    ):
        self.connection_manager.iam_role = "iam_role"
        self.connection_manager.iam_role_session_duration = 21600

        self.connection_manager.get_session()

        self.mock_session.client.return_value.assume_role.assert_called_once_with(
            RoleArn=self.connection_manager.iam_role,
            RoleSessionName="{0}-session".format(
                self.connection_manager.iam_role.split("/")[-1]
            ),
            DurationSeconds=21600,
        )

    def test_get_session__with_iam_role__returning_empty_credentials__raises_invalid_aws_credentials_error(
        self,
    ):
        self.connection_manager._boto_sessions = {}
        self.connection_manager.iam_role = "iam_role"

        self.mock_session.get_credentials.return_value = None

        with pytest.raises(InvalidAWSCredentialsError):
            self.connection_manager.get_session(
                self.profile, self.region, self.connection_manager.iam_role
            )

    def test_get_client_with_no_pre_existing_clients(self):
        service = "s3"
        region = "eu-west-1"
        profile = None
        iam_role = None
        stack = self.stack_name

        client = self.connection_manager._get_client(
            service, region, profile, stack, iam_role
        )
        expected_client = self.mock_session.client.return_value
        assert client == expected_client
        self.mock_session.client.assert_any_call(service)

    def test_get_client_with_existing_client(self):
        service = "cloudformation"
        region = "eu-west-1"
        iam_role = None
        profile = None
        stack = self.stack_name

        client_1 = self.connection_manager._get_client(
            service, region, profile, stack, iam_role
        )
        client_2 = self.connection_manager._get_client(
            service, region, profile, stack, iam_role
        )
        assert client_1 == client_2
        assert self.mock_session.client.call_count == 1

    @patch("sceptre.connection_manager.boto3.session.Session.get_credentials")
    def test_get_client_with_existing_client_and_profile_none(
        self, mock_get_credentials
    ):
        service = "cloudformation"
        region = "eu-west-1"
        iam_role = None
        profile = None
        stack = self.stack_name

        self.connection_manager.profile = None
        client_1 = self.connection_manager._get_client(
            service, region, profile, stack, iam_role
        )
        client_2 = self.connection_manager._get_client(
            service, region, profile, stack, iam_role
        )
        assert client_1 == client_2

    @mock_s3
    def test_call_with_valid_service_and_call(self):
        service = "s3"
        command = "list_buckets"

        connection_manager = ConnectionManager(region=self.region)
        return_value = connection_manager.call(service, command, {})
        assert return_value["ResponseMetadata"]["HTTPStatusCode"] == 200

    @mock_s3
    def test_call_with_valid_service_and_stack_name_call(self):
        service = "s3"
        command = "list_buckets"

        connection_manager = ConnectionManager(region=self.region, stack_name="stack")

        return_value = connection_manager.call(service, command, {}, stack_name="stack")
        assert return_value["ResponseMetadata"]["HTTPStatusCode"] == 200

    def test_create_session_environment_variables__no_token__returns_envs_dict(self):
        self.mock_session.configure_mock(
            **{
                "region_name": "us-west-2",
                "get_credentials.return_value.access_key": "new_access_key",
                "get_credentials.return_value.secret_key": "new_secret_key",
                "get_credentials.return_value.token": None,
            }
        )

        result = self.connection_manager.create_session_environment_variables()
        expected = {
            "AWS_ACCESS_KEY_ID": "new_access_key",
            "AWS_SECRET_ACCESS_KEY": "new_secret_key",
            "AWS_DEFAULT_REGION": "us-west-2",
            "AWS_REGION": "us-west-2",
        }
        assert expected == result

    def test_create_session_environment_variables__has_session_token__returns_envs_dict_with_token(
        self,
    ):
        self.mock_session.configure_mock(
            **{
                "region_name": "us-west-2",
                "get_credentials.return_value.access_key": "new_access_key",
                "get_credentials.return_value.secret_key": "new_secret_key",
                "get_credentials.return_value.token": "my token",
            }
        )

        result = self.connection_manager.create_session_environment_variables()
        expected = {
            "AWS_ACCESS_KEY_ID": "new_access_key",
            "AWS_SECRET_ACCESS_KEY": "new_secret_key",
            "AWS_DEFAULT_REGION": "us-west-2",
            "AWS_REGION": "us-west-2",
            "AWS_SESSION_TOKEN": "my token",
        }
        assert expected == result

    def test_create_session_environment_variables__include_system_envs_true__adds_envs_removing_profile_and_token(
        self,
    ):
        self.environment_variables.update(
            AWS_PROFILE="my_profile",  # We expect this popped out
            AWS_SESSION_TOKEN="my token",  # This should be removed if there's no token
            OTHER="value-blah-blah",  # we expect this to be in dictionary coming out,
        )

        self.mock_session.configure_mock(
            **{
                "region_name": "us-west-2",
                "get_credentials.return_value.access_key": "new_access_key",
                "get_credentials.return_value.secret_key": "new_secret_key",
                "get_credentials.return_value.token": None,
            }
        )

        result = self.connection_manager.create_session_environment_variables(
            include_system_envs=True
        )
        expected = {
            "AWS_ACCESS_KEY_ID": "new_access_key",
            "AWS_SECRET_ACCESS_KEY": "new_secret_key",
            "AWS_DEFAULT_REGION": "us-west-2",
            "AWS_REGION": "us-west-2",
            "OTHER": "value-blah-blah",
        }
        assert expected == result

    def test_create_session_environment_variables__include_system_envs_false__does_not_add_system_envs(
        self,
    ):
        self.environment_variables.update(
            AWS_PROFILE="my_profile",  # We expect this popped out
            AWS_SESSION_TOKEN="my token",  # This should be removed if there's no token
            OTHER="value-blah-blah",  # we expect this to be in dictionary coming out,
        )

        self.mock_session.configure_mock(
            **{
                "region_name": "us-west-2",
                "get_credentials.return_value.access_key": "new_access_key",
                "get_credentials.return_value.secret_key": "new_secret_key",
                "get_credentials.return_value.token": None,
            }
        )

        result = self.connection_manager.create_session_environment_variables(
            include_system_envs=False
        )
        expected = {
            "AWS_ACCESS_KEY_ID": "new_access_key",
            "AWS_SECRET_ACCESS_KEY": "new_secret_key",
            "AWS_DEFAULT_REGION": "us-west-2",
            "AWS_REGION": "us-west-2",
        }
        assert expected == result


class TestRetry:
    def test_retry_boto_call_returns_response_correctly(self):
        def func(*args, **kwargs):
            return sentinel.response

        response = _retry_boto_call(func)()

        assert response == sentinel.response

    @patch("sceptre.connection_manager.time.sleep")
    def test_retry_boto_call_pauses_when_request_limit_hit(self, mock_sleep):
        mock_func = Mock()
        mock_func.side_effect = [
            ClientError(
                {"Error": {"Code": "Throttling", "Message": "Request limit hit"}},
                sentinel.operation,
            ),
            sentinel.response,
        ]
        # The attribute function.__name__ is required by the decorator @wraps.
        mock_func.__name__ = "mock_func"

        _retry_boto_call(mock_func)()
        mock_sleep.assert_called_once_with(1)

    def test_retry_boto_call_raises_non_throttling_error(self):
        mock_func = Mock()
        mock_func.side_effect = ClientError(
            {"Error": {"Code": 500, "Message": "Boom!"}}, sentinel.operation
        )
        # The attribute function.__name__ is required by the decorator @wraps.
        mock_func.__name__ = "mock_func"

        with pytest.raises(ClientError) as e:
            _retry_boto_call(mock_func)()
        assert e.value.response["Error"]["Code"] == 500
        assert e.value.response["Error"]["Message"] == "Boom!"

    @patch("sceptre.connection_manager.time.sleep")
    def test_retry_boto_call_raises_retry_limit_exceeded_exception(self, mock_sleep):
        mock_func = Mock()
        mock_func.side_effect = ClientError(
            {"Error": {"Code": "Throttling", "Message": "Request limit hit"}},
            sentinel.operation,
        )
        # The attribute function.__name__ is required by the decorator @wraps.
        mock_func.__name__ = "mock_func"

        with pytest.raises(RetryLimitExceededError):
            _retry_boto_call(mock_func)()
