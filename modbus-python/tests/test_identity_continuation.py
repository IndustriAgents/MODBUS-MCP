"""FC 43/14 continuation is bounded and never disguises partial results."""

import asyncio

import pytest
from conftest import FakeResponse

from modbus_mcp import cli


def page(information, *, more=0, next_id=0, read_code=3):
    result = FakeResponse(function_code=43)
    result.information = information
    result.more_follows = more
    result.next_object_id = next_id
    result.read_code = read_code
    result.sub_function_code = 14
    result.number_of_objects = len(information)
    return result


def serve(client, pages):
    calls = []

    async def read(**kwargs):
        calls.append(kwargs)
        return pages.pop(0)

    client.read_device_information = read
    return calls


def test_continuation_follows_next_id_and_preserves_original_selector(client, ctx):
    calls = serve(client, [page({0: b"Vendor"}, more=255, next_id=128), page({128: b"Extended"})])
    result = asyncio.run(cli.read_device_information(ctx))
    assert result["success"] is True
    assert result["data"] == {"0": "Vendor", "128": "Extended"}
    assert result["meta"]["complete"] is True and result["meta"]["pages"] == 2
    assert [call["object_id"] for call in calls] == [0, 128]
    assert {call["read_code"] for call in calls} == {3}


@pytest.mark.parametrize(
    "bad",
    [
        page({128: b"bad"}, more=255, next_id=128),
        page({0: b"duplicate"}),
        page({128: b"bad"}, more=1, next_id=129),
        page({128: b"bad"}, read_code=1),
        page({128: [b"duplicate", b"value"]}),
    ],
)
def test_malformed_later_page_retains_only_validated_partial_data(client, ctx, bad):
    calls = serve(client, [page({0: b"Vendor"}, more=255, next_id=128), bad])
    result = asyncio.run(cli.read_device_information(ctx))
    assert result["success"] is False
    assert result["data"] == {"0": "Vendor"}
    assert result["meta"]["complete"] is False
    assert len(calls) == 2


def test_individual_access_never_follows_continuation(client, ctx):
    calls = serve(client, [page({0: b"Vendor"}, more=255, next_id=1, read_code=4)])
    result = asyncio.run(cli.read_device_information(ctx, read_code=4))
    assert result["success"] is False and len(calls) == 1


def test_later_exception_is_not_an_empty_success(client, ctx):
    error = FakeResponse(error=2, function_code=43)
    serve(client, [page({0: b"Vendor"}, more=255, next_id=128), error])
    result = asyncio.run(cli.read_device_information(ctx))
    assert result["success"] is False and result["meta"]["exception_code"] == 2
    assert result["meta"]["complete"] is False and result["data"] == {"0": "Vendor"}


def test_individual_access_rejects_wrong_object_even_if_valid_shape(client, ctx):
    serve(client, [page({1: b"Other"}, read_code=4)])
    result = asyncio.run(cli.read_device_information(ctx, object_id=2, read_code=4))
    assert result["success"] is False and result["data"] == {}


def test_initial_unknown_stream_object_may_restart_at_zero_per_spec(client, ctx):
    serve(client, [page({0: b"Vendor"})])
    result = asyncio.run(cli.read_device_information(ctx, object_id=99))
    assert result["success"] is True and result["data"] == {"0": "Vendor"}


@pytest.mark.parametrize(
    "field",
    ["sub_function_code", "read_code", "number_of_objects", "more_follows", "next_object_id"],
)
def test_missing_required_native_field_is_not_fabricated(client, ctx, field):
    malformed = page({0: b"Vendor"})
    delattr(malformed, field)
    serve(client, [malformed])
    result = asyncio.run(cli.read_device_information(ctx))
    assert result["success"] is False
    assert result["meta"]["complete"] is False


def test_initial_stream_restart_can_continue_from_new_cursor(client, ctx):
    calls = serve(client, [page({0: b"Vendor"}, more=255, next_id=2), page({2: b"Version"})])
    result = asyncio.run(cli.read_device_information(ctx, object_id=99))
    assert result["success"] is True and result["meta"]["restarted_at_zero"] is True
    assert [call["object_id"] for call in calls] == [99, 2]


def test_native_pymodbus_continuation_enum_is_accepted(client, ctx):
    from pymodbus.constants import MoreData

    serve(
        client,
        [
            page({0: b"Vendor"}, more=MoreData.KEEP_READING, next_id=2),
            page({2: b"Version"}, more=MoreData.NOTHING),
        ],
    )
    result = asyncio.run(cli.read_device_information(ctx))
    assert result["success"] is True and result["meta"]["pages"] == 2
