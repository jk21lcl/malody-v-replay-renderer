from __future__ import annotations

import struct
from dataclasses import dataclass

from .errors import ReplayRendererError


@dataclass(frozen=True)
class Field:
    number: int
    wire_type: int
    value: int | bytes


def read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift <= 63:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
        shift += 7
    raise ReplayRendererError("invalid_protobuf", "Invalid or truncated protobuf varint.")


def parse_fields(data: bytes) -> list[Field]:
    fields: list[Field] = []
    offset = 0
    while offset < len(data):
        tag, offset = read_varint(data, offset)
        number = tag >> 3
        wire_type = tag & 0x07
        if number == 0:
            raise ReplayRendererError("invalid_protobuf", "Protobuf field number 0 is invalid.")

        if wire_type == 0:
            value, offset = read_varint(data, offset)
        elif wire_type == 1:
            end = offset + 8
            if end > len(data):
                raise ReplayRendererError("invalid_protobuf", "Truncated fixed64 field.")
            value = data[offset:end]
            offset = end
        elif wire_type == 2:
            length, offset = read_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise ReplayRendererError("invalid_protobuf", "Truncated length-delimited field.")
            value = data[offset:end]
            offset = end
        elif wire_type == 5:
            end = offset + 4
            if end > len(data):
                raise ReplayRendererError("invalid_protobuf", "Truncated fixed32 field.")
            value = data[offset:end]
            offset = end
        else:
            raise ReplayRendererError(
                "unsupported_protobuf_wire_type",
                f"Unsupported protobuf wire type {wire_type} in field {number}.",
            )
        fields.append(Field(number, wire_type, value))
    return fields


def signed_int64(value: int) -> int:
    return value - (1 << 64) if value & (1 << 63) else value


def float32(value: bytes) -> float:
    if len(value) != 4:
        raise ReplayRendererError("invalid_protobuf", "A fixed32 value must contain four bytes.")
    return struct.unpack("<f", value)[0]


def first_varint(fields: list[Field], number: int, default: int | None = None) -> int:
    for field in fields:
        if field.number == number and field.wire_type == 0:
            return int(field.value)
    if default is not None:
        return default
    raise ReplayRendererError("missing_replay_field", f"Missing varint field {number}.")


def first_bytes(fields: list[Field], number: int, required: bool = True) -> bytes | None:
    for field in fields:
        if field.number == number and field.wire_type == 2:
            return bytes(field.value)
    if required:
        raise ReplayRendererError("missing_replay_field", f"Missing bytes field {number}.")
    return None


def decode_text(fields: list[Field], number: int, required: bool = True) -> str:
    raw = first_bytes(fields, number, required=required)
    if raw is None:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReplayRendererError(
            "invalid_replay_text", f"Replay field {number} is not valid UTF-8."
        ) from exc
