"""Steam authentication WebAPI protobuf messages (wire-compatible subset).

Field numbers/types come from SteamDatabase/Protobufs' authentication protocol.
Use protobuf's descriptor/message APIs rather than maintaining a handwritten wire
codec. Only the messages used by this plugin are included; unknown response fields
are ignored by protobuf for forward compatibility.

Reference:
https://github.com/SteamDatabase/Protobufs/blob/master/steam/steammessages_auth.steamclient.proto
"""

import base64
from typing import Any

from google.protobuf import descriptor_pb2, descriptor_pool, json_format, message_factory

_F = descriptor_pb2.FieldDescriptorProto
_STRING = _F.TYPE_STRING
_UINT64 = _F.TYPE_UINT64
_INT32 = _F.TYPE_INT32
_BOOL = _F.TYPE_BOOL
_BYTES = _F.TYPE_BYTES
_FLOAT = _F.TYPE_FLOAT
_FIXED64 = _F.TYPE_FIXED64
_UINT32 = _F.TYPE_UINT32

# (field number, field name, scalar type or nested message name, repeated)
_SCHEMAS = {
    "DeviceDetails": [
        (1, "device_friendly_name", _STRING, False),
        (2, "platform_type", _INT32, False),
        (3, "os_type", _INT32, False),
        (4, "gaming_device_type", _UINT32, False),
        (5, "client_count", _UINT32, False),
        (6, "machine_id", _BYTES, False),
        (7, "app_type", _INT32, False),
    ],
    "AllowedConfirmation": [
        (1, "confirmation_type", _INT32, False),
        (2, "associated_message", _STRING, False),
    ],
    "GetPasswordRSAPublicKey_Request": [(1, "account_name", _STRING, False)],
    "GetPasswordRSAPublicKey_Response": [
        (1, "publickey_mod", _STRING, False),
        (2, "publickey_exp", _STRING, False),
        (3, "timestamp", _UINT64, False),
    ],
    "BeginAuthSessionViaCredentials_Request": [
        (1, "device_friendly_name", _STRING, False),
        (2, "account_name", _STRING, False),
        (3, "encrypted_password", _STRING, False),
        (4, "encryption_timestamp", _UINT64, False),
        (5, "remember_login", _BOOL, False),
        (6, "platform_type", _INT32, False),
        (7, "persistence", _INT32, False),
        (8, "website_id", _STRING, False),
        (9, "device_details", "DeviceDetails", False),
        (10, "guard_data", _STRING, False),
        (11, "language", _UINT32, False),
        (12, "qos_level", _INT32, False),
    ],
    "BeginAuthSessionViaCredentials_Response": [
        (1, "client_id", _UINT64, False),
        (2, "request_id", _BYTES, False),
        (3, "interval", _FLOAT, False),
        (4, "allowed_confirmations", "AllowedConfirmation", True),
        (5, "steamid", _UINT64, False),
        (6, "weak_token", _STRING, False),
        (7, "agreement_session_url", _STRING, False),
        (8, "extended_error_message", _STRING, False),
    ],
    "BeginAuthSessionViaQR_Request": [
        (1, "device_friendly_name", _STRING, False),
        (2, "platform_type", _INT32, False),
        (3, "device_details", "DeviceDetails", False),
        (4, "website_id", _STRING, False),
    ],
    "BeginAuthSessionViaQR_Response": [
        (1, "client_id", _UINT64, False),
        (2, "challenge_url", _STRING, False),
        (3, "request_id", _BYTES, False),
        (4, "interval", _FLOAT, False),
        (5, "allowed_confirmations", "AllowedConfirmation", True),
        (6, "version", _INT32, False),
    ],
    "PollAuthSessionStatus_Request": [
        (1, "client_id", _UINT64, False),
        (2, "request_id", _BYTES, False),
        (3, "token_to_revoke", _FIXED64, False),
    ],
    "PollAuthSessionStatus_Response": [
        (1, "new_client_id", _UINT64, False),
        (2, "new_challenge_url", _STRING, False),
        (3, "refresh_token", _STRING, False),
        (4, "access_token", _STRING, False),
        (5, "had_remote_interaction", _BOOL, False),
        (6, "account_name", _STRING, False),
        (7, "new_guard_data", _STRING, False),
        (8, "agreement_session_url", _STRING, False),
    ],
    "UpdateAuthSessionWithSteamGuardCode_Request": [
        (1, "client_id", _UINT64, False),
        (2, "steamid", _FIXED64, False),
        (3, "code", _STRING, False),
        (4, "code_type", _INT32, False),
    ],
    "UpdateAuthSessionWithSteamGuardCode_Response": [
        (7, "agreement_session_url", _STRING, False),
    ],
    "GenerateAccessTokenForApp_Request": [
        (1, "refresh_token", _STRING, False),
        (2, "steamid", _FIXED64, False),
        (3, "renewal_type", _INT32, False),
    ],
    "GenerateAccessTokenForApp_Response": [
        (1, "access_token", _STRING, False),
        (2, "refresh_token", _STRING, False),
    ],
}


def _build_messages() -> dict[str, Any]:
    file = descriptor_pb2.FileDescriptorProto(
        name="astrbot_steam_auth.proto", package="astrbot.steam.auth", syntax="proto2"
    )
    for name, fields in _SCHEMAS.items():
        message = file.message_type.add(name=name)
        for number, field_name, field_type, repeated in fields:
            field = message.field.add(
                name=field_name,
                number=number,
                label=_F.LABEL_REPEATED if repeated else _F.LABEL_OPTIONAL,
            )
            if isinstance(field_type, str):
                field.type = _F.TYPE_MESSAGE
                field.type_name = f".astrbot.steam.auth.{field_type}"
            else:
                field.type = field_type
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file)
    return {
        name: message_factory.GetMessageClass(
            pool.FindMessageTypeByName(f"astrbot.steam.auth.{name}")
        )
        for name in _SCHEMAS
    }


_MESSAGES = _build_messages()


def encode_request(method: str, data: dict[str, Any]) -> str:
    message = _MESSAGES[f"{method}_Request"]()
    json_format.ParseDict(data, message)
    return base64.b64encode(message.SerializeToString()).decode("ascii")


def decode_response(method: str, body: bytes) -> dict[str, Any]:
    message = _MESSAGES[f"{method}_Response"]()
    message.ParseFromString(body)
    return json_format.MessageToDict(message, preserving_proto_field_name=True)
